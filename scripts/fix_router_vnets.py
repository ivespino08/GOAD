#!/usr/bin/env python3
"""
fix_router_vnets.py

Generic remediation for the recurring "hub router adapter/vmnet misbinding"
bug documented across Challenge101, Challenge130, Challenge78 (and now
Challenge70/Challenge137): any VM needing more than 4 total network adapters
(NAT + mgmt + 3+ r2r links) overflows VMware Workstation's primary virtual
PCI bus, spilling extra adapters onto a second PCI bridge. This can cause
`vagrant-vmware-desktop`'s own vnet auto-assignment to land some adapters on
the wrong virtual switch relative to what every other VM on that same
subnet expects -- a hypervisor-level wiring problem invisible to the guest
OS and unfixable from inside it.

DESIGN NOTE -- deliberately generic, no scenario-specific data:
  - We never read config.json's topology.r2r_links (its schema differs
    across scenarios -- string pairs, router_a/router_b, or {id,ip} dicts).
    Instead we derive the entire adapter-to-subnet graph purely from each
    VM's own Vagrantfile `:networks` array (ethernet0=NAT, ethernet1=mgmt
    via box[:ip], ethernet(2+i)=networks[i]) -- every scenario's Vagrantfile
    already encodes this identically, so nothing scenario-specific is ever
    hardcoded here.
  - We never gate on the "PCI slot gap" fingerprint. We always compute the
    expected vnet for every adapter on every VM and compare it to actual --
    the slot-gap is logged as a diagnostic hint only, never a precondition,
    so this also catches any *other* mechanism that produces a vnet
    mismatch, not just this one PCI-bridge-overflow cause.

CROSS-CHECK / GROUND-TRUTH RULE:
  - Group all adapters, across ALL VMs, by subnet (mgmt subnet has many
    members -- one per VM in the scenario; each r2r-link subnet has
    exactly 2).
  - Groups with 3+ members: majority vote on the reported vnet value is the
    ground truth; any adapter disagreeing with the majority gets patched.
  - Groups with exactly 2 members (r2r links) that disagree: trust
    whichever endpoint has <=4 total ethernet adapters (never subject to
    the PCI-bridge overflow, so its vnet assignment was never at risk).
    If BOTH endpoints have >4 adapters and disagree, this is flagged for
    manual review rather than guessed at -- no confident ground truth
    exists in that case from adapter-count alone.

FIX MECHANISM (matches the confirmed-working manual sequence -- see the
Challenge101/Scenario12 write-ups): power off via `vmrun` directly (NEVER
`vagrant halt`/`reload`, which re-asserts the wrong cached mapping every
time Vagrant's provider touches the VM's power state), patch the .vmx
in place, power back on via `vmrun`, wait for the mgmt IP to accept SSH,
then clear the stale known_hosts entry.

USAGE:
    fix_router_vnets.py --vagrantfile <path/to/providers/vmware/Vagrantfile> \\
                         --machines-dir <path/to/providers/vmware/.vagrant/machines> \\
                         [--dry-run] [--vmrun /path/to/vmrun]

VERIFY-BEFORE-TRUSTING NOTE:
    `.vagrant/` is local, gitignored state -- its exact `machines/<name>/
    vmware_desktop/id` file format wasn't available to inspect while
    writing this (no live Vagrant run to read from). `locate_vmx()` below
    tries the documented/likely JSON shapes first, then falls back to a
    filesystem search. Run with --dry-run first on your machine and confirm
    the discovered .vmx paths are correct before letting it power-cycle
    anything.
"""

import argparse
import ipaddress
import json
import re
import shutil
import subprocess
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path


# ---------------------------------------------------------------------------
# Vagrantfile parsing -- pure regex, matches the format used by every
# scenario's providers/vmware/Vagrantfile (see template/provider/vmware/Vagrantfile):
#   { :name => "router-1", :ip => "192.168.57.171", ...,
#     :networks => ["10.47.77.1", "192.168.245.1", ...] },
# ---------------------------------------------------------------------------

def strip_ruby_comments(text: str) -> str:
    """Drop everything from an unescaped '#' to end of line. Safe here because
    none of the actual data (IPs, VM names, box names) in a GOAD Vagrantfile
    contains '#' -- this only exists to prevent documentation comments (like
    the boxes.each loop's own example text) from being misread as real data."""
    return "\n".join(line.split("#", 1)[0] for line in text.splitlines())


def extract_boxes_array(text: str) -> str:
    """Return just the `boxes = [ ... ]` array literal, via bracket-depth
    matching -- NOT everything from the first :name => to end-of-file.

    This matters because the ACTUAL file passed to this script is the
    rendered workspace Vagrantfile (template + scenario fragment), which
    has a `boxes.each do |box| ... end` loop -- full of documentation
    comments and code -- AFTER the boxes array closes. Without bounding to
    just the array itself, the LAST box's "chunk" swallows all of that
    trailing text, including any comment that happens to contain something
    that looks like `:networks => [...]` (this is exactly what happened:
    the loop's own "Add :networks => ["ip1", "ip2"] to a box hash..."
    documentation comment got misread as a real box's adapter list).
    """
    idx = text.find("boxes = [")
    if idx == -1:
        idx = text.find("boxes=[")
    if idx == -1:
        raise RuntimeError("could not find a 'boxes = [' array literal in this Vagrantfile")

    start = text.find("[", idx)
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "[":
            depth += 1
        elif text[i] == "]":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    raise RuntimeError("unbalanced brackets while looking for the end of the boxes array")


def parse_vagrantfile(path: Path):
    """Return {vm_name: {"mgmt_ip": str, "networks": [ip, ...]}}"""
    text = strip_ruby_comments(path.read_text())
    boxes_text = extract_boxes_array(text)
    boxes = {}

    # Split on each box entry by finding ":name =>" boundaries, now bounded
    # to ONLY the boxes array itself (see extract_boxes_array above).
    name_positions = [m.start() for m in re.finditer(r':name\s*=>\s*"', boxes_text)]
    name_positions.append(len(boxes_text))

    for i in range(len(name_positions) - 1):
        chunk = boxes_text[name_positions[i]:name_positions[i + 1]]

        name_m = re.search(r':name\s*=>\s*"([^"]+)"', chunk)
        ip_m = re.search(r':ip\s*=>\s*"([^"]+)"', chunk)
        if not name_m or not ip_m:
            continue

        name = name_m.group(1)
        mgmt_ip = ip_m.group(1)

        networks = []
        net_m = re.search(r':networks\s*=>\s*\[([^\]]*)\]', chunk)
        if net_m:
            networks = [ip.strip().strip('"') for ip in net_m.group(1).split(",") if ip.strip()]

        boxes[name] = {"mgmt_ip": mgmt_ip, "networks": networks}

    return boxes


def subnet_of(ip_str: str, prefix: int = 24) -> str:
    """Every network in this repo's scenarios is /24 -- confirmed across
    Challenge70/101/130/137's config.json. If a future scenario uses a
    different prefix, pass --prefix accordingly."""
    return str(ipaddress.ip_network(f"{ip_str}/{prefix}", strict=False))


# ---------------------------------------------------------------------------
# .vmx parsing / editing
# ---------------------------------------------------------------------------

VMX_KV_RE = re.compile(r'^(\S+)\s*=\s*"(.*)"\s*$')


def parse_vmx(path: Path):
    """Return (ordered list of {'key':..., 'raw_line':...}, dict of key->value)."""
    lines = path.read_text().splitlines()
    kv = {}
    for line in lines:
        m = VMX_KV_RE.match(line.strip())
        if m:
            kv[m.group(1).lower()] = m.group(2)
    return lines, kv


def ethernet_adapters(vmx_kv: dict):
    """Return {index:int -> {'vnet':.., 'mac':.., 'present':bool}} for every
    ethernetN.* entry found, sorted by N. ethernet0 (NAT) is included but
    the caller should generally skip it (see NOTE in main())."""
    adapters = {}
    idx_re = re.compile(r'^ethernet(\d+)\.(\w+)$')
    for key, val in vmx_kv.items():
        m = idx_re.match(key)
        if not m:
            continue
        idx = int(m.group(1))
        field = m.group(2)
        adapters.setdefault(idx, {})[field] = val
    return adapters


def patch_vmx_vnet(path: Path, index: int, new_vnet: str, dry_run: bool):
    lines = path.read_text().splitlines(keepends=False)
    target_key = f"ethernet{index}.vnet"
    changed = False
    for i, line in enumerate(lines):
        m = VMX_KV_RE.match(line.strip())
        if m and m.group(1).lower() == target_key:
            indent = line[: len(line) - len(line.lstrip())]
            lines[i] = f'{indent}{target_key} = "{new_vnet}"'
            changed = True
            break
    if not changed:
        raise RuntimeError(f"{target_key} not found in {path} -- refusing to append blindly")
    if dry_run:
        print(f"    [dry-run] would patch {path.name}: {target_key} -> {new_vnet}")
        return
    path.write_text("\n".join(lines) + "\n")


# ---------------------------------------------------------------------------
# Locating each VM's live .vmx via Vagrant's local state
# ---------------------------------------------------------------------------

def locate_vmx(machines_dir: Path, vm_name: str) -> Path | None:
    id_file = machines_dir / vm_name / "vmware_desktop" / "id"
    if id_file.exists():
        raw = id_file.read_text().strip()
        # Try JSON first (documented/likely shape for this plugin's id file).
        try:
            data = json.loads(raw)
            for key in ("vmx_path", "vmxFile", "id"):
                candidate = data.get(key) if isinstance(data, dict) else None
                if candidate and str(candidate).endswith(".vmx"):
                    p = Path(candidate)
                    if p.exists():
                        return p
        except (json.JSONDecodeError, AttributeError):
            pass
        # Some versions store a bare path.
        if raw.endswith(".vmx") and Path(raw).exists():
            return Path(raw)

    # Fallback: filesystem search in common VMware VM directories.
    search_roots = [
        Path.home() / "vmware",
        Path.home() / "Documents" / "Virtual Machines",
        Path.home() / "Virtual Machines",
        Path("/var/lib/vmware"),
    ]
    for root in search_roots:
        if not root.exists():
            continue
        for candidate in root.rglob(f"*{vm_name}*.vmx"):
            return candidate

    return None


# ---------------------------------------------------------------------------
# vmrun wrapper
# ---------------------------------------------------------------------------

def vmrun(vmrun_bin: str, *args, dry_run: bool = False):
    cmd = [vmrun_bin, *args]
    if dry_run:
        print(f"    [dry-run] would run: {' '.join(cmd)}")
        return
    subprocess.run(cmd, check=True)


def wait_for_ssh(ip: str, timeout: int = 120):
    import socket
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((ip, 22), timeout=3):
                return True
        except OSError:
            time.sleep(3)
    return False


def clear_known_hosts(ip: str, dry_run: bool):
    cmd = ["ssh-keygen", "-R", ip]
    if dry_run:
        print(f"    [dry-run] would run: {' '.join(cmd)}")
        return
    subprocess.run(cmd, check=False)  # non-fatal if entry absent


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--vagrantfile", required=True, type=Path)
    ap.add_argument("--machines-dir", required=True, type=Path,
                     help="e.g. ad/<Scenario>/providers/vmware/.vagrant/machines")
    ap.add_argument("--prefix", type=int, default=24)
    ap.add_argument("--vmrun", default="vmrun")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    boxes = parse_vagrantfile(args.vagrantfile)
    if not boxes:
        print("No boxes parsed from Vagrantfile -- nothing to do.", file=sys.stderr)
        sys.exit(1)

    if not args.dry_run and shutil.which(args.vmrun) is None:
        print(f"ERROR: '{args.vmrun}' not found on PATH. Nothing has been touched. "
              f"Install VMware Workstation / add vmrun to PATH, or pass --vmrun "
              f"with an explicit path, or use --dry-run to preview without it.",
              file=sys.stderr)
        sys.exit(1)

    # ---- Step 1: build subnet groups purely from the Vagrantfile ----------
    # group[subnet] = list of (vm_name, adapter_index, declared_ip)
    groups = defaultdict(list)
    for vm_name, info in boxes.items():
        groups[subnet_of(info["mgmt_ip"], args.prefix)].append((vm_name, 1, info["mgmt_ip"]))
        for i, ip in enumerate(info["networks"]):
            adapter_index = 2 + i
            groups[subnet_of(ip, args.prefix)].append((vm_name, adapter_index, ip))

    # ---- Step 2: read every VM's live .vmx ---------------------------------
    vmx_paths, vmx_lines, vmx_kv, vmx_adapters = {}, {}, {}, {}
    total_adapters = {}
    missing = []
    for vm_name in boxes:
        path = locate_vmx(args.machines_dir, vm_name)
        if not path:
            missing.append(vm_name)
            continue
        vmx_paths[vm_name] = path
        lines, kv = parse_vmx(path)
        vmx_lines[vm_name] = lines
        vmx_kv[vm_name] = kv
        adapters = ethernet_adapters(kv)
        vmx_adapters[vm_name] = adapters
        total_adapters[vm_name] = len(adapters)

    if missing:
        print(f"WARNING: could not locate .vmx for: {missing} -- skipping these VMs. "
              f"Check locate_vmx()'s assumptions against your real .vagrant dir.",
              file=sys.stderr)

    # ---- Step 3: for each subnet group, determine ground truth and diff ---
    # patches[vm_name] = {adapter_index: correct_vnet}
    patches = defaultdict(dict)
    ambiguous = []

    for subnet, members in groups.items():
        reported = []
        for vm_name, idx, _ip in members:
            adapters = vmx_adapters.get(vm_name)
            if not adapters or idx not in adapters or "vnet" not in adapters[idx]:
                continue
            reported.append((vm_name, idx, adapters[idx]["vnet"]))

        if len(reported) < 2:
            continue  # can't cross-check a single-member view

        values = [v for _, _, v in reported]
        if len(set(values)) == 1:
            continue  # already consistent, nothing to do

        if len(reported) >= 3:
            # Majority vote.
            vote = Counter(values).most_common(1)[0][0]
            for vm_name, idx, val in reported:
                if val != vote:
                    patches[vm_name][idx] = vote
        else:
            # Exactly 2 members disagreeing: trust the one with <=4 total
            # adapters (never subject to the PCI-bridge overflow).
            (vm_a, idx_a, val_a), (vm_b, idx_b, val_b) = reported
            a_safe = total_adapters.get(vm_a, 99) <= 4
            b_safe = total_adapters.get(vm_b, 99) <= 4
            if a_safe and not b_safe:
                patches[vm_b][idx_b] = val_a
            elif b_safe and not a_safe:
                patches[vm_a][idx_a] = val_b
            else:
                ambiguous.append((subnet, reported))

    if ambiguous:
        print("AMBIGUOUS -- both endpoints have >4 adapters, no confident "
              "ground truth from adapter count alone. Needs manual review:",
              file=sys.stderr)
        for subnet, reported in ambiguous:
            print(f"  {subnet}: {reported}", file=sys.stderr)

    if not patches:
        print("No vnet mismatches found. Nothing to do.")
        return

    # ---- Step 4: apply patches, one VM (power cycle) at a time ------------
    # Each VM is isolated in its own try/except: one VM's power-cycle/patch
    # failure must not prevent attempting the remaining VMs, and must not
    # leave a half-patched .vmx (patch_vmx_vnet only ever rewrites one
    # complete, already-existing line at a time, so a failure between two
    # adapter patches leaves the file in a valid, just-partially-corrected
    # state rather than a corrupt one).
    failed_vms = []
    for vm_name, adapter_fixes in patches.items():
        path = vmx_paths[vm_name]
        print(f"{vm_name}: {len(adapter_fixes)} adapter(s) to fix -> {path}")
        for idx, correct_vnet in adapter_fixes.items():
            current = vmx_adapters[vm_name][idx]["vnet"]
            print(f"    ethernet{idx}.vnet: {current} -> {correct_vnet}")

        try:
            vmrun(args.vmrun, "stop", str(path), "hard", dry_run=args.dry_run)

            for idx, correct_vnet in adapter_fixes.items():
                patch_vmx_vnet(path, idx, correct_vnet, dry_run=args.dry_run)

            vmrun(args.vmrun, "start", str(path), "nogui", dry_run=args.dry_run)

            mgmt_ip = boxes[vm_name]["mgmt_ip"]
            if not args.dry_run:
                print(f"    waiting for SSH on {mgmt_ip} ...")
                if not wait_for_ssh(mgmt_ip):
                    print(f"    WARNING: {mgmt_ip} did not come up on port 22 within timeout",
                          file=sys.stderr)
            clear_known_hosts(mgmt_ip, dry_run=args.dry_run)
        except (subprocess.CalledProcessError, RuntimeError, OSError) as exc:
            print(f"    ERROR fixing {vm_name}: {exc} -- skipping to next VM. "
                  f"{vm_name} may need manual attention (check its power state).",
                  file=sys.stderr)
            failed_vms.append(vm_name)
            continue

    if failed_vms:
        print(f"Done, with errors on: {failed_vms}", file=sys.stderr)
        sys.exit(1)
    print("Done.")


if __name__ == "__main__":
    main()
