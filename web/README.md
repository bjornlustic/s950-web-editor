# S950 sample loader

Browser page that converts audio to Akai S950 samples and pushes them
straight into the sampler's RAM over Wi-Fi, or onto the ZuluSCSI's card
over USB-C, with no DISK page and no disk image to build.

The page is three steps, top to bottom:

1. **Connection.** *Find devices* looks for the board on Wi-Fi and for
   ZuluSCSI cards and consoles on USB. Wi-Fi: the board's IP (from
   zuluscsi.ini `LoaderIP`); the board stays on the sampler's SCSI bus and
   loads happen live. USB-C: plug the board into the computer, press
   *Mount card* (the board is told over its USB console to show the card as
   a disk and leaves the bus), write files with *Save to card*, then
   *Eject & hand back* (the board reboots onto the bus). The card's Wi-Fi
   network (`WiFiSSID`/`WiFiPassword` in zuluscsi.ini) is set here too,
   while the card is mounted; the board joins it after Eject. USB-C mode
   uses macOS tools (`/Volumes`, `diskutil`, `networksetup`) and needs
   `pyserial` for the console.
2. **Sampler.** *Check now* writes a command into the mailbox and waits
   for the S950 to answer; only an answer proves it is in drop-box mode
   (idle, not on the DISK or RECORD page, no note sounding, no error
   banner). *Put sampler in drop-box mode* presses EDIT SAMPLE over the
   MIDI service ops if a MIDI interface is connected (`mido` +
   `python-rtmidi`). Nothing loads until this light is green: Send/Load
   are gated on it, and the server refuses a load with the reason rather
   than writing an announcement the machine would never act on.
3. **Import audio.** Drop files, name them, send.

The live machine mirror (LCD and lamps over MIDI) is not on the page; the
code (`/api/panel`, `tools/s950mirror.py`) stays for scripts.

```bash
python3 web/server.py          # http://localhost:8150
```

## Flow

Drop audio -> the browser decodes it, sums to mono, resamples to 40 kHz and
makes 16-bit PCM -> `POST /api/sample` -> `tools/s950wifi.plan_wav()` encodes
it to the S950's own disk format and deposits it in the local mirror
(`build/S950_HD0.img`: FAT allocation, directory entry, mailbox serial),
recording the changed byte ranges -> those ranges go to the ZuluSCSI over
UDP 5150 and into the image it is serving -> SuperOS 5.0 polls mailbox
block 3 about once a second while the sampler is idle, sees the new serial
and hands the file to the stock loader.

The sample appears in memory, named, within about a second of the sampler
going idle. The board never leaves SCSI mode and the sampler stays on the
bus throughout.

## Requirements

- SuperOS **5.0** on the S950 (the drop-box poller).
  Boot it from the Gotek or from the `SUPEROS` volume on the
  card.
- A SCSI volume selected once on the DISK page, so the firmware's "an hd
  volume is active" gate is open. Booting the OS from the card does this on
  its own.
- The ZuluSCSI running the SuperOS loader firmware
  (the separate ZuluSCSI repo), joined to Wi-Fi, with `S950/HD00_512.hda`
  on the card. Build a blank one with `python3 tools/s950dropbox.py new`
  and put it there with `python3 tools/s950card.py install` (one USB
  session).

  None of this is needed to try the encoders offline: `--local` writes to
  the image file directly, and `python3 selftest.py` exercises the whole
  format with no hardware at all.

## The mirror

`build/S950_HD0.img` is a local copy of the image the board serves; they
must stay identical. Every deposit is planned against the mirror, and a push
that fails leaves its ranges in `build/S950_HD0.img.pending.akpatch` for the
next attempt. "Verify card" reads the whole served image back and compares
it (117 s for 64 MB).

## API

The page talks to `web/server.py` over a JSON API that other software can
use too (`--listen` + `--token` for the LAN). Reference: `docs/API.md`;
Python client: `tools/s950api.py`.

## Speed

640-900 KB/s write, 750-950 KB/s read, measured against the board on
2026-09-08. A one-second 40 kHz sample is planned, pushed and verified in
0.4 s. These were measured on the author's own board; the emulation proof
behind them lives in the S950 OS repo (`tools/test_wifi950.py`), which is
not needed to use this one.
