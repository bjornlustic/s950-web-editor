#!/usr/bin/env python3
"""Live peek/poke/call into a running S950 over the MIDI in jack.

The S950 keeps three Akai SysEx SERVICE ops, byte-identical to the stock
v1.2 ROM (handler table 0x7F84, opcode table 0x7F76):

  op 0E  PEEK   receive addr+len, transmit that RAM range back on MIDI OUT
  op 0C  POKE   receive an address, write the message bytes into RAM there
  op 0D  CALL   receive a 2-byte address and `jmp ax` to it

So a host with a MIDI cable can read and write the sampler's RAM while it
runs.  This is the only path that reaches live RAM without a firmware
change (Wi-Fi and USB-C both reach the disk image, not the machine), which
is what makes unattended hardware testing of the transwave morph possible:
sound a note, poke the loop-window position, peek the voice's node pointer
to see WHEN the change took effect.

THE FRAME (established against the real firmware parser, 0x8DAC..0x8E5C):

    F0  47  <ch>  <op>  40 00 00  <7-bit payload>  <cksum>  F7

  - <ch> is the exclusive channel byte the machine matches at 0x8DE5;
    0x9E7B holds it (0 by default).
  - The op is looked up in the table at 0x7F76.
  - Between the op and the handler the parser eats exactly THREE bytes:
    the 0x40 selector (0x8E09), one stored byte (0x8E32 -> 0xB68E), and a
    0x00 (0x8E3D).  An earlier note in this tree said FOUR - it was wrong,
    and every probe built on it was dropped one byte short of the handler.
  - Payload is 7-bit encoded as (value & 0x7F, value >> 7) byte PAIRS -
    the low seven bits, then the top bit - and the running XOR of every
    encoded byte is the checksum before F7 (builder 0x90DF, checker 0xB571).

A PEEK reply is the same shape with a canned header; after the seven
header bytes the body is (value, topbit) pairs: first the echoed
addr(2)+len(2), then the RAM, then the checksum and F7.  decode_reply
handles it.

  s950live.py peek  0x7F76 8            read 8 bytes, hex + ascii
  s950live.py poke  0xB690 aa 55 3c     write bytes
  s950live.py call  0x1234              jmp to an address (see the warning)
  s950live.py press EDIT_SAMPLE 0 2 ENT press panel keys (names or 0xNN)
  s950live.py --self-test               prove all three against the emulator

  --port UX16   pick the MIDI port      --ch 0   exclusive channel

CALL (0D) is unauthenticated code execution over the MIDI in jack.  It is
stock Akai behaviour, not something added here, but it is the sharp one:
use peek and poke for testing and reach for call only when nothing else
will do.  The driver never sends a call unless you ask for it by name.
"""
import argparse
import struct
import sys

PEEK, POKE, CALL = 0x0E, 0x0C, 0x0D
HDR_LEN = 7                      # F0 47 ch op 40 00 00

# A panel key press is one byte in the key buffer: the ROM's matrix scan
# (0x0308) translates through the table at 0x9AE4 and stores the code at
# [0xB567]; the page loop takes it with an xchg at 0x7E3.  Poking the
# code there is the same press (see press() for the one catch).  [0xB59A] is the running section's lamp
# mask (0xFF = no section), which is how a press is confirmed.
KEYBUF, SECTMASK = 0xB567, 0xB59A
_LAMP = {'EDIT_PROGRAM': 0, 'EDIT_SAMPLE': 1, 'RECORD': 2, 'PLAY': 3,
         'MIDI': 4, 'UTILITY': 5, 'DISK': 6}      # SECTMASK bit per section
KEYS = {'PLAY': 0x90, 'RECORD': 0x91, 'EDIT_SAMPLE': 0x92,
        'EDIT_PROGRAM': 0x93, 'DISK': 0x94, 'MIDI': 0x95, 'UTILITY': 0x96,
        'MASTER_TUNE': 0x97, 'CURSOR_LEFT': 0x08, 'CURSOR_RIGHT': 0x09,
        'PAGE_UP': 0x11, 'PAGE_DOWN': 0x10, 'SPACE': 0x20, 'ENT': 0x0D,
        'ON': 0x2B, 'OFF': 0x2D, 'LETTER': 0x0A, 'PB': 0x0F,
        **{str(d): 0x30 + d for d in range(10)}}


def _encode(data):
    """7-bit encode: each byte becomes (low7, topbit)."""
    out = []
    for b in data:
        out += [b & 0x7F, (b >> 7) & 1]
    return out


def _checksum(encoded):
    ck = 0
    for b in encoded:
        ck ^= b
    return ck


def frame(op, payload, ch=0):
    """Build one SERVICE-op SysEx message (a list of data bytes incl. F0/F7)."""
    enc = _encode(payload)
    return [0xF0, 0x47, ch, op, 0x40, 0x00, 0x00] + enc + [_checksum(enc), 0xF7]


def frame_peek(addr, n, ch=0):
    return frame(PEEK, list(struct.pack('<HH', addr, n)), ch)


def frame_poke(addr, data, ch=0):
    # op 0C receives 4 prologue bytes (addr_lo, addr_hi, and two the
    # handler discards) then writes the rest at [addr] until F7.
    return frame(POKE, list(struct.pack('<H', addr)) + [0, 0] + list(data), ch)


def frame_call(addr, ch=0):
    return frame(CALL, list(struct.pack('<H', addr)), ch)


def decode_reply(msg):
    """A PEEK reply -> (addr, length, data). `msg` is the bytes between and
    excluding F0..F7, or the whole message; either is accepted."""
    m = list(msg)
    if m and m[0] == 0xF0:
        m = m[1:]
    if m and m[-1] == 0xF7:
        m = m[:-1]
    body = m[HDR_LEN - 1:-1]                # drop 47 ch op 40 00 00 header, cksum
    vals = [body[i] | (body[i + 1] << 7) for i in range(0, len(body) - 1, 2)]
    addr, length = vals[0] | (vals[1] << 8), vals[2] | (vals[3] << 8)
    return addr, length, bytes(vals[4:])


# --------------------------------------------------------------------------
# Transports.  The library is transport-agnostic: MidoTransport talks to a
# real machine, EmuTransport drives the emulator for the self-test.

class MidoTransport:
    def __init__(self, port=None):
        import mido
        self.mido = mido
        ins, outs = mido.get_input_names(), mido.get_output_names()
        i = _pick(ins, port)
        o = _pick(outs, port)
        if not i or not o:
            sys.exit(f'need a MIDI in and out; saw in={ins} out={outs}')
        self.inp, self.out = mido.open_input(i), mido.open_output(o)

    def send(self, data):
        self.out.send(self.mido.Message('sysex', data=data[1:-1]))

    def recv(self, secs=0.5):
        import time
        end = time.time() + secs
        while time.time() < end:
            for msg in self.inp.iter_pending():
                if msg.type == 'sysex':
                    return [0xF0] + list(msg.data) + [0xF7]
            time.sleep(0.005)
        return None


class EmuTransport:
    """Drives tools/s950emu, filling the RX ring the way the ACIA does."""
    def __init__(self, variant='403'):
        import s950emu
        import s950dropbox as box
        suf = '' if variant == '402' else f'_{variant}'
        img = open(f'build/super950{suf}.bin', 'rb').read()
        self.m = s950emu.S950Emu(img, disk=box.new_image(box.DEF_VOL, 0x321),
                                 sector=512)
        self.m.boot()
        self.m.run_secs(0.5)

    def send(self, data):
        self.m.midi_sent()                 # clear the TX log
        self.m.midi_ring(bytes(data))

    def recv(self, secs=0.5):
        self.m.run_secs(secs)
        out = self.m.midi_sent()
        return list(out) if out else None


def _pick(names, want):
    if want:
        for n in names:
            if want.lower() in n.lower():
                return n
        return None
    for n in names:
        if 'ux16' in n.lower():
            return n
    return names[0] if names else None


# --------------------------------------------------------------------------
# The three operations, over any transport.

def peek(tp, addr, n, ch=0):
    tp.send(frame_peek(addr, n, ch))
    reply = tp.recv()
    if reply is None:
        raise RuntimeError(f'no PEEK reply for {addr:#06x} - check --ch '
                           f'(exclusive channel at 0x9E7B) and the cable')
    _, _, data = decode_reply(reply)
    return data[:n]


def poke(tp, addr, data, ch=0, verify=True):
    tp.send(frame_poke(addr, data, ch))
    if not verify:
        return True
    back = peek(tp, addr, len(data), ch)
    if bytes(back) != bytes(data):
        raise RuntimeError(f'POKE verify failed at {addr:#06x}: '
                           f'wrote {bytes(data).hex()} read {bytes(back).hex()}')
    return True


def key_code(name):
    """'EDIT_SAMPLE', 'edit sample', '7' or '0x92' -> the key code."""
    k = name.upper().replace(' ', '_').replace('-', '_')
    if k in KEYS:
        return KEYS[k]
    return int(name, 0)


def press(tp, keys, ch=0, gap=0.3):
    """Press panel keys in order.  `gap` seconds between keys lets the page
    loop take one before the next overwrites it.

    A section key is acted on only by the tick at 0x11A7, and only on a
    pass where the tick latch [0xB56C] differs from the counter [0xB5E6];
    otherwise the page loop's xchg at 0x7E3 takes the code first and
    drops it.  So each press writes 0xB567..0xB56C in one message: the
    code, the four cells between as they were read, and a stale latch,
    which makes the check run straight after this SysEx is handled.
    """
    import time
    for k in keys:
        mid = bytes(peek(tp, KEYBUF + 1, 5, ch))
        poke(tp, KEYBUF, bytes([key_code(k)]) + mid[:4] + bytes([mid[4] ^ 0x80]),
             ch, verify=False)
        time.sleep(gap)


def call(tp, addr, ch=0):
    """jmp to `addr`.  No reply, no return: the routine there takes over."""
    tp.send(frame_call(addr, ch))


# --------------------------------------------------------------------------

def _frame_check():
    """The wire format, no machine needed."""
    assert frame_peek(0x7F76, 8) == [0xF0, 0x47, 0, 0x0E, 0x40, 0, 0,
                                     0x76, 0, 0x7F, 0, 8, 0, 0, 0, 0x01, 0xF7]
    assert frame_poke(0xB567, [0x92])[7:-2] == [0x67, 0, 0x35, 1, 0, 0, 0, 0,
                                                0x12, 1]
    reply = [0xF0, 0x47, 0, 0x0E, 0x40, 0, 0] + _encode(
        [0x76, 0x7F, 2, 0, 0xAA, 0x01]) + [0, 0xF7]
    assert decode_reply(reply) == (0x7F76, 2, bytes([0xAA, 0x01]))
    assert key_code('edit sample') == 0x92 and key_code('0x2b') == 0x2B
    print('[PASS] frame encode/decode, key names')


def _self_test():
    """Prove peek/poke/call against the emulator's real firmware handlers."""
    _frame_check()
    sys.path.insert(0, 'tools')
    try:
        tp = EmuTransport('403')
    except (ImportError, OSError) as e:
        print(f'[SKIP] emulator half ({e}); it lives in the S950 OS repo')
        return

    rom = open('roms/s950_v12b_64k.bin', 'rb').read()
    got = peek(tp, 0x7F76, 8)
    assert got == rom[0x7F76:0x7F7E], \
        f'peek ROM: {got.hex()} != {rom[0x7F76:0x7F7E].hex()}'
    print(f'[PASS] peek 0x7F76..0x7F7D = {got.hex(" ")} (matches ROM)')

    tgt, val = 0xB690, bytes([0xAA, 0x55, 0x3C])
    assert peek(tp, tgt, 3) != val, 'scratch already held the test value'
    poke(tp, tgt, val)                     # verify=True reads it back
    print(f'[PASS] poke {tgt:#06x} <- {val.hex(" ")}, read back equal')

    # Every section key from every section, the way the page loop sees it,
    # then page keys: the screen must match a finger on the matrix.
    seq = ['EDIT_SAMPLE', 'MIDI', 'PLAY', 'EDIT_PROGRAM', 'UTILITY', 'DISK',
           'EDIT_SAMPLE', 'RECORD', 'PLAY']
    for name in seq:
        press(tp, [name], gap=0)
        tp.m.run_secs(0.5)
        m = peek(tp, SECTMASK, 1)[0]
        assert m == 1 << _LAMP[name], f'press {name}: section mask {m:#04x}'
    print(f'[PASS] press {" > ".join(seq)}: every section lamp followed')
    keys = ['EDIT_SAMPLE', 'PAGE_DOWN', 'PAGE_DOWN', 'CURSOR_RIGHT']
    finger = EmuTransport('403').m
    for k in keys:
        press(tp, [k], gap=0)
        tp.m.run_secs(0.5)
        finger.press(KEYS[k])
    assert tp.m.lcd.lines() == finger.lcd.lines(), \
        f'{tp.m.lcd.lines()} != {finger.lcd.lines()}'
    print(f'[PASS] press {" > ".join(keys)}: LCD equals a matrix press')

    # CALL: prove the primitive dispatches `jmp ax` to the requested
    # address, WITHOUT executing anything - hook the jmp and stop there.
    from unicorn import UC_HOOK_CODE
    from unicorn.x86_const import UC_X86_REG_AX
    seen = {}

    def _h(uc, a, s, u):
        if a == 0x9481:                    # the `jmp ax` inside op 0D
            seen['ax'] = uc.reg_read(UC_X86_REG_AX)
            uc.emu_stop()
    tp.m.uc.hook_add(UC_HOOK_CODE, _h)
    call(tp, 0x1234)
    tp.m.run_secs(0.5)
    assert seen.get('ax') == 0x1234, f'call dispatched to {seen.get("ax")}'
    print(f'[PASS] call 0x1234 reached jmp ax with ax=0x1234 (not executed)')
    print('ALL PASS')


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--port', help='MIDI port substring (default: UX16)')
    ap.add_argument('--ch', type=lambda x: int(x, 0), default=0,
                    help='exclusive channel (0x9E7B, default 0)')
    ap.add_argument('--self-test', action='store_true',
                    help='prove the driver against the emulator')
    sub = ap.add_subparsers(dest='cmd')
    p = sub.add_parser('peek'); p.add_argument('addr'); p.add_argument('n')
    p = sub.add_parser('poke'); p.add_argument('addr'); p.add_argument('bytes', nargs='+')
    p = sub.add_parser('call'); p.add_argument('addr')
    p = sub.add_parser('press'); p.add_argument('keys', nargs='+')
    a = ap.parse_args()

    if a.self_test:
        _self_test()
        return
    if not a.cmd:
        ap.print_help()
        return

    tp = MidoTransport(a.port)
    if a.cmd == 'peek':
        addr, n = int(a.addr, 0), int(a.n, 0)
        data = peek(tp, addr, n, a.ch)
        for off in range(0, len(data), 16):
            row = data[off:off + 16]
            hexs = ' '.join(f'{b:02x}' for b in row)
            asc = ''.join(chr(b) if 32 <= b < 127 else '.' for b in row)
            print(f'{addr + off:#06x}  {hexs:<48}  {asc}')
    elif a.cmd == 'poke':
        addr = int(a.addr, 0)
        data = bytes(int(b, 0) for b in a.bytes)
        poke(tp, addr, data, a.ch)
        print(f'poked {len(data)} bytes at {addr:#06x}, verified')
    elif a.cmd == 'press':
        press(tp, a.keys, a.ch)
        m = peek(tp, SECTMASK, 1, a.ch)[0]
        print(f'pressed {len(a.keys)} keys; section lamp mask {m:#04x}')
    elif a.cmd == 'call':
        addr = int(a.addr, 0)
        print(f'WARNING: jmp to {addr:#06x} over MIDI. No return.')
        call(tp, addr, a.ch)


if __name__ == '__main__':
    main()
