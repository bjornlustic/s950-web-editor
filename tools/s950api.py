#!/usr/bin/env python3
"""Client for the S950 drop-box HTTP API (web/server.py), stdlib only.

One method per /api/v1 endpoint.  See docs/API.md for the reference.

  api = S950('http://192.168.1.20:8150', token='secret')
  api.put('kick.wav', name='KICK', load=True)   # into the drop-box + RAM
  api.state()                                   # what the sampler holds
  api.get('KICK', 'kick_from_card.wav')

WAV input: any 16-bit PCM WAV, mono or stereo, any sample rate.  The
server sums the channels and linearly resamples to `rate` (default
40000 Hz) itself (build_sounddisk.wav_points), so the client sends the
file as it is and only checks up front that it is 16-bit PCM, which is
the one thing the server cannot fix.

CLI:
  python3 tools/s950api.py --url http://host:8150 --token T put kick.wav [--name KICK] [--load] [--rate 40000]
  python3 tools/s950api.py ... ls | state | status | version
  python3 tools/s950api.py ... rm NAME [--type S|P]
  python3 tools/s950api.py ... get NAME out.wav [--trim]
  python3 tools/s950api.py ... load NAME | unload NAME [--type S|P]
  python3 tools/s950api.py ... program NAME --sample SMP [--lokey 24 --hikey 127]
  python3 tools/s950api.py ... clear | push | verify
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import wave


class APIError(Exception):
    def __init__(self, status, message):
        super().__init__(f'{status}: {message}')
        self.status = status


class S950:
    def __init__(self, url='http://127.0.0.1:8150', token=None, timeout=60):
        self.url = url.rstrip('/') + '/api/v1/'
        self.token = token or os.environ.get('S950_API_TOKEN')
        self.timeout = timeout

    def _call(self, method, name, params=None, body=None, ctype=None,
              raw=False):
        url = self.url + name
        params = {k: v for k, v in (params or {}).items() if v is not None}
        if params:
            url += '?' + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, data=body, method=method)
        if self.token:
            req.add_header('Authorization', f'Bearer {self.token}')
        if ctype:
            req.add_header('Content-Type', ctype)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                data = r.read()
        except urllib.error.HTTPError as e:
            try:
                msg = json.loads(e.read().decode())['error']
            except Exception:                                # noqa: BLE001
                msg = e.reason
            raise APIError(e.code, msg) from None
        return data if raw else json.loads(data.decode())

    # -- read ---------------------------------------------------------------
    def version(self):
        return self._call('GET', 'version')

    def status(self):
        return self._call('GET', 'status')

    def ls(self):
        """The drop-box directory: [{name, type, size, slot, block}]."""
        return self.status()['files']

    def fields(self):
        return self._call('GET', 'fields')

    def program(self, name):
        return self._call('GET', 'program', {'name': name})

    def get(self, name, out=None, trim=False):
        """The sample as stored on the card, decoded to a 16-bit mono WAV.
        Returns the bytes; writes them to `out` when given."""
        wav = self._call('GET', 'wav', {'name': name,
                                        'trim': '1' if trim else '0'},
                         raw=True)
        if out:
            with open(out, 'wb') as f:
                f.write(wav)
        return wav

    # -- write --------------------------------------------------------------
    def put(self, path, name=None, load=True, push=True, mode='L',
            rate=40000, **edit):
        """Upload a WAV into the drop-box.  load=True also announces it, so
        the sampler pulls it into RAM on its next idle poll.  edit: start,
        end, loop, loudness, pitch (see /api/v1/fields)."""
        with wave.open(path) as w:
            if w.getsampwidth() != 2 or w.getcomptype() != 'NONE':
                raise ValueError(f'{path}: need 16-bit PCM, got '
                                 f'{8 * w.getsampwidth()}-bit {w.getcomptype()}')
        name = name or os.path.splitext(os.path.basename(path))[0]
        with open(path, 'rb') as f:
            body = f.read()
        q = {'name': name, 'mode': mode, 'rate': rate,
             'load': int(bool(load)), 'push': int(bool(push))}
        q.update(edit)
        return self._call('POST', 'sample', q, body, 'audio/wav')

    def make_program(self, name, sample=None, lokey=24, hikey=127,
                     keygroups=None, load=True):
        """One keygroup around `sample`, or the full keygroups list
        (see docs/API.md) as a JSON body."""
        q = {'name': name, 'sample': sample, 'lokey': lokey, 'hikey': hikey,
             'load': int(bool(load))}
        body = ctype = None
        if keygroups:
            body = json.dumps({'name': name, 'keygroups': keygroups}).encode()
            ctype = 'application/json'
        return self._call('POST', 'program', q, body, ctype)

    def announce(self, name, ftype='S'):
        return self._call('POST', 'announce', {'name': name, 'type': ftype})

    def load(self, name, ftype='S'):
        """Announce and wait until the sampler holds it; returns state."""
        return self._call('POST', 'load', {'name': name, 'type': ftype})

    def unload(self, name, ftype='S'):
        """Remove from the sampler's RAM (the card keeps the file)."""
        return self._call('POST', 'unload', {'name': name, 'type': ftype})

    def state(self):
        """Ask the sampler what is resident: {items, used_words, free_words}."""
        return self._call('POST', 'state')

    def delete(self, name, ftype=None):
        """Remove a file from the card."""
        return self._call('POST', 'delete', {'name': name, 'type': ftype})

    def clear(self):
        return self._call('POST', 'clear')

    def push(self):
        return self._call('POST', 'push')

    def verify(self):
        return self._call('POST', 'verify')


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--url', default=os.environ.get('S950_API_URL',
                                                    'http://127.0.0.1:8150'))
    ap.add_argument('--token', default=None, help='or env S950_API_TOKEN')
    sub = ap.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('put')
    p.add_argument('wav')
    p.add_argument('--name')
    p.add_argument('--load', action='store_true',
                   help='also pull it into the sampler')
    p.add_argument('--rate', type=int, default=40000)
    p.add_argument('--mode', default='L', choices='LS')
    for c in ('ls', 'state', 'status', 'version', 'clear', 'push', 'verify'):
        sub.add_parser(c)
    for c in ('rm', 'load', 'unload'):
        p = sub.add_parser(c)
        p.add_argument('name')
        p.add_argument('--type', default=None if c == 'rm' else 'S')
    p = sub.add_parser('get')
    p.add_argument('name')
    p.add_argument('out')
    p.add_argument('--trim', action='store_true')
    p = sub.add_parser('program')
    p.add_argument('name')
    p.add_argument('--sample', required=True)
    p.add_argument('--lokey', type=int, default=24)
    p.add_argument('--hikey', type=int, default=127)
    p.add_argument('--no-load', action='store_true')
    a = ap.parse_args(argv)
    api = S950(a.url, a.token)
    try:
        if a.cmd == 'put':
            out = api.put(a.wav, a.name, load=a.load, rate=a.rate, mode=a.mode)
        elif a.cmd == 'ls':
            for f in api.ls():
                print(f"{f['name']:10} {f['type']} {f['size']:>8}")
            return 0
        elif a.cmd == 'get':
            wav = api.get(a.name, a.out, trim=a.trim)
            print(f'{a.out}: {len(wav)} bytes')
            return 0
        elif a.cmd == 'rm':
            out = api.delete(a.name, a.type)
        elif a.cmd == 'load':
            out = api.load(a.name, a.type)
        elif a.cmd == 'unload':
            out = api.unload(a.name, a.type)
        elif a.cmd == 'program':
            out = api.make_program(a.name, a.sample, a.lokey, a.hikey,
                                   load=not a.no_load)
        else:
            out = getattr(api, a.cmd)()
    except (APIError, ValueError, urllib.error.URLError) as e:
        sys.exit(f'error: {e}')
    print(json.dumps(out, indent=1))
    return 0


if __name__ == '__main__':
    sys.exit(main())
