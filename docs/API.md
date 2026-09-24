# S950 drop-box HTTP API (v1)

`web/server.py` is the one process that owns the mirror of the ZuluSCSI
image the sampler polls. It deposits samples and programs into that
mirror, pushes the changed sectors to the board over UDP and asks the
sampler what it holds. This is its HTTP API, for DAW scripts, other
editors and phones on the LAN.

Endpoints live under `/api/v1/`. The same handlers are also mounted
under `/api/` for the bundled page; use `/api/v1/` from your own code.

## Starting the server

Local only (default, no auth):

    python3 web/server.py --image build/S950_HD0.img --host 192.168.1.250

On the LAN, a token is mandatory; the server refuses to start without one:

    S950_API_TOKEN=secret python3 web/server.py --listen 0.0.0.0 --image build/S950_HD0.img
    # or --token secret

It prints the page URL with the token in it, e.g.
`http://localhost:8150/?token=secret`. Open that on another device
(replace `localhost` with the server's LAN address); the page reads the
token from its URL and sends it with every request.

Flags: `--port` (8150), `--image` (mirror file), `--host` / `--id`
(ZuluSCSI address and image id), `--listen` (bind address, 127.0.0.1),
`--token` (or env `S950_API_TOKEN`), `--no-browser`.

## Auth

When a token is configured every `/api` request needs one of

    Authorization: Bearer <token>
    ?token=<token>            (query string, for links the browser follows)

Otherwise: `401 {"error": "missing or wrong token"}`. Static files (the
editor page) never need the token. Without a configured token (loopback
only) nothing is checked.

## CORS

Every response carries `Access-Control-Allow-Origin: *`. `OPTIONS` on
any path answers `204` with `Access-Control-Allow-Methods: GET, POST,
OPTIONS` and `Access-Control-Allow-Headers: Authorization, Content-Type`,
so a browser app on another origin can call the API directly. The
preflight itself needs no token.

## Errors

Always JSON `{"error": "<message>"}`:

| status | when |
|---|---|
| 400 | bad or missing parameter, body not a WAV, no such file in the drop-box |
| 401 | token configured and missing or wrong |
| 404 | unknown endpoint (or static file) |
| 500 | anything else: board unreachable, push failed, sampler did not answer in time |

## Endpoints

All parameters are query-string. Names are 10 characters max, upper-cased
ASCII (S950 file names); longer names are cut. `type` is `S` (sample) or
`P` (program).

### GET /api/v1/version

    {"api": 1, "os": "5.0.0", "image": "HD0.img", "host": "192.168.1.250", "id": 0, "auth": true}

### GET /api/v1/status

Board reachability, the mirror's mailbox, pending (not yet pushed)
ranges, the drop-box directory and the server log since the last call.

    {"host": "192.168.1.250", "id": 0, "board": true, "ping_ms": 4,
     "image": "HD0.img", "image_name": "HD0.img", "volume": "DROPBOX",
     "mailbox": {"serial": 0, "name": "", "type": ""},
     "pending_ranges": 0, "pending_bytes": 0,
     "files": [{"slot": 0, "name": "KICK", "type": "S", "size": 5502, "block": 5}],
     "free_blocks": 795, "total_blocks": 801,
     "log": ["[15:53:37] KICK: stored on the card, 3628 points, 3 ranges, 6656 bytes"]}

`files` is the listing (`ls`). Blocks are 8 KB.

### POST /api/v1/sample

Upload a WAV into the drop-box. Body: the WAV bytes (see WAV
requirements). Params:

| param | default | meaning |
|---|---|---|
| name | SAMPLE | drop-box file name |
| mode | L | replay mode: `L` looped, `O` one-shot (`S` is accepted as a synonym for `O`), `A` alternating |
| rate | 40000 | sample rate written to the header; the audio is resampled to it (4000..48000) |
| load | 1 | `1` announce it so the sampler pulls it into RAM; `0` store on the card only |
| push | 1 | `1` push to the board now; `0` leave it pending (`/push` later) |
| start, end, loop, loudness, pitch | unset | EDIT SAMPLE fields set before the sample reaches the machine; ranges in `/fields` |

    {"serial": 0, "points": 3628, "ranges": 3, "planned_bytes": 6656,
     "pushed": true, "pushed_bytes": 6656, "loaded": false, "rate": 40000, "edit": {}}

`serial` is the mailbox serial the announcement got (0 when `load=0`).

### GET /api/v1/wav?name=&trim=

The sample as it is on the card, decoded to a 16-bit mono WAV
(`audio/wav`, headers `X-S950-Points`, `X-S950-Rate`). `trim=1` returns
only the replay range the machine plays. The second half of the points
is 8-bit on the disk itself, so that is what comes back.

### POST /api/v1/announce?name=&type=S

Re-announce a file already on the card; the sampler loads it at its next
idle poll. Returns at once.

    {"serial": 1, "ranges": 1, "pushed_bytes": 512}

### POST /api/v1/load?name=&type=S

Announce and WAIT (up to 12 s) until the sampler holds it. Returns the
state (below) plus `seconds`. 500 if it never appears (too big, or the
sampler is busy).

### POST /api/v1/state

Ask the sampler what is resident and wait for its reply (about a second).

    {"serial": 3, "banks": 48, "total_words": 3145728,
     "used_words": 3628, "free_words": 3142100,
     "items": [{"name": "KICK", "type": "S", "points": 3628}], "seconds": 1.1}

(`points` is null for programs. Shape from `s950dropbox.status()`; the
capture rig here has no sampler attached, so this example is assembled,
not recorded.)

### POST /api/v1/unload?name=&type=S

Delete an item from the sampler's RAM (the card keeps the file). Returns
the fresh state, as `/state`.

### GET /api/v1/program?name=

An existing drop-box program parsed into the editor's form:

    {"name": "KICKPRG", "bytes": 108, "keygroups": [{"sample": "KICK", "sample_loud": "2 SAMPLE",
      "hikey": 60, "lokey": 36, "attack": 0, "decay": 80, "sustain": 99, "release": 30, ... }]}

### POST /api/v1/program

Build a program and deposit it. Simple form, query only:
`?name=KICKPRG&sample=KICK&lokey=36&hikey=60&load=1`. Multi-keygroup
form: a JSON body `{"name": "...", "keygroups": [{"sample": "KICK",
"lokey": 36, "hikey": 60, "filter": 94, ...}]}` with any keys from
`/fields` (`sample_loud`, `start`, `end` also allowed). With `load=1`
the samples it needs are loaded into the sampler first, one at a time.

    {"name": "KICKPRG", "sample": "KICK", "lokey": 36, "hikey": 60, "keygroups": [],
     "bytes": 108, "serial": 0, "ranges": 3, "pushed_bytes": 1536,
     "loaded": false, "sample_resident": false}

### GET /api/v1/fields

The 24 keygroup fields and 5 sample fields the editor can set, with
their ranges:

    {"keygroup": [{"key": "hikey", "offset": 0, "min": 0, "max": 127, "label": "high key"}, ...],
     "sample":   [{"key": "start", "min": 0, "max": 16777216, "label": "replay start (points)"}, ...]}

### POST /api/v1/delete?name=&type=

Remove one file from the card (frees its blocks, wipes the directory
entry, pushes). `type` optional. 400 if there is no such file.

    {"name": "KICKPRG", "type": "P", "size": 108, "ranges": 2, "pushed_bytes": 1024}

### POST /api/v1/clear

Empty the drop-box volume (files only; the sampler is not asked to load
anything).

    {"files": 1, "ranges": 2, "pushed_bytes": 1024}

### POST /api/v1/push

Push whatever is pending (after `push=0` uploads or a failed push).

    {"ranges": 0, "bytes": 0, "seconds": 0.0, "kbs": 0}

### POST /api/v1/verify

Read the whole served image back and compare with the mirror.

    {"differing": 0, "bytes": 6561792, "seconds": 0.27}

## WAV requirements

16-bit PCM, mono or stereo, any sample rate. The server sums the channels,
linearly resamples to `rate` (40 kHz default) and normalises to a 32000
peak; the S950 stores the first half of the points as 12-bit-in-16 words
and the second half as 8-bit. 24-bit, float and compressed WAVs are
rejected (400, or by the client before sending).

Memory: one point is one word of sample memory. The example machine has
48 banks (3 MB); `/state` tells you what is free.

## Load / announce semantics

- A deposit with `load=1` writes the file and puts its name in the
  mailbox block. The sampler polls that block about once a second, on an
  idle pass of its main loop, and pulls the announced file into RAM.
- Idle means: no voice sounding, not in the DISK section. A held note
  delays the load; `/load` and `/state` wait for it (up to 12 s / 8 s),
  `/announce` does not.
- The mailbox holds one request. Two announcements inside one poll
  interval lose the first; `/load`, `/program` and `/unload` serialise
  themselves, but do not fire `/announce` in a tight loop.
- The SCSI volume must be selected on the sampler once (DISK page, the
  `DROPBOX` volume) after power-up; until then nothing is polled.
- `load=0` stores the file on the card without touching the sampler.
  That is how a library is built up; `/announce` or `/load` pulls one in
  when wanted.

## Concurrency and rate

One server owns the mirror image and the pending-patch file next to it.
Do not run two servers on the same image, and do not write the image
with the CLI tools while a server is up. Inside one server every request
takes a global lock, so calls are serialised; the ones that talk to the
sampler (`load`, `unload`, `state`, `program` with `load=1`) hold it for a
second or more. Uploads run at whatever the Wi-Fi link gives (a 525 KB
sample takes a few seconds).

## Quickstart: curl

    B=http://192.168.1.20:8150/api/v1; H="Authorization: Bearer secret"
    curl -H "$H" $B/version
    curl -H "$H" -X POST --data-binary @kick.wav "$B/sample?name=KICK&load=1"
    curl -H "$H" -X POST $B/state
    curl -H "$H" $B/status | python3 -m json.tool
    curl -H "$H" -o back.wav "$B/wav?name=KICK"
    curl -H "$H" -X POST "$B/delete?name=KICK"

## Quickstart: Python

`tools/s950api.py` is a stdlib-only client (copy the one file).

    from s950api import S950, APIError
    api = S950('http://192.168.1.20:8150', token='secret')
    api.put('kick.wav', name='KICK', load=True)     # upload + pull into RAM
    print(api.state()['free_words'])
    api.make_program('KICKPRG', 'KICK', lokey=36, hikey=60)
    api.get('KICK', 'back.wav')
    api.delete('KICK')

CLI (token also via `S950_API_TOKEN`, url via `S950_API_URL`):

    python3 tools/s950api.py --url http://192.168.1.20:8150 --token secret put kick.wav --load
    python3 tools/s950api.py ... ls
    python3 tools/s950api.py ... state
    python3 tools/s950api.py ... get KICK out.wav
    python3 tools/s950api.py ... rm KICK

The server's API tests (`tools/test_api950.py`) run against an emulated
board and live in the S950 OS repo, not here.

## Connection, sampler mode and the card's Wi-Fi

    GET  /api/v1/devices[?host=IP]   what is reachable: the board over Wi-Fi
                                     (ping, served image), mounted ZuluSCSI
                                     cards on USB, ZuluSCSI consoles on USB,
                                     and the current connection
    POST /api/v1/connect?mode=wifi&host=IP[&id=N]
    POST /api/v1/connect?mode=usb&volume=/Volumes/NAME
                                     switch the editor's device.  usb writes
                                     straight into S950/HD00_512.hda on the
                                     card (the board is off the SCSI bus)
    POST /api/v1/eject               eject the USB card; the board reboots
                                     onto the bus; the editor goes back to
                                     Wi-Fi
    GET  /api/v1/wifi                the card's WiFiSSID (usb mode), the
                                     Mac's known networks and the one it
                                     is on
    POST /api/v1/wifi?ssid=..&password=..
                                     write WiFiSSID/WiFiPassword into
                                     zuluscsi.ini on the mounted card (usb
                                     mode only; blank password keeps the
                                     card's; never logged)
    GET  /api/v1/mode[?timeout=S&port=MIDI]
                                     is the sampler polling the drop box?
                                     {polling, reason, seconds, section,
                                     midi}; a mailbox command must be
                                     answered within timeout (default 3 s)
    POST /api/v1/mode/enter[?port=MIDI]
                                     press EDIT SAMPLE over the MIDI service
                                     ops (leaves DISK/RECORD), then probe
    GET  /api/v1/panel[?port=MIDI&ch=0&voices=1]
                                     the front panel read over MIDI: LCD
                                     text, section, lamps (read only;
                                     tools/s950mirror.py)

`port` is a substring of the MIDI port name (mido); the default is
`UX16 2`, the author's interface, so pass your own.

Loads are gated: /sample with load=1, /announce, /load and /program with
load=1 first probe the sampler (4 s) and answer 400 "the sampler is not in
drop-box mode: ..." instead of writing an announcement it would never act
on.  Over usb they answer 400 too: store with load=0, then eject.
