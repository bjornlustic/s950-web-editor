# S950 sample loader

Browser page that converts audio to Akai S950 samples and pushes them
straight into the sampler's RAM over Wi-Fi, with no DISK page, no disk image
to build and no card to mount.

```bash
python3 web/server.py          # http://localhost:8150
```

## Flow

Drop audio -> the browser decodes it, sums to mono, resamples to 40 kHz and
makes 16-bit PCM -> `POST /api/sample` -> `tools/s950wifi.plan_wav()` encodes
it to the S950's own disk format and deposits it in the local mirror
(`build/S950_HD0.img`: FAT allocation, directory entry, mailbox serial),
recording the changed byte ranges -> those ranges go to the ZuluSCSI over
UDP 5150 and into the image it is serving -> SuperOS 4.0.3 polls mailbox
block 3 about once a second while the sampler is idle, sees the new serial
and hands the file to the stock loader.

The sample appears in memory, named, within about a second of the sampler
going idle. The board never leaves SCSI mode and the sampler stays on the
bus throughout.

## Requirements

- SuperOS **4.0.3** on the S950 (the drop-box poller).
  Boot it from the Gotek or from the `SUPEROS` volume on the
  card.
- A SCSI volume selected once on the DISK page, so the firmware's "an hd
  volume is active" gate is open. Booting the OS from the card does this on
  its own.
- The ZuluSCSI running the SuperOS loader firmware
  (the separate ZuluSCSI repo), joined to Wi-Fi, with `S950/HD00_512.hda`
  on the card. Copy it there over USB, or build a blank one with
  `python3 tools/s950dropbox.py new`.

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
