#!/bin/bash
set -e

# Install dependencies
sudo apt-get update -qq
sudo apt-get install -y git python3-pip rsync sshpass curl

# Install Ansible
pip3 install --user ansible ansible-lint pywinrm

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
# all lab VMs. The Vagrantfile sets config.ssh.insert_key = false on all
# non-PROVISIONING VMs, so they all keep this well-known key in their
# authorized_keys. The key is publicly available from HashiCorp/Vagrant.
#
# NOTE: If this environment has no internet access during jumpbox setup,
# download vagrant.key.rsa manually and place it at scripts/vagrant.key.rsa
# in the GOAD repo before running. The script will use that copy instead.
mkdir -p ~/.vagrant.d

if [ -f ~/GOAD/scripts/vagrant.key.rsa ]; then
  # Use pre-downloaded copy from repo (for air-gapped environments)
  cp ~/GOAD/scripts/vagrant.key.rsa ~/.vagrant.d/insecure_private_key
  echo "[*] Vagrant insecure key installed from repo copy"
else
  # Download from HashiCorp's Vagrant repository
  curl -fsSL https://raw.githubusercontent.com/hashicorp/vagrant/main/keys/vagrant.key.rsa \
    -o ~/.vagrant.d/insecure_private_key
  echo "[*] Vagrant insecure key downloaded from GitHub"
fi

chmod 600 ~/.vagrant.d/insecure_private_key

echo "[*] Jumpbox setup complete."
