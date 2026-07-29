#!/bin/bash
set -e

# Install dependencies
sudo apt-get update -qq
sudo apt-get install -y git python3-pip rsync sshpass curl

# Install Ansible and required Python packages
pip3 install --user ansible ansible-lint pywinrm jmespath netaddr

# Install required Ansible collections
~/.local/bin/ansible-galaxy collection install ansible.utils community.general community.docker ansible.posix

# Clone GOAD if not already present
if [ ! -d ~/GOAD ]; then
  git clone https://github.com/ivespino08/GOAD.git ~/GOAD
fi

# Configure goad.ini (only if it doesn't already exist -- don't clobber whatever
# the framework itself may have already written there for the current run).
mkdir -p ~/.goad
if [ ! -f ~/.goad/goad.ini ]; then
cat > ~/.goad/goad.ini << 'EOF'
[default]
ip_range = 192.168.57
lab = Scenario3
provider = vmware

[aws]
[azure]
[proxmox]
EOF
fi

# Install the Vagrant insecure private key so Ansible can authenticate to
# all lab VMs. The Vagrantfile sets config.ssh.insert_key = false to prevent
# Vagrant replacing the key, but if VMs were already provisioned this ensures
# the key is distributed via password auth (sshpass + ssh-copy-id below).
#
# For air-gapped environments: pre-download vagrant.key.rsa and place it at
# scripts/vagrant.key.rsa in the GOAD repo; the script uses that copy instead.
mkdir -p ~/.vagrant.d

if [ -f ~/GOAD/scripts/vagrant.key.rsa ]; then
  cp ~/GOAD/scripts/vagrant.key.rsa ~/.vagrant.d/insecure_private_key
  echo "[*] Vagrant insecure key installed from repo copy"
else
  curl -fsSL https://raw.githubusercontent.com/hashicorp/vagrant/main/keys/vagrant.key.rsa \
    -o ~/.vagrant.d/insecure_private_key
  echo "[*] Vagrant insecure key downloaded from GitHub"
fi

chmod 600 ~/.vagrant.d/insecure_private_key
ssh-keygen -y -f ~/.vagrant.d/insecure_private_key > ~/.vagrant.d/insecure_private_key.pub

# ---------------------------------------------------------------------------
# Distribute the SSH key.
#
# HISTORY: an earlier version of this script took LAB/PROVIDER as $1/$2, but
# local_jumpbox.py's provision() ran it as plain `bash setup.sh` with nothing
# identifying which scenario was active -- so that version silently defaulted
# to the wrong scenario except by coincidence of overlapping IP ranges (this
# is exactly what caused Scenario12's docker-1/6/7/8/9/10 to get missed). The
# fix at the time was to scan every scenario's inventory under
# ~/GOAD/ad/*/providers/*/ and distribute to the union of all their host
# IPs -- correct, but its cost scales with the TOTAL number of scenarios in
# the repo, not just this one deployment, since it re-checks scenarios that
# aren't even running right now.
#
# CURRENT FIX: local_jumpbox.py's provision() now passes LAB_NAME/
# PROVIDER_NAME as env vars (it always had this information on self.lab_name/
# self.provider.provider_name -- it just was never threaded through to this
# script). So this now scopes to just the current scenario's own inventory
# file, which is O(1) regardless of how many other scenarios exist in the
# repo. The all-scenario scan is kept below purely as a defensive fallback
# -- e.g. if this script is ever run by hand without those env vars set, or
# against an older goad.py that doesn't set them yet.
# ---------------------------------------------------------------------------

IP_RANGE=$(grep -oP '^ip_range\s*=\s*\K.*' ~/.goad/goad.ini 2>/dev/null || echo "192.168.57")

if [ -n "${LAB_NAME:-}" ] && [ -n "${PROVIDER_NAME:-}" ]; then
  INVENTORY_FILE="$HOME/GOAD/ad/${LAB_NAME}/providers/${PROVIDER_NAME}/inventory"
  echo "[*] LAB_NAME=${LAB_NAME} PROVIDER_NAME=${PROVIDER_NAME} passed in -- scoping to this scenario's own inventory only."
  if [ -f "$INVENTORY_FILE" ]; then
    INVENTORY_GLOB="$INVENTORY_FILE"
  else
    echo "[!] Expected inventory file not found at $INVENTORY_FILE -- falling back to scanning all scenarios."
    INVENTORY_GLOB=""
  fi
else
  echo "[!] LAB_NAME/PROVIDER_NAME not set (running this script manually? older goad.py?) -- falling back to scanning all scenarios."
  INVENTORY_GLOB=""
fi

if [ -n "$INVENTORY_GLOB" ]; then
  mapfile -t ALL_HOSTS < <(
    grep -ohP 'ansible_host=\{\{ip_range\}\}\.\K[0-9]+' "$INVENTORY_GLOB" 2>/dev/null \
      | sed "s/^/${IP_RANGE}./" \
      | sort -u
  )
else
  echo "[*] Scanning all scenario provider inventories under ~/GOAD/ad/*/providers/*/..."
  mapfile -t ALL_HOSTS < <(
    find ~/GOAD/ad -path '*/providers/*/inventory' -type f 2>/dev/null \
      | xargs -r grep -ohP 'ansible_host=\{\{ip_range\}\}\.\K[0-9]+' 2>/dev/null \
      | sed "s/^/${IP_RANGE}./" \
      | sort -u
  )
fi

if [ "${#ALL_HOSTS[@]}" -eq 0 ]; then
  echo "[!] No ansible_host={{ip_range}}.N entries found under ~/GOAD/ad/*/providers/*/inventory"
  echo "[!] Nothing to distribute the key to -- check that ~/GOAD/ad/ exists and has scenarios in it."
else
  echo "[*] Found ${#ALL_HOSTS[@]} unique lab host IPs across all scenarios: ${ALL_HOSTS[*]}"
  for host in "${ALL_HOSTS[@]}"; do
    echo "[*] Copying key to $host..."
    sshpass -p vagrant ssh-copy-id \
      -o StrictHostKeyChecking=no \
      -o ConnectTimeout=5 \
      -i ~/.vagrant.d/insecure_private_key \
      vagrant@$host \
      && echo "[+] $host OK" \
      || echo "[!] $host FAILED -- may already have the key, not be up yet, or belong to a scenario not currently running"
  done
fi

echo "[*] Jumpbox setup complete."
