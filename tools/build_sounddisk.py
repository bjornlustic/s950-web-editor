#!/usr/bin/env python3
"""Build an S950 SOUND disk full of looped samples from a WAV.

Made for the 4.0.1 replay-pool test: the stock pool is 300 nodes and a
looped resident sample takes 3, so ~96 looped samples exercise the pool
right up to (but under) the stock ceiling - far past the 254-node
(84-sample) ceiling of the first 4.0.1 cut.

On-disk formats (reversed from archive/sounddisk_amen_2026-08-07.img,
loader-verified by emulation - see notes/s950_disk_boot.md):
  - directory: 64 x 24-byte entries (name[10], type +0x10, len16 +0x11,
    start block +0x14); FAT word/block at +0x600 (bit15 = chain end).
    DD: dir+FAT blocks 0..3, data from 4.  HD: blocks 0..4, data from 5.
  - type 'S' file = 0x3C header + PCM in the S950's split encoding:
    the FIRST half of the points as signed 16-bit LE words, the SECOND
    half as one byte each (top 8 bits) - see pack_pcm.  The header is
    the RAM sample header minus its 10 runtime bytes: name[10], zeros,
    +0x10 dword points, +0x14 sample RATE in Hz (hardware-tuned: a
    40 kHz sample with 25000 here played 8 semitones flat - the field
    is a frequency, not the period the first RE guessed), +0x16 0x3C0,
    +0x18 50, +0x1A replay mode 'L'/'O', +0x1C dword end, +0x20 dword
    start, +0x24 dword loop length, +0x2B 'N', +0x36 0x8000, +0x38 1.
    The 16-bit dir length is the true length mod 64K (FAT-chain-driven
    load, verified against both amen samples).
  - type 'P' file = 0x26 program header + 0x46 keygroup; the keygroup
    carries sample NAMES (resolved to headers at load).

Usage: build_sounddisk.py <in.wav> <outprefix> [--hd] [--count N]
       [--points P] (defaults: 96 slices x 2600 points @ 40 kHz
       = 250 Kwords, fits an unexpanded machine)
"""
import os
import struct
import sys
import wave

BLOCK = 1024
RATE = 40000                  # resample target AND the header rate
                              # field at +0x14 (Hz)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# The 108-byte TONE PRGRM record this module clones, lifted once out of the
# amen sounddisk (that floppy image itself is not in this repo: it carries a
# sample we do not own).  Extraction was: FAT at 0x600, chain from block 307,
# first 0x6C bytes.
PRGRM_TEMPLATE = os.path.join(ROOT, 'data', 'program_template.bin')


def wav_points(path, want, rate=RATE):
    """Mono-summed, linearly resampled to `rate`, full-scale 16-bit.

    The S950 has no fixed sample rate: the rate is a per-sample header
    field the hardware tunes to, so a shorter, lower-rate sample trades
    bandwidth for memory.  RATE is only the default.
    """
    w = wave.open(path)
    nch, sw, sr, n = (w.getnchannels(), w.getsampwidth(),
                      w.getframerate(), w.getnframes())
    raw = w.readframes(n)
    mono = []
    for i in range(n):
        acc = 0
        for c in range(nch):
            o = (i * nch + c) * sw
            v = int.from_bytes(raw[o:o + sw], 'little', signed=True)
            acc += v
        mono.append(acc / nch / (1 << (8 * sw - 1)))
    out = []
    total = int(n * rate / sr)
    for i in range(total):
        x = i * sr / rate
        j = int(x)
        f = x - j
        a = mono[j]
        b = mono[min(j + 1, n - 1)]
        out.append(a + (b - a) * f)
    peak = max(abs(v) for v in out) or 1
    return [int(v / peak * 32000) for v in out]


def sample_file(name, pts, mode='L', rate=RATE, start=None, end=None,
                loop=None, loudness=None, pitch=None):
    """Encode a sample.  The optional arguments are the fields the EDIT
    SAMPLE pages expose, set here so a sample can be trimmed and balanced
    BEFORE it ever reaches the sampler:

      start/end   replay points (header +0x20 / +0x1C).  Trimming here
                  costs no memory - the whole sample is still loaded -
                  but it is what the machine plays.
      loop        loop length (+0x24); only meaningful in 'L' mode.
      loudness    +0x18, the panel's 0..99 (template ships 50).
      pitch       +0x16, nominal pitch (template ships 0x3C0).
    """
    pts = pts[:len(pts) & ~1]     # the loader splits the file at
                                  # points/2 words: keep points even
    h = bytearray(0x3C)
    h[0:10] = name
    struct.pack_into('<I', h, 0x10, len(pts))
    struct.pack_into('<H', h, 0x14, rate)
    struct.pack_into('<H', h, 0x16, 0x3C0 if pitch is None else pitch)
    struct.pack_into('<H', h, 0x18, 50 if loudness is None else loudness)
    # replay mode byte: stock HDRFIX (0x6E70) builds a one-shot list for
    # 'O', alternating for 'A' and a loop for ANYTHING else.  The editor,
    # the API and s950wifi say 'S' for one-shot, which the machine played
    # looped; map it here, where every caller comes through.
    mode = {'S': 'O'}.get(mode, mode)
    if mode not in ('L', 'O', 'A'):
        raise ValueError(f'bad replay mode {mode!r} (L, O/S or A)')
    h[0x1A] = ord(mode)
    lo = 0 if start is None else max(0, min(len(pts), start))
    hi = len(pts) if end is None else max(lo, min(len(pts), end))
    struct.pack_into('<I', h, 0x1C, hi)         # end
    struct.pack_into('<I', h, 0x20, lo)         # start
    # loop length: full for looped replay; the archive's one-shot
    # samples ship 2000 (inert in 'O' mode)
    dfl = len(pts) if mode == 'L' else min(2000, len(pts))
    struct.pack_into('<I', h, 0x24,
                     dfl if loop is None else max(0, min(len(pts), loop)))
    h[0x2B] = ord('N')
    struct.pack_into('<H', h, 0x36, 0x8000)
    struct.pack_into('<H', h, 0x38, 1)
    return bytes(h) + pack_pcm(pts)


def unpack_pcm(data, npoints):
    """The inverse of pack_pcm: on-disk sample bytes back to points.

    First half of the points are verbatim signed 16-bit LE words; the
    second half are ONE byte each, the top 8 bits, which the loader puts
    in the replay word's high byte.  So the tail comes back at 8-bit
    resolution - that is what is on the disk, not a loss here.
    """
    half = npoints // 2
    out = []
    for i in range(half):
        out.append(struct.unpack_from('<h', data, i * 2)[0])
    tail = data[half * 2:half * 2 + (npoints - half)]
    for b in tail:
        out.append(struct.unpack('<h', bytes([0, b]))[0])
    return out


def pack_pcm(pts):
    """S950 on-disk sample encoding (RE'd from the ROM loader: RS
    service 0x2507/0x2555, S-load engine 0x3F34; halves confirmed by
    listening tests on hardware):
      - FIRST half of the points: one SIGNED 16-bit LE word each,
        the sample verbatim.  The loader rep-movsw's these into the
        replay plane unchanged (the shl-4 loop at 0x25C9 feeds a
        second plane at +[0x9EF6], not the replay data).
      - SECOND half: ONE byte each - the top 8 bits - which the
        loader stores into the replay word's HIGH byte (8-bit tail;
        stock disks have the same half-quality tail).
    Total = 3*points/2 bytes, matching the save-side size dispatcher
    at 0x22E8 (3*len/2 + 0x3C) and both archive sample dir entries."""
    n = len(pts) & ~1                       # loader splits at n/2 words
    pts = pts[:n]
    words = b''.join(struct.pack('<h', p) for p in pts[:n // 2])
    tail = bytes(((p >> 8) & 0xFF) for p in pts[n // 2:])
    return words + tail


# ---------------------------------------------------------------------------
# Keygroup fields (0x46 bytes), from notes/s950_port_map.md.  Every offset
# here is round-tripped through a real load by test_progedit.py: written
# into a program file, loaded by the sampler, read back out of RAM.
#
#   name: (offset, size, lo, hi, what the machine's own page calls it)
KG_FIELDS = {
    'hikey':     (0x00, 1, 0, 127,  'high key'),
    'lokey':     (0x01, 1, 0, 127,  'low key'),
    'attack':    (0x03, 1, 0, 99,   'attack'),
    'decay':     (0x04, 1, 0, 99,   'decay'),
    'sustain':   (0x05, 1, 0, 99,   'sustain'),
    'release':   (0x06, 1, 0, 99,   'release'),
    'velsens':   (0x07, 1, 0, 99,   'velocity to filter'),
    'keytrack':  (0x08, 1, 0, 99,   'filter keytrack'),
    'velloud':   (0x0B, 1, 0, 99,   'velocity to loudness'),
    'lfodelay':  (0x0F, 1, 0, 99,   'LFO delay'),
    'lforate':   (0x10, 1, 0, 99,   'LFO rate'),
    'lfodepth':  (0x11, 1, 0, 99,   'LFO depth'),
    'midich':    (0x14, 1, 0, 15,   'MIDI channel offset'),
    'vcfamount': (0x17, 1, 0, 99,   'VCF envelope amount'),
    'vcfattack': (0x22, 1, 0, 99,   'VCF attack'),
    'vcfdecay':  (0x23, 1, 0, 99,   'VCF decay'),
    'vcfsustain': (0x24, 1, 0, 99,  'VCF sustain'),
    'vcfrelease': (0x25, 1, 0, 99,  'VCF release'),
    'filter':    (0x2C, 1, 0, 99,   'filter (soft)'),
    'loudness':  (0x2D, 1, 0, 99,   'loudness (soft)'),
    'filterhi':  (0x42, 1, 0, 99,   'filter (loud)'),
    'loudnesshi': (0x43, 1, 0, 99,  'loudness (loud)'),
    # SuperOS additions (4.0.2c per-keygroup work, PERKG)
    'porta':     (0x3C, 1, 0, 127,  'portamento time'),  # *04 Porta 0-127
    'aacap':     (0x3D, 1, 0, 3,    'anti-alias cap mode'),
}
KG_SAMPLE_SOFT = 0x18       # 10 bytes
KG_SAMPLE_LOUD = 0x2E       # 10 bytes (stock relink 0x01BE, KG page 0x5A39)
KG_START = 0x3A             # word, bit 15 = "set"  (SuperOS)
# The End pair sits in padding bytes: stock S950 code resolves and prints
# the loud sample name at kg+0x2E..0x37 (the relink 0x018C walk does
# `add di,0x2e`), so kg+0x38..0x3D and kg+0x27 are spare.  An earlier
# version wrote the loud name at kg+0x30, which made kg+0x39 look like
# its tenth character and left the loud section unlinked on the sampler
# (tools/test_s900fix.py K2).
KG_END_LO = 0x39
KG_END_HI = 0x27            # bit 7 = "set"
KG_CHAIN = 0x44
KG_SIZE = 0x46


def _name10(s):
    if isinstance(s, str):
        s = s.encode('ascii', 'replace')
    return (s + b' ' * 10)[:10]


def program_file(first_sample, pname=b'POOLTEST  ', lokey=None,
                 hikey=None, keygroups=None):
    """Clone the amen disk's TONE PRGRM, repoint its keygroup at the
    first sample (both the program name and the kg sample name).

    ONE keygroup, and deliberately so.  The keygroup chain at kg+0x44 is a
    raw RAM pointer that the loader takes VERBATIM - a deliberately bogus
    0xDEAD written into a file came back out of RAM unchanged - and a
    program lands wherever memory happens to be free (0xC5F6 with nothing
    else resident, 0xC70E behind one sample).  So the host cannot chain a
    second keygroup: it cannot know the address.  Machine-saved multi-
    keygroup programs work because the DISK page clears memory first and
    the layout is then deterministic; building one here would not.
    Chain stays 0.

    lokey/hikey set the keygroup's key range (kg+1 low, kg+0 high, MIDI
    note numbers); the template's own range is kept when they are None.
    """
    p = bytearray(open(PRGRM_TEMPLATE, 'rb').read()[:0x6C])
    p[0:10] = pname
    i = bytes(p).find(b'AMEN2     ')
    assert i > 0, 'kg sample name not found in the template program'
    p[i:i + 10] = first_sample
    if hikey is not None:
        p[0x26 + 0] = max(0, min(127, hikey))
    if lokey is not None:
        p[0x26 + 1] = max(0, min(127, lokey))
    struct.pack_into('<H', p, 0x26 + 0x44, 0)
    if not keygroups:
        p[0x17] = 1
        return bytes(p)

    # More than one keygroup.  Header +0x17 is the COUNT: the loader
    # copies exactly that many 0x46-byte keygroups out of the file and
    # ignores the rest, which is why a file with three keygroups and a
    # stale count of 1 loaded only the first (found by setting each
    # candidate header byte to 3 and seeing which one made three appear).
    # The chain at kg+0x44 stays 0: the loader takes it VERBATIM and the
    # host cannot know where the program will land, so the count - not the
    # chain - is what builds the list.
    tmpl = bytearray(p[0x26:0x26 + KG_SIZE])
    out = bytearray(p[:0x26])
    for spec in keygroups:
        if not isinstance(spec, dict):          # (sample, lo, hi)
            smp, lo, hi = spec
            spec = {'sample': smp, 'lokey': lo, 'hikey': hi}
        kg = bytearray(tmpl)
        kg[KG_SAMPLE_SOFT:KG_SAMPLE_SOFT + 10] = _name10(spec['sample'])
        loud = spec.get('sample_loud') or spec['sample']
        kg[KG_SAMPLE_LOUD:KG_SAMPLE_LOUD + 10] = _name10(loud)
        for key, val in spec.items():
            if key in ('sample', 'sample_loud', 'start', 'end') or val is None:
                continue
            if key not in KG_FIELDS:
                raise ValueError(f'unknown keygroup field {key!r}')
            off, size, lo, hi, _ = KG_FIELDS[key]
            v = max(lo, min(hi, int(val)))
            kg[off] = v if size == 1 else v & 0xFF
        # SuperOS per-keygroup Start/End, on the panel's 1/16384-of-the-
        # sample scale (*04 cc,fff): bit 15 marks "set", and the End pair
        # is split across two bytes (kg+0x39 low, kg+0x27 high).  0 means
        # off, as on the panel (0,000): a set End of 0 would be a
        # zero-length note.
        if spec.get('start'):
            struct.pack_into('<H', kg, KG_START,
                             (int(spec['start']) & 0x7FFF) | 0x8000)
        if spec.get('end'):
            e = int(spec['end']) & 0x7FFF
            kg[KG_END_LO] = e & 0xFF
            kg[KG_END_HI] = ((e >> 8) & 0x7F) | 0x80
        struct.pack_into('<H', kg, KG_CHAIN, 0)
        out += kg
    out[0x17] = len(keygroups)
    return bytes(out)


def parse_program(body):
    """A program file back to the dict form program_file() takes.

    The exact inverse of the writer, so an existing program can be pulled
    into an editor, changed and written back.  Fields read straight out of
    KG_FIELDS, so the two cannot drift: tools/test_progedit.py builds a
    program from a spec, parses it back, and demands the same spec.
    """
    if len(body) < 0x26 + KG_SIZE:
        raise ValueError(f'program file is {len(body)} bytes, too short')
    count = body[0x17] or 1
    have = (len(body) - 0x26) // KG_SIZE
    if count > have:                 # trust the file, not the count
        count = have
    out = {'name': bytes(body[0:10]).rstrip().decode('ascii', 'replace'),
           'keygroups': []}
    for i in range(count):
        kg = body[0x26 + i * KG_SIZE:0x26 + (i + 1) * KG_SIZE]
        spec = {'sample': bytes(kg[KG_SAMPLE_SOFT:KG_SAMPLE_SOFT + 10])
                .rstrip().decode('ascii', 'replace')}
        loud = bytes(kg[KG_SAMPLE_LOUD:KG_SAMPLE_LOUD + 10])
        spec['sample_loud'] = loud.rstrip().decode('ascii', 'replace')
        for key, (off, size, lo, hi, _what) in KG_FIELDS.items():
            spec[key] = kg[off]
        # the SuperOS replay points are only present when their set bit is
        start = struct.unpack_from('<H', kg, KG_START)[0]
        spec['start'] = (start & 0x7FFF) if start & 0x8000 else None
        spec['end'] = ((kg[KG_END_LO] | ((kg[KG_END_HI] & 0x7F) << 8))
                       if kg[KG_END_HI] & 0x80 else None)
        out['keygroups'].append(spec)
    return out


def build(files, hd):
    nblocks = 1600 if hd else 800
    start = 5 if hd else 4
    # the directory is 64 entries (0x600 bytes) - more would overflow
    # into the FAT and hang the sampler's background dir scan
    assert len(files) <= 64, f'{len(files)} files > 64 directory slots'
    d = bytearray(nblocks * BLOCK)
    blk = start
    for i, (name, typ, body) in enumerate(files):
        e = bytearray(24)
        e[0:10] = name
        e[0x10] = ord(typ)
        struct.pack_into('<H', e, 0x11, len(body) & 0xFFFF)
        struct.pack_into('<H', e, 0x14, blk)
        d[i * 24:(i + 1) * 24] = e
        nblk = (len(body) + BLOCK - 1) // BLOCK
        for j in range(nblk):
            nxt = 0x8000 if j == nblk - 1 else blk + j + 1
            struct.pack_into('<H', d, 0x600 + 2 * (blk + j), nxt)
        d[blk * BLOCK:blk * BLOCK + len(body)] = body
        blk += nblk
    assert blk <= nblocks, f'{blk} blocks > {nblocks}'
    return bytes(d), blk


def os_file(path, oslen=0xC524):
    # oslen: through the resident home - 0xC524 (402/403), 0xC2AC
    # (401), 0xFF80 (402b: the high block must be carried)
    osbin = bytearray(open(path, 'rb').read()[:oslen])
    if osbin[0x43] == 3:
        osbin[0x43] = 2               # boot-source marker: disk image
    lo, hi = 0xB9F0, 0xBC00           # load-buffer wrap straddle window
    assert not any(osbin[lo:hi]), 'OS has content in the straddle window'
    return bytes(osbin)


def main():
    args = sys.argv[1:]
    hd = '--hd' in args
    if hd:
        args.remove('--hd')
    count, points, prefix, prog, osimg = 96, 2600, 'T', True, None
    oslen, name1, pname = 0xC524, None, b'POOLTEST  '
    if '--os' in args:
        i = args.index('--os'); osimg = args[i + 1]; del args[i:i + 2]
    if '--oslen' in args:
        i = args.index('--oslen'); oslen = int(args[i + 1], 0)
        del args[i:i + 2]
    if '--name' in args:               # single-sample name (count 1)
        i = args.index('--name')
        name1 = args[i + 1].ljust(10)[:10].encode()
        del args[i:i + 2]
    if '--pname' in args:              # program name
        i = args.index('--pname')
        pname = args[i + 1].ljust(10)[:10].encode()
        del args[i:i + 2]
    if '--prefix' in args:
        i = args.index('--prefix'); prefix = args[i + 1]; del args[i:i + 2]
    if '--noprog' in args:
        prog = False
        args.remove('--noprog')
    mode = 'L'
    if '--oneshot' in args:                # replay mode 'O': no loop
        mode = 'O'
        args.remove('--oneshot')
    if '--count' in args:
        i = args.index('--count'); count = int(args[i + 1]); del args[i:i + 2]
    if '--points' in args:
        i = args.index('--points')
        points = int(args[i + 1]) if args[i + 1] != 'all' else 0
        del args[i:i + 2]
    wav, outprefix = args[:2]
    pts = wav_points(wav, count * points)
    points = points or len(pts)        # --points all: the whole wav
    files = []
    if osimg:
        files.append((b'S950SUPER ', 'M', os_file(osimg, oslen)))
    step = max(1, (len(pts) - points) // max(1, count - 1))
    first_sample = None
    for i in range(count):
        o = min(i * step, max(0, len(pts) - points))
        sl = pts[o:o + points]
        sl += [0] * (points - len(sl))
        name = (name1 if name1 and count == 1
                else f'{prefix}{i + 1:02d} LOOP  '.encode()[:10])
        first_sample = first_sample or name
        files.append((name, 'S', sample_file(name, sl, mode)))
    if prog:
        # point the keygroup at the first SAMPLE (files[0] is the OS
        # when --os is given)
        files.append((pname, 'P', program_file(first_sample, pname)))
    d, used = build(files, hd)
    open(outprefix + '.img', 'wb').write(d)
    print(f'OK: {outprefix}.img ({"HD" if hd else "DD"}, {count} looped '
          f'samples x {points} points ({points / RATE * 1000:.0f} ms), '
          f'{count * points // 1000} Kwords, {used} blocks)')


if __name__ == '__main__':
    main()
