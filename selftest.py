#!/usr/bin/env python3
"""One runnable check that this repo can still build a drop-box image.

    python3 selftest.py

Blank image -> deposit a sample -> deposit a program that points at it ->
read both back and check the mailbox announces the last deposit.  If this
passes, the encoders and the on-disk format in tools/ are intact.
"""
import os
import struct
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'tools'))
import build_sounddisk as bs
import s950dropbox as box


def main():
    d = box.new_image()

    pts = [int(3000 * (i % 64 - 32) / 32) for i in range(4096)]
    smp = bs.sample_file(b'SELFTEST  ', pts)
    box.deposit(d, b'SELFTEST  ', 'S', smp)

    prg = bs.program_file(b'SELFTEST  ', pname=b'SELFPRG   ', lokey=24, hikey=96)
    box.deposit(d, b'SELFPRG   ', 'P', prg)

    assert sorted(box.listing(d)) == [('SELFPRG', 'P', len(prg)),
                                      ('SELFTEST', 'S', len(smp))], box.listing(d)
    assert box.read_file(d, b'SELFTEST  ', ftype='S') == (bytes(smp), 'S')
    assert box.read_file(d, b'SELFPRG   ', ftype='P') == (bytes(prg), 'P')
    assert bytes(prg).find(b'SELFTEST  ') > 0, 'program lost its keygroup sample name'

    mb = d[box.MAILBOX_BLK * box.BLOCK:box.MAILBOX_BLK * box.BLOCK + box.MB_LEN]
    assert mb[:4] == b'SRX1', mb[:4]
    serial, = struct.unpack_from('<H', mb, 4)
    assert serial == 2, serial
    assert mb[box.MB_NAME:box.MB_NAME + 10] == b'SELFPRG   ', mb[16:26]
    assert mb[box.MB_TYPE] == ord('P')

    print('ok: image built, 2 files deposited, mailbox announces SELFPRG (serial 2)')


if __name__ == '__main__':
    main()
