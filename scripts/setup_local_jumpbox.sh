#!/bin/bash

# setup_local_jumpbox.sh
# Mirrors GOAD's setup_local_jumpbox.sh exactly, adapted for the scenario project.
# This script is NOT run by Vagrant — it is SCPd to the PROVISIONING VM
# and executed remotely by LocalJumpBox.provision() in local_jumpbox.py.
# SSH key management is handled by the GOAD framework, not this script.

# Ensure eth1 static IP is correctly configured via netplan
# generic/ubuntu2204 uses netplan, not NetworkManager
cat > /etc/netplan/99-eth1-static.yaml << 'NETPLAN'
network:
  version: 2
  ethernets:
    eth1:
      addresses:
        - 192.168.57.3/24
      dhcp4: false
NETPLAN
chmod 600 /etc/netplan/99-eth1-static.yaml
netplan apply
sleep 5

# Install git and python3
sudo apt-get update
sudo apt-get install -y git python3-venv python3-pip rsync

# Clone or update the scenario project repo
SCENARIO_REPO=/home/vagrant/GOAD
GIT_FOLDER=$SCENARIO_REPO/.git
if [ ! -d $GIT_FOLDER ]
then
    rm -rf $SCENARIO_REPO
    git clone https://github.com/ivespino08/GOAD.git $SCENARIO_REPO
    cd $SCENARIO_REPO
else
    cd $SCENARIO_REPO
    git pull
fi

# Install ansible and python dependencies
python3 -m pip install --upgrade pip
cd $SCENARIO_REPO
python3 -m pip install -r requirements.yml

# Install ansible galaxy requirements
cd $SCENARIO_REPO/ansible
/home/vagrant/.local/bin/ansible-galaxy install -r requirements.yml

# Set colour prompt
sudo sed -i '/force_color_prompt=yes/s/^#//g' /home/*/.bashrc
sudo sed -i '/force_color_prompt=yes/s/^#//g' /root/.bashrc

echo "[PROVISIONING] Setup complete. Run playbooks from $SCENARIO_REPO/ansible/"
