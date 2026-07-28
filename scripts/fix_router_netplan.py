#!/usr/bin/env python3
"""
fix_router_netplan.py

Fixes the OTHER half of the PCI-bridge-overflow bug (see
GOAD_Scenario_Build_Summary.md and fix_router_vnets.py's own docstring):
on any VM needing >4 total adapters, the two that spill onto the second
PCI bridge can enumerate EARLIER than expected in the guest kernel's
legacy ethX naming -- so Vagrant's own private_network provisioner (which
assigns each declared network's static IP by ordinal position: "2nd
declared network -> guest's eth1") ends up writing the wrong IP onto the
wrong physical adapter. The .vmx vnet wiring itself can be entirely
correct (fix_router_vnets.py finds nothing to patch) while the guest is
still completely unreachable, because its addresses are bound to the
wrong physical port relative to what each port is actually wired to.

CONFIRMED EXAMPLE (router-1, Challenge130):
    guest eth1 (MAC ...a0) is actually .vmx's ethernet4 (wired to vmnet5)
    -- but got the mgmt IP 192.168.57.41, which the .vmx's ethernet1
    (wired to vmnet2, where mgmt actually lives) was supposed to have.

FIX: rewrite the guest's netplan config to bind each IP to its MAC
address (with `set-name` to also pin a stable, predictable ethX name
matching the ORIGINAL intended ethernetN-index convention) instead of
relying on positional/name-based matching, which is exactly what broke.
Applied over `vagrant ssh <vm>` -- that goes over the NAT adapter
(ethernet0), which is independent of whatever the private-network
adapters are currently doing, so it stays reachable even when mgmt is
completely misdirected.

Shares its Vagrantfile/.vmx parsing with fix_router_vnets.py (same
directory) rather than duplicating it, so both scripts always agree on
the same MAC/subnet/adapter-index data.

USAGE:
    fix_router_netplan.py --vagrantfile <path/to/providers/vmware/Vagrantfile> \\
                           --machines-dir <path/to/providers/vmware/.vagrant/machines> \\
                           [--dry-run] [--vm NAME [--vm NAME ...]]

    --vm can be given multiple times to limit which VMs are checked/fixed
    (useful for testing against just router-1 first). Omit it to check
    every VM in the Vagrantfile.
"""

import argparse
import re
import subprocess
import sys
import time
from pathlib import Path

# Reuse the exact same parsing this repo's fix_router_vnets.py already
# uses and has been validated against real rendered Vagrantfiles -- do
# NOT reimplement Vagrantfile/.vmx parsing separately here.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from fix_router_vnets import parse_vagrantfile, locate_vmx, parse_vmx, ethernet_adapters


LINK_RE = re.compile(r'^\d+:\s+([^:\s]+):.*?link/(?:ether|loopback)\s+([0-9a-f:]+)', re.IGNORECASE)


def parse_ip_link_output(text: str):
    """Return {ifname: mac} from `ip -o link show` output, skipping lo."""
    result = {}
    for line in text.splitlines():
        m = LINK_RE.match(line.strip())
        if m:
            name, mac = m.group(1), m.group(2).lower()
            if name != "lo":
                result[name] = mac
    return result


def vagrant_ssh(provider_dir: Path, vm_name: str, remote_cmd: str,
                 input_text: str = None, dry_run: bool = False, timeout: int = 60):
    cmd = ["vagrant", "ssh", vm_name, "-c", remote_cmd]
    if dry_run:
        preview = remote_cmd if len(remote_cmd) < 100 else remote_cmd[:100] + "..."
        stdin_note = f" (with {len(input_text)}-byte stdin)" if input_text else ""
        print(f"    [dry-run] would run in {provider_dir}: vagrant ssh {vm_name} -c \"{preview}\"{stdin_note}")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
    return subprocess.run(cmd, cwd=str(provider_dir), input=input_text,
                           capture_output=True, text=True, timeout=timeout)


def build_intended_mapping(vm_info: dict, vmx_adapters: dict):
    """Return [(vmx_index, mac, set_name, netplan_stanza_dict), ...] in
    vmx_index order, for every present adapter (including ethernet0/NAT)."""
    entries = []
    for idx in sorted(vmx_adapters):
        mac = vmx_adapters[idx].get("generatedaddress")
        if not mac:
            continue
        set_name = f"eth{idx}"
        if idx == 0:
            stanza = {"match": {"macaddress": mac}, "set-name": set_name, "dhcp4": True}
        elif idx == 1:
            stanza = {"match": {"macaddress": mac}, "set-name": set_name,
                      "addresses": [f"{vm_info['mgmt_ip']}/24"]}
        else:
            net_ip = vm_info["networks"][idx - 2] if (idx - 2) < len(vm_info["networks"]) else None
            if net_ip is None:
                continue
            stanza = {"match": {"macaddress": mac}, "set-name": set_name,
                      "addresses": [f"{net_ip}/24"]}
        entries.append((idx, mac, set_name, stanza))
    return entries


def render_netplan_yaml(entries):
    """Hand-render rather than depend on PyYAML being available on the
    control host -- this is a small, fixed, well-understood structure."""
    lines = ["network:", "  version: 2", "  renderer: networkd", "  ethernets:"]
    for idx, mac, set_name, stanza in entries:
        key = f"id{idx}"
        lines.append(f"    {key}:")
        lines.append(f"      match:")
        lines.append(f"        macaddress: \"{mac}\"")
        lines.append(f"      set-name: {stanza['set-name']}")
        if stanza.get("dhcp4"):
            lines.append("      dhcp4: true")
        else:
            addr = stanza["addresses"][0]
            lines.append(f"      addresses: [\"{addr}\"]")
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--vagrantfile", required=True, type=Path)
    ap.add_argument("--machines-dir", required=True, type=Path)
    ap.add_argument("--vm", action="append", default=None,
                     help="Limit to this VM (repeatable). Omit to check every VM.")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--reboot-timeout", type=int, default=180)
    args = ap.parse_args()

    boxes = parse_vagrantfile(args.vagrantfile)
    if not boxes:
        print("No boxes parsed from Vagrantfile -- nothing to do.", file=sys.stderr)
        sys.exit(1)

    target_vms = args.vm if args.vm else list(boxes.keys())
    provider_dir = args.vagrantfile.parent

    any_fixed = False
    for vm_name in target_vms:
        if vm_name not in boxes:
            print(f"WARNING: {vm_name} not found in this Vagrantfile, skipping", file=sys.stderr)
            continue

        vmx_path = locate_vmx(args.machines_dir, vm_name)
        if not vmx_path:
            print(f"WARNING: could not locate .vmx for {vm_name}, skipping", file=sys.stderr)
            continue
        _, vmx_kv = parse_vmx(vmx_path)
        vmx_adapters = ethernet_adapters(vmx_kv)

        intended = build_intended_mapping(boxes[vm_name], vmx_adapters)
        if not intended:
            continue

        print(f"{vm_name}: checking current guest interface/MAC mapping via `vagrant ssh` (NAT path)...")
        result = vagrant_ssh(provider_dir, vm_name, "ip -o link show", dry_run=args.dry_run)
        if not args.dry_run and result.returncode != 0:
            print(f"    ERROR: could not reach {vm_name} via `vagrant ssh` (NAT) -- "
                  f"this is more serious than the original symptom, since NAT access "
                  f"is usually resilient. stderr: {result.stderr.strip()}", file=sys.stderr)
            continue

        current = {} if args.dry_run else parse_ip_link_output(result.stdout)

        # Compare: for every intended (mac -> set_name), does the CURRENT
        # ifname holding that MAC already match? If everything already
        # agrees, skip this VM entirely -- no SSH writes, no reboot.
        mismatches = []
        if not args.dry_run:
            mac_to_current_name = {mac: name for name, mac in current.items()}
            for idx, mac, set_name, stanza in intended:
                actual_name = mac_to_current_name.get(mac)
                if actual_name != set_name:
                    mismatches.append((idx, mac, set_name, actual_name))

        if not args.dry_run and not mismatches:
            print(f"    {vm_name}: guest interface naming already matches expected MAC mapping, skipping.")
            continue

        if not args.dry_run:
            print(f"    {vm_name}: {len(mismatches)} interface(s) misnamed relative to their MAC:")
            for idx, mac, expected_name, actual_name in mismatches:
                print(f"      mac={mac} expected={expected_name} actually-named={actual_name}")

        any_fixed = True
        yaml_text = render_netplan_yaml(intended)
        target_file = "/etc/netplan/99-mac-fix.yaml"

        print(f"    Writing corrected netplan config to {vm_name}:{target_file} ...")
        r = vagrant_ssh(provider_dir, vm_name, f"sudo tee {target_file} > /dev/null",
                         input_text=yaml_text, dry_run=args.dry_run)
        if not args.dry_run and r.returncode != 0:
            print(f"    ERROR writing netplan config to {vm_name}: {r.stderr.strip()}", file=sys.stderr)
            continue

        print(f"    Locking down permissions and disabling conflicting netplan files...")
        disable_others = (
            f"sudo chmod 600 {target_file} && "
            f"sudo bash -c 'shopt -s nullglob; for f in /etc/netplan/*.yaml; do "
            f"[ \"$f\" = \"{target_file}\" ] || mv \"$f\" \"$f.orig\"; done'"
        )
        r = vagrant_ssh(provider_dir, vm_name, disable_others, dry_run=args.dry_run)
        if not args.dry_run and r.returncode != 0:
            print(f"    ERROR disabling old netplan files on {vm_name}: {r.stderr.strip()}", file=sys.stderr)
            continue

        print(f"    Validating and applying netplan...")
        r = vagrant_ssh(provider_dir, vm_name, "sudo netplan generate && sudo netplan apply",
                         dry_run=args.dry_run)
        if not args.dry_run and r.returncode != 0:
            print(f"    ERROR: netplan generate/apply failed on {vm_name}: {r.stderr.strip()} -- "
                  f"the old netplan files were renamed to *.yaml.orig and can be restored manually.",
                  file=sys.stderr)
            continue

        # Renaming a live interface out from under itself is unreliable;
        # reboot to guarantee the rename + addressing take effect cleanly.
        print(f"    Rebooting {vm_name} to apply the interface rename cleanly...")
        vagrant_ssh(provider_dir, vm_name, "sudo reboot", dry_run=args.dry_run)

        if not args.dry_run:
            deadline = time.time() + args.reboot_timeout
            up = False
            while time.time() < deadline:
                time.sleep(5)
                probe = vagrant_ssh(provider_dir, vm_name, "true", dry_run=False, timeout=15)
                if probe.returncode == 0:
                    up = True
                    break
            if not up:
                print(f"    WARNING: {vm_name} did not come back up over `vagrant ssh` within "
                      f"{args.reboot_timeout}s -- check it manually.", file=sys.stderr)
                continue

            print(f"    {vm_name} is back up. Current interface/address state:")
            r = vagrant_ssh(provider_dir, vm_name, "ip -o addr show", dry_run=False)
            for line in r.stdout.splitlines():
                print(f"      {line}")

    if not any_fixed:
        print("No guest netplan mismatches found on any checked VM. Nothing to do.")
    print("Done.")


if __name__ == "__main__":
    main()
