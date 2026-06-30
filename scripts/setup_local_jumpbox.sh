#!/bin/bash

# setup_local_jumpbox.sh
# Mirrors GOAD's setup_local_jumpbox.sh exactly, adapted for the scenario project.
# This script is NOT run by Vagrant — it is SCPd to the PROVISIONING VM
# and executed remotely by LocalJumpBox.provision() in local_jumpbox.py.
# SSH key management is handled by the GOAD framework, not this script.

# Install git and python3
sudo apt-get update
sudo apt-get install -y git python3-venv python3-pip rsync

# Clone or update the GOAD repo
GOAD_REPO=/home/vagrant/GOAD
GIT_FOLDER=$GOAD_REPO/.git
if [ ! -d $GIT_FOLDER ]
then
    rm -rf $GOAD_REPO
    git clone https://github.com/ivespino08/GOAD.git $GOAD_REPO
    cd $GOAD_REPO
else
    cd $GOAD_REPO
    git pull
fi

# Install ansible and python dependencies
python3 -m pip install --upgrade pip
cd $GOAD_REPO
python3 -m pip install -r requirements.yml

# Install ansible galaxy requirements
cd $GOAD_REPO/ansible
/home/vagrant/.local/bin/ansible-galaxy install -r requirements.yml

# ── Configure ~/.goad/goad.ini ─────────────────────────────────────────────
# The framework reads this file to determine lab, provider, provisioner,
# and ip_range. ip_range is used to substitute {{ip_range}} in provider
# inventories via Utils.replace_in_file() — if wrong, all host IPs break.
# Default fallback is 192.168.56 which would break Scenario3 (uses .57).
GOAD_CONFIG_DIR=/home/vagrant/.goad
GOAD_CONFIG_FILE=$GOAD_CONFIG_DIR/goad.ini
mkdir -p $GOAD_CONFIG_DIR

cat > $GOAD_CONFIG_FILE << 'GOADINI'
[default]
; lab: GOAD / GOAD-Light / MINILAB / NHA / SCCM / Scenario3
lab = Scenario3

; provider : virtualbox / vmware / vmware_esxi / aws / azure / proxmox
provider = vmware

; provisioner method : local / remote
provisioner = local

; ip_range (3 first ip digits)
; MUST match the management network in the Vagrantfile (192.168.57.x)
; Used by the framework to substitute {{ip_range}} in provider inventories
ip_range = 192.168.57

[aws]
aws_region = eu-west-3
aws_zone = eu-west-3c

[azure]
az_location = westeurope

[proxmox]
pm_api_url = https://192.168.1.1:8006/api2/json
pm_user = infra_as_code@pve
pm_node = GOAD
pm_pool = GOAD
pm_full_clone = false
pm_storage = local
pm_vlan = 10
pm_network_bridge = vmbr3
pm_network_model = e1000
GOADINI

chmod 600 $GOAD_CONFIG_FILE
echo "[PROVISIONING] goad.ini written to $GOAD_CONFIG_FILE"

# Set colour prompt
sudo sed -i '/force_color_prompt=yes/s/^#//g' /home/*/.bashrc
sudo sed -i '/force_color_prompt=yes/s/^#//g' /root/.bashrc

echo "[PROVISIONING] Setup complete."
echo "[PROVISIONING] ip_range set to 192.168.57 — matches Scenario3 Vagrantfile."
echo "[PROVISIONING] Run 'cd $GOAD_REPO && python3 goad.py' to start the framework."
