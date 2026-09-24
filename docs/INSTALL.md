# Install the SuperOS-950 web editor

For Akai S950 owners. At the end you drop a WAV on a browser page and it
lands in the sampler's memory a few seconds later.

## 1. What you need

1. An Akai S950 running **SuperOS-950 5.0**, from
   <https://superos303.com>. You need the download for step 3.
2. A SCSI interface in the S950: the Akai **IB-109** or a **Rephlux**
   SCSI board.
3. A **ZuluSCSI** as the S950's SCSI disk. Two ways to reach it:
   - **Wi-Fi** (live loading, sampler stays running): the ZuluSCSI must
     run the loader firmware. Install that first:
     <https://github.com/bjornlustic/s950-zuluscsi/blob/superos/INSTALL.md>
   - **USB-C** (card mode): the board is plugged into your computer and
     the editor writes to its card. The sampler loads the files after you
     hand the card back. **macOS only.**
4. A computer with **Python 3.7 or newer**. Check with `python3 --version`.
5. Optional Python packages:
   - `pip3 install pyserial`: needed for USB-C mode and for putting the
     disk image on the card with `tools/s950card.py`.
   - `pip3 install mido python-rtmidi`: needed only for the **Put sampler
     in drop-box mode** button and other MIDI features. Needs a MIDI
     interface cabled both ways to the S950.

What runs where:

| Part | macOS | Windows / Linux |
|---|---|---|
| Web page, Wi-Fi loading | yes | yes |
| USB-C mode, Mount card, Eject, setting Wi-Fi from the page | yes | no (edit the card in a card reader) |
| `tools/s950card.py install` | yes | no (copy by hand, step 3) |

## 2. Get the code

Either:

```bash
git clone https://github.com/bjornlustic/s950-web-editor.git
cd s950-web-editor
```

or download the ZIP from the GitHub page (**Code > Download ZIP**),
unzip it, and open a terminal in that folder.

Check it:

```bash
python3 selftest.py
```

It should end with `ok: web/server.py and its tools import`. No hardware
needed.

## 3. Make the disk image and put it on the card

The editor keeps a copy of the card's S950 disk on your computer, in
`build/S950_HD0.img`. **This copy and the image on the card must stay
identical**, so make one file and put the same file in both places.

1. Copy the SCSI image from the SuperOS-950 download and add the drop-box
   volume to it:

   ```bash
   mkdir -p build
   cp /path/to/SuperOS-950-v5.0.0-SCSI.img build/S950_HD0.img
   python3 tools/s950dropbox.py addvol build/S950_HD0.img
   ```

   `addvol` keeps the `SUPEROS` volume (the bootable OS) and adds a
   `DROPBOX` volume next to it.

   No SuperOS image to hand? `python3 tools/s950dropbox.py new
   build/S950_HD0.img` makes a blank disk with only the drop-box volume.
   It erases the file it writes to.

2. Put it on the card as `S950/HD00_512.hda` (SCSI ID 0, the only ID the
   S950 uses).

   **macOS, board on USB-C:**

   ```bash
   python3 tools/s950card.py install
   ```

   This puts the board in card-reader mode, copies the image, adds
   `Dir1 = "S950"` under `[SCSI]` in `zuluscsi.ini`, and ejects.

   **Any computer, card in a card reader:**
   1. Create a folder `S950` in the root of the card.
   2. Copy `build/S950_HD0.img` into it and rename the copy to
      `HD00_512.hda`.
   3. In `zuluscsi.ini`, under `[SCSI]`, add `Dir1 = "S950"`. If `Dir1` is
      taken, use the next free `Dir2` to `Dir9`.
   4. Eject the card and put it back in the board.

## 4. Start the editor

```bash
python3 web/server.py
```

Your browser opens <http://localhost:8150>. Stop the editor with Ctrl+C.

Useful options:

| Option | What it does |
|---|---|
| `--host 192.168.1.250` | The board's `LoaderIP` from `zuluscsi.ini`. You can also type it on the page. |
| `--port 8150` | Web page port. |
| `--image build/S950_HD0.img` | The local copy of the card image. |
| `--id 0` | SCSI ID of the S950 image. Leave at 0. |
| `--no-browser` | Do not open a browser window. |
| `--listen <address> --token <secret>` | Let other computers on your network use the page. Any address other than this computer needs a token. |
| `--local` | **No board.** Writes only to the local file. For trying the page without hardware. |

> **Warning: `--local` looks like it works.** The page reports the board
> as present and every transfer as pushed, but nothing leaves your
> computer. If samples never arrive, make sure you did not start with
> `--local`.

## 5. On the sampler

1. Boot SuperOS-950 5.0: from the floppy or Gotek, or from the `SUPEROS`
   volume on the card (DISK page, load entire disk, ENT).
2. Select a SCSI volume once on the DISK page. Booting from the card
   does this for you.
3. **Leave the DISK page.** Press **EDIT SAMPLE** or **EDIT PRGRM**. The
   sampler does not check for new files while DISK or RECORD is up, and
   not on PLAY after a visit to DISK.
4. Let any note finish and clear any error message on the display.

## 6. In the page

The page has three numbered steps.

**Step 1: Connection**

Wi-Fi:
1. Press **Find devices**.
2. Check the **Board IP** (your `LoaderIP`), then press **Connect over
   Wi-Fi**.

USB-C (macOS):
1. Plug the board into the computer with USB-C.
2. Press **Mount card**. The board leaves the sampler's SCSI bus and its
   card appears as a disk.
3. Pick the card and press **Use this card** if it is not already chosen.
4. To set the Wi-Fi network the board joins: choose or type the network
   name under **Board joins network**, enter the password, press **Save
   to card**. The board uses it after Eject.
5. When done, press **Eject & hand back to sampler**. The board reboots
   onto the SCSI bus.

**Step 2: Sampler**

1. Press **Check now**. The light turns green when the sampler answers.
   Nothing can be loaded into memory until it is green.
2. If it stays off: on the panel, press EDIT SAMPLE (see section 5), then
   **Check now** again. With MIDI set up (section 8), **Put sampler in
   drop-box mode** presses EDIT SAMPLE for you.

Over USB-C the sampler cannot answer (the board is off its bus). Files are
stored on the card and loaded after Eject.

**Step 3: Import audio**

1. Drop a WAV (or AIFF, MP3, FLAC) on the drop area, or press **choose
   files**.
2. Edit the name if you like (10 characters, A-Z 0-9 space).
3. Leave **Load into S950 RAM** ticked. Pick **One-shot** or **Looped**
   and a rate (40000 Hz is the stock rate).
4. Press **Send all**.

## 7. First load: what you should see

1. The transfer log shows the file being written and pushed.
2. Within a few seconds of the sampler being idle, the sample is in its
   memory under the name you gave it.
3. To confirm from the page, press **Read from S950** in the **Sampler
   memory** panel. The sample is listed there.
4. The file also stays in the **Drop-box volume** list, so it can be
   loaded again later with **Load selected**.

## 8. MIDI setup (optional)

Needed only for **Put sampler in drop-box mode** and for the MIDI tools
in [MIDI.md](MIDI.md).

1. `pip3 install mido python-rtmidi`
2. Cable both ways: interface MIDI OUT to S950 MIDI IN, and S950 MIDI OUT
   to interface MIDI IN. Do not loop the S950's output back into its
   input.
3. Leave the S950 on MIDI channel 1 with omni off (SuperOS-950 starts
   that way). The editor talks on channel 1.
4. In step 2 of the page, type your interface's port name (any part of
   it) in the **MIDI** box. The default `UX16 2` is the author's
   interface. List your ports with:

   ```bash
   python3 -c "import mido; print(mido.get_input_names())"
   ```

## 9. Troubleshooting

**Three traps**

1. Started with `--local`: nothing leaves your computer although the page
   says pushed. Restart without it.
2. Sampler on the DISK or RECORD page, or on PLAY after DISK: it does not
   poll and nothing loads. Go to EDIT SAMPLE or EDIT PRGRM.
3. Sending faster than the sampler polls can lose a request, because the
   sampler takes one at a time. If a sample does not arrive, select it in
   **Drop-box volume** and press **Load selected**.

**Nothing loads**

- Step 2 light not green: see section 5.
- **Pending** in the top bar is not 0: a transfer did not finish. Press
  **Push pending**.
- You wrote files over USB-C and then switched to Wi-Fi: the local copy
  no longer matches the card. Before pressing Eject, copy the card image
  back:
  `cp /Volumes/<CARD>/S950/HD00_512.hda build/S950_HD0.img`.
  **Verify card** compares the two.

**Device not found**

- Wi-Fi: `python3 tools/zulu_udp.py ping --host <LoaderIP>` must answer.
  If not, see the ZuluSCSI install guide's troubleshooting.
- Board answers but says no image at id 0: the card has no
  `S950/HD00_512.hda`, or `zuluscsi.ini` lacks `Dir1 = "S950"` (section 3).
- USB-C: **Mount card** needs `pyserial` and macOS. The card must contain
  `zuluscsi.ini` to be recognised.
- Editor will not start and says `build/S950_HD0.img does not exist`: do
  section 3.

**Firewall**

- The page is served on TCP 8150 on this computer only (unless you use
  `--listen`).
- The editor talks to the board on UDP port 5150. A firewall or a guest
  Wi-Fi network that blocks devices from reaching each other will stop
  it. Put the computer and the board on the same ordinary network.

## Build your own

To control the sampler from your own software, see [MIDI.md](MIDI.md)
(MIDI) and [API.md](API.md) (HTTP).
