# S950 web editor and drop box

Drop audio on a browser page and it appears in an Akai S950's RAM seconds
later. No DISK page, no disk image to build by hand, no card to eject.

    python3 tools/s950dropbox.py new build/S950_HD0.img
    python3 web/server.py --local

Full flow, requirements and speed numbers: [web/README.md](web/README.md).
HTTP API: [docs/API.md](docs/API.md), with a stdlib-only client in
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

## Requires

Python 3, stdlib only. SuperOS 5.0 on the sampler for the drop-box poller
(separate release channel; no firmware or ROM ships here).

## Check

    python3 selftest.py

Blank image, deposit a sample and a program, read both back, check the
mailbox. No hardware.

## Provenance

The encoders in `tools/` (`build_hdimage.py`, `build_sounddisk.py`,
`s950dropbox.py`, `s950wifi.py`, `s950api.py`) are copied from the S950 OS
repo, which stays upstream for them: fix a format bug there too.
`tools/zulu_udp.py` is shared verbatim with the ZuluSCSI firmware repo and
the S1000 web editor; that firmware repo is its canonical copy.
`data/program_template.bin` is the 108-byte TONE PRGRM record
`build_sounddisk.program_file()` clones, lifted once out of a sounddisk
floppy image that is not redistributed here.

MIT, see [LICENSE](LICENSE).
