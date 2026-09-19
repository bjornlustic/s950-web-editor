#!/usr/bin/env python3
"""Build a bootable Akai S950 SCSI hard-disk image (ZuluSCSI/BlueSCSI).

The stock S950 1.2B ROM boots any type-'M' file from the ACTIVE volume
through the volume-generic loader (0x32DC, dest 0x1000:0x4610, int 0x11
tail) - floppy or SCSI hard disk alike.  This tool emits a flat disk
image whose single volume carries an OS image as a bootable 'M' file:
write it to a ZuluSCSI/BlueSCSI SD card as the SCSI ID 0 device (the
S950 driver always selects target id 0, LUN 0), select the volume on
the DISK page, and "load entire disk" (option 1) boots the OS.
Emulation proof: tools/test_scsiboot.py runs this image through the
real ROM code against the behavioral SCSI model (tools/scsi950.py).

On-disk layout (RE of the 1.2B hd driver, notes/s950_disk_boot.md):
  block = 0x2000 bytes (8 KB).  The driver addresses the target in the
  sector size READ CAPACITY reports and converts ([0x3113]), so the
  same flat image works behind a 512-byte-sector target (ZuluSCSI):
  block n is simply the 8 KB at byte offset n*0x2000.
  block 0     master directory: 128 x 12-byte VOLUME entries
              (name[10] + start-block word, 0 = free); the dir read
              stages 0x604 bytes of it at 0x1000:0000
  blocks 1-2  hd FAT, one word per block, next-block index, bit15 =
              end of chain (staged at 0x1000:0604, first data block 4
              per the free-block scan 0x2FBE)
  block 3     reserved (never scanned)
  blocks 4-   data.  A volume's start block holds its directory:
              128 x 24-byte FILE entries (name[10] +0, type +0x10,
              len16 bytes +0x11, start block +0x14), read to RAM
              0xA2E6; file content is FAT-chained from block 4 up.

The driver caps the usable device at [0x1E3C] <= 0x1FFF blocks, i.e.
64 MB - the default image size (the FAT for it exactly fills blocks
1-2, and the staging FAT top 0x1000:0x4604 sits just under the OS
load buffer 0x1000:0x4610).

Type-'M' file limits on the hd (8 KB-block wrap analysis, mirrored
from the floppy notes):
  - the load-buffer wrap makes RAM 0xB9F0..0xBFFF uncarriable (the
    straddling 8 KB block DMAs its tail past the segment while the
    int 0x11 copy source wraps) - wider than the floppy's 1 KB window;
    the file must ship zeros there.
  - the block AFTER the wrap (file bytes 0xC000..) DMAs to
    0x1000:0610, right on top of the staged hd FAT at 0x1000:0604 -
    and the loader (0x33E6) walks the FAT to its END MARK, reading
    one more staged entry after every block, including the last.  A
    contiguously-allocated chain puts that entry inside the clobbered
    range and the walk runs away (emulation-verified).  This builder
    therefore places the post-wrap block at a HIGH block number whose
    staged FAT entry (0x604+2*blk) sits above the clobber extent
    (0x610+postlen) - a pure layout fix, byte-identical mechanics on
    real hardware.  NOTE: the stock save path allocates contiguously,
    so an OS saved to hd by the factory backdoor only boots up to
    length 0xC000 (its cap is 0xA27F anyway).
  - a file longer than 0xE000 needs a second post-wrap block, whose
    surviving-entry bound exceeds the largest addressable block - so
    0xE000 is a hard cap.

Usage: build_hdimage.py <os_image.bin> <out.img>
       [--name "S950SUPER "]  the 'M' file name (10 chars)
       [--volname "SUPEROS   "]  the volume name (10 chars) - the name
                            to select on the DISK page
       [--oslen N]          length of the OS file (default: full
                            input, or 0xC524 for a 64KB EPROM image)
       [--blocks N]         disk size in 8 KB blocks (default 0x2000
                            = 64 MB, the stock driver's cap)
"""
import os
import struct
import sys

BLOCK = 0x2000
MASTER_ENTRIES = 128          # 12-byte volume entries in block 0
DIR_ENTRIES = 128             # 24-byte file entries in a volume dir
FAT_OFF = BLOCK               # FAT lives in blocks 1..2
FIRST_DATA = 4                # free-block scan starts here (0x2FBE)
DEFAULT_BLOCKS = 0x2000       # 64 MB: driver caps [0x1E3C] at 0x1FFF
# RAM window a type-'M' hd file cannot carry (8 KB straddle block tail)
STRADDLE = (0xB9F0, 0xC000)
MAX_OSLEN = 0xE000            # one post-wrap block only (see docstring)

VOLNAME = b'SUPEROS   '


def build(files, volname=VOLNAME, nblocks=DEFAULT_BLOCKS):
    """files: list of (name10, type_char, payload) -> flat disk image.

    The volume directory goes in block 4; file data is FAT-chained
    contiguously from block 5.
    """
    assert len(volname) == 10
    assert nblocks <= 0x2000, f'{nblocks:#x} blocks > driver cap'
    d = bytearray(nblocks * BLOCK)

    def fat_set(blk, val):
        struct.pack_into('<H', d, FAT_OFF + 2 * blk, val)

    # master directory: one volume, dir in block 4
    vol_blk = FIRST_DATA
    d[0:10] = volname
    struct.pack_into('<H', d, 10, vol_blk)
    fat_set(vol_blk, 0x8000)               # single-block volume dir

    nxt = vol_blk + 1
    high = min(nblocks - 2, 0x1FFE)        # entry 0x1FFF is never staged
    for i, (name, ftype, payload) in enumerate(files):
        assert len(name) == 10
        e = FIRST_DATA * BLOCK + i * 24    # entry i in the volume dir
        d[e:e + 10] = name
        d[e + 0x10] = ord(ftype)
        struct.pack_into('<H', d, e + 0x11, len(payload) & 0xFFFF)
        struct.pack_into('<H', d, e + 0x14, nxt)
        if ftype == 'M' and len(payload) > 0xC000:
            # boot file crossing the load-buffer wrap: the post-wrap
            # block goes to a high block so its staged FAT entry
            # survives the DMA clobber of the staging FAT (docstring)
            postlen = len(payload) - 0xC000
            assert len(payload) <= MAX_OSLEN
            b1 = high
            high -= 1
            assert 0x604 + 2 * b1 >= 0x610 + postlen, \
                'post-wrap FAT entry inside the clobber extent'
            for k in range(5):             # file bytes 0..0xBFFF: 6 blocks
                fat_set(nxt + k, nxt + k + 1)
            fat_set(nxt + 5, b1)
            fat_set(b1, 0x8000)
            d[nxt * BLOCK:nxt * BLOCK + 0xC000] = payload[:0xC000]
            d[b1 * BLOCK:b1 * BLOCK + postlen] = payload[0xC000:]
            nxt += 6
        else:
            nblk = (len(payload) + BLOCK - 1) // BLOCK
            for k in range(nblk):
                fat_set(nxt + k,
                        0x8000 if k == nblk - 1 else nxt + k + 1)
            d[nxt * BLOCK:nxt * BLOCK + len(payload)] = payload
            nxt += nblk
        assert nxt <= high + 1, 'files exceed the disk'
    return bytes(d)


def build_image(osbin, name=b'S950SUPER ', volname=VOLNAME, boot_flag=2,
                nblocks=DEFAULT_BLOCKS):
    """Bootable image: the OS as the volume's only file (type 'M')."""
    osbin = bytearray(osbin)
    # boot-source marker, same convention as the floppy disks
    # ([0x43]: 3 in EPROM, 2 in a disk-saved image; 2 keeps the loaded
    # OS off the power-on auto-boot path)
    if len(osbin) > 0x43 and osbin[0x43] == 3:
        osbin[0x43] = boot_flag
    assert len(osbin) <= MAX_OSLEN, \
        f'OS file {len(osbin):#x} > hd cap {MAX_OSLEN:#x}'
    lo, hi = STRADDLE
    if len(osbin) > lo:
        assert not any(osbin[lo:hi]), \
            f'OS file has content in the uncarriable window ' \
            f'{lo:#x}..{hi:#x} (hd 8 KB straddle)'
    return build([(name, 'M', bytes(osbin))], volname, nblocks)


def main():
    args = sys.argv[1:]
    name, volname, oslen, nblocks = 'S950SUPER ', 'SUPEROS   ', None, \
        DEFAULT_BLOCKS
    if '--name' in args:
        i = args.index('--name'); name = args[i + 1]; del args[i:i + 2]
    if '--volname' in args:
        i = args.index('--volname'); volname = args[i + 1]
        del args[i:i + 2]
    if '--oslen' in args:
        i = args.index('--oslen'); oslen = int(args[i + 1], 0)
        del args[i:i + 2]
    if '--blocks' in args:
        i = args.index('--blocks'); nblocks = int(args[i + 1], 0)
        del args[i:i + 2]
    os_path, out = args[:2]
    osbin = open(os_path, 'rb').read()
    if len(osbin) == 0x10000:            # full EPROM image
        oslen = oslen or 0xC524          # through the SuperOS blob home
    oslen = oslen or len(osbin)
    osbin = osbin[:oslen]
    d = build_image(osbin, name.encode('ascii'), volname.encode('ascii'),
                    nblocks=nblocks)
    open(out, 'wb').write(d)
    print(f'OK: {out} ({nblocks:#x} x 8 KB blocks = '
          f'{nblocks * BLOCK // (1 << 20)} MB, volume {volname!r}, '
          f"'M' file {name!r} {len(osbin):#x} bytes)")
    print('Write to the ZuluSCSI/BlueSCSI SD card as the SCSI ID 0 '
          'disk (e.g. HD0.img);')
    print('on the S950: DISK page -> select volume '
          f'{volname.strip()!r} -> "load entire disk" (1) -> ENT.')


if __name__ == '__main__':
    main()
