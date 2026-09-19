#!/usr/bin/env python3
"""S950 SCSI drop-box: the HOST side of 4.0.3 direct-to-RAM transfer.

SuperOS 4.0.3 polls block 3 of the SCSI disk (the MAILBOX) about once
a second while the sampler is idle.  This module is the other half:
it keeps a DROP-BOX VOLUME on the disk image (a normal S950 hd volume,
stock directory + FAT layout, notes/s950_disk_boot.md) and announces
every deposit in the mailbox.  The firmware then selects the volume,
re-reads its directory and hands the named file to the stock loader,
so the sample lands in RAM without anyone touching the DISK page.

Mailbox (block 3, never scanned by the stock driver):
  +0  'SRX1'          magic
  +4  serial (word)   bumped on every deposit; 0 = nothing yet
  +6  volume[10]      the drop-box volume name
  +16 name[10]        the file just deposited
  +26 type            'S' sample / 'P' program
  +27..95             reserved

The image can be any mutable buffer: a bytearray (tests), or an mmap
of the image file behind a LIVE target (the Raspberry Pi bridge,
tools/../notes/s950_scsi_dropbox.md).  On a ZuluSCSI/BlueSCSI SD
card the same image works offline: put files in, re-insert, and the
sampler picks the newest one up at the first poll.

Usage:
  s950dropbox.py new    IMAGE [--volname DROPBOX] [--blocks 0x2000]
  s950dropbox.py addvol IMAGE [--volname DROPBOX]
  s950dropbox.py put IMAGE FILE.wav [--name NAME] [--oneshot]
  s950dropbox.py put IMAGE FILE.s --sfile [--name NAME]
  s950dropbox.py put IMAGE FILE.p --pfile [--name NAME]
  s950dropbox.py clear  IMAGE [--volname DROPBOX]
  s950dropbox.py status IMAGE
  s950dropbox.py watch IMAGE FOLDER        (deposit every new .wav)

`new` FORMATS: it writes a blank disk with one drop-box volume, and
anything already on the image is gone.  To put a drop-box on a disk
that already carries something - the SuperOS SCSI image, whose
SUPEROS volume holds the bootable type-'M' OS file - use `addvol`,
which adds the volume alongside and leaves every existing volume
byte-identical.  That is the shipping card: one SD image the customer
loads the OS from AND drops samples into.

Files from the sampler's point of view are the S950's own disk files:
a WAV is resampled to 40 kHz and encoded with build_sounddisk
(sample_file / pack_pcm, the split 12-bit format the loader expects).
"""
import mmap
import os
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from build_hdimage import BLOCK, FAT_OFF, FIRST_DATA, DEFAULT_BLOCKS  # noqa
import build_sounddisk                                                 # noqa

MAGIC = b'SRX1'
MAILBOX_BLK = 3
MB_LEN = 96
MB_VOL = 6                    # volume name[10]
MB_NAME = 16                  # file name[10]
MB_TYPE = 26                  # 'S' sample / 'P' program
MB_CMDSER = 28                # host's command serial (word)
MB_CMDOP = 30                 # opcode; 1 = publish the resident status
MB_CMDBLK = 32                # block the sampler writes its reply to
STAT_MAGIC = b'SST1'
STAT_MAX = 32
WORDS_PER_BANK = 0x8000
DEF_VOL = b'DROPBOX   '
MASTER_ENTRIES = 128
DIR_ENTRIES = 128
HIGHEST = 0x1FFE              # the stock dir read never stages entry 0x1FFF


def _name10(s):
    if isinstance(s, str):
        s = s.encode('ascii')
    s = s.upper()[:10]
    return s + b' ' * (10 - len(s))


def fat_get(d, blk):
    return struct.unpack_from('<H', d, FAT_OFF + 2 * blk)[0]


def fat_set(d, blk, val):
    struct.pack_into('<H', d, FAT_OFF + 2 * blk, val)


def nblocks(d):
    return min(len(d) // BLOCK, 0x2000)


# ---- volumes (master directory, block 0) -----------------------------------

def volumes(d):
    out = {}
    for i in range(MASTER_ENTRIES):
        e = i * 12
        blk = struct.unpack_from('<H', d, e + 10)[0]
        if blk:
            out[bytes(d[e:e + 10])] = blk
    return out


def find_volume(d, volname):
    return volumes(d).get(_name10(volname))


def create_volume(d, volname):
    volname = _name10(volname)
    if find_volume(d, volname):
        return find_volume(d, volname)
    slot = None
    for i in range(MASTER_ENTRIES):
        if struct.unpack_from('<H', d, i * 12 + 10)[0] == 0:
            slot = i
            break
    if slot is None:
        raise RuntimeError('master directory full')
    blk = free_blocks(d, 1)[0]
    fat_set(d, blk, 0x8000)
    d[blk * BLOCK:(blk + 1) * BLOCK] = bytes(BLOCK)
    d[slot * 12:slot * 12 + 10] = volname
    struct.pack_into('<H', d, slot * 12 + 10, blk)
    return blk


def new_image(volname=DEF_VOL, blocks=DEFAULT_BLOCKS):
    d = bytearray(blocks * BLOCK)
    create_volume(d, volname)
    return d


# ---- blocks --------------------------------------------------------------

def free_blocks(d, n):
    out = []
    top = min(nblocks(d) - 1, HIGHEST)
    for blk in range(FIRST_DATA, top + 1):
        if fat_get(d, blk) == 0:
            out.append(blk)
            if len(out) == n:
                return out
    raise RuntimeError(f'disk full: {n} blocks wanted, {len(out)} free')


def free_chain(d, start):
    blk = start
    while blk:
        nxt = fat_get(d, blk)
        fat_set(d, blk, 0)
        if nxt & 0x8000 or nxt == 0:
            break
        blk = nxt & 0x7FFF


# ---- files (volume directory) --------------------------------------------

def entries(d, volblk):
    """(slot, name, type, length, start) for every used slot."""
    out = []
    base = volblk * BLOCK
    for i in range(DIR_ENTRIES):
        e = base + i * 24
        t = d[e + 0x10]
        if t:
            length = struct.unpack_from('<H', d, e + 0x11)[0] | \
                (d[e + 0x13] << 16)
            start = struct.unpack_from('<H', d, e + 0x14)[0]
            out.append((i, bytes(d[e:e + 10]), chr(t), length, start))
    return out


def deposit(d, name, ftype, payload, volname=DEF_VOL, notify=True):
    """Write `payload` as file `name` (type 'S'/'P') into the drop-box
    volume (created if missing; a same-name same-type file is
    replaced), then announce it in the mailbox.  Returns the serial.

    With notify=False the file is written to the card but NOT announced,
    so the sampler leaves it alone: that is how a library is built up on
    the card without every file landing in RAM as it arrives.  Announce
    it later with announce() (s950wifi's 'announce' command) to load it.
    """
    name = _name10(name)
    volblk = create_volume(d, volname)
    base = volblk * BLOCK
    slot = None
    for i, nm, t, ln, st in entries(d, volblk):
        if nm == name and t == ftype:
            free_chain(d, st)
            slot = i
            break
    if slot is None:
        for i in range(DIR_ENTRIES):
            if d[base + i * 24 + 0x10] == 0:
                slot = i
                break
    if slot is None:
        raise RuntimeError('drop-box directory full (128 files)')
    nblk = max(1, (len(payload) + BLOCK - 1) // BLOCK)
    chain = free_blocks(d, nblk)
    for k, blk in enumerate(chain):
        fat_set(d, blk, 0x8000 if k == nblk - 1 else chain[k + 1])
        piece = payload[k * BLOCK:(k + 1) * BLOCK]
        d[blk * BLOCK:blk * BLOCK + len(piece)] = piece
    e = base + slot * 24
    ent = bytearray(24)
    ent[0:10] = name
    ent[0x10] = ord(ftype)
    struct.pack_into('<H', ent, 0x11, len(payload) & 0xFFFF)
    ent[0x13] = (len(payload) >> 16) & 0xFF
    struct.pack_into('<H', ent, 0x14, chain[0])
    d[e:e + 24] = ent
    if not notify:
        return 0
    serial = (mailbox(d)['serial'] + 1) & 0xFFFF or 1
    announce(d, serial, _name10(volname), name, ftype)
    return serial


def clear(d, volname=DEF_VOL):
    """Empty the drop-box volume: free every file's blocks and wipe its
    directory, keeping the volume itself.  Returns the number of files
    removed.  The mailbox is left announcing nothing new, so the sampler
    does not try to load anything as a result."""
    volblk = find_volume(d, volname)
    if not volblk:
        return 0
    n = 0
    for slot, name, ftype, length, start in entries(d, volblk):
        free_chain(d, start)
        e = volblk * BLOCK + slot * 24
        d[e:e + 24] = bytes(24)
        n += 1
    return n


def remove(d, name, volname=DEF_VOL, ftype=None):
    """Delete one file from the drop-box volume: free its FAT chain and
    wipe its 24-byte directory entry, exactly as clear() does per file.

    If the mailbox is still announcing the file being deleted, the
    announcement is wiped too.  Otherwise the next poll of a power-up -
    when the firmware's memory of the last serial is zero again - would
    point the loader at a file that is no longer there ('SCSI RX: file
    not found', test_403 X6).

    Returns the (name, type, length) removed, or None if there was no
    such file.
    """
    name = _name10(name)
    volblk = find_volume(d, volname)
    if not volblk:
        return None
    for slot, nm, t, length, start in entries(d, volblk):
        if nm != name or (ftype is not None and t != ftype):
            continue
        free_chain(d, start)
        e = volblk * BLOCK + slot * 24
        d[e:e + 24] = bytes(24)
        mb = mailbox(d)
        if mb and mb['name'] == name and mb['volume'] == _name10(volname):
            o = MAILBOX_BLK * BLOCK
            d[o:o + MB_LEN] = bytes(MB_LEN)
        return (nm, t, length)
    return None


def read_file(d, name, volname=DEF_VOL, ftype=None):
    """Pull a file's bytes back out of the image by walking its FAT chain.

    Returns (bytes, type) or None.  The mirror is byte-identical to the
    card, so this needs no board traffic.
    """
    name = _name10(name)
    volblk = find_volume(d, volname)
    if not volblk:
        return None
    for slot, nm, t, length, start in entries(d, volblk):
        if nm != name or (ftype is not None and t != ftype):
            continue
        out, blk, left = bytearray(), start, length
        while blk and left > 0:
            take = min(left, BLOCK)
            out += d[blk * BLOCK:blk * BLOCK + take]
            left -= take
            nxt = fat_get(d, blk)
            if nxt & 0x8000 or nxt == 0:
                break
            blk = nxt
        return bytes(out), t
    return None


def sample_points(d, name, volname=DEF_VOL):
    """A drop-box sample decoded back to points, with its header fields."""
    got = read_file(d, name, volname, 'S')
    if not got:
        return None
    body, _ = got
    if len(body) < 0x3C:
        return None
    n = struct.unpack_from('<I', body, 0x10)[0]
    rate = struct.unpack_from('<H', body, 0x14)[0]
    return {'points': build_sounddisk.unpack_pcm(body[0x3C:], n),
            'npoints': n, 'rate': rate,
            'loudness': struct.unpack_from('<H', body, 0x18)[0],
            'pitch': struct.unpack_from('<H', body, 0x16)[0],
            'mode': chr(body[0x1A]),
            'start': struct.unpack_from('<I', body, 0x20)[0],
            'end': struct.unpack_from('<I', body, 0x1C)[0],
            'loop': struct.unpack_from('<I', body, 0x24)[0]}


def listing(d, volname=DEF_VOL):
    """(name, type, length) for every file in the drop-box volume."""
    volblk = find_volume(d, volname)
    if not volblk:
        return []
    return [(nm.rstrip().decode('ascii', 'replace'), t, ln)
            for _, nm, t, ln, _ in entries(d, volblk)]


def announce(d, serial, volname, name, ftype):
    mb = bytearray(MB_LEN)
    mb[0:4] = MAGIC
    struct.pack_into('<H', mb, 4, serial)
    mb[6:16] = _name10(volname)
    mb[16:26] = _name10(name)
    mb[26] = ord(ftype)
    d[MAILBOX_BLK * BLOCK:MAILBOX_BLK * BLOCK + MB_LEN] = mb


def status_block(d):
    """The block the sampler writes its reply to.

    The last block of the image, marked used in the FAT so free_blocks()
    never hands it to a file.  A constant would not do: the 64 MB card has
    8192 blocks and the test images have 801, so any fixed number is past
    the end of one of them.
    """
    blk = nblocks(d) - 1
    if fat_get(d, blk) == 0:
        fat_set(d, blk, 0x8000)             # reserved, end-of-chain marker
    return blk


def ask_status(d):
    """Bump the mailbox command serial so the sampler publishes what is
    resident at its next idle poll.  Returns the serial to expect back."""
    o = MAILBOX_BLK * BLOCK
    if bytes(d[o:o + 4]) != MAGIC:          # no mailbox yet: make one
        announce(d, 0, DEF_VOL, b' ' * 10, ' ')
        d[o + 4:o + 6] = b'\x00\x00'         # ... announcing nothing
    ser = (struct.unpack_from('<H', d, o + MB_CMDSER)[0] + 1) & 0xFFFF or 1
    struct.pack_into('<H', d, o + MB_CMDSER, ser)
    d[o + MB_CMDOP] = 1
    struct.pack_into('<H', d, o + MB_CMDBLK, status_block(d))
    return ser


def ask_delete(d, name, ftype='S'):
    """Ask the sampler to delete a RESIDENT item (its memory, not the
    card).  Reuses the mailbox's name/type fields, which is why a delete
    and a deposit cannot be in flight at once - ask, then poll, then ask
    for a status to see the memory come back."""
    o = MAILBOX_BLK * BLOCK
    if bytes(d[o:o + 4]) != MAGIC:
        announce(d, 0, DEF_VOL, b' ' * 10, ' ')
        d[o + 4:o + 6] = b'\x00\x00'
    d[o + MB_NAME:o + MB_NAME + 10] = _name10(name)
    d[o + MB_TYPE] = ord(ftype)
    ser = (struct.unpack_from('<H', d, o + MB_CMDSER)[0] + 1) & 0xFFFF or 1
    struct.pack_into('<H', d, o + MB_CMDSER, ser)
    d[o + MB_CMDOP] = 2
    struct.pack_into('<H', d, o + MB_CMDBLK, status_block(d))
    return ser


def status(d):
    """Parse the sampler's reply block.  None until it has answered."""
    o = status_block(d) * BLOCK
    if bytes(d[o:o + 4]) != STAT_MAGIC:
        return None
    ser, banks, count = struct.unpack_from('<HHH', d, o + 4)
    items = []
    for i in range(min(count, STAT_MAX)):
        e = o + 10 + i * 14
        name = bytes(d[e:e + 10])
        length = int.from_bytes(bytes(d[e + 11:e + 14]), 'little')
        kind = chr(d[e + 10])
        # Only a SAMPLE header carries a length at +0x10; a program's
        # word there is something else, so do not present it as a size.
        items.append({'name': name.rstrip().decode('ascii', 'replace'),
                      'type': kind,
                      'points': length if kind == 'S' else None})
    total = banks * WORDS_PER_BANK
    used = sum(i['points'] for i in items if i['type'] == 'S')
    return {'serial': ser, 'banks': banks, 'total_words': total,
            'used_words': used, 'free_words': total - used, 'items': items}


def mailbox(d):
    o = MAILBOX_BLK * BLOCK
    mb = bytes(d[o:o + MB_LEN])
    if mb[0:4] != MAGIC:
        return {'serial': 0, 'volume': None, 'name': None, 'type': None}
    return {'serial': struct.unpack_from('<H', mb, 4)[0],
            'volume': mb[6:16], 'name': mb[16:26], 'type': chr(mb[26])}


def deposit_wav(d, path, name=None, mode='L', volname=DEF_VOL,
                notify=True, rate=build_sounddisk.RATE, **edit):
    if name is None:
        name = os.path.splitext(os.path.basename(path))[0]
    pts = build_sounddisk.wav_points(path, None, rate=rate)
    body = build_sounddisk.sample_file(_name10(name), pts, mode, rate=rate,
                                       **edit)
    return deposit(d, name, 'S', body, volname, notify=notify), len(pts)


# ---- CLI -------------------------------------------------------------------

def _open_image(path):
    f = open(path, 'r+b')
    return f, mmap.mmap(f.fileno(), 0)


def _arg(args, flag, default=None, conv=str):
    if flag in args:
        i = args.index(flag)
        v = conv(args[i + 1])
        del args[i:i + 2]
        return v
    return default


def main():
    args = sys.argv[1:]
    if not args or args[0] not in ('new', 'addvol', 'put', 'clear', 'status',
                                   'watch'):
        print(__doc__)
        sys.exit(2)
    cmd = args.pop(0)
    volname = _arg(args, '--volname', DEF_VOL.decode().strip())
    if cmd == 'new':
        blocks = _arg(args, '--blocks', DEFAULT_BLOCKS, lambda x: int(x, 0))
        path = args[0]
        open(path, 'wb').write(new_image(volname, blocks))
        print(f'OK: {path} ({blocks:#x} x 8 KB blocks, drop-box volume '
              f'{_name10(volname)!r}, mailbox empty)')
        return
    path = args.pop(0)
    f, d = _open_image(path)
    try:
        if cmd == 'addvol':
            # create_volume is idempotent and additive: it takes the next
            # free master-directory slot and one free block for the new
            # volume's own directory, so an existing volume (SUPEROS and
            # its bootable OS file) is untouched.
            had = volumes(d)
            blk = create_volume(d, volname)
            if _name10(volname) in had:
                print(f'OK: {path} already had volume '
                      f'{_name10(volname)!r} at block {blk}, unchanged')
            else:
                print(f'OK: {path} gained drop-box volume '
                      f'{_name10(volname)!r} at block {blk}; kept '
                      + ', '.join(repr(v) for v in had))
        elif cmd == 'clear':
            n = clear(d, volname)
            print(f'OK: {path} drop-box '
                  f'{_name10(volname).decode().rstrip()!r} emptied '
                  f'({n} files removed)')
        elif cmd == 'status':
            mb = mailbox(d)
            print(f'mailbox: serial {mb["serial"]}, last {mb["type"]} '
                  f'{mb["name"]!r} in volume {mb["volume"]!r}')
            for vn, blk in volumes(d).items():
                print(f'volume {vn!r} (dir block {blk}):')
                for i, nm, t, ln, st in entries(d, blk):
                    print(f'  [{i:3}] {t} {nm!r} {ln:7} bytes @ block {st}')
        elif cmd == 'put':
            name = _arg(args, '--name')
            mode = 'O' if '--oneshot' in args else 'L'
            if '--oneshot' in args:
                args.remove('--oneshot')
            raw = None
            for flag, t in (('--sfile', 'S'), ('--pfile', 'P')):
                if flag in args:
                    args.remove(flag)
                    raw = t
            src = args[0]
            if raw:
                nm = name or os.path.splitext(os.path.basename(src))[0]
                serial = deposit(d, nm, raw, open(src, 'rb').read(),
                                 volname)
                print(f'OK: {raw} file {_name10(nm)!r} deposited, '
                      f'serial {serial}')
            else:
                serial, n = deposit_wav(d, src, name, mode, volname)
                print(f'OK: {src} -> {n} points at {build_sounddisk.RATE} '
                      f'Hz, serial {serial}')
        elif cmd == 'watch':
            folder = args[0]
            seen = {}
            print(f'watching {folder} -> {path} (volume {volname}); ^C '
                  'to stop')
            while True:
                for fn in sorted(os.listdir(folder)):
                    if not fn.lower().endswith('.wav'):
                        continue
                    p = os.path.join(folder, fn)
                    mt = os.path.getmtime(p)
                    if seen.get(fn) == mt:
                        continue
                    if time.time() - mt < 1.0:
                        continue          # still being written
                    seen[fn] = mt
                    try:
                        serial, n = deposit_wav(d, p, None, 'L', volname)
                        d.flush()
                        print(f'{time.strftime("%H:%M:%S")} {fn}: '
                              f'{n} points, serial {serial}')
                    except Exception as e:          # keep watching
                        print(f'{fn}: {e}')
                time.sleep(1.0)
        d.flush()
    finally:
        d.close()
        f.close()


if __name__ == '__main__':
    main()
