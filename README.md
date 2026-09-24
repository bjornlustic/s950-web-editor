# SuperOS-950 web editor and drop box

Drop audio on a browser page and it appears in an Akai S950's RAM seconds
later. No DISK page, no disk image to build by hand. The same repo is the
starting point for writing your own software that controls the sampler.

## Requirements

- An Akai S950 running **SuperOS-950 5.0**, from
  <https://superos303.com>. No firmware, ROM or OS image ships in this
  repo.
- A SCSI interface in the S950: the Akai **IB-109** or a **Rephlux** SCSI
  board.
- A **ZuluSCSI** as the S950's SCSI disk. For live loading over Wi-Fi it
  runs the superOS loader firmware (a separate repo); over USB-C the
  editor writes to its card directly.
- Python 3 (stdlib only). For MIDI control, a MIDI interface cabled both
  ways and `pip install mido python-rtmidi`. The USB-C card mode uses
  macOS tools and `pyserial`.

## Quick start

    python3 selftest.py                               # no hardware needed
    mkdir -p build && python3 tools/s950dropbox.py new build/S950_HD0.img
    python3 web/server.py                             # opens http://localhost:8150

Then follow the three steps on the page: connect to the ZuluSCSI (Wi-Fi
or USB-C), check that the sampler is in drop-box mode, drop audio. Full
flow and speed numbers: [web/README.md](web/README.md). `--local` writes
only to the local image file, for trying things without a board (see the
traps below).

## Build your own

Two ways into the sampler, both documented for application developers:

- **MIDI** reaches the running machine: play notes, move parameters with
  CCs, press panel keys, and read or write RAM with the stock Akai SysEx
  service ops (PEEK 0E, POKE 0C, CALL 0D). Guide with the exact frame,
  encoding, checksum, timing, a standalone `mido` example, key-press
  simulation, an address table and a safety section:
  **[docs/MIDI.md](docs/MIDI.md)**. Reference client:
  [tools/s950live.py](tools/s950live.py).
- **HTTP** reaches the disk the sampler polls: upload a WAV as a sample,
  build a program, load it into RAM, list what is resident, download,
  delete. Reference: **[docs/API.md](docs/API.md)**; stdlib-only client:
  [tools/s950api.py](tools/s950api.py).

## The drop box is a file format plus a mailbox convention

The Wi-Fi loader is only one way to deliver it. The sampler polls **block 3
of the SCSI disk, the MAILBOX, about once a second while idle**. 96 bytes:

    +0   'SRX1'      magic
    +4   serial      word, bumped on every deposit; 0 = nothing yet
    +6   volume[10]  drop-box volume name (default 'DROPBOX   ')
    +16  name[10]    the file just deposited
    +26  type        'S' sample / 'P' program
    +27..95          reserved

A deposit is: write the file into a normal S950 hd volume on the image
(stock directory + FAT), then bump the serial and name in the mailbox. The
firmware sees the serial change, re-reads the volume directory and hands the
named file to the stock loader.

**Ordering is a correctness requirement, not a detail:** write the mailbox
range LAST, so a half-applied patch never announces a file whose blocks have
not landed yet.

That is the whole contract. Anything that can write the image can implement
it: a Pi, a card reader, a card swapped by hand. `tools/s950dropbox.py`
works offline on a card: put files in, re-insert, and the sampler picks the
newest one up at the first poll.

Delivery over Wi-Fi uses a separate ZuluSCSI board running a forked
firmware; that is its own repo, and its UDP protocol client is
[tools/zulu_udp.py](tools/zulu_udp.py) here as well.

## Three traps that cost this project real time

1. **`--local` writes to a local file while reporting `board: true` and
   `pushed: true`.** It exists for offline work and for the emulator. A whole
   hardware session was lost to it. Check the `local` field in
   `GET /api/status`.
2. **The poller is gated on some pages.** With the DISK section up, and on
   PLAY after visiting DISK, the sampler does not poll and nothing loads.
   Land on EDIT SAMPLE or EDIT PRGRM.
3. **Deposits announced faster than the poll interval can be lost** - the
   mailbox holds ONE request. `do_program` in `web/server.py` serialises for
   this reason; `do_sample` does not.

## Check

    python3 selftest.py

Blank image, deposit a sample and a program, read both back, check the
mailbox, check the MIDI frame codec, import the server. No hardware.

## Provenance

The encoders in `tools/` (`build_hdimage.py`, `build_sounddisk.py`,
`s950dropbox.py`, `s950wifi.py`, `s950api.py`, `s950live.py`,
`s950mirror.py`, `s950card.py`) and `web/` are copied from the S950 OS
repo, which stays upstream for them: fix a format bug there too.
`tools/zulu_udp.py` is shared verbatim with the ZuluSCSI firmware repo and
the S1000 web editor; that firmware repo is its canonical copy.
`data/program_template.bin` is the 108-byte TONE PRGRM record
`build_sounddisk.program_file()` clones, lifted once out of a sounddisk
floppy image that is not redistributed here.

MIT, see [LICENSE](LICENSE).
