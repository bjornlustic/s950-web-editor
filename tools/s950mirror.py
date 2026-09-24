#!/usr/bin/env python3
"""What the S950 is showing and doing, read live over the MIDI channel.

The sampler's panel is 40x2 characters and eight LEDs, and all of it is
in RAM the SysEx service ops can read, so a browser can mirror the front
of the machine with nothing attached but a MIDI cable:

  LCD          0xAAE6 and 0xABEE, 40 bytes each - the two line buffers
               the stream routine fills ([0x9EE0] points at the one it
               is writing).  The emulator reads the same two.
  lamps        [0x9E55], the cached last write to port 0x64
  voices       0xFD54 + n*0x35: +0 flags, +0x0B the node it plays next,
               +0x13 its keygroup, +0x2F the voice INDEX
  morph        the keygroup's depth (kg+0x38) and stored End
               (kg+0x27 high with bit 7 as the stored flag, kg+0x39 low)

Sanity first, every read: voice n's +0x2F must equal n.  If the base or
the stride were wrong every number downstream would be fiction, and one
peek settles it.  (The S900 line uses voice+0x31 for the same purpose
and recommended it; this is that idea on our offsets.)

READ ONLY: this mirrors the machine.  To drive it, s950live.press()
presses panel keys (a one-byte poke of the key cell alone is taken by
the page loop and a section key is dropped; press() explains the fix).

    python3 tools/s950mirror.py                 once, to the terminal
    python3 tools/s950mirror.py --watch         until interrupted
    python3 tools/s950mirror.py --self-test     against the emulator
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import s950live                                              # noqa: E402

LCD1, LCD2, LCDW = 0xAAE6, 0xABEE, 40
LAMPS, LCDPTR = 0x9E55, 0x9EE0
# [0xB59A] is the ACTIVE SECTION's lamp mask, set from the table at 0x0E45
# when the page loop dispatches a section key (0x90 PLAY -> 0x08, 0x93
# EDIT PROGRAM -> 0x01 ...).  It is steadier than the port-0x64 cache,
# which the scan multiplexes, and 0xFF means no section is running at all
# - which is where an error banner leaves the machine.
SECTMASK = 0xB59A
VOICE0, VSTRIDE, NVOICE = 0xFD54, 0x35, 8
V_FLAGS, V_NODE, V_KG, V_INDEX = 0x00, 0x0B, 0x13, 0x2F
KG_DEPTH, KG_ENDHI, KG_ENDLO = 0x38, 0x27, 0x39
# port 0x64 bit -> lamp, measured on the machine (s950emu.LED_BITS).  The
# bit order is NOT the panel's left-to-right order, and a lamp is lit when
# its bit is LOW.
LED_BITS = {0: 'EDIT PROGRAM', 1: 'EDIT SAMPLE', 2: 'RECORD', 3: 'PLAY',
            4: 'MIDI', 5: 'UTILITY', 6: 'DISK', 7: 'MIDI RECEIVE'}
PANEL_ORDER = ('PLAY', 'RECORD', 'EDIT SAMPLE', 'EDIT PROGRAM',
               'MIDI', 'UTILITY', 'DISK', 'MIDI RECEIVE')


def _text(b):
    return ''.join(chr(c) if 32 <= c < 127 else ' ' for c in b)


def panel(tp, ch=0, voices=True):
    """One snapshot of the machine.  Raises if the voice-index check fails."""
    peek = s950live.peek
    out = {'lcd': [_text(peek(tp, LCD1, LCDW, ch)),
                   _text(peek(tp, LCD2, LCDW, ch))],
           'lcd_ptr': int.from_bytes(bytes(peek(tp, LCDPTR, 2, ch)), 'little'),
           'lamps': peek(tp, LAMPS, 1, ch)[0]}
    mask = peek(tp, SECTMASK, 1, ch)[0]
    out['section_mask'] = mask
    out['section'] = ('(none - no section running)' if mask == 0xFF else
                      next((n for i, n in LED_BITS.items() if mask >> i & 1),
                           f'{mask:#04x}'))
    lit = {LED_BITS[i] for i in range(8) if not out['lamps'] >> i & 1}
    out['lamp_names'] = [n for n in PANEL_ORDER if n in lit]
    out['lamp_state'] = [{'name': n, 'lit': n in lit} for n in PANEL_ORDER]
    if not voices:
        return out
    vs = []
    for n in range(NVOICE):
        base = VOICE0 + n * VSTRIDE
        b = bytes(peek(tp, base, 0x30, ch))
        if b[V_INDEX] != n:
            raise RuntimeError(
                f'voice {n} at {base:#06x} reports index {b[V_INDEX]} - the '
                f'base or stride is wrong, so nothing below can be trusted')
        node = int.from_bytes(b[V_NODE:V_NODE + 2], 'little')
        v = {'voice': n, 'flags': b[V_FLAGS], 'sounding': bool(b[V_FLAGS] & 0x1C),
             'node': node, 'kg': int.from_bytes(b[V_KG:V_KG + 2], 'little')}
        if 0x8000 < node < VOICE0:
            nb = bytes(peek(tp, node, 10, ch))
            v['window'] = {'count': int.from_bytes(nb[2:4], 'little'),
                           'addr': int.from_bytes(nb[4:7], 'little'),
                           'mode': nb[7],
                           'private': int.from_bytes(nb[8:10], 'little') == node,
                           'parked': bool(nb[0] & 0x40)}
        vs.append(v)
    out['voices'] = vs
    kgs = {v['kg'] for v in vs if 0x1000 < v['kg'] < 0xFD00}
    out['keygroups'] = []
    for kg in sorted(kgs):
        b = bytes(peek(tp, kg, 0x46, ch))
        out['keygroups'].append(
            {'addr': kg, 'sample': _text(b[0x18:0x22]).strip(),
             'midi_ch': b[0x14] + 1, 'depth': b[KG_DEPTH],
             'end': ((b[KG_ENDHI] & 0x7F) << 8) | b[KG_ENDLO],
             'end_stored': bool(b[KG_ENDHI] & 0x80)})
    return out


def render(p):
    bar = '+' + '-' * (LCDW + 2) + '+'
    lines = [bar, f'| {p["lcd"][0]} |', f'| {p["lcd"][1]} |', bar,
             f'section {p["section"]}   lamps {p["lamps"]:#04x} '
             f'{" ".join(p["lamp_names"]) or "(none)"}']
    for kg in p.get('keygroups', []):
        lines.append(f'kg {kg["addr"]:#06x} {kg["sample"]!r} ch{kg["midi_ch"]} '
                     f'morph depth {kg["depth"]} End {kg["end"]}'
                     f'{"" if kg["end_stored"] else " (not stored)"}')
    for v in p.get('voices', []):
        if not v['sounding'] and not v['node']:
            continue
        w = v.get('window')
        lines.append(
            f'voice {v["voice"]} flags {v["flags"]:#04x}'
            f'{" SOUNDING" if v["sounding"] else ""} node {v["node"]:#06x}'
            + (f' window {w["addr"]:#08x} count {w["count"]}'
               f'{" private" if w["private"] else ""}' if w else ''))
    return '\n'.join(lines)


def _self_test():
    import s950emu
    import s950dropbox as box
    img = open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), 'build', 'super950_403.bin'), 'rb').read()
    m = s950emu.S950Emu(img, disk=box.new_image(box.DEF_VOL, 0x321),
                        sector=512)
    m.boot()
    m.run_secs(0.5)

    class TP:
        def send(s, d):
            m.midi_sent(); m.midi_ring(bytes(d))

        def recv(s, secs=0.5):
            m.run_secs(secs); o = m.midi_sent(); return list(o) if o else None

    p = panel(TP())
    print(render(p))
    assert len(p['lcd'][0]) == LCDW and len(p['lcd'][1]) == LCDW
    assert p['lcd'][0].strip(), 'the LCD came back blank'
    assert len(p['voices']) == NVOICE
    print('[PASS] panel read back over the service ops, voice index checked')
    print('ALL PASS')


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--port', default='UX16 2')
    ap.add_argument('--ch', type=lambda x: int(x, 0), default=0)
    ap.add_argument('--watch', action='store_true')
    ap.add_argument('--interval', type=float, default=1.0)
    ap.add_argument('--self-test', action='store_true')
    a = ap.parse_args()
    if a.self_test:
        return _self_test()
    tp = s950live.MidoTransport(a.port)
    while True:
        print(render(panel(tp, a.ch)))
        if not a.watch:
            return
        time.sleep(a.interval)
        print()


if __name__ == '__main__':
    main()
