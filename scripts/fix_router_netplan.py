#!/usr/bin/env python3
"""
fix_router_netplan.py

Fixes the guest-side half of the PCI-bridge-overflow bug (see
fix_router_vnets.py's own docstring for the host-side half).

REVISION NOTE: the first version of this script tried to fix this via
netplan's `match: macaddress` + `set-name`, generating .link/.network
files for systemd-networkd to apply. Confirmed on a real router-1
(Challenge130) that this does NOT work on this box: `ethtool -P` shows
the permanent-MAC query itself works fine, but nothing was ever applied
(networkctl showed the adapters "unmanaged", journalctl showed zero
activity for them). Root cause: `net.ifnames=0 biosdevname=0` is present
on this box's kernel command line, which tells udev to skip its whole
predictable-naming subsystem -- exactly the mechanism `.link`-file
renaming depends on. So the config itself was correct; the renaming
mechanism it relied on was disabled at the udev level, unrelated to
anything we wrote.

CURRENT APPROACH: skip netplan/udev renaming entirely for the affected
adapters. Instead, push a systemd oneshot service that runs at boot,
resolves each known MAC to whatever the kernel currently calls it (via
plain `ip -o link show` -- no PermanentMACAddress query, no udev, no
rename), and assigns the IP directly via `ip addr add`. eth0/NAT is left
completely alone -- its original netplan-managed DHCP config already
worked before any of this, so it's restored rather than replaced.

Shares its Vagrantfile/.vmx parsing with fix_router_vnets.py (same
directory) rather than duplicating it.

USAGE:
    fix_router_netplan.py --vagrantfile <path/to/providers/vmware/Vagrantfile> \\
                           --machines-dir <path/to/providers/vmware/.vagrant/machines> \\
                           [--vm NAME [--vm NAME ...]] [--dry-run]
"""

import argparse
import re
import shutil
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fix_router_vnets import parse_vagrantfile, locate_vmx, parse_vmx, ethernet_adapters


LINK_RE = re.compile(r'^\d+:\s+([^:\s]+):.*?link/(?:ether|loopback)\s+([0-9a-f:]+)', re.IGNORECASE)

ONESHOT_SERVICE_NAME = "goad-mac-netconfig.service"
ONESHOT_SCRIPT_PATH = "/usr/local/sbin/goad-mac-netconfig.sh"
ONESHOT_UNIT_PATH = f"/etc/systemd/system/{ONESHOT_SERVICE_NAME}"


def parse_ip_link_output(text: str):
    result = {}
    for line in text.splitlines():
        m = LINK_RE.match(line.strip())
        if m:
            name, mac = m.group(1), m.group(2).lower()
            if name != "lo":
                result[name] = mac
    return result


def vagrant_ssh(provider_dir: Path, vm_name: str, remote_cmd: str,
                 input_text: str = None, dry_run: bool = False, timeout: int = 60,
                 label: str = None):
    cmd = ["vagrant", "ssh", vm_name, "-c", remote_cmd]
    if dry_run:
        preview = remote_cmd if len(remote_cmd) < 100 else remote_cmd[:100] + "..."
        stdin_note = f" (with {len(input_text)}-byte stdin)" if input_text else ""
        print(f"    [dry-run] would run in {provider_dir}: vagrant ssh {vm_name} -c \"{preview}\"{stdin_note}")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    # Deliberately NOT using text=True here: on Windows, Python's text-mode
    # stdin writes translate every \n to \r\n, which corrupts any script
    # content piped through `vagrant ssh -c "sudo tee ..."` -- the guest
    # ends up with e.g. "#!/bin/bash\r" as its shebang line, and the kernel
    # then fails with "No such file or directory" trying to exec an
    # interpreter path with a trailing \r that doesn't exist. Operating on
    # raw bytes here avoids any OS-level newline translation entirely.
    input_bytes = None
    if input_text is not None:
        # Defensive second layer: normalize any \r\n/\r that made it in
        # from some other source (e.g. copy-pasted content) to plain \n
        # before encoding, regardless of platform.
        input_bytes = input_text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")

    raw = subprocess.run(cmd, cwd=str(provider_dir), input=input_bytes,
                          capture_output=True, timeout=timeout)
    stdout = raw.stdout.decode("utf-8", errors="replace")
    stderr = raw.stderr.decode("utf-8", errors="replace")
    result = subprocess.CompletedProcess(cmd, raw.returncode, stdout, stderr)

    if result.returncode != 0 and label:
        # `vagrant ssh -c` mixes the remote command's real output (plus
        # vagrant's own "==> vmname:" banner lines) into stdout, not
        # stderr -- printing only stderr on failure was silently dropping
        # the actually-useful diagnostic text. Show both.
        print(f"    ERROR {label} on {vm_name} (exit {result.returncode}):", file=sys.stderr)
        if result.stdout.strip():
            print(f"    --- stdout ---\n{result.stdout}", file=sys.stderr)
        if result.stderr.strip():
            print(f"    --- stderr ---\n{result.stderr}", file=sys.stderr)
    return result


def build_intended_mapping(vm_info: dict, vmx_adapters: dict):
    """Return [(vmx_index, mac, cidr), ...] for adapters 1..N (mgmt + r2r
    links). ethernet0/NAT is deliberately excluded -- left alone entirely,
    handled by the box's original, already-working netplan config."""
    entries = []
    for idx in sorted(vmx_adapters):
        if idx == 0:
            continue
        mac = vmx_adapters[idx].get("generatedaddress")
        if not mac:
            continue
        if idx == 1:
            cidr = f"{vm_info['mgmt_ip']}/24"
        else:
            net_ip = vm_info["networks"][idx - 2] if (idx - 2) < len(vm_info["networks"]) else None
            if net_ip is None:
                continue
            cidr = f"{net_ip}/24"
        entries.append((idx, mac, cidr))
    return entries


def render_oneshot_script(entries):
    lines = [
        "#!/bin/bash",
        "# Generated by fix_router_netplan.py -- assigns static IPs to r2r/mgmt",
        "# adapters by CURRENT MAC (not PermanentMACAddress/udev rename, which",
        "# doesn't apply on boxes booted with net.ifnames=0). Idempotent: safe",
        "# to run on every boot.",
        "#",
        "# Also persists the resolved MAC->current-ifname mapping to",
        "# /etc/goad/iface-roles.env, so shared Ansible roles (network/",
        "# resolve_iface_roles) can read which physical interface actually",
        "# holds each role (mgmt, r2r peer 0, r2r peer 1, ...) instead of",
        "# assuming a fixed eth{N} position -- that position assumption is",
        "# exactly what this whole bug breaks.",
        "set -u",
        "mkdir -p /etc/goad",
        ": > /etc/goad/iface-roles.env",
        "",
    ]
    for idx, mac, cidr in entries:
        env_key = "GOAD_IFACE_MGMT" if idx == 1 else f"GOAD_IFACE_R2R_{idx - 2}"
        lines += [
            f'# ethernet{idx} -> {cidr} ({env_key})',
            f'MAC="{mac}"',
            f'CIDR="{cidr}"',
            'IFACE=$(ip -o link show | awk -v mac="$MAC" \'tolower($0) ~ tolower(mac) {print $2}\' | tr -d ":" | head -1)',
            'if [ -z "$IFACE" ]; then',
            '  echo "WARNING: no interface currently has MAC $MAC" >&2',
            'else',
            '  ip link set "$IFACE" up',
            '  ip addr flush dev "$IFACE" scope global',
            '  ip addr add "$CIDR" dev "$IFACE"',
            f'  echo "{env_key}=$IFACE" >> /etc/goad/iface-roles.env',
            'fi',
            "",
        ]
    return "\n".join(lines)


ONESHOT_UNIT_TEXT = f"""[Unit]
Description=Assign static IPs to r2r/mgmt adapters by current MAC (works around net.ifnames=0 disabling udev-based netplan renaming)
After=network-pre.target
Before=network-online.target
ConditionPathExists={ONESHOT_SCRIPT_PATH}

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart={ONESHOT_SCRIPT_PATH}

[Install]
WantedBy=multi-user.target
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--vagrantfile", required=True, type=Path)
    ap.add_argument("--machines-dir", required=True, type=Path)
    ap.add_argument("--vm", action="append", default=None)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    boxes = parse_vagrantfile(args.vagrantfile)
    if not boxes:
        print("No boxes parsed from Vagrantfile -- nothing to do.", file=sys.stderr)
        sys.exit(1)

    if not args.dry_run and shutil.which("vagrant") is None:
        print("ERROR: 'vagrant' not found on PATH. Nothing has been touched. "
              "Make sure vagrant is installed and on PATH, or use --dry-run to preview.",
              file=sys.stderr)
        sys.exit(1)

    target_vms = args.vm if args.vm else list(boxes.keys())
    provider_dir = args.vagrantfile.parent

    for vm_name in target_vms:
        try:
            process_one_vm(vm_name, boxes, provider_dir, args)
        except Exception as exc:
            print(f"ERROR: unexpected failure processing {vm_name}: {exc} -- "
                  f"skipping to next VM. {vm_name} may need manual attention.", file=sys.stderr)
            continue

    print("Done.")


def process_one_vm(vm_name, boxes, provider_dir, args):
        if vm_name not in boxes:
            print(f"WARNING: {vm_name} not found in this Vagrantfile, skipping", file=sys.stderr)
            return

        vmx_path = locate_vmx(args.machines_dir, vm_name)
        if not vmx_path:
            print(f"WARNING: could not locate .vmx for {vm_name}, skipping", file=sys.stderr)
            return
        _, vmx_kv = parse_vmx(vmx_path)
        vmx_adapters = ethernet_adapters(vmx_kv)

        intended = build_intended_mapping(boxes[vm_name], vmx_adapters)
        if not intended:
            return

        print(f"{vm_name}: checking current guest state via `vagrant ssh` (NAT path)...")
        link_result = vagrant_ssh(provider_dir, vm_name, "ip -o link show", dry_run=args.dry_run)
        if not args.dry_run and link_result.returncode != 0:
            print(f"    ERROR: could not reach {vm_name} via `vagrant ssh` (NAT). "
                  f"stderr: {link_result.stderr.strip()}", file=sys.stderr)
            return
        addr_result = vagrant_ssh(provider_dir, vm_name, "ip -o addr show", dry_run=args.dry_run)

        already_correct = False
        if not args.dry_run:
            mac_to_ifname = {mac: name for name, mac in parse_ip_link_output(link_result.stdout).items()}
            addr_by_ifname = defaultdict(set)
            addr_line_re = re.compile(r'^\d+:\s+(\S+?)\s+inet\s+([\d.]+)/')
            for line in addr_result.stdout.splitlines():
                m = addr_line_re.match(line.strip())
                if m:
                    addr_by_ifname[m.group(1)].add(m.group(2))

            already_correct = True
            for idx, mac, cidr in intended:
                ifname = mac_to_ifname.get(mac)
                ip_only = cidr.split("/")[0]
                if not ifname or ip_only not in addr_by_ifname.get(ifname, set()):
                    already_correct = False
                    break

        if not args.dry_run and already_correct:
            print(f"    {vm_name}: every expected IP is already present, skipping.")
            return

        print(f"    Restoring original netplan files on {vm_name} EXCEPT any that assign "
              f"eth1+ by name (that's the actual source of the bug -- e.g. Vagrant's own "
              f"generated config -- leaving it active would just re-fight our fix every boot)...")
        restore_cmd = (
            "sudo rm -f /etc/netplan/99-mac-fix.yaml && "
            "sudo bash -c 'shopt -s nullglob; for f in /etc/netplan/*.yaml.orig; do "
            "target=\"${f%.orig}\"; "
            "if grep -Eq \"eth[1-9]:\" \"$f\"; then "
            "echo \"leaving $f disabled (assigns eth1+ by name)\"; "
            "else mv \"$f\" \"$target\"; fi; "
            "done' && "
            "sudo netplan generate && sudo netplan apply"
        )
        r = vagrant_ssh(provider_dir, vm_name, restore_cmd, dry_run=args.dry_run, label="restoring netplan")
        if not args.dry_run and r.returncode != 0:
            return

        script_text = render_oneshot_script(intended)
        print(f"    Writing {ONESHOT_SCRIPT_PATH} on {vm_name}...")
        r = vagrant_ssh(provider_dir, vm_name, f"sudo tee {ONESHOT_SCRIPT_PATH} > /dev/null",
                         input_text=script_text, dry_run=args.dry_run, label="writing oneshot script")
        if not args.dry_run and r.returncode != 0:
            return

        print(f"    Writing {ONESHOT_UNIT_PATH} on {vm_name}...")
        r = vagrant_ssh(provider_dir, vm_name, f"sudo tee {ONESHOT_UNIT_PATH} > /dev/null",
                         input_text=ONESHOT_UNIT_TEXT, dry_run=args.dry_run, label="writing systemd unit")
        if not args.dry_run and r.returncode != 0:
            return

        print(f"    Enabling and starting {ONESHOT_SERVICE_NAME} on {vm_name}...")
        enable_cmd = (
            f"sudo chmod +x {ONESHOT_SCRIPT_PATH} && "
            f"sudo systemctl daemon-reload && "
            f"sudo systemctl enable --now {ONESHOT_SERVICE_NAME}"
        )
        r = vagrant_ssh(provider_dir, vm_name, enable_cmd, dry_run=args.dry_run, label="enabling service")
        if not args.dry_run and r.returncode != 0:
            print(f"    Checking service status/journal on {vm_name} for the real error...")
            status = vagrant_ssh(provider_dir, vm_name,
                                  f"sudo systemctl status {ONESHOT_SERVICE_NAME} --no-pager; "
                                  f"sudo journalctl -u {ONESHOT_SERVICE_NAME} -b --no-pager | tail -30",
                                  dry_run=False)
            print(status.stdout)
            return

        if not args.dry_run:
            print(f"    {vm_name}: current interface/address state after running the fix:")
            r = vagrant_ssh(provider_dir, vm_name, "ip -o addr show", dry_run=False)
            for line in r.stdout.splitlines():
                print(f"      {line}")


if __name__ == "__main__":
    main()
