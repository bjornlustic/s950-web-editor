# Building your own SuperOS-950 application: MIDI and HTTP

This is the developer guide for controlling an Akai S950 running
SuperOS-950 5.0 from your own software. It covers everything a host can do
through the MIDI IN / MIDI OUT jacks (play the sampler, move its
parameters, press its panel keys, read and write its RAM) and points at
the HTTP API for moving samples and programs.

Status legend, used throughout: **HW** = measured on a real S950,
**EMU** = proven against the real firmware running in an emulator,
**ROM** = read from the disassembly of the stock v1.2 ROM,
**MANUAL** = from the SuperOS-950 5.0 owner's manual.

Reference client: [`tools/s950live.py`](../tools/s950live.py), one file,
Python 3. It needs `mido` and `python-rtmidi` for a real port; the wire
format itself is stdlib only. `python3 selftest.py` checks its framing.

Contents:

0. [Two ways in](#0-two-ways-in)
1. [Setup: cables and channel](#1-setup-cables-and-channel)
2. [Playing and parameters (channel messages)](#2-playing-and-parameters-channel-messages)
3. [The service ops: PEEK 0E, POKE 0C, CALL 0D](#3-the-service-ops-peek-0e-poke-0c-call-0d)
4. [Timing](#4-timing)
5. [A standalone Python example](#5-a-standalone-python-example)
6. [Pressing panel keys](#6-pressing-panel-keys)
7. [Useful addresses](#7-useful-addresses)
8. [Safety](#8-safety)
9. [Library reference (`tools/s950live.py`)](#9-library-reference-toolss950livepy)
10. [The HTTP API](#10-the-http-api)
11. [The Akai S900](#11-the-akai-s900)

## 0. Two ways in

| you want to | use | reaches |
|---|---|---|
| put samples and programs into the sampler | HTTP API, [API.md](API.md) (section 10) | the disk image the sampler polls |
| play notes, move filter / envelopes / LFO / transwave | MIDI channel messages (section 2) | the sound engine |
| press panel keys, change pages, type values | POKE of the key buffer (section 6) | the page loop |
| read the screen, the lamps, voices, any RAM | PEEK (sections 3, 7) | live RAM |
| write RAM | POKE (sections 3, 8) | live RAM |

The two are independent. MIDI needs only a cable: no Wi-Fi board, no
drop box, no server. The HTTP API needs the ZuluSCSI board and
`web/server.py`, not MIDI (except for its optional "put sampler in
drop-box mode" button, which uses the MIDI key press below).

## 1. Setup: cables and channel

- Connect **both** directions: your interface's MIDI OUT to the S950's
  MIDI IN (everything you send), and the S950's MIDI OUT to your
  interface's MIDI IN (PEEK replies). POKE and key presses work with the
  first cable alone, but then nothing can be confirmed.
- Do not build a MIDI loop (the S950's OUT merged or thru'd back into
  its IN). A PEEK reply is framed as op 0C (section 3), so a looped
  reply arrives as a POKE.
- **Channel.** The `<ch>` byte of every service op must equal the
  sampler's basic MIDI channel byte at RAM **[0x9E7B]**: 0 = channel 1
  ... 15 = channel 16. If bit 7 of that byte is set (omni on), any
  `<ch>` is accepted (ROM, parser state 0x8DE5). superOS wakes with omni
  OFF on channel 1, so the byte is **0x00**. Loading settings from disk
  restores whatever they carry. If PEEK gets no answer, try each channel
  0..15.

## 2. Playing and parameters (channel messages)

Status: MANUAL, and HW for notes, CC 74 and CC 87.

- **Channel:** the machine wakes with omni OFF on **channel 1**.
  Keygroup channel offsets (EDIT PROGRAM page *16) and omni apply to
  every controller below.
- **Notes:** note on / off with velocity. Above note 83 the replay rate
  wraps an octave: notes 84..96 play the pitches of 72..83 (HW, a
  hardware property of the machine).
- **CC values are parameter values.** A CC writes its number straight
  into the parameter; values above the parameter's range clamp to it.
- **Scope:** a CC writes every keygroup on the receiving channel and
  takes effect immediately; CC 74 also moves notes already sounding.
  StartCC and EndCC are read at note on, so they shape the next note.

### Fixed controllers (always on, nothing to assign)

| CC | parameter | range |
|---|---|---|
| 72 | amplitude envelope release | 0-99 |
| 73 | amplitude envelope attack | 0-99 |
| 74 | filter cutoff, live on sounding notes | 0-99 |
| 75 | amplitude envelope decay | 0-99 |
| 76 | LFO rate, or the clock division while MIDI clock is arriving | 0-99 |
| 77 | LFO depth | 0-99 |
| 78 | LFO delay | 0-99 |
| 79 | amplitude envelope sustain | 0-99 |
| 80 | filter envelope attack | 0-99 |
| 81 | filter envelope decay | 0-99 |
| 82 | filter envelope sustain | 0-99 |
| 83 | filter envelope release | 0-99 |
| 85 | filter envelope amount | 0-100 = -50..+50 |
| 86 | LFO desync | 64 and up = ON |
| 87 | transwave depth (EDIT PROGRAM *14 EndLFO) | 0-99 |

### Assignable controllers (UTILITY page *01 / *02)

Defaults below. Assignments reset to these at every boot; they are not
saved to disk.

| default CC | name | parameter |
|---|---|---|
| 5 | PortaCC | portamento time, per keygroup |
| 11 | LoudnessCC | keygroup loudness, 0-100 = -50..+50 |
| 12 | PitchCC | keygroup transpose, 64 = centre, +/-50 semitones |
| 13 | StartCC | sample start, coarse (MSB) |
| 13 + 32 = 45 | (follows StartCC) | sample start, fine (LSB); off when StartCC > 95 |
| off | EndCC | sample end = loop window position (transwave sweep); 127 = full sample |
| off | AAcapCC | anti-alias cap, value quartile = cap 0-3 |

StartCC alone gives 128 steps; StartCC + its LSB gives 16384 steps, exact
to the word on samples shorter than 16384 words.

### Reserved, clock, MIDI out

- **CC 1, 7, 64, 123** (mod wheel, volume, sustain, all notes off) are
  taken by the factory OS first; they cannot be assigned.
- **MIDI clock** locks every LFO (CC 76 then picks the division); **MIDI
  Start** aligns them to the downbeat. Always on.
- **MIDI OUT:** with FiltKnob ON (UTILITY *02), panel edits of Filter
  and Loudness are sent as CC 74 and LoudnessCC on the basic channel
  (HW for CC 74). Service op PEEK replies also come out of MIDI OUT.

## 3. The service ops: PEEK 0E, POKE 0C, CALL 0D

Status: ROM for the format (the handlers are the stock Akai v1.2 ROM's,
unchanged by superOS: opcode table 0x7F76, handler table 0x7F84), EMU for
all three ops, HW for PEEK and POKE.

The opcode table at 0x7F76 lists 14 ops: `00..0A` are the standard Akai
S900-format dump and request messages (not covered here), then `0C`,
`0D`, `0E`. This guide covers the last three.

### Frame

```
F0 47 <ch> <op> 40 00 00 <payload, 7-bit encoded> <checksum> F7
```

| field | value |
|---|---|
| `47` | Akai manufacturer ID |
| `<ch>` | the basic channel byte, section 1 (0x00 by default) |
| `<op>` | `0E` peek, `0C` poke, `0D` call |
| `40 00 00` | **exactly three bytes after the op**. The parser takes `40` (state 0x8E09), then one byte it stores at [0xB68E] (0x8E32), then requires `00` (0x8E3D). Sending four shifts the payload by one byte: a PEEK is ignored, and a POKE can land at the wrong address |
| payload | every raw byte `b` is sent as TWO bytes: `b & 0x7F` (low 7 bits), then `b >> 7` (the top bit, 0 or 1) |
| checksum | running XOR of every encoded payload byte (both bytes of every pair) |

All multi-byte numbers in the raw payload are little-endian.

The receiver (0x96C4) decodes each pair as `low7 | topbit << 7` and XORs
both bytes into [0xB571]; the transmitter (0x90DF) does the same in
reverse. The checksum byte is compared at F7.

### Worked example (generated by `tools/s950live.py`)

PEEK 8 bytes at 0x7F76, the opcode table:

```
raw payload        76 7F  08 00              addr 0x7F76, len 8, little-endian
encoded pairs      76 00  7F 00  08 00  00 00
checksum           76 ^ 00 ^ 7F ^ 00 ^ 08 ^ 00 ^ 00 ^ 00 = 01
frame              F0 47 00 0E 40 00 00  76 00 7F 00 08 00 00 00  01 F7
```

`python3 -c "import sys; sys.path.insert(0,'tools'); import s950live as L; print(bytes(L.frame_peek(0x7F76, 8)).hex(' '))"`
prints that frame.

### 0E PEEK: read RAM

Raw payload: `addr_lo addr_hi len_lo len_hi` (exactly four bytes, and
the checksum must match, or the request is ignored).

The reply comes out of MIDI OUT. Its header is a canned template at ROM
0x7FA9, `F0 47 xx 0C 40 00 00`, with `xx` filled from RAM [0x9E70]; note
that the op byte is **0C**. After the header come encoded pairs holding
the echoed `addr(2)` and `len(2)`, then the `len` RAM bytes, then the
checksum and F7. The reply to the example above, with the ROM's opcode
table in it:

```
F0 47 00 0C 40 00 00  76 00 7F 00 08 00 00 00
                      00 00 01 00 02 00 03 00 04 00 05 00 06 00 07 00  01 F7
                      = bytes 00 01 02 03 04 05 06 07
```

That reply is the standard "is it alive and on this channel" probe. Its
body is the same on every S950: it is the ROM's opcode table.

Decoding: drop F0, the six header bytes after it, the checksum and F7;
pair up the rest as `low | top << 7`; the first four values are the
echoed address and length.

### 0C POKE: write RAM

Raw payload: `addr_lo addr_hi 00 00 data...`. The handler reads four
values (address plus two it ignores), then writes every following value
to `addr`, `addr+1`, ... until F7. There is no reply; confirm with a
PEEK (`poke()` does this unless `verify=False`).

The bytes are written **as they arrive**; the checksum is checked only
at F7, and a bad checksum does not undo the write (ROM, 0x9468 and
0x96C4). Build frames with code, not by hand.

```
F0 47 00 0C 40 00 00  67 00 35 01  00 00 00 00  12 01  40 F7
                      addr 0xB567  two ignored  0x92   checksum
```

(That is the naive one-byte key press; section 6 explains why a key
press needs six bytes.)

### 0D CALL: jump to an address

Raw payload: `addr_lo addr_hi`. After the checksum passes, the handler
jumps to that address (`jmp ax` at 0x9481). Nothing returns and nothing
is replied; whatever code is at the address takes over the machine. This
is stock Akai behaviour: anything on the MIDI IN jack can do it.
Applications have no reason to use it; PEEK and POKE plus key presses
cover every documented use. `s950live.py` sends it only when asked by
name.

## 4. Timing

- **One message at a time.** When a service-op SysEx arrives, the
  handler busy-polls the MIDI receive ring (0xFBD4..0xFCD3, filled by
  the receive interrupt at 0x0F61) byte by byte, with a short inter-byte
  timeout ([0xB63A] = 0x1F ticks, ROM 0x96C4 / 0x9228). A message that
  stalls mid-way is dropped. Send each message as one burst, as any
  normal MIDI interface does with a SysEx.
- **Wait for the reply** to a PEEK before sending anything else. The
  reference client waits up to 0.5 s; replies arrive well inside that
  (HW).
- A POKE has no reply. Either PEEK it back (which also paces you) or
  leave a gap before the next message.
- **Drain your input** before sending a PEEK, so a stale reply is not
  taken for the new one.
- Key presses: leave about 0.3 s between keys (section 6).

## 5. A standalone Python example

No project code, only `mido`:

```
pip install mido python-rtmidi
python3 s950_hello.py "UX16"        # any substring of your port name
```

```python
#!/usr/bin/env python3
"""Probe an S950 over MIDI, then press EDIT SAMPLE."""
import sys
import time

import mido

CH = 0                                   # basic channel byte: 0 = channel 1


def frame(op, raw):
    enc = []
    for b in raw:
        enc += [b & 0x7F, b >> 7]        # (low 7 bits, top bit)
    ck = 0
    for b in enc:
        ck ^= b                          # running XOR of the encoded bytes
    return [0x47, CH, op, 0x40, 0x00, 0x00] + enc + [ck]   # mido adds F0/F7


def peek(inp, out, addr, n, timeout=0.5):
    for _ in inp.iter_pending():         # drop anything stale
        pass
    out.send(mido.Message('sysex', data=frame(
        0x0E, [addr & 0xFF, addr >> 8, n & 0xFF, n >> 8])))
    end = time.time() + timeout
    while time.time() < end:
        for m in inp.iter_pending():
            d = list(m.data) if m.type == 'sysex' else []
            if d[:1] == [0x47] and len(d) > 7:
                body = d[6:-1]           # after the header, before checksum
                vals = [body[i] | body[i + 1] << 7
                        for i in range(0, len(body) - 1, 2)]
                return bytes(vals[4:4 + n])   # skip echoed addr + len
        time.sleep(0.005)
    raise RuntimeError(f'no reply to PEEK {addr:#06x}: check both cables '
                       f'and the channel')


def poke(out, addr, data):
    out.send(mido.Message('sysex', data=frame(
        0x0C, [addr & 0xFF, addr >> 8, 0, 0] + list(data))))


def press(inp, out, code):
    """Key code into 0xB567 plus a stale tick latch, in ONE message."""
    mid = peek(inp, out, 0xB568, 5)      # 0xB568..0xB56C as they are now
    poke(out, 0xB567, [code] + list(mid[:4]) + [mid[4] ^ 0x80])


def main():
    want = sys.argv[1] if len(sys.argv) > 1 else 'UX16'
    pick = lambda names: next(n for n in names if want.lower() in n.lower())
    inp = mido.open_input(pick(mido.get_input_names()))
    out = mido.open_output(pick(mido.get_output_names()))

    table = peek(inp, out, 0x7F76, 8)
    print('opcode table:', table.hex(' '))          # 00 01 02 ... 07
    press(inp, out, 0x92)                           # EDIT SAMPLE
    time.sleep(0.3)
    mask = peek(inp, out, 0xB59A, 1)[0]
    print('section mask', hex(mask), '(0x02 = EDIT SAMPLE)')


if __name__ == '__main__':
    main()
```

This is the same logic as `tools/s950live.py` (`frame`, `peek`, `poke`,
`press`, `decode_reply`), cut to the minimum.

## 6. Pressing panel keys

Status: EMU (every section key from every section, and page keys whose
resulting screen equals a physical press). The web editor's "put
sampler in drop-box mode" button uses it.

A key press is one byte, the **key code**, in the key buffer at
**0xB567**. That is where the firmware's own panel scan (0x0308) puts a
debounced key, so the page loop cannot tell a poked key from a finger.

```
python3 tools/s950live.py --port UX16 press EDIT_SAMPLE          # go to EDIT SAMPLE
python3 tools/s950live.py --port UX16 press PAGE_DOWN 1 2 0 ENT  # next page, type 120, enter
```

```python
import s950live as L
tp = L.MidoTransport('UX16')            # substring of the MIDI port name
L.press(tp, ['EDIT_PROGRAM', 'PAGE_DOWN', 'CURSOR_RIGHT'])
```

### Key codes

| key | code | key | code |
|---|---|---|---|
| PLAY | 0x90 | CURSOR_LEFT | 0x08 |
| RECORD | 0x91 | CURSOR_RIGHT | 0x09 |
| EDIT_SAMPLE | 0x92 | PAGE_UP | 0x11 |
| EDIT_PROGRAM | 0x93 | PAGE_DOWN | 0x10 |
| DISK | 0x94 | SPACE | 0x20 (also pages) |
| MIDI | 0x95 | ENT | 0x0D |
| UTILITY | 0x96 | ON (+) | 0x2B |
| MASTER_TUNE | 0x97 | OFF (-) | 0x2D |
| digits 0-9 | 0x30-0x39 (ASCII) | LETTER | 0x0A |
| | | PB | 0x0F |

`s950live.KEYS` holds this table; `press()` accepts the names, digits
as strings, or any code as `'0xNN'`. The superOS pages (*01, *02 ...)
open from the UTILITY key.

### A press is a six-byte POKE, not one byte

Section keys (0x90-0x97) are acted on only by the timer tick at 0x11A7,
and only on a pass where the tick latch **[0xB56C]** differs from the
tick counter **[0xB5E6]** (ROM: `xchg [0xB56C], al` then compare). On
any other pass the page loop's `xchg [0xB567]` at 0x07E5 takes the code
first and drops it. That is why a one-byte POKE of 0xB567 works from
PLAY but is lost inside EDIT SAMPLE: it loses a race.

So a press writes **0xB567..0xB56C in ONE POKE message**:

| address | value |
|---|---|
| 0xB567 | the key code |
| 0xB568..0xB56B | exactly the four bytes just peeked there |
| 0xB56C | the byte just peeked there, XOR 0x80 |

The flipped latch makes the tick run its section check straight after
the SysEx is handled, before the page loop reads the key. Page keys
(digits, cursor, ENT) pass that check untouched and reach the page loop
as usual. Example, pressing EDIT SAMPLE when PEEK 0xB568 5 returned
`00 00 00 00 05` (illustrative values; always peek first):

```
PEEK  F0 47 00 0E 40 00 00  68 00 35 01 05 00 00 00  59 F7
POKE  F0 47 00 0C 40 00 00  67 00 35 01 00 00 00 00
                            12 01 00 00 00 00 00 00 00 00 05 01  44 F7
                            0x92  B568..B56B  0x85 (0x05 ^ 0x80)
```

### Confirming a press

- **[0xB59A]**, 1 byte: the running section's lamp mask. One bit set per
  section, **0xFF = no section running** (what an error banner leaves).
  Bits come from the ROM table at 0x0E45.

  | bit | 0 | 1 | 2 | 3 | 4 | 5 | 6 |
  |---|---|---|---|---|---|---|---|
  | section | EDIT PROGRAM | EDIT SAMPLE | RECORD | PLAY | MIDI | UTILITY | DISK |

- For page keys, the LCD text buffers (section 7, with its caveat).
- Allow about 0.3 s between keys (`press(gap=0.3)`, the default).

### Known traps

- While the DISK or RECORD section is up, the drop box does not poll;
  pressing EDIT_SAMPLE is how a host gets it polling again.
- Older reports of section keys being ignored inside EDIT SAMPLE, or
  wedged after EDIT PROGRAM, were the one-byte POKE losing the race
  above. With the six-byte press neither reproduces (EMU).
- PB (0x0F) is handled by the tick itself (it sets a flag and clears the
  key), exactly as a physical PB press is.

## 7. Useful addresses

RAM addresses of SuperOS-950 5.0 on the S950. Status per row; everything
here is safe to PEEK at any time.

| address | size | meaning | status / source |
|---|---|---|---|
| 0x7F76 | 14 | service opcode table `00..0A 0C 0D 0E`; the first 8 bytes read `00 01 .. 07`: the liveness probe | ROM |
| 0x7F84 | 28 | handler table, one word per opcode | ROM |
| 0x9E7B | 1 | basic MIDI channel (0 = ch 1), bit 7 = omni; the `<ch>` the service ops must carry | ROM 0x8DE5, HW |
| 0x9E55 | 1 | last value written to the lamp port 0x64, **active low** (lamp lit when its bit is 0). Bits as the section table in 6; bit 7 = MIDI RECEIVE | HW |
| 0xB59A | 1 | running section lamp mask (section 6), 0xFF = none | ROM, EMU, HW |
| 0xB567 | 1 | key buffer (0 = empty) | ROM 0x07E5 |
| 0xB56C | 1 | tick latch (section 6) | ROM 0x11AE |
| 0xB5E6 | 1 | tick counter (section 6) | ROM 0x11AB |
| 0xAAE6 | 40 | LCD line 1 text buffer (ASCII). Holds the last text streamed through it; pages drawn straight from their templates bypass it, so it can lag the display. Confirm sections with 0xB59A, not with this | EMU |
| 0xABEE | 40 | LCD line 2 text buffer, same caveat | EMU |
| 0xFD54 + n*0x35 | 0x35 | voice n, n = 0..7: +0x00 flags, +0x0B next replay node, +0x13 keygroup pointer, +0x2F voice index (equals n: check it first, it proves the base and stride) | EMU, HW |
| 0xC5B0 + n*0x46 | 0x46 | resident directory, 198 header slots | ROM (program change scan 0x87C0) |

Keygroup fields, relative to a keygroup pointer read from a voice's
+0x13:

| offset | meaning | may POKE? |
|---|---|---|
| +0x27 | stored End, high byte (bit 7 = stored flag) | yes |
| +0x28 | sample header pointer (word) | **never** (section 8) |
| +0x38 | transwave depth, 0-99 (what CC 87 writes) | yes, or send CC 87 |
| +0x39 | stored End, low byte | yes |

Sample header (from keygroup +0x28, read only): +0x10 length in words,
+0x24 loop length (may POKE; the loop-length field).

`tools/s950mirror.py` reads the LCD, lamps and voices this way and is a
second worked client; `GET /api/v1/panel` serves the same over HTTP.

## 8. Safety

The rule: **poke value fields, never structural ones.** Before writing a
cell, ask which firmware routine normally writes it and what else that
routine updates. If the answer is "a resolver" or "a list rebuild", use
the real path (a MIDI CC, a panel key press, or a drop-box deposit), not
a POKE.

Safe, because the firmware's own CC and panel paths write these same
cells:

- the key buffer 0xB567..0xB56C, via the six-byte press
- keygroup stored End (+0x27 / +0x39) and transwave depth (+0x38); or
  just send CC 87 / EndCC
- a sample header's loop length (+0x24)

Hangs the machine (HW):

- **keygroup +0x28, the sample header pointer.** The voice's replay
  list was built for the old sample; the next note-on hangs. To play a
  different sample, deposit a program that uses it (HTTP API) and let
  the loader bind it.
- **A replay node, or a voice's node pointer, written by hand.** It
  bypasses the engine's bounds checks.
- **[0x1EF6], the active volume name**, while the ZuluSCSI board is off
  the SCSI bus.

What to expect:

- A POKE writes as it receives (section 3): a truncated or corrupted
  message can leave a partial write. Build frames in code; verify with
  PEEK.
- **A power cycle recovers** from any bad POKE. Everything the service
  ops touch is RAM.
- **Nothing is saved unless you save.** POKEs, key presses and CCs change
  the running machine only. Samples and programs reach disk only when
  you (or the page you pressed your way to) save them, and assignable
  CC numbers reset at every boot.
- Do not CALL (0D). There is no documented use for it.

## 9. Library reference (`tools/s950live.py`)

```
peek(tp, addr, n, ch=0) -> bytes
poke(tp, addr, data, ch=0, verify=True)     # raises if read-back differs
press(tp, keys, ch=0, gap=0.3)              # names, '7', or '0x92'
call(tp, addr, ch=0)                        # 0D, see section 3
frame_peek(addr, n) / frame_poke(addr, data) / frame_call(addr) -> list of bytes
decode_reply(sysex) -> (addr, length, data)
KEYS, KEYBUF (0xB567), SECTMASK (0xB59A)
```

A transport is any object with `send(list_of_bytes)` (a whole F0..F7
message) and `recv(secs) -> list_of_bytes | None`. `MidoTransport(port)`
is the real one; write your own for WebMIDI, a serial bridge, or a
test double.

CLI:

```
python3 tools/s950live.py --port UX16 peek 0x7F76 8
python3 tools/s950live.py --port UX16 poke 0xB567 0x96      # naive one-byte UTILITY press
python3 tools/s950live.py --port UX16 press UTILITY
python3 tools/s950live.py --self-test      # wire format; the emulator half lives in the S950 OS repo
```

`--ch` sets the channel byte if it is not 0. Without `--port` the first
port whose name contains "UX16" is used, else the first port.

## 10. The HTTP API

MIDI reaches the running machine; the HTTP API reaches its disk. Run
`python3 web/server.py` (add `--listen` and `--token` for the LAN) and
you get JSON endpoints to upload a WAV as an S950 sample, build a
program around it, load either into the sampler's RAM through the drop
box, list what is resident and how much memory is free, download a
sample back as WAV, and delete. Full reference: [API.md](API.md);
stdlib-only Python client: [`tools/s950api.py`](../tools/s950api.py).

A typical application uses both: the HTTP API to put sounds in, MIDI
to play them and move their parameters.

## 11. The Akai S900

An S900 running SuperOS-900 5.0 answers the same Akai service ops
(PEEK 0E, POKE 0C, CALL 0D) with its own RAM addresses. Those are
documented in the SuperOS-900 manual at
<https://superos303.com/doc/s900>. Nothing in section 7 applies to the
S900.
