#!/bin/bash
set -e

# Install dependencies
sudo apt-get update -qq
sudo apt-get install -y git python3-pip rsync sshpass curl

# Install Ansible and required Python packages
pip3 install --user ansible ansible-lint pywinrm jmespath

# Clone GOAD if not already present
if [ ! -d ~/GOAD ]; then
  git clone https://github.com/Orange-Cyberdefense/GOAD.git ~/GOAD
fi

# Configure goad.ini — sets ip_range so {{ip_range}} substitution resolves
# to 192.168.57 rather than the default 192.168.56 fallback in settings.py
mkdir -p ~/.goad
cat > ~/.goad/goad.ini << 'EOF'
[default]
ip_range = 192.168.57
lab = Scenario3
provider = vmware

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

# Distribute the insecure public key to all lab VMs via password auth.
# All Vagrant boxes use the default password 'vagrant' for the vagrant user,
# so ssh-copy-id can add the public key to authorized_keys on each VM.
# This is necessary if insert_key = false didn't prevent key replacement
# (e.g. VMs were provisioned before the Vagrantfile change was applied).
echo "[*] Distributing SSH public key to lab VMs..."

LAB_HOSTS=(
  "192.168.57.11"   # router-1
  "192.168.57.12"   # router-2
  "192.168.57.13"   # router-3
  "192.168.57.14"   # router-4
  "192.168.57.15"   # router-5
  "192.168.57.21"   # pc-1
  "192.168.57.23"   # pc-3
  "192.168.57.24"   # pc-4
  "192.168.57.25"   # pc-5
  "192.168.57.28"   # pc-8
  "192.168.57.31"   # docker-11
  "192.168.57.32"   # docker-12
)

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
