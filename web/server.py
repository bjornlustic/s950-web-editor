#!/usr/bin/env python3
"""S950 sample loader: browser -> local mirror image -> ZuluSCSI over Wi-Fi.

Serves web/ and a small JSON API over the tooling that the emulation suite
proves end to end (tools/test_wifi950.py):

  GET  /api/program?name=         an existing drop-box program parsed into
                                  the editor's form (POST creates one)
  GET  /api/wav?name=&trim=       a drop-box sample decoded back to a WAV,
                                  for auditioning or export (trim=1 gives
                                  only what the machine will play)
  GET  /api/status                board reachability, served image, the
                                  mirror's mailbox, pending ranges, the
                                  drop-box directory
  POST /api/sample?name=&mode=&push=&load=&rate=
                                  body = 16-bit mono 40 kHz WAV; encodes it
                                  to the S950 disk format, deposits it in
                                  the mirror and (push=1) pushes the changed
                                  ranges to the board.  load=0 stores it on
                                  the card without announcing it, so the
                                  sampler does not pull it into RAM
  POST /api/announce?name=&type=  re-announce a file already on the disk, so
                                  the sampler loads it at its next poll
  POST /api/program?name=&sample=&lokey=&hikey=&load=
                                  build a one-keygroup program around a
                                  sample and deposit it
  POST /api/load?name=&type=      announce a drop-box file and WAIT until
                                  the sampler holds it; returns the state
  POST /api/unload?name=&type=    delete an item from the SAMPLER'S MEMORY
                                  and return the fresh status
  POST /api/clear                 empty the drop-box volume (files only;
                                  nothing is asked to load)
  POST /api/state                 ask the SAMPLER what is resident and how
                                  much sample memory is free, and wait for
                                  its reply (it answers on an idle poll)
  POST /api/delete?name=&type=    delete one file from the drop-box volume
                                  (frees its blocks, wipes its directory
                                  entry, pushes the change)
  POST /api/push                  push whatever is pending
  POST /api/verify                read the whole served image back and
                                  compare it with the mirror
  GET  /api/version               {"api": 1, "os": "4.0.3", ...}

Every endpoint is also served under /api/v1/<name>, the public path
docs/API.md documents.  Errors are JSON {"error": ...}: 400 bad params,
401 missing/wrong token, 404 unknown endpoint or file, 500 anything else.
Every response carries Access-Control-Allow-Origin: * and OPTIONS answers
the preflight, so browser apps on other origins can call it.

The mirror (--image) and the image the board serves must stay identical;
every deposit is planned against the mirror and a failed push leaves the
pending patch on disk for the next attempt.

  python3 web/server.py [--port 8150] [--image build/S950_HD0.img]
                        [--host 192.168.1.250] [--id 0] [--no-browser]
                        [--listen 127.0.0.1] [--token T]

--listen on anything but a loopback address needs --token (or
S950_API_TOKEN); with a token set, every /api request must carry
`Authorization: Bearer T` (or ?token=T, which is how the page's WAV links
work).  Static files stay open.
"""
import argparse
import ipaddress
import json
import os
import struct
import sys
import subprocess
import tempfile
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'tools'))
import s950dropbox as box                                      # noqa: E402
import build_sounddisk                                        # noqa: E402
import s950wifi as W                                           # noqa: E402
from build_hdimage import FIRST_DATA                             # noqa: E402

WEB = os.path.join(ROOT, 'web')
MIME = {'.html': 'text/html', '.js': 'text/javascript',
        '.css': 'text/css', '.json': 'application/json'}


class State:
    def __init__(self, image, host, sid, local=False, mode='wifi', card=None):
        self.image = image
        self.host = host
        self.id = sid
        self.local = local
        self.mode = mode            # 'wifi' (board over UDP) or 'usb'
        self.card = card            # the mounted card's volume in usb mode
        self.lock = threading.Lock()
        self.log = []
        self.ready = None           # last drop-box mode probe (see do_mode)

    def note(self, line):
        self.log.append(f'[{time.strftime("%H:%M:%S")}] {line}')
        del self.log[:-200]

    def drain_log(self):
        out, self.log = self.log, []
        return out


S = None
TOKEN = None                    # set by main(); None = no auth
OS_VERSION = '5.0.0'


def do_version():
    return {'api': 1, 'os': OS_VERSION, 'image': os.path.basename(S.image),
            'host': S.host, 'id': S.id, 'auth': bool(TOKEN)}


def mirror_bytes():
    with open(S.image, 'rb') as f:
        return bytearray(f.read())


def board_status():
    if S.local:
        return True, 0, os.path.basename(S.image) + ' (local)'
    """(board answers, ping ms, served filename).

    Reachability and "the id we push to actually exists" are separate
    facts: a board that pings but has no image at this id is the normal
    state before HD0.img has been put on the card, and the page has to say
    so rather than showing the board as dead.
    """
    try:
        z = W._loader(S.host, timeout=0.8)
        ping = round(z.ping() * 1000)
    except Exception:                                        # noqa: BLE001
        return False, None, ''
    try:
        return True, ping, z.info(S.id).get('filename', '')
    except Exception as e:                                   # noqa: BLE001
        return True, ping, f'no image at id {S.id} ({e})'


def free_blocks(d):
    top = min(box.nblocks(d) - 1, box.HIGHEST)
    return sum(1 for b in range(FIRST_DATA, top + 1) if box.fat_get(d, b) == 0)


def status():
    d = mirror_bytes()
    mb = box.mailbox(d)
    volblk = box.find_volume(d, box.DEF_VOL)
    files = []
    if volblk:
        for slot, name, ftype, length, start in box.entries(d, volblk):
            files.append({'slot': slot,
                          'name': name.decode('ascii', 'replace').rstrip(),
                          'type': ftype, 'size': length, 'block': start})
    p = W.pending_path(S.image)
    pending = W.read_patch(p) if os.path.exists(p) else []
    ok, ping, name = board_status()
    return {
        'host': S.host, 'id': S.id, 'board': ok, 'ping_ms': ping,
        # --local never speaks to the board but still answers board: true,
        # which once cost a whole hardware session: every deposit went into
        # a local file while the status looked healthy.  Say so outright.
        'local': S.local,
        'mode': S.mode, 'card': S.card, 'ready': S.ready,
        'image': os.path.basename(S.image), 'image_name': name,
        'volume': box.DEF_VOL.decode().rstrip(),
        'mailbox': {
            'serial': mb['serial'],
            'name': (mb['name'] or b'').decode('ascii', 'replace').rstrip(),
            'type': mb['type'] or '',
        },
        'pending_ranges': len(pending),
        'pending_bytes': sum(len(x) for _, x in pending),
        'files': files,
        'free_blocks': free_blocks(d),
        'total_blocks': box.nblocks(d),
        'log': S.drain_log(),
    }


class LocalCard:
    """The board's read side, served straight off the mirror.

    In --local mode the mirror IS the image the sampler reads, so a
    "read it back off the board" is a read of this file - which is how
    the command channel works without a ZuluSCSI: the host writes a
    request into the mailbox, the firmware answers into the status
    block, and both sides see the same bytes.
    """

    def __init__(self, path):
        self.path = path

    def ping(self):
        return 0.0

    def info(self, sid):
        return {'filename': os.path.basename(self.path)}

    def read(self, sid, off, n):
        with open(self.path, 'rb') as f:
            f.seek(off)
            return f.read(n)

    def write_ranges(self, sid, writes, progress=None):
        """Nothing to send: planning already wrote the served file."""
        return None


def card():
    """The loader object to read the served image with, or None."""
    return LocalCard(S.image) if S.local else None


def push_now():
    # --local: the mirror IS the served image (the emulator serves the
    # same file through tools/s950panel.py --card, or a card is in a
    # reader), so there is no board to push to and a deposit is complete
    # the moment it is planned.
    t = time.time()
    n, nbytes = W.push(S.image, S.host, S.id, loader=card())
    dt = time.time() - t
    return {'ranges': n, 'bytes': nbytes, 'seconds': dt,
            'local': S.local,
            'kbs': round(nbytes / 1024 / max(dt, 1e-6))}


def do_sample(query, body):
    name = query.get('name', ['SAMPLE'])[0]
    mode = query.get('mode', ['L'])[0]
    want_push = query.get('push', ['1'])[0] == '1'
    # load=0 writes the sample to the card WITHOUT announcing it, so the
    # sampler leaves it alone.  That is how a library is built up on the
    # ZuluSCSI; /api/announce loads one into RAM when it is wanted.
    want_load = query.get('load', ['1'])[0] == '1'
    # The S950 has no fixed sample rate: it is a per-sample header field
    # the hardware tunes to, so a lower rate buys memory at the cost of
    # bandwidth.  The browser has already resampled to this, so the same
    # figure has to go in the header or the sample plays at the wrong
    # pitch.
    rate = int(query.get('rate', ['40000'])[0])
    if not 4000 <= rate <= 48000:
        raise ValueError(f'sample rate {rate} out of range (4000..48000)')
    # EDIT SAMPLE fields, set before the sample ever reaches the machine
    def opt(k, lo, hi):
        v = query.get(k, [''])[0]
        if v == '':
            return None
        n = int(v)
        if not lo <= n <= hi:
            raise ValueError(f'{k} {n} out of range ({lo}..{hi})')
        return n
    edit = {'start': opt('start', 0, 1 << 24),
            'end': opt('end', 0, 1 << 24),
            'loop': opt('loop', 0, 1 << 24),
            'loudness': opt('loudness', 0, 99),
            'pitch': opt('pitch', 0, 0xFFFF)}
    if mode not in ('L', 'S', 'O', 'A'):
        raise ValueError(f'bad mode {mode!r}')
    if len(body) < 45 or body[:4] != b'RIFF':
        raise ValueError('body is not a WAV')
    if want_load:
        require_dropbox()
    with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as f:
        f.write(body)
        tmp = f.name
    try:
        (serial, points), ranges = W.plan_wav(S.image, tmp, name=name,
                                              mode=mode, notify=want_load,
                                              rate=rate, **edit)
    finally:
        os.unlink(tmp)
    planned = sum(len(d) for _, d in ranges)
    S.note(f'{name}: {"stored on the card" if not want_load else f"serial {serial}"}, '
           f'{points} points, {len(ranges)} ranges, {planned} bytes')
    out = {'serial': serial, 'points': points, 'ranges': len(ranges),
           'planned_bytes': planned, 'pushed': False, 'pushed_bytes': 0,
           'local': S.local,
           'loaded': want_load, 'rate': rate,
           'edit': {k: v for k, v in edit.items() if v is not None}}
    if want_push:
        r = push_now()
        out['pushed'] = True
        out['pushed_bytes'] = r['bytes']
        S.note(f'pushed {r["bytes"]} bytes in {r["seconds"]:.1f} s '
               f'({r["kbs"]} KB/s)')
    return out


def do_announce(query):
    name = query.get('name', [''])[0]
    ftype = query.get('type', ['S'])[0]
    if not name:
        raise ValueError('name required')
    require_dropbox()

    def bump(d):
        serial = (box.mailbox(d)['serial'] + 1) & 0xFFFF or 1
        box.announce(d, serial, box.DEF_VOL, box._name10(name), ftype)
        return serial

    serial, ranges = W.plan(S.image, bump)
    r = push_now()
    S.note(f'announced {name} as serial {serial}')
    return {'serial': serial, 'ranges': len(ranges),
            'pushed_bytes': r['bytes']}


def do_delete(query):
    """Delete one file from the drop-box volume and push the change.

    Host-side only: the FAT chain is freed and the 24-byte directory entry
    wiped in the mirror, then the changed ranges go to the board.  No
    firmware involvement, so nothing can go wrong on the sampler.  If the
    file being deleted is the one the mailbox still announces, the
    announcement is wiped with it, otherwise the next power-up would point
    the loader at a file that is gone.
    """
    name = query.get('name', [''])[0]
    ftype = query.get('type', [None])[0] or None
    if not name:
        raise ValueError('name required')
    gone, ranges = W.plan_remove(S.image, name, ftype=ftype)
    if not gone:
        raise ValueError(f'no file {name!r} in the drop-box')
    nm, t, length = gone
    r = push_now()
    S.note(f'deleted {nm.rstrip().decode()} type {t} ({length} bytes), '
           f'pushed {r["bytes"]} bytes')
    return {'name': nm.rstrip().decode('ascii', 'replace'), 'type': t,
            'size': length, 'ranges': len(ranges),
            'pushed_bytes': r['bytes']}


def do_wav(query):
    """A drop-box sample, decoded back to a WAV.

    Serves both auditioning it in the browser and exporting it: the
    sample never left the card, so this is the only way to hear what is
    actually stored rather than what was sent.  The second half of the
    points is 8-bit on the disk itself, so that is what comes back.
    """
    name = query.get('name', [''])[0]
    if not name:
        raise ValueError('name required')
    d = mirror_bytes()
    got = box.sample_points(d, name)
    if not got:
        raise ValueError(f'no sample {name!r} in the drop-box')
    pts, rate = got['points'], got['rate'] or 40000
    trim = query.get('trim', ['0'])[0] == '1'
    if trim:                       # what the machine will actually play
        pts = pts[got['start']:max(got['start'] + 1, got['end'])]
    body = b''.join(struct.pack('<h', max(-32768, min(32767, p)))
                    for p in pts)
    hdr = (b'RIFF' + struct.pack('<I', 36 + len(body)) + b'WAVEfmt '
           + struct.pack('<IHHIIHH', 16, 1, 1, rate, rate * 2, 2, 16)
           + b'data' + struct.pack('<I', len(body)))
    return hdr + body, got


def do_read_program(query):
    """An existing drop-box program, parsed into the editor's own form.

    So a program made on the machine (or by an earlier session) can be
    pulled in, changed and written back rather than rebuilt from memory.
    """
    name = query.get('name', [''])[0]
    if not name:
        raise ValueError('name required')
    got = box.read_file(mirror_bytes(), name, ftype='P')
    if not got:
        raise ValueError(f'no program {name!r} in the drop-box')
    prog = build_sounddisk.parse_program(got[0])
    prog['bytes'] = len(got[0])
    return prog


def do_fields():
    """The keygroup fields the editor can set, straight from the table
    tools/test_progedit.py round-trips through a real load."""
    return {'keygroup': [{'key': k, 'offset': off, 'min': lo, 'max': hi,
                          'label': what}
                         for k, (off, size, lo, hi, what)
                         in build_sounddisk.KG_FIELDS.items()],
            'sample': [
                {'key': 'start', 'min': 0, 'max': 1 << 24,
                 'label': 'replay start (points)'},
                {'key': 'end', 'min': 0, 'max': 1 << 24,
                 'label': 'replay end (points)'},
                {'key': 'loop', 'min': 0, 'max': 1 << 24,
                 'label': 'loop length (points)'},
                {'key': 'loudness', 'min': 0, 'max': 99, 'label': 'loudness'},
                {'key': 'pitch', 'min': 0, 'max': 0xFFFF,
                 'label': 'nominal pitch'},
            ]}


def do_program(query, body=b''):
    """Build a program from drop-box samples and deposit it.

    One keygroup per sample, each over its own key range, via
    kgs=SAMPLE:lo:hi,...  The count in header +0x17 is what builds the
    list; the chain at kg+0x44 stays 0 because the loader takes it
    verbatim and a program lands wherever memory is free, so the host
    cannot know an address to chain to.  See program_file().
    """
    name = query.get('name', [''])[0]
    sample = query.get('sample', [''])[0]
    # name/sample are checked AFTER the body is parsed: with keygroups they
    # come from the JSON, not the query string
    # kgs=SAMPLE:lo:hi,SAMPLE:lo:hi,...  one keygroup each; the single
    # sample/lokey/hikey form stays for the simple case
    kgs = []
    if body:
        # a JSON body, because a query string cannot carry two dozen
        # fields per keygroup: [{"sample": .., "lokey": .., "filter": ..}]
        spec = json.loads(body.decode('utf-8'))
        kgs = spec.get('keygroups') or []
        name = spec.get('name') or name
        for kg in kgs:
            if not kg.get('sample'):
                raise ValueError('every keygroup needs a sample')
            for k in kg:
                if k not in ('sample', 'sample_loud', 'start', 'end') and \
                        k not in build_sounddisk.KG_FIELDS:
                    raise ValueError(f'unknown keygroup field {k!r}')
    else:
        for part in [x for x in query.get('kgs', [''])[0].split(',') if x]:
            bits = part.split(':')
            if len(bits) != 3:
                raise ValueError(f'bad keygroup {part!r}, want SAMPLE:lo:hi')
            kgs.append({'sample': bits[0], 'lokey': int(bits[1]),
                        'hikey': int(bits[2])})
    for kg in kgs:
        lo, hi = kg.get('lokey', 0), kg.get('hikey', 127)
        if not 0 <= lo <= hi <= 127:
            raise ValueError(f'{kg["sample"]}: bad key range {lo}..{hi}')
    lokey = int(query.get('lokey', ['24'])[0])
    hikey = int(query.get('hikey', ['127'])[0])
    if not kgs and not 0 <= lokey <= hikey <= 127:
        raise ValueError(f'bad key range {lokey}..{hikey}')
    if kgs:
        sample = kgs[0]['sample']
    want_load = query.get('load', ['1'])[0] == '1'
    # A program resolves its keygroup against a sample that is ALREADY in
    # memory (test_403 X15).  Depositing the program alone leaves it
    # pointing at a name the sampler does not hold, so it loads and does
    # not play - which looks like the builder is broken.  Load the sample
    # first when it is missing.
    sample_loaded = False
    if want_load:
        require_dropbox()
        st = W.refresh_state(S.image, S.host, S.id, loader=card())
        need = sorted({k['sample'] for k in kgs}
                      | {k.get('sample_loud') for k in kgs} - {None}) \
            if kgs else [sample]
        missing = [n for n in need
                   if not any(i['name'] == n and i['type'] == 'S'
                              for i in st['items'])]
        # One at a time: the mailbox holds a single request, so announcing
        # two samples inside one poll interval loses the first.
        for want in missing:
            def announce_sample(d, nm=want):
                ser = (box.mailbox(d)['serial'] + 1) & 0xFFFF or 1
                box.announce(d, ser, box.DEF_VOL, box._name10(nm), 'S')
                return ser
            W.plan(S.image, announce_sample)
            push_now()
            st = W.refresh_state(S.image, S.host, S.id, loader=card())
            if not any(i['name'] == want and i['type'] == 'S'
                       for i in st['items']):
                raise ValueError(
                    f'{want!r} would not load, so the program would have '
                    f'nothing to play; is it still on the card?')
        sample_loaded = True

    body = build_sounddisk.program_file(box._name10(sample),
                                        box._name10(name),
                                        lokey=lokey, hikey=hikey,
                                        keygroups=kgs or None)
    serial, ranges = W.plan_file(S.image, name, 'P', body, notify=want_load)
    r = push_now()
    S.note(f'program {name}: sample {sample}, keys {lokey}..{hikey}, '
           f'{len(body)} B')
    return {'name': name, 'sample': sample, 'lokey': lokey, 'hikey': hikey,
            'keygroups': kgs,
            'bytes': len(body), 'serial': serial, 'ranges': len(ranges),
            'pushed_bytes': r['bytes'], 'loaded': want_load,
            'sample_resident': sample_loaded}


def do_load(query):
    """Announce a drop-box file and wait until the sampler actually holds
    it, then return the fresh state.  /api/announce only asks; this is the
    one to call when the caller is going to show the result."""
    name = query.get('name', [''])[0]
    ftype = query.get('type', ['S'])[0]
    if not name:
        raise ValueError('name required')
    require_dropbox()
    t = time.time()
    st = W.load_resident(S.image, name, ftype, S.host, S.id,
                         loader=card())
    st['seconds'] = round(time.time() - t, 1)
    S.note(f'loaded {name}: {st["used_words"]} words used, '
           f'{st["free_words"]} free')
    return st


def do_unload(query):
    """Delete an item from the SAMPLER'S MEMORY (not the card).

    This is the one that answers "I need to remove something to fit this":
    the reply is a fresh status, so the caller sees the memory come back
    rather than being told it did.
    """
    name = query.get('name', [''])[0]
    ftype = query.get('type', ['S'])[0]
    if not name:
        raise ValueError('name required')
    t = time.time()
    st = W.delete_resident(S.image, name, ftype, S.host, S.id,
                           loader=card())
    st['seconds'] = round(time.time() - t, 1)
    S.note(f'unloaded {name}: {st["used_words"]} words used, '
           f'{st["free_words"]} free')
    return st


def do_clear():
    """Empty the drop-box volume: every file's blocks freed, its directory
    wiped, the volume itself kept.  db.clear() leaves the mailbox alone,
    so the sampler is not asked to load anything as a result."""
    n, ranges = W.plan(S.image, box.clear)
    if not n:
        return {'files': 0, 'ranges': 0, 'pushed_bytes': 0}
    r = push_now()
    S.note(f'cleared {n} files from the drop-box, {r["bytes"]} B pushed')
    return {'files': n, 'ranges': len(ranges), 'pushed_bytes': r['bytes']}


def do_state():
    """Ask the SAMPLER what is in its memory, and wait for the answer.

    The editor cannot infer this.  Samples can be loaded and memory
    cleared from the DISK page without the drop-box being involved at
    all, so anything the host remembers is a guess - this is the button
    that replaces the guess with the machine's own answer.
    """
    t = time.time()
    st = W.refresh_state(S.image, S.host, S.id, loader=card())
    st['seconds'] = round(time.time() - t, 1)
    S.note(f"state: {len(st['items'])} resident, {st['used_words']} words "
           f"used, {st['free_words']} free")
    return st


def do_verify():
    t = time.time()
    bad = W.verify(S.image, S.host, S.id)
    return {'differing': bad, 'bytes': os.path.getsize(S.image),
            'seconds': time.time() - t}


_MIRROR = {'tp': None, 'port': None}


def do_panel(q):
    """Mirror the machine's front panel, live, over the MIDI service ops.

    The LCD and the section lamps are just RAM, so a browser can show
    what the sampler is showing with nothing attached but a MIDI cable -
    and, unlike everything else in this editor, it needs neither the
    board nor the drop box.  Useful on its own, and useful when a deposit
    does not appear: the panel says whether the machine is on the DISK
    page, sitting on an error banner, or in no section at all.

    READ ONLY.  s950live.press() is the way to drive it.
    """
    import s950mirror
    import s950live
    port = q.get('port', ['UX16 2'])[0]
    if _MIRROR['tp'] is None or _MIRROR['port'] != port:
        _MIRROR['tp'] = midi_tp(port)                  # keep the port open
        _MIRROR['port'] = port
    try:
        return s950mirror.panel(_MIRROR['tp'], int(q.get('ch', ['0'])[0], 0),
                                voices=q.get('voices', ['1'])[0] != '0')
    except RuntimeError as e:
        _MIRROR['tp'] = None
        raise ValueError(str(e))


# ---------------------------------------------------------------------------
# Connection: which device the editor talks to, and how.
#
#   wifi  the ZuluSCSI serves the image to the sampler and takes our writes
#         over UDP 5150.  The sampler stays on the bus; deposits load at its
#         next idle poll.
#   usb   the board is plugged into this Mac over USB-C.  It leaves the SCSI
#         bus and mounts its card as a disk, so the editor writes straight
#         into the image file on the card.  The sampler cannot see the card
#         until the card is EJECTED, which reboots the board back onto the
#         bus; anything announced meanwhile loads at the first poll after.
# ---------------------------------------------------------------------------
import s950card as card_tool                                    # noqa: E402

IMAGE_DIR, IMAGE_NAME = 'S950', 'HD00_512.hda'


def ini_get(path, key):
    try:
        for line in open(path, errors='replace'):
            m = __import__('re').match(r'^\s*' + key + r'\s*=\s*"?([^"\r\n]*)"?', line)
            if m:
                return m.group(1)
    except OSError:
        pass
    return ''


def usb_cards():
    """Every mounted ZuluSCSI card (a volume with a zuluscsi.ini)."""
    out = []
    try:
        names = os.listdir('/Volumes')
    except OSError:
        names = []
    for name in names:
        vol = os.path.join('/Volumes', name)
        ini = os.path.join(vol, card_tool.INI)
        if not os.path.exists(ini):
            continue
        img = os.path.join(vol, IMAGE_DIR, IMAGE_NAME)
        out.append({'volume': vol, 'name': name, 'image': img,
                    'has_image': os.path.exists(img),
                    'ssid': ini_get(ini, 'WiFiSSID'),
                    'loader_ip': ini_get(ini, 'LoaderIP')})
    return out


def usb_boards():
    """ZuluSCSI consoles on USB, by product name (never by position)."""
    try:
        return [{'port': p, 'product': n, 'serial': sn}
                for p, n, sn in card_tool._usb_serial_ports() if 'ZuluSCSI' in n]
    except Exception:                                         # noqa: BLE001
        return []


def do_devices(q):
    host = q.get('host', [S.host])[0]
    ok, ping, name = (False, None, '')
    try:
        z = W._loader(host, timeout=0.8)
        ping = round(z.ping() * 1000)
        ok = True
        try:
            name = z.info(S.id).get('filename', '')
        except Exception as e:                               # noqa: BLE001
            name = f'no image at id {S.id} ({e})'
    except Exception:                                         # noqa: BLE001
        pass
    return {'current': {'mode': S.mode, 'host': S.host, 'id': S.id,
                        'image': S.image, 'card': S.card, 'local': S.local},
            'wifi': {'host': host, 'reachable': ok, 'ping_ms': ping,
                     'image_name': name},
            'usb': {'cards': usb_cards(), 'boards': usb_boards()}}


def do_connect(q):
    """Switch the editor to a device.  mode=wifi&host=..[&id=..] or
    mode=usb&volume=/Volumes/NAME."""
    mode = q.get('mode', [''])[0]
    if mode == 'wifi':
        host = q.get('host', [S.host])[0].strip()
        sid = int(q.get('id', [S.id])[0])
        try:
            ipaddress.ip_address(host)
        except ValueError:
            raise ValueError(f'{host!r} is not an IP address')
        z = W._loader(host, timeout=1.5)
        try:
            ping = round(z.ping() * 1000)
        except Exception as e:                               # noqa: BLE001
            raise ValueError(f'no ZuluSCSI answers at {host} (is the board '
                             f'on, joined to this Wi-Fi, and running the '
                             f'SuperOS loader firmware?): {e}')
        mirror = q.get('image', [S.mirror_default])[0]
        if not os.path.exists(mirror):
            raise ValueError(f'the local mirror {mirror} does not exist')
        S.mode, S.host, S.id, S.local, S.card, S.image = \
            'wifi', host, sid, False, None, mirror
        S.ready = None
        S.note(f'connected over Wi-Fi to {host} id {sid} ({ping} ms)')
        return do_devices({})
    if mode == 'usb':
        vol = q.get('volume', [''])[0]
        cards = {c['volume']: c for c in usb_cards()}
        if vol not in cards:
            raise ValueError(f'{vol or "(none)"} is not a mounted ZuluSCSI '
                             f'card; plug the board into this Mac over USB-C '
                             f'and wait for its card to appear in Finder')
        c = cards[vol]
        if not c['has_image']:
            raise ValueError(f'{vol} has no {IMAGE_DIR}/{IMAGE_NAME}; run '
                             f'python3 tools/s950card.py install first')
        S.mode, S.local, S.card, S.image = 'usb', True, vol, c['image']
        S.ready = None
        S.note(f'connected over USB-C: writing into {c["image"]} (the '
               f'sampler sees it after Eject)')
        return do_devices({})
    raise ValueError('mode must be wifi or usb')


def mac_networks():
    """The Wi-Fi networks this Mac knows, and the one it is on.  macOS no
    longer reveals scanned SSIDs to a script, so the preferred list is
    what can be offered; anything else is typed."""
    known, current = [], ''
    try:
        out = subprocess.run(['networksetup', '-listpreferredwirelessnetworks',
                              'en0'], capture_output=True, text=True,
                             timeout=5).stdout
        known = [l.strip() for l in out.splitlines()[1:] if l.strip()]
        cur = subprocess.run(['networksetup', '-getairportnetwork', 'en0'],
                             capture_output=True, text=True, timeout=5).stdout
        if ':' in cur and 'not associated' not in cur:
            current = cur.split(':', 1)[1].strip()
    except Exception:                                         # noqa: BLE001
        pass
    return known, current


def do_wifi_get():
    known, current = mac_networks()
    card = None
    if S.mode == 'usb' and S.card:
        ini = os.path.join(S.card, card_tool.INI)
        card = {'volume': S.card, 'ssid': ini_get(ini, 'WiFiSSID'),
                'has_password': bool(ini_get(ini, 'WiFiPassword')),
                'loader_ip': ini_get(ini, 'LoaderIP')}
    return {'mac_current': current, 'known': known, 'card': card,
            'editable': card is not None}


def do_wifi_set(q):
    """Write WiFiSSID / WiFiPassword into zuluscsi.ini on the mounted card.
    The board reads them at boot, i.e. after Eject.  The password is
    written to the card and nowhere else (not logged, not echoed)."""
    if S.mode != 'usb' or not S.card:
        raise ValueError('the Wi-Fi network is set on the card: connect '
                         'over USB-C first')
    ssid = q.get('ssid', [''])[0].strip()
    pw = q.get('password', [''])[0]
    if not ssid:
        raise ValueError('network name required')
    if any(c in ssid + pw for c in '"\r\n'):
        raise ValueError('quotes and line breaks are not allowed')
    ini = os.path.join(S.card, card_tool.INI)
    text = open(ini, errors='replace').read()
    import re
    lines = text.splitlines()
    out, seen = [], set()
    for line in lines:
        m = re.match(r'^(\s*)(WiFiSSID|WiFiPassword)\s*=', line)
        if m:
            key = m.group(2)
            seen.add(key)
            val = ssid if key == 'WiFiSSID' else pw
            if key == 'WiFiPassword' and not pw:
                continue                       # keep the card's password
            out.append(f'{m.group(1)}{key} = "{val}"')
        else:
            out.append(line)
    # missing keys go under [SCSI]
    add = [k for k in ('WiFiSSID', 'WiFiPassword') if k not in seen
           and (k == 'WiFiSSID' or pw)]
    if add:
        for i, line in enumerate(out):
            if line.strip().lower() == '[scsi]':
                for k in reversed(add):
                    out.insert(i + 1, f'{k} = "{ssid if k == "WiFiSSID" else pw}"')
                break
        else:
            out += ['[SCSI]'] + [f'{k} = "{ssid if k == "WiFiSSID" else pw}"'
                                 for k in add]
    bak = os.path.join(ROOT, 'build', 'zuluscsi.ini.bak')
    try:
        __import__('shutil').copyfile(ini, bak)
    except OSError:
        pass
    open(ini, 'w').write('\n'.join(out) + '\n')
    S.note(f'card Wi-Fi set to {ssid!r} (joins after Eject; backup in '
           f'build/zuluscsi.ini.bak)')
    return do_wifi_get()


def do_mount(q):
    """Put the ZuluSCSI into card-reader mode over its USB console (the
    's', 'y' keys) and wait for the card to mount, then connect to it.
    The board leaves the SCSI bus while mounted: the sampler has no disk
    until Eject."""
    vols = usb_cards()
    if not vols:
        port, board = card_tool.find_port(q.get('serial', [None])[0])
        if not port:
            raise ValueError('no ZuluSCSI console on USB: plug the board '
                             'into this Mac with USB-C (the S950 can stay '
                             'on)')
        S.note(f'[{board}] entering card-reader mode')
        try:
            card_tool.console(port, 'sy', wait=0.3)
        except Exception as e:                               # noqa: BLE001
            raise ValueError(f'could not talk to the console at {port}: {e}')
        deadline = time.time() + 60
        while time.time() < deadline and not usb_cards():
            time.sleep(1.0)
        vols = usb_cards()
        if not vols:
            raise ValueError('the card did not mount within 60 s; check '
                             'Finder, or run python3 tools/s950card.py status')
    return do_connect({'mode': ['usb'], 'volume': [vols[0]['volume']]})


def do_eject():
    """Hand the card back to the sampler: eject the USB disk, wait for the
    board to come back on the SCSI bus, and switch the editor to Wi-Fi."""
    if S.mode != 'usb' or not S.card:
        raise ValueError('not connected over USB-C')
    vol = S.card
    r = subprocess.run(['diskutil', 'eject', vol], capture_output=True,
                       text=True)
    if r.returncode:
        raise ValueError(f'eject failed: {r.stderr.strip() or r.stdout.strip()}')
    S.note(f'ejected {vol}; the board reboots onto the SCSI bus')
    S.mode, S.local, S.card, S.image = 'wifi', False, None, S.mirror_default
    S.ready = None
    return {'ejected': vol, 'next': 'the board is rebooting into SCSI mode; '
            'it joins Wi-Fi in a few seconds and the sampler loads whatever '
            'was announced at its next idle poll'}


_MIDI = {'tp': None, 'port': None}


def midi_tp(port='UX16 2'):
    import s950live
    if _MIDI['tp'] is None or _MIDI['port'] != port:
        import mido
        names = mido.get_input_names()
        if not any(port.lower() in n.lower() for n in names):
            ux = sorted({n for n in names if 'UX16' in n}) or ['none']
            raise ValueError(f'no MIDI port matching {port!r}; UX16 ports '
                             f'seen: {", ".join(ux)}. Plug in the S950\'s '
                             f'UX16, or press EDIT SAMPLE on the panel')
        _MIDI['tp'] = s950live.MidoTransport(port)
        _MIDI['port'] = port
    return _MIDI['tp']


def midi_section(port):
    """The sampler's section over the MIDI service ops, or None."""
    try:
        import s950mirror
        tp = midi_tp(port)
        p = s950mirror.panel(tp, 0, voices=False)
        return p['section']
    except BaseException as e:                                 # noqa: BLE001
        _MIDI['tp'] = None
        S.note(f'MIDI section read failed: {e}')
        return None


def do_mode(q):
    """Is the sampler in DROP-BOX MODE, i.e. polling the mailbox?

    The only proof is an answer: a command is written into the mailbox and
    the sampler has `timeout` seconds to reply.  It replies only on an
    idle poll, which the DISK and RECORD pages, an error banner and a
    sounding voice all hold off.  Over USB-C the board is off the bus, so
    the sampler cannot answer at all until Eject.
    """
    timeout = float(q.get('timeout', ['3'])[0])
    port = q.get('port', ['UX16 2'])[0]
    out = {'mode': S.mode, 'polling': False, 'reason': '', 'section': None,
           'midi': False}
    if S.mode == 'usb':
        out['reason'] = ('connected over USB-C: the board is off the SCSI '
                         'bus, so the sampler cannot poll until the card '
                         'is ejected. Files are stored on the card; press '
                         'Eject to hand it back.')
        S.ready = False
        return out
    ok, ping, _ = board_status()
    if not ok:
        out['reason'] = f'the ZuluSCSI at {S.host} does not answer'
        S.ready = False
        return out
    t = time.time()
    try:
        st = W.refresh_state(S.image, S.host, S.id, loader=card(),
                             timeout=timeout)
        out['polling'] = True
        out['seconds'] = round(time.time() - t, 1)
        out['resident'] = len(st['items'])
        out['reason'] = (f'the sampler answered in {out["seconds"]} s: it is '
                         f'polling the drop box')
    except RuntimeError as e:
        out['reason'] = str(e)
    sec = midi_section(port)
    if sec is not None:
        out['midi'] = True
        out['section'] = sec
        if not out['polling']:
            if 'DISK' in sec or 'RECORD' in sec:
                out['reason'] += f'. The panel is on the {sec} page'
            elif 'none' in sec:
                out['reason'] += '. No section is running (an error banner?)'
    S.ready = out['polling']
    return out


def do_mode_enter(q):
    """Put the sampler into drop-box mode from here: press EDIT SAMPLE over
    the MIDI service ops (s950live.press, retried until the section
    changes), then probe again."""
    port = q.get('port', ['UX16 2'])[0]
    if S.mode == 'usb':
        raise ValueError('over USB-C the sampler cannot poll: eject the '
                         'card first')
    try:
        import s950live
        tp = midi_tp(port)
        SECTKEY, EDIT_SAMPLE = 0xB62C, 'EDIT_SAMPLE'
        want = bytes.fromhex('526b')
        for i in range(12):
            if bytes(s950live.peek(tp, SECTKEY, 2)) == want:
                break
            s950live.press(tp, [EDIT_SAMPLE], gap=0)
            time.sleep(0.4 + 0.1 * i)
        else:
            raise ValueError('the sampler did not take the EDIT SAMPLE key '
                             'over MIDI; press it on the panel')
    except ValueError:
        raise
    except BaseException as e:                                 # noqa: BLE001
        _MIDI['tp'] = None
        raise ValueError(f'no MIDI path to the sampler ({e}); on the panel '
                         f'press EDIT SAMPLE or PLAY and release any held '
                         f'key')
    S.note('pressed EDIT SAMPLE over MIDI')
    time.sleep(0.5)
    return do_mode(q)


def require_dropbox():
    """A load needs the sampler polling.  Probe (cheaply) before writing an
    announcement the machine would never act on."""
    if S.mode == 'usb':
        raise ValueError('over USB-C the sampler cannot load anything: '
                         'untick "Load into S950 RAM" to store the file on '
                         'the card, then Eject to hand the card back')
    m = do_mode({'timeout': ['4']})
    if not m['polling']:
        raise ValueError('the sampler is not in drop-box mode: ' + m['reason'])


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, fmt, *args):
        pass

    def end_headers(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        super().end_headers()

    def send_json(self, obj, code=200):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_file(self, path):
        if not os.path.isfile(path):
            return self.send_json({'error': 'not found'}, 404)
        with open(path, 'rb') as f:
            data = f.read()
        self.send_response(200)
        self.send_header('Content-Type',
                         MIME.get(os.path.splitext(path)[1],
                                  'application/octet-stream'))
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(data)

    def route(self):
        """(path, query) with /api/v1/x folded onto /api/x, or None after
        a 401 has been sent."""
        u = urllib.parse.urlparse(self.path)
        path, q = u.path, urllib.parse.parse_qs(u.query)
        if path.startswith('/api/v1/'):
            path = '/api/' + path[len('/api/v1/'):]
        if TOKEN and path.startswith('/api/'):
            got = self.headers.get('Authorization', '')
            if got != f'Bearer {TOKEN}' and q.get('token', [''])[0] != TOKEN:
                self.send_json({'error': 'missing or wrong token'}, 401)
                return None
        return path, q

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers',
                         'Authorization, Content-Type')
        self.send_header('Access-Control-Max-Age', '86400')
        self.send_header('Content-Length', '0')
        self.end_headers()

    def do_GET(self):
        r = self.route()
        if not r:
            return
        path, q = r
        try:
            with S.lock:
                if path == '/api/status':
                    return self.send_json(status())
                if path == '/api/devices':
                    return self.send_json(do_devices(q))
                if path == '/api/wifi':
                    return self.send_json(do_wifi_get())
                if path == '/api/mode':
                    return self.send_json(do_mode(q))
                if path == '/api/version':
                    return self.send_json(do_version())
                if path == '/api/fields':
                    return self.send_json(do_fields())
                if path == '/api/program':
                    return self.send_json(do_read_program(q))
                if path == '/api/panel':
                    return self.send_json(do_panel(q))
                if path == '/api/wav':
                    wav, meta = do_wav(q)
        except ValueError as e:
            return self.send_json({'error': str(e)}, 400)
        except Exception as e:                               # noqa: BLE001
            S.note(f'ERROR {path}: {e}')
            return self.send_json({'error': str(e)}, 500)
        if path == '/api/wav':
            self.send_response(200)
            self.send_header('Content-Type', 'audio/wav')
            self.send_header('Content-Length', str(len(wav)))
            self.send_header('Content-Disposition',
                             f'attachment; filename="'
                             f'{q.get("name", ["sample"])[0]}.wav"')
            self.send_header('X-S950-Points', str(meta['npoints']))
            self.send_header('X-S950-Rate', str(meta['rate']))
            self.end_headers()
            return self.wfile.write(wav)
        if path.startswith('/api/'):
            return self.send_json({'error': 'no such endpoint'}, 404)
        name = path.lstrip('/') or 'index.html'
        if '..' in name or name.startswith('/'):
            return self.send_json({'error': 'bad path'}, 400)
        return self.send_file(os.path.join(WEB, name))

    def do_POST(self):
        r = self.route()
        if not r:
            return
        path, q = r
        n = int(self.headers.get('Content-Length') or 0)
        body = self.rfile.read(n) if n else b''
        try:
            with S.lock:
                if path == '/api/sample':
                    return self.send_json(do_sample(q, body))
                if path == '/api/announce':
                    return self.send_json(do_announce(q))
                if path == '/api/program':
                    return self.send_json(do_program(q, body))
                if path == '/api/load':
                    return self.send_json(do_load(q))
                if path == '/api/unload':
                    return self.send_json(do_unload(q))
                if path == '/api/clear':
                    return self.send_json(do_clear())
                if path == '/api/state':
                    return self.send_json(do_state())
                if path == '/api/delete':
                    return self.send_json(do_delete(q))
                if path == '/api/push':
                    return self.send_json(push_now())
                if path == '/api/verify':
                    return self.send_json(do_verify())
                if path == '/api/connect':
                    return self.send_json(do_connect(q))
                if path == '/api/wifi':
                    return self.send_json(do_wifi_set(q))
                if path == '/api/mount':
                    return self.send_json(do_mount(q))
                if path == '/api/eject':
                    return self.send_json(do_eject())
                if path == '/api/mode/enter':
                    return self.send_json(do_mode_enter(q))
            return self.send_json({'error': 'no such endpoint'}, 404)
        except ValueError as e:
            return self.send_json({'error': str(e)}, 400)
        except Exception as e:                               # noqa: BLE001
            S.note(f'ERROR {path}: {e}')
            return self.send_json({'error': str(e)}, 500)


def main():
    global S
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', type=int, default=8150)
    ap.add_argument('--image',
                    default=os.path.join(ROOT, 'build', 'S950_HD0.img'),
                    help='local mirror of the image the board serves')
    ap.add_argument('--host', default=W.DEF_HOST)
    ap.add_argument('--id', type=int, default=W.DEF_ID)
    ap.add_argument('--no-browser', action='store_true')
    ap.add_argument('--listen', default='127.0.0.1',
                    help='bind address; anything but loopback needs --token')
    ap.add_argument('--token', default=os.environ.get('S950_API_TOKEN'),
                    help='Bearer token every /api request must carry '
                         '(env S950_API_TOKEN)')
    ap.add_argument('--local', action='store_true',
                    help='no board: the mirror is the served image '
                         '(a card in a card reader)')
    a = ap.parse_args()
    global TOKEN
    TOKEN = a.token or None
    if not ipaddress.ip_address(a.listen).is_loopback and not TOKEN:
        sys.exit(f'--listen {a.listen} is reachable from the LAN: '
                 f'set --token or S950_API_TOKEN')
    if not os.path.exists(a.image):
        sys.exit(f'{a.image} does not exist. Make a blank one with:\n'
                 f'  python3 tools/s950dropbox.py new {a.image}\n'
                 f'or, to add the drop box to the image your card already '
                 f'serves:\n'
                 f'  python3 tools/s950dropbox.py addvol {a.image}')
    S = State(a.image, a.host, a.id, local=a.local,
              mode='local' if a.local else 'wifi')
    S.mirror_default = a.image
    S.note(f'mirror {a.image}, board {a.host} id {a.id}')
    srv = ThreadingHTTPServer((a.listen, a.port), Handler)
    url = f'http://localhost:{a.port}'
    if TOKEN:
        url += f'/?token={urllib.parse.quote(TOKEN)}'
    print(f'S950 sample loader on {url}  (image {a.image}, '
          f'board {a.host} id {a.id}, listening on {a.listen})')
    if not a.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print('\nstopped')


if __name__ == '__main__':
    main()
