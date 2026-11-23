#!/usr/bin/env python3
"""Flash the 2C23T from macOS through the stock FNIRSI bootloader ("FNIRSI IAP").

Why copying does not work on macOS: the bootloader emulates a minimal FAT12
volume and flashes the sectors it receives. The macOS FAT driver (Finder or cp)
also writes AppleDouble `._*` files and `.fseventsd`, and flushes its buffered
writes in an order the bootloader does not expect, so the device ignores the
copy and keeps its old firmware. OpenScope-2C53T hit the same problem
(scripts/iap_flash.py); this tool does the same thing:

  1. unmount the volume and read it from /dev/rdiskN into an image (before)
  2. copy the .bin into a copy of that image with mtools (after), bypassing
     the macOS FAT driver
  3. compare the two images block by block and write only the changed blocks
     (FAT + directory + file data) straight to /dev/rdiskN
  4. wait for the volume to vanish: the bootloader got the whole file, flashed
     it and rebooted

Requirements: `brew install mtools`, and root (raw disk writes).

Run it from Terminal.app. macOS privacy settings may refuse raw access to
removable disks for other apps, even as root ("Operation not permitted").

    sudo python3 tools/flash_iap.py --dry-run dist/F2C23T-v2026.11.7-HW4.0-08007000.bin
    sudo python3 tools/flash_iap.py dist/F2C23T-v2026.11.7-HW4.0-08007000.bin

Enter bootloader mode first: power off, hold MENU, press power, connect USB.
The 8 MB "NO NAME" drive the running firmware exposes is not the bootloader.
"""
import argparse
import os
import plistlib
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path

LABEL_HINT = "IAP"  # the bootloader volume is named "FNIRSI IAP"
WAIT_REBOOT_S = 60
MTOOLS_ENV = dict(os.environ, MTOOLS_SKIP_CHECK="1")


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def diskutil_info(name):
    r = subprocess.run(["diskutil", "info", "-plist", name], capture_output=True)
    return plistlib.loads(r.stdout) if r.returncode == 0 else {}


def find_iap_disk():
    out = subprocess.run(["diskutil", "list", "-plist", "external"], capture_output=True).stdout
    for name in plistlib.loads(out).get("AllDisks", []):
        info = diskutil_info(name)
        if LABEL_HINT in (info.get("VolumeName") or "").upper():
            return name, info
    return None, None


def check_image(path):
    """Check that the reset vector matches the address in the file name."""
    data = path.read_bytes()
    sp, reset = struct.unpack_from("<II", data, 0)
    base = reset & 0xFFFFF000
    m = re.search(r"-(0800[0-9A-Fa-f]{4})\.bin$", path.name)
    if not (0x20000000 <= sp <= 0x20040000):
        sys.exit(f"Not an application image (SP=0x{sp:08X}).")
    if m and int(m.group(1), 16) != base:
        sys.exit(f"Reset vector 0x{reset:08X} does not match {m.group(1)} in the file name.")
    return base, len(data)


def changed_runs(before, after, bs):
    """Contiguous [start, end) runs of bs-sized blocks that differ."""
    runs, start, idx = [], None, 0
    with open(before, "rb") as fb, open(after, "rb") as fa:
        while True:
            b, a = fb.read(bs), fa.read(bs)
            if not a:
                break
            if a != b and start is None:
                start = idx
            elif a == b and start is not None:
                runs.append((start, idx))
                start = None
            idx += 1
    if start is not None:
        runs.append((start, idx))
    return runs


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("image", type=Path)
    ap.add_argument("--dry-run", action="store_true", help="do everything except writing to the device")
    args = ap.parse_args()

    if sys.platform != "darwin":
        sys.exit("macOS only. On Windows, copy the .bin onto the drive; on Linux use mcopy -i /dev/sdX.")
    if not args.image.is_file():
        sys.exit(f"File not found: {args.image}")
    mcopy = shutil.which("mcopy") or "/opt/homebrew/bin/mcopy"
    mdir = os.path.join(os.path.dirname(mcopy), "mdir")
    if not os.path.exists(mcopy):
        sys.exit("mtools is missing: brew install mtools")
    if os.geteuid() != 0:
        sys.exit("Run with sudo (raw writes to /dev/rdiskN).")

    base, size = check_image(args.image)
    name, info = find_iap_disk()
    if not name:
        sys.exit("No 'FNIRSI IAP' drive. Power off, hold MENU, press power, connect USB, then retry.\n"
                 "(The 8 MB 'NO NAME' drive belongs to the running firmware, not the bootloader.)")
    bs = int(info.get("DeviceBlockSize") or 512)
    total = int(info.get("TotalSize") or info.get("Size") or 0)
    raw = f"/dev/r{name}"
    print(f"Firmware: {args.image.name}  ({size} bytes, base 0x{base:08X})")
    print(f"Drive:    {info.get('VolumeName')}  /dev/{name}  {total} bytes, block {bs}")
    if size > total:
        sys.exit("The image is larger than the bootloader drive.")

    run(["diskutil", "unmountDisk", "force", f"/dev/{name}"])
    tmp = Path(tempfile.mkdtemp(prefix="iap_"))
    before, after = tmp / "before.img", tmp / "after.img"
    try:
        r = run(["dd", f"if={raw}", f"of={before}", "bs=65536"])
        if r.returncode != 0:
            sys.exit("Reading the drive failed: " + r.stderr.strip()
                     + "\n(Operation not permitted: run from Terminal.app and allow removable volumes.)")
        shutil.copyfile(before, after)
        listing = run([mdir, "-b", "-i", str(before), "::/"], env=MTOOLS_ENV)
        print("On drive:", " ".join(os.path.basename(l) for l in listing.stdout.split()) or "-")
        m = run([mcopy, "-o", "-i", str(after), str(args.image), f"::/{args.image.name}"], env=MTOOLS_ENV)
        if m.returncode != 0:
            sys.exit("mcopy failed: " + (m.stderr or m.stdout).strip())
        runs = changed_runs(before, after, bs)
        nblk = sum(e - s for s, e in runs)
        print(f"To write: {nblk} blocks ({nblk * bs} bytes) in {len(runs)} runs")
        if args.dry_run:
            print("--dry-run: nothing written. Images kept in", tmp)
            return
        for s, e in runs:
            w = run(["dd", f"if={after}", f"of={raw}", f"bs={bs}", f"skip={s}", f"seek={s}",
                     f"count={e - s}", "conv=notrunc"])
            if w.returncode != 0:
                err = w.stderr.lower()
                if any(x in err for x in ("not configured", "no such", "i/o error", "input/output")):
                    print("The device dropped the drive mid-write: the bootloader has the file and is flashing.")
                    break
                sys.exit("Write failed: " + w.stderr.strip())
        subprocess.run(["sync"])
        print("Written, waiting for the device to flash and reboot...", flush=True)
        dev = Path(f"/dev/{name}")
        t0 = time.monotonic()
        while time.monotonic() - t0 < WAIT_REBOOT_S:
            if not dev.exists():
                print(f"OK: the bootloader drive vanished after {time.monotonic() - t0:.0f}s, "
                      "the device is booting the new firmware.")
                return
            time.sleep(0.5)
        print("The drive is still there after 60 s. Check the device screen.")
    finally:
        if not args.dry_run:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
