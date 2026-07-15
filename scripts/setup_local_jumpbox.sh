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
# CHANGED FROM THE PREVIOUS VERSION OF THIS SCRIPT: that version took LAB and
# PROVIDER as $1/$2 and only pushed keys for that one scenario's inventory.
# local_jumpbox.py's provision() actually runs this script as plain
# `bash setup.sh` -- confirmed from the real source, no arguments, no
# environment variable, nothing that identifies which scenario is currently
# being deployed. Under real GOAD usage (not a manual by-hand run with
# explicit args), LAB would always fall back to its default and this script
# would always distribute keys for the WRONG scenario except by coincidence
# of overlapping IP ranges -- which is exactly what happened with Scenario12's
# docker-1/6/7/8/9/10 (the addresses that don't happen to overlap with
# Scenario3's router IPs).
#
# Fix: stop trying to guess which single scenario is "active" from inside
# this script (there's no reliable signal available to do that with). Instead,
# scan every scenario's provider inventory under ~/GOAD/ad/*/providers/*/ and
# distribute the key to the union of all their host IPs. ssh-copy-id is cheap
# and idempotent, and an IP whose VM isn't up yet (e.g. a different scenario
# that isn't currently running) just fails fast with ConnectTimeout below --
# so this is safe to run unconditionally on every jumpbox provision, and any
# new scenario added later is covered automatically with no script changes.
# ---------------------------------------------------------------------------

IP_RANGE=$(grep -oP '^ip_range\s*=\s*\K.*' ~/.goad/goad.ini 2>/dev/null || echo "192.168.57")

echo "[*] Scanning all scenario provider inventories under ~/GOAD/ad/*/providers/*/..."

mapfile -t ALL_HOSTS < <(
  find ~/GOAD/ad -path '*/providers/*/inventory' -type f 2>/dev/null \
    | xargs -r grep -ohP 'ansible_host=\{\{ip_range\}\}\.\K[0-9]+' 2>/dev/null \
    | sed "s/^/${IP_RANGE}./" \
    | sort -u
)

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
