#!/usr/bin/env python3
"""S950 Wi-Fi drop-box: deposit a sample and push it to the running ZuluSCSI.

The chain, end to end:

    audio -> s950dropbox.deposit() into a LOCAL MIRROR of the card image
          -> the changed byte ranges (diffed, 512-byte granular)
          -> AKPATCH1 file
          -> ZuluSCSI SuperOS loader over UDP (tools/zulu_udp.py), which
             writes them into the image it is serving and invalidates its
             read prefetch
          -> SuperOS 4.0.3 rx_poll sees the mailbox serial change at its
             next idle poll and hands the file to the stock loader

Nothing here touches the SCSI bus or the DISK page, and the board keeps
serving the S950 throughout.  The mirror and the card image must stay
identical: every deposit is planned against the mirror, so a push that
fails leaves the pending patch on disk and the next push retries it.

The mailbox range is written LAST (it is the newest range in the patch and
`push` sorts it to the end), so a half-applied patch never announces a file
whose blocks are not there yet.

  s950wifi.py put   IMAGE FILE.wav [--name NAME] [--oneshot] [--no-push]
                    [--rate HZ]   sample rate; default 40000
                    [--no-load]   store on the card without loading to RAM
  s950wifi.py put   IMAGE FILE.s --sfile [--name NAME]
  s950wifi.py unload IMAGE NAME [--type S]      delete an item from the
                                                SAMPLER'S MEMORY (frees it)
  s950wifi.py state  IMAGE [--host H] [--id N]  ask the SAMPLER what is in
                                                its memory and how much is
                                                free (survives DISK-page
                                                loads we never saw)
  s950wifi.py ls     IMAGE                      list the drop-box volume
  s950wifi.py rm     IMAGE NAME [--type S]      delete ONE drop-box file
  s950wifi.py clear  IMAGE [--host H] [--id N]  empty the drop-box volume
  s950wifi.py forget IMAGE [--host H] [--id N]  clear the announcement, so
                                                a power-cycle loads nothing
  s950wifi.py push  IMAGE [--host H] [--id N]
  s950wifi.py status IMAGE [--host H] [--id N]
  s950wifi.py verify IMAGE [--host H] [--id N]   whole-image compare

--host defaults to $ZULU_HOST or 192.168.1.250; --id to 0 (the S950 driver
always selects target id 0, LUN 0).
"""
import os
import struct
import sys
import time
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import s950dropbox as db                                          # noqa: E402
from build_hdimage import BLOCK                                   # noqa: E402

DEF_HOST = os.environ.get('ZULU_HOST', '192.168.1.250')
DEF_ID = 0
GRAIN = 512                     # the board's sector size; range granularity
PATCH_MAGIC = b'AKPATCH1'


# ---- AKPATCH1 (same container as AkaiS1000/tools/akai_disk.py) -------------

def write_patch(path, writes):
    """magic 8 | count u32 | { offset u64, length u32, crc32 u32, data }*"""
    with open(path, 'wb') as f:
        f.write(PATCH_MAGIC)
        f.write(struct.pack('<I', len(writes)))
        for off, data in writes:
            f.write(struct.pack('<QII', off, len(data),
                                zlib.crc32(data) & 0xFFFFFFFF))
            f.write(data)


def read_patch(path):
    with open(path, 'rb') as f:
        if f.read(8) != PATCH_MAGIC:
            raise SystemExit(f'not an AKPATCH1 file: {path}')
        (count,) = struct.unpack('<I', f.read(4))
        out = []
        for i in range(count):
            off, length, crc = struct.unpack('<QII', f.read(16))
            data = f.read(length)
            if len(data) != length:
                raise SystemExit(f'patch truncated in record {i}')
            if (zlib.crc32(data) & 0xFFFFFFFF) != crc:
                raise SystemExit(f'CRC mismatch in record {i} (offset {off})')
            out.append((off, data))
        return out


def merge(writes):
    """Coalesce overlapping/adjacent ranges into one ordered list.

    Later records win where they overlap earlier ones, which is what
    stacking two deposits means: the second one's directory and mailbox
    bytes are the ones to push.

    Every range comes from diff_ranges() and so starts on a GRAIN
    boundary; the merge is per grain-sized chunk, which keeps it linear in
    the number of chunks rather than in bytes.
    """
    chunks = {}
    for off, data in writes:
        if off % GRAIN:
            raise ValueError(f'range at {off} is not {GRAIN}-aligned')
        for i in range(0, len(data), GRAIN):
            chunks[(off + i) // GRAIN] = data[i:i + GRAIN]
    out = []
    for idx in sorted(chunks):
        off = idx * GRAIN
        if out and off == out[-1][0] + len(out[-1][1]):
            out[-1][1] += chunks[idx]
        else:
            out.append([off, bytearray(chunks[idx])])
    return [(o, bytes(d)) for o, d in out]


# ---- planning against the mirror ------------------------------------------

def diff_ranges(before, after, grain=GRAIN):
    """Changed byte ranges, snapped out to `grain` and coalesced."""
    if len(before) != len(after):
        raise ValueError('image size changed')
    hits = []
    n = (len(before) + grain - 1) // grain
    mb, ma = memoryview(before), memoryview(after)
    for i in range(n):
        a, b = i * grain, min((i + 1) * grain, len(before))
        if mb[a:b] != ma[a:b]:
            if hits and hits[-1][1] == a:
                hits[-1][1] = b
            else:
                hits.append([a, b])
    return [(a, bytes(ma[a:b])) for a, b in hits]


def pending_path(image):
    return image + '.pending.akpatch'


def plan(image, fn, *args, **kw):
    """Run a s950dropbox mutation against the mirror, record the ranges.

    Returns (result, ranges).  The ranges are appended to the image's
    pending patch, so several deposits can be planned and pushed at once.
    """
    with open(image, 'rb') as f:
        before = f.read()
    d = bytearray(before)
    result = fn(d, *args, **kw)
    ranges = diff_ranges(before, d)
    if not ranges:
        return result, []
    with open(image, 'r+b') as f:
        for off, data in ranges:
            f.seek(off)
            f.write(data)
        f.flush()
        os.fsync(f.fileno())
    p = pending_path(image)
    old = read_patch(p) if os.path.exists(p) else []
    write_patch(p, merge(old + ranges))
    return result, ranges


def plan_wav(image, path, name=None, mode='L', volname=db.DEF_VOL,
             notify=True, rate=None, **edit):
    kw = {} if rate is None else {'rate': rate}
    kw.update({k: v for k, v in edit.items() if v is not None})
    return plan(image, db.deposit_wav, path, name=name, mode=mode,
                volname=volname, notify=notify, **kw)


def plan_file(image, name, ftype, payload, volname=db.DEF_VOL, notify=True):
    return plan(image, db.deposit, name, ftype, payload, volname=volname,
                notify=notify)


def refresh_state(image, host=DEF_HOST, sid=DEF_ID, loader=None,
                  timeout=8.0, poll=0.4):
    """Ask the sampler what is in its memory, and wait for the answer.

    The editor cannot infer this: the DISK page loads and clears memory
    without going anywhere near the drop-box, so anything the host
    remembers about the machine's RAM is a guess.  This asks.

    Bumps the mailbox command serial, pushes it, then re-reads the reply
    block off the BOARD until the sampler answers with the serial we
    asked for - it only answers on an idle poll, so this takes about a
    second, longer if a voice is sounding or the DISK section is up.
    The reply is copied into the mirror so the two stay byte-identical.
    """
    ser = plan(image, db.ask_status)[0]
    push(image, host, sid, loader=loader)
    return await_reply(image, ser, host, sid, loader=loader,
                       timeout=timeout, poll=poll)


def await_reply(image, ser, host=DEF_HOST, sid=DEF_ID, loader=None,
                timeout=8.0, poll=0.4):
    """Re-read the reply block off the BOARD until the sampler answers the
    command `ser`, and copy the answer into the mirror."""
    z = loader or _loader(host, timeout=2.0)
    with open(image, 'rb') as f:
        mirror = bytearray(f.read())
    blk = db.status_block(mirror)
    off, n = blk * BLOCK, 10 + db.STAT_MAX * 14
    deadline = time.time() + timeout
    while time.time() < deadline:
        got = z.read(sid, off, n)
        mirror[off:off + n] = got
        st = db.status(mirror)
        if st and st['serial'] == ser:
            with open(image, 'r+b') as f:      # keep the mirror in step
                f.seek(off)
                f.write(got)
            return st
        time.sleep(poll)
    raise RuntimeError(f'the sampler did not answer command {ser} within '
                       f'{timeout:.0f}s (a voice sounding, or the DISK or '
                       f'RECORD section up, holds the poll off)')


def load_resident(image, name, ftype='S', host=DEF_HOST, sid=DEF_ID,
                  loader=None, timeout=12.0):
    """Announce a drop-box file and WAIT until the sampler holds it.

    Announcing only asks; the load happens at the sampler's next idle
    poll, so reading the status straight afterwards reports the state
    from before the load and the caller shows stale numbers.  This polls
    the sampler's own account until the item appears, so what it returns
    is true when it returns it.
    """
    def bump(d):
        ser = (db.mailbox(d)['serial'] + 1) & 0xFFFF or 1
        db.announce(d, ser, db.DEF_VOL, db._name10(name), ftype)
        return ser
    plan(image, bump)
    push(image, host, sid, loader=loader)
    want = db._name10(name).rstrip().decode('ascii', 'replace')
    deadline = time.time() + timeout
    st = None
    while time.time() < deadline:
        st = refresh_state(image, host, sid, loader=loader, timeout=timeout)
        if any(i['name'] == want and i['type'] == ftype for i in st['items']):
            return st
    raise RuntimeError(f'{want!r} did not appear in the sampler\'s memory '
                       f'within {timeout:.0f}s - too big to fit, or the '
                       f'poll is held off by a sounding voice or the DISK '
                       f'section')


def delete_resident(image, name, ftype='S', host=DEF_HOST, sid=DEF_ID,
                    loader=None, timeout=10.0):
    """Delete an item from the SAMPLER'S MEMORY and confirm it is gone.

    Nothing on the card changes; this frees sample memory, which is what
    the editor needs when the next sample will not fit.  Confirmation is
    a status read, not an assumption: the sampler is asked what it holds
    afterwards and the item has to be absent from the answer.
    """
    ser = plan(image, db.ask_delete, name, ftype)[0]
    push(image, host, sid, loader=loader)
    # Wait for the reply to THIS command.  Opcode 2 publishes the new state
    # itself, so there is no second request: asking for a delete and then
    # asking for a status is two serials, and both land on the card between
    # two polls - the sampler sees only the second and the delete never
    # happens, which is exactly what it did on hardware.
    st = await_reply(image, ser, host, sid, loader=loader, timeout=timeout)
    want = db._name10(name).rstrip().decode('ascii', 'replace')
    if any(i['name'] == want and i['type'] == ftype for i in st['items']):
        raise RuntimeError(f'{want!r} is still resident after the delete')
    return st


def plan_remove(image, name, volname=db.DEF_VOL, ftype=None):
    """Delete one drop-box file in the mirror and record the ranges."""
    return plan(image, db.remove, name, volname=volname, ftype=ftype)


def listing(image, volname=db.DEF_VOL):
    with open(image, 'rb') as f:
        return db.listing(bytearray(f.read()), volname)


# ---- pushing to the board --------------------------------------------------

def _loader(host, timeout=1.0):
    import zulu_udp
    return zulu_udp.ZuluLoader(host, timeout=timeout)


def mailbox_offset():
    return db.MAILBOX_BLK * BLOCK


def mailbox_last(writes):
    """Reorder so the GRAIN chunk holding the mailbox head is written last.

    merge() fuses block 0 (master directory), 1-2 (FAT) and 3 (mailbox)
    into one contiguous range, so the split has to happen inside a record,
    not between records: the announcing chunk is cut out and appended.
    Until it lands the firmware reads the old serial, so a push that dies
    half way never points the loader at blocks that are not there yet.
    """
    mb = mailbox_offset()
    cut = mb - mb % GRAIN
    head, tail = [], []
    for off, data in writes:
        if off <= cut < off + len(data):
            a = cut - off
            for piece in ((off, data[:a]), (cut + GRAIN,
                                            data[a + GRAIN:])):
                if piece[1]:
                    head.append(piece)
            tail.append((cut, data[a:a + GRAIN]))
        else:
            head.append((off, data))
    return head + tail


def push(image, host=DEF_HOST, sid=DEF_ID, progress=None, loader=None):
    """Apply the pending patch to the served image and verify it.

    The mailbox range goes last: until it lands the firmware sees the old
    serial and will not try to load a file whose blocks are still missing.
    """
    p = pending_path(image)
    if not os.path.exists(p):
        return 0, 0
    writes = read_patch(p)
    if not writes:
        os.remove(p)
        return 0, 0
    z = loader or _loader(host)
    z.write_ranges(sid, mailbox_last(writes), progress=progress)
    os.remove(p)
    return len(writes), sum(len(d) for _, d in writes)


def verify(image, host=DEF_HOST, sid=DEF_ID, loader=None, chunk=64 * 1024):
    """Compare the whole served image with the mirror.  Returns byte count
    of the ranges that differ (0 = identical)."""
    z = loader or _loader(host)
    size = os.path.getsize(image)
    bad = 0
    with open(image, 'rb') as f:
        off = 0
        while off < size:
            n = min(chunk, size - off)
            want = f.read(n)
            got = z.read(sid, off, n)
            if got != want:
                bad += sum(1 for a, b in zip(want, got) if a != b)
            off += n
    return bad


# ---- CLI -------------------------------------------------------------------

def _arg(args, flag, default=None, conv=str):
    if flag in args:
        i = args.index(flag)
        v = conv(args[i + 1])
        del args[i:i + 2]
        return v
    return default


def _flag(args, name):
    if name in args:
        args.remove(name)
        return True
    return False


def main():
    args = sys.argv[1:]
    if not args or args[0] not in ('put', 'push', 'clear', 'forget',
                                   'status', 'verify', 'ls', 'rm', 'state', 'unload'):
        sys.exit(__doc__)
    cmd = args.pop(0)
    host = _arg(args, '--host', DEF_HOST)
    sid = _arg(args, '--id', DEF_ID, int)
    image = args.pop(0)

    if cmd == 'put':
        name = _arg(args, '--name')
        sfile = _flag(args, '--sfile')
        pfile = _flag(args, '--pfile')
        no_push = _flag(args, '--no-push')
        mode = 'S' if _flag(args, '--oneshot') else 'L'
        src = args.pop(0)
        # --no-load writes the file to the card but does not announce it,
        # so the sampler leaves it where it is instead of loading it into
        # RAM.  That is how a library is built up on the ZuluSCSI.
        notify = not _flag(args, '--no-load')
        rate = _arg(args, '--rate', None, int)   # header rate AND the
                                                 # resample target
        if sfile or pfile:
            body = open(src, 'rb').read()
            nm = name or os.path.splitext(os.path.basename(src))[0]
            (serial, ranges) = plan_file(image, nm, 'S' if sfile else 'P',
                                         body, notify=notify)
            pts = len(body)
            unit = 'bytes'
        else:
            (res, ranges) = plan_wav(image, src, name=name, mode=mode,
                                     notify=notify, rate=rate)
            serial, pts = res
            unit = 'points'
        print(f'planned: {"stored on the card, not announced" if not notify else f"serial {serial}"}, '
              f'{pts} {unit}, '
              f'{len(ranges)} ranges, {sum(len(d) for _, d in ranges)} bytes')
        if no_push:
            return
        cmd = 'push'

    if cmd == 'forget':
        # The announcement lives on the card but the firmware's memory of
        # which serial it last acted on lives in the image, so it is zero
        # at every power-on: the first poll after a boot sees the last
        # deposit as new and loads it again.  Zeroing the mailbox here
        # leaves nothing to announce, which is the host-side cure and
        # costs no firmware.
        def wipe(d):
            o = db.MAILBOX_BLK * BLOCK
            d[o:o + db.MB_LEN] = bytes(db.MB_LEN)
            return 1
        _, ranges = plan(image, wipe)
        print(f'announcement cleared, {len(ranges)} ranges to push')
        cmd = 'push'

    if cmd == 'rm':
        name = args[0] if args else None
        if not name:
            print('usage: s950wifi.py rm IMAGE NAME [--type S]')
            return
        ftype = _arg(args, '--type')
        gone, ranges = plan_remove(image, name, ftype=ftype)
        if not gone:
            print(f'no file {name!r} in the drop-box')
            return
        nm, t, ln = gone
        print(f'removed {nm.rstrip().decode()!r} type {t} '
              f'({ln} bytes), {len(ranges)} ranges to push')
        cmd = 'push'

    if cmd == 'unload':
        name = args[0] if args else None
        if not name:
            print('usage: s950wifi.py unload IMAGE NAME [--type S]')
            return
        st = delete_resident(image, name, _arg(args, '--type', 'S'),
                             host, sid)
        print(f"{name} unloaded; used {st['used_words']}, "
              f"free {st['free_words']} words")
        for i in st['items']:
            sz = (f"{i['points']:>8} points" if i['points'] is not None
                  else '         -')
            print(f"  {i['name']:<11}{i['type']}{sz}")
        return

    if cmd == 'state':
        st = refresh_state(image, host, sid)
        print(f"banks {st['banks']}  total {st['total_words']} words")
        print(f"used {st['used_words']}  free {st['free_words']} "
              f"({100 * st['free_words'] // max(st['total_words'], 1)}% free)")
        for i in st['items']:
            sz = (f"{i['points']:>8} points" if i['points'] is not None
                  else '         -')
            print(f"  {i['name']:<11}{i['type']}{sz}")
        return

    if cmd == 'ls':
        files = listing(image)
        if not files:
            print('drop-box is empty')
        for nm, t, ln in files:
            print(f'  {nm:<10} {t}  {ln:>9} bytes')
        return

    if cmd == 'clear':
        n, ranges = plan(image, db.clear)
        print(f'cleared {n} files from the drop-box, '
              f'{len(ranges)} ranges to push')
        if not n:
            return
        cmd = 'push'

    if cmd == 'push':
        t = time.time()
        total = [0]

        def prog(n):
            total[0] = n
            sys.stderr.write(f'\r{n} bytes')
        n, nbytes = push(image, host, sid, progress=prog)
        sys.stderr.write('\n')
        if not n:
            print('nothing pending')
            return
        dt = time.time() - t
        print(f'pushed {n} ranges, {nbytes} bytes, verified, '
              f'{dt:.1f} s ({nbytes / 1024 / max(dt, 1e-6):.0f} KB/s)')

    elif cmd == 'status':
        with open(image, 'rb') as f:
            d = bytearray(f.read())
        mb = db.mailbox(d)
        print(f"mirror mailbox: serial {mb['serial']}, "
              f"last {mb['name']} {mb['type']} in volume {mb['volume']}")
        p = pending_path(image)
        if os.path.exists(p):
            w = read_patch(p)
            print(f'pending: {len(w)} ranges, '
                  f'{sum(len(x) for _, x in w)} bytes')
        else:
            print('pending: none')
        try:
            z = _loader(host, timeout=0.8)
            print(f'board: ping {z.ping() * 1000:.0f} ms, id {sid} '
                  f'{z.info(sid)}')
        except Exception as e:                                # noqa: BLE001
            print(f'board: unreachable ({e})')

    elif cmd == 'verify':
        bad = verify(image, host, sid)
        print('image matches the mirror' if not bad
              else f'{bad} bytes differ')


if __name__ == '__main__':
    main()
