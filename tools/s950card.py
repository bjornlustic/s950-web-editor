#!/usr/bin/env python3
"""Put the S950 image on the ZuluSCSI's SD card over USB card-reader mode.

Only needed ONCE per card: after this the Wi-Fi loader writes into the image
in place (tools/s950wifi.py) and the card never has to be mounted again.

One card can serve both samplers at the same time.  ZuluSCSI scans the
directories named by `[SCSI] Dir` and `Dir1..Dir9` in zuluscsi.ini, and the
SCSI id comes from the file name, so:

    S1000/HD50_512.hda    the S1000's disk, id 5
    S1000/NE7.img         its DaynaPORT device, id 7
    S950/HD00_512.hda     the S950's disk, id 0 (the S950 driver only ever
                          selects target id 0, LUN 0)

    [SCSI]
    Dir  = "S1000"
    Dir1 = "S950"

No firmware change and no per-machine build: the machine is a line of
config, and both ids are served whichever sampler the module is plugged
into.

  s950card.py status
  s950card.py install [--image build/S950_HD0.img] [--dir S950]
                      [--name HD00_512.hda] [--serial /dev/cu.usbmodemXXX]
  s950card.py eject
"""
import argparse
import os
import re
import shutil
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
try:
    import serial
except ImportError:
    sys.exit('pip3 install pyserial')

INI = 'zuluscsi.ini'


def find_port(override=None, want='Pico'):
    """The S950 board's console port, chosen by USB product name.

    NEVER pick by position.  This Mac has two ZuluSCSI boards on it:

        ZuluSCSI Pico 2 DaynaPORT   the S950's, running the SuperOS loader
        ZuluSCSI Blaster            the MPC2000XL's

    and an earlier version of this matched on "ZuluSCSI" alone, took the
    first hit, and mounted the MPC's card - which reboots that board into
    card-reader mode and takes the drive away from the MPC mid-session
    (2026-09-08).  Match the specific product, and if two candidates still
    match, refuse rather than guess.

    `want` is a substring of the product name; pass a device path or a USB
    serial number as `override` to pin one exactly.
    """
    if override:
        if os.path.exists(override):
            return override, 'override'
        # allow a USB serial number as the override
        cands = [(p, n, sn) for p, n, sn in _usb_serial_ports()
                 if sn == override]
        if cands:
            return cands[0][0], cands[0][1]
        return override, 'override'
    cands = [(p, n) for p, n, _ in _usb_serial_ports()
             if 'ZuluSCSI' in n and want in n and os.path.exists(p)]
    if len(cands) > 1:
        names = ', '.join(f'{n} at {p}' for p, n in cands)
        sys.exit(f'more than one ZuluSCSI matches {want!r}: {names}.\n'
                 f'Pass --serial with the device path or USB serial number.')
    if cands:
        return cands[0]
    others = [n for p, n, _ in _usb_serial_ports()
              if 'ZuluSCSI' in n and os.path.exists(p)]
    if others:
        print(f'note: found {", ".join(sorted(set(others)))} but no '
              f'{want!r} board; not touching another machine\'s card',
              file=sys.stderr)
    return None, None


def _usb_serial_ports():
    """[(device path, USB product name, USB serial number)] for every
    USB serial device, from ioreg."""
    out = subprocess.run(['ioreg', '-l', '-w0'],
                         capture_output=True, text=True).stdout
    prod = sn = None
    found = []
    for line in out.splitlines():
        m = re.search(r'"USB Product Name" = "([^"]+)"', line)
        if m:
            prod, sn = m.group(1), None
            continue
        m = re.search(r'"USB Serial Number" = "([^"]+)"', line)
        if m:
            sn = m.group(1)
            continue
        m = re.search(r'"IOCalloutDevice" = "([^"]+)"', line)
        if m and prod:
            found.append((m.group(1), prod, sn))
    return found


def console(port, keys, wait=1.0):
    with serial.Serial(port, 115200, timeout=0.3) as s:
        time.sleep(0.3)
        s.reset_input_buffer()
        for k in keys:
            s.write(k.encode())
            time.sleep(0.15)
        time.sleep(wait)
        return s.read(30000).decode(errors='replace')


def card_volume():
    """The mounted ZuluSCSI card, identified by its zuluscsi.ini."""
    for name in os.listdir('/Volumes'):
        p = os.path.join('/Volumes', name)
        if os.path.exists(os.path.join(p, INI)):
            return p
    return None


def wait_for(fn, secs, what):
    end = time.time() + secs
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.5)
    sys.exit(f'timed out waiting for {what}')


def set_dirs(path, want):
    """Make sure every directory in `want` is scanned, without disturbing
    anything else in the file.  Returns the new text, or None if unchanged."""
    text = open(path).read()
    keys = dict(re.findall(r'^\s*(Dir\d?)\s*=\s*"?([^"\r\n]*)"?',
                           text, re.M))
    have = {v.strip('/').strip() for v in keys.values()}
    missing = [d for d in want if d not in have]
    if not missing:
        return None
    free = [f'Dir{i}' for i in range(1, 10) if f'Dir{i}' not in keys]
    if len(free) < len(missing):
        sys.exit('no free Dir1..Dir9 slot in zuluscsi.ini')
    add = ''.join(f'{k} = "{d}"\n' for k, d in zip(free, missing))
    m = re.search(r'^\[SCSI\]\s*$', text, re.M)
    if not m:
        return text.rstrip('\n') + f'\n\n[SCSI]\n{add}'
    return text[:m.end() + 1] + add + text[m.end() + 1:]


def cmd_status(a):
    port, board = find_port(a.serial)
    print(f'console: {port or "not found"} ({board or "-"})')
    vol = card_volume()
    print(f'card mounted: {vol or "no (SCSI mode)"}')
    if vol:
        for d in sorted(os.listdir(vol)):
            p = os.path.join(vol, d)
            if os.path.isdir(p) and not d.startswith('.'):
                imgs = [f for f in sorted(os.listdir(p))
                        if not f.startswith('.')]
                print(f'  {d}/  {len(imgs)} files: {", ".join(imgs[:6])}')
        print('--- zuluscsi.ini [SCSI] ---')
        for line in open(os.path.join(vol, INI)):
            if re.match(r'^\s*(Dir\d?|WiFiSSID|LoaderIP)\s*=', line):
                print('  ' + line.rstrip())


def cmd_install(a):
    image = a.image if os.path.isabs(a.image) else os.path.join(ROOT, a.image)
    if not os.path.exists(image):
        sys.exit(f'{image} does not exist')
    size = os.path.getsize(image)
    port, board = find_port(a.serial)
    if not port:
        sys.exit('ZuluSCSI console not found on USB')
    vol = card_volume()
    if not vol:
        print(f'[{board}] entering card-reader mode (console s,y)')
        console(port, 'sy', wait=0.3)
        vol = wait_for(card_volume, 60, 'the card to mount')
    print(f'card at {vol}')

    dest_dir = os.path.join(vol, a.dir)
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, a.name)
    st = os.statvfs(vol)
    free = st.f_bavail * st.f_frsize
    have = os.path.getsize(dest) if os.path.exists(dest) else 0
    if size - have > free:
        sys.exit(f'need {size - have} bytes, {free} free on the card')

    print(f'copying {size} bytes -> {a.dir}/{a.name}')
    t = time.time()
    shutil.copyfile(image, dest)
    with open(dest, 'rb') as f:
        os.fsync(f.fileno())
    dt = time.time() - t
    print(f'  {dt:.0f} s ({size / 1024 / 1024 / max(dt, 1e-6):.1f} MB/s)')

    ini = os.path.join(vol, INI)
    new = set_dirs(ini, [a.dir])
    if new is None:
        print(f'zuluscsi.ini already scans {a.dir}/')
    else:
        shutil.copyfile(ini, os.path.join(ROOT, 'build',
                                          'zuluscsi.ini.bak'))
        open(ini, 'w').write(new)
        print(f'zuluscsi.ini: added {a.dir}/ to the scan list '
              f'(backup in build/zuluscsi.ini.bak)')

    if not a.no_eject:
        cmd_eject(a)


def cmd_eject(a):
    vol = card_volume()
    if not vol:
        print('card is not mounted')
        return
    print(f'ejecting {vol}')
    subprocess.run(['diskutil', 'eject', vol], check=False,
                   capture_output=True)
    port, _ = find_port(a.serial)
    if not port:
        print('console gone; the board reboots into SCSI mode on its own')
        return
    def back():
        try:
            return 'Console Commands' in console(port, '?', wait=1.5)
        except Exception:                                    # noqa: BLE001
            return False
    wait_for(back, 60, 'the board to come back in SCSI mode')
    print('board is back on the SCSI bus')


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest='cmd', required=True)
    for name in ('status', 'install', 'eject'):
        p = sub.add_parser(name)
        p.add_argument('--serial', help='override the console port')
        if name == 'install':
            p.add_argument('--image', default='build/S950_HD0.img')
            p.add_argument('--dir', default='S950')
            p.add_argument('--name', default='HD00_512.hda')
            p.add_argument('--no-eject', action='store_true')
    a = ap.parse_args()
    {'status': cmd_status, 'install': cmd_install,
     'eject': cmd_eject}[a.cmd](a)


if __name__ == '__main__':
    main()
