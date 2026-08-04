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

LAN-SWITCH SECONDARY IPS (added for Challenge43): some scenarios' routers
front more than one LAN switch and need extra secondary IPs on the mgmt
interface (e.g. Challenge43's router-1/2/3), added by the scenario's own
topology-routers.yml at Ansible-run time. But this script's oneshot
service does `ip addr flush dev "$IFACE" scope global` on every boot
before re-adding its one known mgmt CIDR -- any secondary IP added by
Ansible on that same interface would get silently wiped on the next boot,
regardless of how it was added (netplan, nmcli, ip addr).

Fixed at the SOURCE rather than layering a second, competing mechanism on
top: this script now derives the scenario's data/config.json path purely
from --vagrantfile (already required, no new CLI input), and if
lab.hosts[<vm_name>].lan_switches exists there, folds those IPs into the
SAME flush-then-rebuild block as the mgmt IP -- one authoritative
assignment per boot, not two racing ones.

This is entirely data-driven, not flag-driven, on purpose: every existing
scenario's config.json has no lan_switches key on any router, so
load_lan_switch_ips() returns [] for all of them and this script's
behavior is byte-for-byte unchanged for every scenario except one whose
config.json actually has that shape. No CLI flag, no per-scenario
opt-in, no action needed from whatever loads scenarios automatically --
new scenarios with multi-switch routers get this "for free" the same way
scenarios without them keep working exactly as before.

USAGE (unchanged):
    fix_router_netplan.py --vagrantfile <path/to/providers/vmware/Vagrantfile> \\
                           --machines-dir <path/to/providers/vmware/.vagrant/machines> \\
                           [--vm NAME [--vm NAME ...]] [--dry-run]
"""

import argparse
import json
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


def find_scenario_config_json(vagrantfile_path: Path):
    """Derive ad/<Scenario>/data/config.json purely from the
    --vagrantfile path (providers/vmware/Vagrantfile), which every
    invocation of this script already requires -- no new CLI input.
    Returns None (not an error) if the expected layout isn't there;
    callers must treat that as "no extra data available", not a failure,
    since not every caller of this script is necessarily pointed at a
    GOAD ad/<Scenario>/ layout at all."""
    try:
        # providers/vmware/Vagrantfile -> providers/vmware -> providers -> ad/<Scenario>
        scenario_dir = vagrantfile_path.resolve().parent.parent.parent
        candidate = scenario_dir / "data" / "config.json"
        return candidate if candidate.is_file() else None
    except Exception:
        return None


def load_lan_switch_ips(config_path, vm_name: str):
    """Return a list of CIDR strings from
    lab.hosts[vm_name].lan_switches[*].ip4, or [] on ANY problem --
    missing file, unparseable JSON, missing keys, wrong shape, whatever.
    This must never raise and must never block the existing mgmt/r2r fix
    from applying; the LAN-switch addition is strictly additive. [] is
    also the correct, silent, expected result for every scenario that
    simply doesn't have this key at all (i.e. everything except
    Challenge43 today)."""
    if config_path is None:
        return []
    try:
        with open(config_path) as f:
            data = json.load(f)
        switches = data["lab"]["hosts"][vm_name]["lan_switches"]
        ips = [sw["ip4"] for sw in switches if isinstance(sw, dict) and sw.get("ip4")]
        return ips
    except Exception:
        return []


def render_oneshot_script(entries, lan_switch_ips=None):
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
        ]
        # LAN-switch secondary IPs (if any) go on the SAME mgmt interface,
        # added right after the primary CIDR within the same flush-once
        # block -- not a separate flush, not a separate script. $IFACE is
        # still in scope here. Only ever non-empty for idx == 1 (mgmt);
        # lan_switch_ips is [] for every scenario without this data, so
        # this loop is a no-op there.
        if idx == 1 and lan_switch_ips:
            for lan_ip in lan_switch_ips:
                lines.append(f'  ip addr add "{lan_ip}" dev "$IFACE"  # LAN switch gateway (from config.json lan_switches)')
        lines += [
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

        # Purely data-driven, no new CLI input: [] for every scenario
        # whose config.json has no lan_switches key on this router (i.e.
        # every scenario except Challenge43 today) -- see
        # find_scenario_config_json()/load_lan_switch_ips() docstrings.
        config_json_path = find_scenario_config_json(args.vagrantfile)
        lan_switch_ips = load_lan_switch_ips(config_json_path, vm_name)

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

            # Also verify any LAN-switch secondary IPs are present on the
            # mgmt interface -- without this, a config.json that gained
            # (or changed) lan_switches after this script last ran would
            # be silently skipped forever, since the mgmt/r2r check above
            # alone would already look "correct".
            if already_correct and lan_switch_ips:
                mgmt_mac = next((mac for idx, mac, _ in intended if idx == 1), None)
                mgmt_ifname = mac_to_ifname.get(mgmt_mac) if mgmt_mac else None
                mgmt_addrs = addr_by_ifname.get(mgmt_ifname, set()) if mgmt_ifname else set()
                for lan_cidr in lan_switch_ips:
                    if lan_cidr.split("/")[0] not in mgmt_addrs:
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

        script_text = render_oneshot_script(intended, lan_switch_ips)
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
