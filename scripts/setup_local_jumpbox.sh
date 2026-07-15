#!/bin/bash
set -e

# Usage: setup_local_jumpbox.sh [LAB] [PROVIDER]
#   LAB      - scenario name under ~/GOAD/ad/, e.g. Scenario3, Challenge200,
#              Challenge5, Scenario12 (default: Scenario3)
#   PROVIDER - vmware or virtualbox (default: vmware)
#
# CHANGED FROM THE ORIGINAL: LAB_HOSTS used to be a hardcoded array covering
# only Scenario3's 12 IPs. Every other scenario built since then reuses
# overlapping-but-not-identical IP ranges (e.g. Challenge5 uses .11-.15/.21-.24,
# which includes .22 -- an address Scenario3 never had, and so was never in
# that list). Any host whose IP wasn't in the hardcoded list never got the
# Vagrant insecure key pushed via ssh-copy-id, causing exactly the
# "Permission denied (publickey,password)" failure hit on Challenge5's
# docker-2. This version instead parses LAB_HOSTS directly out of the
# scenario's own provider inventory file, so it's correct for any scenario
# without needing to remember to update a hardcoded list.

LAB="${1:-Scenario3}"
PROVIDER="${2:-vmware}"
IP_RANGE="192.168.57"

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

# Configure goad.ini — sets ip_range so {{ip_range}} substitution resolves
# to 192.168.57 rather than the default 192.168.56 fallback in settings.py,
# and lab/provider to whatever was passed on the command line.
mkdir -p ~/.goad
cat > ~/.goad/goad.ini << EOF
[default]
ip_range = $IP_RANGE
lab = $LAB
provider = $PROVIDER

[aws]
[azure]
[proxmox]
EOF

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

# Generate the public key file from the private key.
# ssh-copy-id requires a .pub file alongside the private key.
ssh-keygen -y -f ~/.vagrant.d/insecure_private_key > ~/.vagrant.d/insecure_private_key.pub

# Distribute the insecure public key to all of THIS scenario's lab VMs via
# password auth, by parsing them straight out of its own provider inventory
# instead of a hardcoded list. All Vagrant boxes use the default password
# 'vagrant' for the vagrant user, so ssh-copy-id can add the public key to
# authorized_keys on each VM.
INVENTORY_FILE=~/GOAD/ad/$LAB/providers/$PROVIDER/inventory

if [ ! -f "$INVENTORY_FILE" ]; then
  echo "[!] Inventory file not found: $INVENTORY_FILE"
  echo "[!] Check that LAB=$LAB and PROVIDER=$PROVIDER are correct, and that"
  echo "[!] this scenario's ad/$LAB/providers/$PROVIDER/inventory exists."
  exit 1
fi

# Each line looks like: hostname   ansible_host={{ip_range}}.N  dict_key=... ...
# Extract the .N suffix and substitute in the real ip_range.
mapfile -t LAB_HOSTS < <(
  grep -oP 'ansible_host=\{\{ip_range\}\}\.\K[0-9]+' "$INVENTORY_FILE" \
    | sed "s/^/${IP_RANGE}./"
)

if [ "${#LAB_HOSTS[@]}" -eq 0 ]; then
  echo "[!] No ansible_host={{ip_range}}.N entries found in $INVENTORY_FILE"
  echo "[!] Nothing to distribute the key to -- check the inventory format."
  exit 1
fi

echo "[*] Distributing SSH public key to ${#LAB_HOSTS[@]} lab VMs for scenario: $LAB ($PROVIDER)..."

for host in "${LAB_HOSTS[@]}"; do
  echo "[*] Copying key to $host..."
  sshpass -p vagrant ssh-copy-id \
    -o StrictHostKeyChecking=no \
    -i ~/.vagrant.d/insecure_private_key \
    vagrant@$host \
    && echo "[+] $host OK" \
    || echo "[!] $host FAILED — may already have the key or be unreachable"
done

echo "[*] Jumpbox setup complete."
