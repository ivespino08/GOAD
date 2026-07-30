#!/usr/bin/env python3
"""
cleanup_vmware_vnets.py

Frees up VMware Workstation vmnets left behind after a scenario is
destroyed. Workstation caps total vmnets at 20 (vmnet0-vmnet19); running
several GOAD scenarios back-to-back without cleanup exhausts that cap and
the NEXT scenario's `vagrant up` fails outright before it even gets to
Ansible.

TWO-PHASE DESIGN -- this has to be split into `collect` and `apply`
because `vagrant destroy` deletes the very `.vagrant/`/`.vmx` state this
script needs to read to know which vmnets belonged to this instance:

    collect  -- run BEFORE `vagrant destroy`, while `.vmx` files still
                exist. Reuses fix_router_vnets.py's own Vagrantfile/.vmx
                parsing (parse_vagrantfile, locate_vmx, parse_vmx,
                ethernet_adapters) -- the same ground truth that script
                already trusts for vnet-mismatch detection -- to read
                every ethernetN.vnet value this instance's VMs actually
                reference, and writes them to a small JSON state file.

    apply    -- run AFTER `vagrant destroy` completes (a vnet still
                attached to a running/existing VM won't reliably detach).
                Reads the state file from `collect` and removes exactly
                those vmnets, MINUS a protected set (default vmnet0,
                vmnet1, vmnet8 -- vmnet1 in particular is this framework's
                own mgmt/host-only network, shared and consistent across
                every scenario, and must never be touched).

FALLBACK (--all): if `collect` was never run, or couldn't determine
anything (e.g. `.vmx` state already gone), `apply --all` instead removes
every vmnet in the range vmnet2-vmnet19 minus the protected set,
regardless of whether this particular scenario used it. This is coarser
-- it can only be run safely between sequential scenario builds, never
while a second scenario might still be up -- so it is only ever a
fallback, never the first choice.

ELEVATION: `vnetlib.exe -- remove adapter vmnetX` requires Administrator
rights on Windows. If the current process isn't already elevated, all
removals for one `apply` invocation are batched into a single temporary
.bat file and launched once via `Start-Process -Verb RunAs -Wait`, so you
get one UAC prompt total rather than one per adapter. Output is captured
to a log file inside the same temp dir (elevated child processes don't
stream stdout back to a non-elevated parent) and printed after the fact.

Windows/VMware-Workstation-only: vnetlib.exe doesn't exist on other
hosts or providers, so both subcommands no-op (with a log line) anywhere
else.

USAGE:
    # phase 1, before `vagrant destroy`:
    cleanup_vmware_vnets.py collect --vagrantfile <path/to/Vagrantfile> \\
        --machines-dir <path/to/.vagrant/machines> --state-file <path>

    # phase 2, after `vagrant destroy`:
    cleanup_vmware_vnets.py apply --state-file <path> \\
        [--vnetlib "C:\\Program Files (x86)\\VMware\\VMware Workstation\\vnetlib.exe"] \\
        [--protect 0,1,8] [--dry-run]

    # standalone fallback, no per-scenario state:
    cleanup_vmware_vnets.py apply --all --protect 0,1,8 [--dry-run]
"""

import argparse
import json
import re
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fix_router_vnets import parse_vagrantfile, locate_vmx, parse_vmx, ethernet_adapters


DEFAULT_VNETLIB_PATH = r"C:\Program Files (x86)\VMware\VMware Workstation\vnetlib.exe"
DEFAULT_PROTECTED = {0, 1, 8}
FALLBACK_SCAN_RANGE = range(0, 20)  # vmnet0-vmnet19 -- Workstation's own cap
VNET_NUM_RE = re.compile(r'vmnet(\d+)', re.IGNORECASE)


def is_windows() -> bool:
    return sys.platform.startswith("win")


def is_elevated() -> bool:
    if not is_windows():
        return False
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


# ---------------------------------------------------------------------------
# collect
# ---------------------------------------------------------------------------

def cmd_collect(args):
    if not is_windows():
        print("Not running on Windows -- vnetlib.exe cleanup doesn't apply here, "
              "skipping collection.")
        return 0

    boxes = parse_vagrantfile(args.vagrantfile)
    if not boxes:
        print("No boxes parsed from Vagrantfile -- nothing to collect.", file=sys.stderr)
        return 1

    vnet_numbers = set()
    missing_vms = []

    for vm_name in boxes:
        vmx_path = locate_vmx(args.machines_dir, vm_name)
        if not vmx_path:
            missing_vms.append(vm_name)
            continue
        _, vmx_kv = parse_vmx(vmx_path)
        adapters = ethernet_adapters(vmx_kv)
        for idx, fields in adapters.items():
            vnet = fields.get("vnet")
            if not vnet:
                continue
            m = VNET_NUM_RE.search(vnet)
            if m:
                vnet_numbers.add(int(m.group(1)))

    if missing_vms:
        print(f"WARNING: could not locate .vmx for: {missing_vms} -- their adapters "
              f"won't be included (apply will still clean up whatever WAS found).",
              file=sys.stderr)

    if not vnet_numbers:
        print("No vmnet references found in this instance's .vmx files -- "
              "nothing to record. `apply` will need --all if run without a state file.",
              file=sys.stderr)
        return 1

    state = {"vnets": sorted(vnet_numbers)}
    args.state_file.parent.mkdir(parents=True, exist_ok=True)
    args.state_file.write_text(json.dumps(state))
    print(f"Recorded {len(vnet_numbers)} vmnet(s) referenced by this instance: "
          f"{sorted(vnet_numbers)} -> {args.state_file}")
    return 0


# ---------------------------------------------------------------------------
# apply
# ---------------------------------------------------------------------------

def build_removal_batch(vnet_numbers, vnetlib_path: str, log_path: Path) -> str:
    lines = ["@echo off"]
    for n in sorted(vnet_numbers):
        lines.append(
            f'"{vnetlib_path}" -- remove adapter vmnet{n} >> "{log_path}" 2>&1'
        )
    lines.append("exit /b 0")
    return "\r\n".join(lines) + "\r\n"


def run_removals(vnet_numbers, vnetlib_path: str, dry_run: bool):
    if not vnet_numbers:
        print("Nothing to remove.")
        return 0

    print(f"Removing vmnet(s): {sorted(vnet_numbers)}")

    if dry_run:
        for n in sorted(vnet_numbers):
            print(f'  [dry-run] would run: "{vnetlib_path}" -- remove adapter vmnet{n}')
        return 0

    tmpdir = Path(tempfile.mkdtemp(prefix="goad-vnet-cleanup-"))
    log_path = tmpdir / "cleanup_log.txt"
    bat_path = tmpdir / "cleanup.bat"
    bat_path.write_text(build_removal_batch(vnet_numbers, vnetlib_path, log_path))

    if is_elevated():
        # Already Administrator (e.g. goad.py itself launched elevated) --
        # no need for the Start-Process/RunAs dance, just run it directly.
        result = subprocess.run(["cmd.exe", "/c", str(bat_path)],
                                 capture_output=True, text=True)
        if log_path.exists():
            print(log_path.read_text())
        if result.stdout.strip():
            print(result.stdout)
        if result.stderr.strip():
            print(result.stderr, file=sys.stderr)
        return result.returncode
    else:
        # Not elevated -- launch the batch file once via a single elevated
        # Start-Process/RunAs, so this is ONE UAC prompt for every vmnet
        # in this batch rather than one prompt per adapter.
        ps_cmd = (
            f"Start-Process -FilePath '{bat_path}' -Verb RunAs -Wait -WindowStyle Hidden"
        )
        print("Requesting Administrator elevation to run vnetlib.exe "
              "(a UAC prompt should appear)...")
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", ps_cmd],
            capture_output=True, text=True
        )
        if log_path.exists():
            print(log_path.read_text())
        else:
            print("WARNING: no cleanup log was produced -- the elevated process may "
                  "not have run (e.g. UAC prompt was declined).", file=sys.stderr)
        if result.stderr.strip():
            print(result.stderr, file=sys.stderr)
        return result.returncode


def cmd_apply(args):
    if not is_windows():
        print("Not running on Windows -- vnetlib.exe cleanup doesn't apply here, "
              "skipping.")
        return 0

    protected = {int(x) for x in args.protect.split(",") if x.strip() != ""}

    if args.all:
        candidates = set(FALLBACK_SCAN_RANGE) - protected
        print(f"--all: removing every vmnet in {min(FALLBACK_SCAN_RANGE)}-"
              f"{max(FALLBACK_SCAN_RANGE)} except protected {sorted(protected)}. "
              f"Only safe to run when no OTHER scenario is currently up.")
    else:
        if not args.state_file or not args.state_file.is_file():
            print(f"No state file found at {args.state_file} -- nothing recorded "
                  f"by `collect`, and --all was not passed. Nothing to do. "
                  f"(Pass --all to fall back to removing every non-protected vmnet.)",
                  file=sys.stderr)
            return 1
        state = json.loads(args.state_file.read_text())
        recorded = set(state.get("vnets", []))
        candidates = recorded - protected
        skipped_protected = recorded & protected
        if skipped_protected:
            print(f"Leaving protected vmnet(s) alone: {sorted(skipped_protected)}")

    rc = run_removals(candidates, args.vnetlib, args.dry_run)

    if not args.all and args.state_file and args.state_file.is_file() and not args.dry_run:
        args.state_file.unlink(missing_ok=True)

    return rc


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="subcommand", required=True)

    p_collect = sub.add_parser("collect", help="record vmnets used by this instance (run BEFORE vagrant destroy)")
    p_collect.add_argument("--vagrantfile", required=True, type=Path)
    p_collect.add_argument("--machines-dir", required=True, type=Path)
    p_collect.add_argument("--state-file", required=True, type=Path)
    p_collect.set_defaults(func=cmd_collect)

    p_apply = sub.add_parser("apply", help="remove vmnets (run AFTER vagrant destroy)")
    p_apply.add_argument("--state-file", type=Path, default=None)
    p_apply.add_argument("--all", action="store_true",
                          help="ignore --state-file; remove every non-protected vmnet in vmnet0-vmnet19")
    p_apply.add_argument("--vnetlib", default=DEFAULT_VNETLIB_PATH)
    p_apply.add_argument("--protect", default="0,1,8",
                          help="comma-separated vmnet numbers to never touch (default: 0,1,8)")
    p_apply.add_argument("--dry-run", action="store_true")
    p_apply.set_defaults(func=cmd_apply)

    args = ap.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
