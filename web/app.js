/* S950 sample loader: decode in the browser, encode and push on the server.
 *
 * The browser does the audio work (decode, mono-sum, resample to 40 kHz,
 * optional normalize) and hands the server a plain 16-bit mono WAV, because
 * that is the input tools/s950dropbox.deposit_wav already takes: the whole
 * server path is then the same code the emulation suite proves
 * (tools/test_wifi950.py).
 */
'use strict';

const RATE = 40000;                 // the S950's stock rate, and the
                                    // default; the machine stores a rate
                                    // per sample and tunes to it, so a
                                    // lower one costs proportionally less
                                    // memory.  chosenRate() is what the
                                    // encoder and the header both use.
function chosenRate() {
  const v = parseInt(el('selRate') ? el('selRate').value : RATE, 10);
  return Number.isFinite(v) ? v : RATE;
}
const NAME_LEN = 10;
const el = (id) => document.getElementById(id);

const S = {
  queue: [],                        // {id, name, file, pcm, mode, state, err}
  next: 1,
  status: null,
  busy: false,
};

/* ---- logging ---------------------------------------------------------- */

function log(line, cls) {
  const pre = el('log');
  const span = document.createElement('span');
  if (cls) span.className = cls;
  span.textContent = line + '\n';
  pre.appendChild(span);
  el('logBody').scrollTop = el('logBody').scrollHeight;
}

function err(msg) {
  const b = el('errBanner');
  b.textContent = msg;
  b.classList.toggle('visible', !!msg);
  if (msg) log(msg, 'err');
}

function state(msg, cls) {
  const s = el('syncMsg');
  s.textContent = msg || '';
  s.className = 'mono' + (cls ? ' state-' + cls : '');
}

/* ---- server ----------------------------------------------------------- */

// Served over the LAN the server wants a token: the page reads it from its
// own URL (?token=) once and sends it with every request.
const TOKEN = new URLSearchParams(location.search).get('token') || '';
function tokenised(url) {
  return TOKEN ? url + '&token=' + encodeURIComponent(TOKEN) : url;
}

async function api(path, opts) {
  opts = opts || {};
  if (TOKEN) opts.headers = { Authorization: 'Bearer ' + TOKEN, ...(opts.headers || {}) };
  const r = await fetch(path, opts);
  const text = await r.text();
  let body;
  try { body = JSON.parse(text); } catch { body = { error: text }; }
  if (!r.ok) throw new Error(body.error || r.statusText);
  return body;
}

async function refresh() {
  try {
    const st = await api('/api/status');
    S.status = st;
    el('boardDot').className = 'dot' + (st.board ? ' on' : ' err');
    el('boardName').textContent = st.board
      ? `${st.host} id ${st.id}` : `${st.host} unreachable`;
    el('pingMs').textContent = st.ping_ms == null
      ? '—' : `${st.ping_ms} ms`;
    el('imageName').textContent = st.image_name || '—';
    el('mailbox').textContent = st.mailbox.serial
      ? `#${st.mailbox.serial} ${st.mailbox.name} (${st.mailbox.type})`
      : 'empty';
    el('pendingInfo').textContent = st.pending_bytes
      ? `${st.pending_ranges} ranges / ${st.pending_bytes} B`
      : '0';
    el('btnPush').disabled = !st.pending_bytes || S.busy;
    el('btnVerify').disabled = !st.board || S.busy;
    el('diskHint').textContent =
      `volume ${st.volume} · ${st.files.length} files · `
      + `${st.free_blocks} free blocks of ${st.total_blocks}`;
    S.files = st.files;              // clearBox needs the count
    const sel = el('progSample');
    if (sel) {
      const keep = sel.value;
      sel.textContent = '';
      for (const f of st.files.filter(x => x.type === 'S')) {
        const o = document.createElement('option');
        o.value = o.textContent = f.name;
        sel.appendChild(o);
      }
      if (keep) sel.value = keep;
    }
    renderDisk(st.files);
    if (st.log) for (const line of st.log) log(line, 'host');
  } catch (e) {
    el('boardDot').className = 'dot err';
    err('server: ' + e.message);
  }
}

function renderDisk(files) {
  const tb = el('diskTbl').querySelector('tbody');
  tb.textContent = '';
  for (const f of files) {
    const tr = document.createElement('tr');
    for (const [v, cls] of [[f.slot, 'num'], [f.name, ''], [f.type, ''],
                            [f.size, 'num'], [f.block, 'num']]) {
      const td = document.createElement('td');
      td.className = cls;
      td.textContent = v;
      tr.appendChild(td);
    }
    const tdSel = document.createElement('td');
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.className = 'pick';
    cb.dataset.name = f.name;
    cb.dataset.type = f.type;
    cb.checked = (S.picked || []).includes(f.name);
    cb.onchange = () => {
      S.picked = [...document.querySelectorAll('#diskTbl .pick')]
        .filter(x => x.checked).map(x => x.dataset.name);
      el('btnLoadSel').disabled = !S.picked.length || S.busy;
    };
    tdSel.appendChild(cb);
    tr.insertBefore(tdSel, tr.firstChild);

    const td = document.createElement('td');
    if (f.type === 'P') {
      const ed = document.createElement('button');
      ed.className = 'sm';
      ed.textContent = 'edit';
      ed.title = 'Read this program off the card into the keygroup editor';
      ed.onclick = () => editProgram(f.name);
      td.appendChild(ed);
    }
    if (f.type === 'S') {
      const play = document.createElement('button');
      play.className = 'sm';
      play.textContent = 'play';
      play.title = 'Decode this sample off the card and play it here - '
        + 'what the machine actually stored, not what was sent';
      play.onclick = () => audition(f.name, play);
      td.appendChild(play);
      const dl = document.createElement('button');
      dl.className = 'sm';
      dl.textContent = 'wav';
      dl.title = 'Download this sample as a WAV';
      dl.onclick = () => {
        const a = document.createElement('a');
        a.href = tokenised('/api/wav?' + new URLSearchParams({ name: f.name }));
        a.download = f.name + '.wav';
        document.body.appendChild(a); a.click(); a.remove();
        log(`exported ${f.name}.wav`);
      };
      td.appendChild(dl);
    }
    const b = document.createElement('button');
    b.className = 'sm';
    b.textContent = 'load into RAM';
    b.title = 'Announce this file in the mailbox: the sampler loads it at '
      + 'its next idle poll';
    b.onclick = () => announce(f.name, f.type);
    td.appendChild(b);
    const d = document.createElement('button');
    d.className = 'sm danger';
    d.textContent = 'delete';
    d.title = 'Free this file\u2019s blocks and wipe its directory entry on '
      + 'the card. Cannot be undone.';
    d.onclick = () => remove(f.name, f.type, f.size);
    td.appendChild(d);
    tr.appendChild(td);
    tb.appendChild(tr);
  }
}

/* ---- audio ------------------------------------------------------------ */

function cleanName(s) {
  return s.toUpperCase().replace(/[^A-Z0-9 ]/g, '').slice(0, NAME_LEN)
    || 'SAMPLE';
}

async function decode(file) {
  const buf = await file.arrayBuffer();
  const ctx = new (window.OfflineAudioContext || window.webkitOfflineAudioContext)(1, 1, 44100);
  const audio = await ctx.decodeAudioData(buf);
  // mono-sum
  const n = audio.length;
  const mono = new Float32Array(n);
  for (let c = 0; c < audio.numberOfChannels; c++) {
    const ch = audio.getChannelData(c);
    for (let i = 0; i < n; i++) mono[i] += ch[i];
  }
  for (let i = 0; i < n; i++) mono[i] /= audio.numberOfChannels;
  // linear resample to the chosen rate, matching build_sounddisk.wav_points
  const rate = chosenRate();
  const total = Math.floor(n * rate / audio.sampleRate);
  const out = new Float32Array(total);
  for (let i = 0; i < total; i++) {
    const x = i * audio.sampleRate / rate;
    const j = Math.floor(x);
    const f = x - j;
    const a = mono[j];
    const b = mono[Math.min(j + 1, n - 1)];
    out[i] = a + (b - a) * f;
  }
  return out;
}

function toPcm(float, normalize) {
  let peak = 0;
  for (const v of float) peak = Math.max(peak, Math.abs(v));
  const g = normalize ? (peak ? 32000 / peak : 1) : 32000;
  const len = float.length & ~1;              // the loader splits at len/2
  const pcm = new Int16Array(len);
  for (let i = 0; i < len; i++) {
    pcm[i] = Math.max(-32000, Math.min(32000, Math.round(float[i] * g)));
  }
  return pcm;
}

function wavBytes(pcm, rate) {
  const bytes = new Uint8Array(44 + pcm.length * 2);
  const dv = new DataView(bytes.buffer);
  const ascii = (o, s) => { for (let i = 0; i < s.length; i++) bytes[o + i] = s.charCodeAt(i); };
  ascii(0, 'RIFF'); dv.setUint32(4, 36 + pcm.length * 2, true);
  ascii(8, 'WAVEfmt '); dv.setUint32(16, 16, true);
  dv.setUint16(20, 1, true); dv.setUint16(22, 1, true);
  dv.setUint32(24, rate, true); dv.setUint32(28, rate * 2, true);
  dv.setUint16(32, 2, true); dv.setUint16(34, 16, true);
  ascii(36, 'data'); dv.setUint32(40, pcm.length * 2, true);
  new Int16Array(bytes.buffer, 44).set(pcm);
  return bytes;
}

/* ---- queue ------------------------------------------------------------ */

function renderQueue() {
  const tb = el('queueTbl').querySelector('tbody');
  tb.textContent = '';
  for (const item of S.queue) {
    const tr = document.createElement('tr');
    tr.className = item.state === 'done' ? 'is-done'
      : item.state === 'new' ? 'is-new' : '';

    const tdName = document.createElement('td');
    const inp = document.createElement('input');
    inp.type = 'text';
    inp.value = item.name;
    inp.maxLength = NAME_LEN;
    inp.oninput = () => { item.name = cleanName(inp.value); };
    tdName.appendChild(inp);
    if (item.err) {
      const e = document.createElement('div');
      e.className = 'err';
      e.textContent = item.err;
      tdName.appendChild(e);
    }
    tr.appendChild(tdName);

    const secs = (item.pcm.length / (item.rate || RATE)).toFixed(2);
    for (const [v, cls] of [[item.file.name, ''],
                            [item.pcm.length, 'num'],
                            [secs, 'num'],
                            [item.mode === 'S' ? 'one-shot' : 'looped', '']]) {
      const td = document.createElement('td');
      td.className = cls;
      td.textContent = v;
      tr.appendChild(td);
    }

    const td = document.createElement('td');
    td.className = 'acts';
    const prev = document.createElement('button');
    prev.className = 'sm';
    prev.textContent = 'play';
    prev.title = 'Hear it as encoded, before it costs a push';
    prev.onclick = () => auditionQueued(item, prev);
    td.appendChild(prev);
    const send = document.createElement('button');
    send.className = 'sm';
    send.textContent = item.state === 'done' ? 'sent' : 'send';
    send.disabled = S.busy || item.state === 'done';
    send.onclick = () => sendOne(item);
    const del = document.createElement('button');
    del.className = 'sm';
    del.textContent = 'remove';
    del.disabled = S.busy;
    del.onclick = () => {
      S.queue = S.queue.filter((q) => q !== item);
      renderQueue();
    };
    td.appendChild(send);
    td.appendChild(del);
    tr.appendChild(td);
    tb.appendChild(tr);
  }
  const pending = S.queue.some((q) => q.state !== 'done');
  el('btnSendAll').disabled = S.busy || !pending;
  el('btnClear').disabled = S.busy || !S.queue.length;
}

async function addFiles(files) {
  err('');
  for (const file of files) {
    try {
      const float = await decode(file);
      const pcm = toPcm(float, el('chkNorm').checked);
      if (!pcm.length) throw new Error('empty after decoding');
      S.queue.push({
        id: S.next++,
        name: cleanName(file.name.replace(/\.[^.]+$/, '')),
        file, pcm,
        mode: el('selMode').value,
        rate: chosenRate(),
        state: 'new',
        err: '',
      });
      log(`queued ${file.name}: ${pcm.length} points, `
        + `${(pcm.length / chosenRate()).toFixed(2)} s @ ${chosenRate()} Hz`);
    } catch (e) {
      err(`${file.name}: ${e.message}`);
    }
  }
  renderQueue();
}

/* ---- sending ---------------------------------------------------------- */

async function sendOne(item) {
  if (S.busy) return;
  S.busy = true;
  renderQueue();
  item.err = '';
  state(`encoding ${item.name}…`, 'busy');
  try {
    const wav = wavBytes(item.pcm, item.rate || RATE);
    // 'Load into S950 RAM' means announce it, NOT whether to push.  It was
    // wired to push=, so unchecking it held the file on this machine and
    // never wrote the card at all - which is not what the label says.  The
    // card write always happens; load= decides whether the sampler pulls it
    // into memory or leaves it on the card as library.
    const q = new URLSearchParams({
      name: item.name,
      mode: item.mode,
      push: '1',
      load: el('chkLoad').checked ? '1' : '0',
      rate: String(item.rate || chosenRate()),
    });
    for (const [k, id] of [['start', 'smpStart'], ['end', 'smpEnd'],
                           ['loudness', 'smpLoud']]) {
      const v = (el(id).value || '').trim();
      if (v !== '') q.set(k, v);
    }
    const t = performance.now();
    const r = await api('/api/sample?' + q.toString(), {
      method: 'POST',
      headers: { 'Content-Type': 'application/octet-stream' },
      body: wav,
    });
    const dt = (performance.now() - t) / 1000;
    item.state = 'done';
    const rate = r.pushed_bytes
      ? ` (${Math.round(r.pushed_bytes / 1024 / Math.max(dt, 1e-6))} KB/s)`
      : '';
    log(`${item.name}: ${r.points} points, ${r.ranges} ranges, `
      + `${r.planned_bytes} B planned, ${r.pushed_bytes} B pushed in `
      + `${dt.toFixed(1)} s${rate}`
      + (r.loaded === false
        ? ' \u2014 stored on the card, not loaded'
        : ' \u2014 announced, the S950 loads it at its next poll'));
    state(`${item.name} sent`, '');
  } catch (e) {
    item.err = e.message;
    err(`${item.name}: ${e.message}`);
    state('failed', 'error');
  } finally {
    S.busy = false;
    renderQueue();
    refresh();
  }
}

async function sendAll() {
  for (const item of S.queue.slice()) {
    if (item.state !== 'done') await sendOne(item);
  }
}

function renderRam(st) {
  const tb = el('ramTbl').querySelector('tbody');
  tb.textContent = '';
  const fmt = (w) => w.toLocaleString() + ' w';
  el('ramUsed').textContent = fmt(st.used_words);
  el('ramFree').textContent = fmt(st.free_words);
  const pct = st.total_words ? st.used_words / st.total_words : 0;
  const bar = el('ramBar');
  bar.style.width = Math.min(100, Math.round(pct * 100)) + '%';
  bar.className = 'meter-fill' + (pct > 0.9 ? ' full' : pct > 0.75 ? ' warn' : '');
  el('ramHint').textContent =
    `${st.items.length} resident \u00b7 ${Math.round((1 - pct) * 100)}% free `
    + `of ${fmt(st.total_words)} (${st.banks} banks) \u00b7 read in ${st.seconds}s`;
  for (const i of st.items) {
    const tr = document.createElement('tr');
    const tdSel = document.createElement('td');
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.className = 'pick';
    cb.dataset.name = i.name;
    cb.dataset.type = i.type;
    cb.onchange = () => {
      el('btnUnloadSel').disabled =
        !picked('#ramTbl .pick').length || S.busy;
    };
    tdSel.appendChild(cb);
    tr.appendChild(tdSel);
    const secs = i.points == null ? '' : (i.points / 40000).toFixed(2);
    for (const [v, cls] of [[i.name, ''], [i.type, ''],
                            [i.points == null ? '\u2014' : i.points.toLocaleString(), 'num'],
                            [secs, 'num']]) {
      const td = document.createElement('td');
      td.className = cls;
      td.textContent = v;
      tr.appendChild(td);
    }
    const td = document.createElement('td');
    const b = document.createElement('button');
    b.className = 'sm danger';
    b.textContent = 'unload';
    b.title = 'Delete this from the sampler\u2019s memory, freeing its '
      + 'space. The copy on the card is untouched.';
    b.onclick = () => unload(i.name, i.type, i.points);
    td.appendChild(b);
    tr.appendChild(td);
    tb.appendChild(tr);
  }
}

// The browser's confirm() is dismissed by reflex and cannot phrase things
// in the app's own words; this returns the same boolean.
function ask(title, body, yes = 'Confirm') {
  return new Promise((resolve) => {
    el('modalTitle').textContent = title;
    el('modalBody').textContent = body;
    el('modalYes').textContent = yes;
    el('modal').hidden = false;
    const done = (v) => {
      el('modal').hidden = true;
      el('modalYes').onclick = el('modalNo').onclick = null;
      resolve(v);
    };
    el('modalYes').onclick = () => done(true);
    el('modalNo').onclick = () => done(false);
  });
}

function progress(label, done, total) {
  const p = el('prog');
  if (total == null) { p.hidden = true; return; }
  p.hidden = false;
  el('progLabel').textContent = `${label} ${done}/${total}`;
  el('progFill').style.width = `${Math.round(100 * done / total)}%`;
}

function picked(sel) {
  return [...document.querySelectorAll(sel)].filter(x => x.checked)
    .map(x => ({ name: x.dataset.name, type: x.dataset.type }));
}

// The machine's own pages group these; mirroring that grouping is what
// makes two dozen numbers legible.  Every key here is round-tripped
// through a real load by tools/test_progedit.py.
const KG_GROUPS = [
  ['Key range', ['lokey', 'hikey']],
  ['Samples', ['sample', 'sample_loud']],
  ['Amplitude envelope', ['attack', 'decay', 'sustain', 'release']],
  ['Velocity', ['velsens', 'velloud']],
  ['Filter', ['filter', 'filterhi', 'keytrack']],
  ['Filter envelope', ['vcfattack', 'vcfdecay', 'vcfsustain', 'vcfrelease',
                       'vcfamount']],
  ['LFO', ['lforate', 'lfodepth', 'lfodelay']],
  ['Loudness', ['loudness', 'loudnesshi']],
  ['MIDI', ['midich']],
  ['SuperOS', ['start', 'end', 'porta', 'aacap']],
];
const SUPEROS_EXTRA = {
  // the *04 Start/End pairs: 16384ths of the sample, 0 = off (the
  // sample's own point); End past 16384 clamps to the sample length
  start: { min: 0, max: 16383, label: 'Start (16384ths, 0 = off)' },
  end: { min: 0, max: 16999, label: 'End (16384ths, 0 = off)' },
};

function fieldMeta(key) {
  const f = (S.fields && S.fields.keygroup || []).find(x => x.key === key);
  if (f) return f;
  if (SUPEROS_EXTRA[key]) return { key, ...SUPEROS_EXTRA[key] };
  return { key, label: key, min: 0, max: 99 };
}

function kgInput(kg, key, idx) {
  const wrap = document.createElement('div');
  wrap.className = 'kg-field';
  const m = fieldMeta(key);
  const lab = document.createElement('label');
  lab.textContent = m.label || key;
  lab.title = m.offset != null ? `keygroup byte +0x${m.offset.toString(16)}`
    : 'SuperOS per-keygroup value';
  wrap.appendChild(lab);
  let inp;
  if (key === 'sample' || key === 'sample_loud') {
    inp = document.createElement('select');
    const blank = document.createElement('option');
    blank.value = ''; blank.textContent = key === 'sample' ? '(pick)' : '(same)';
    inp.appendChild(blank);
    for (const f of (S.files || []).filter(x => x.type === 'S')) {
      const o = document.createElement('option');
      o.value = o.textContent = f.name;
      inp.appendChild(o);
    }
    inp.value = kg[key] || '';
  } else {
    inp = document.createElement('input');
    inp.type = 'number';
    if (m.min != null) inp.min = m.min;
    if (m.max != null) inp.max = m.max;
    inp.placeholder = 'template';
    inp.value = kg[key] == null ? '' : kg[key];
  }
  inp.onchange = () => {
    const v = inp.value === '' ? null
      : (inp.type === 'number' ? parseInt(inp.value, 10) : inp.value);
    if (v === null) delete kg[key]; else kg[key] = v;
    if (key === 'sample' || key === 'lokey' || key === 'hikey') renderKgs();
  };
  wrap.appendChild(inp);
  return wrap;
}

function renderKgs() {
  const host = el('kgList');
  host.textContent = '';
  S.kgs = S.kgs || [];
  S.kgs.forEach((kg, idx) => {
    const card = document.createElement('div');
    card.className = 'kg' + (S.openKg === idx ? ' open' : '');
    const head = document.createElement('div');
    head.className = 'kg-head';
    const n = document.createElement('span');
    n.className = 'kg-n';
    n.textContent = `KG ${idx + 1}`;
    const sum = document.createElement('span');
    sum.className = 'mono';
    sum.textContent = `${kg.sample || '(no sample)'}  `
      + `keys ${kg.lokey ?? 0}-${kg.hikey ?? 127}`;
    const spacer = document.createElement('span');
    spacer.className = 'spacer';
    const toggle = document.createElement('button');
    toggle.className = 'sm';
    toggle.textContent = S.openKg === idx ? 'hide' : 'edit';
    toggle.onclick = () => { S.openKg = S.openKg === idx ? -1 : idx; renderKgs(); };
    const del = document.createElement('button');
    del.className = 'sm danger';
    del.textContent = 'remove';
    del.onclick = () => { S.kgs.splice(idx, 1); S.openKg = -1; renderKgs(); };
    head.append(n, sum, spacer, toggle, del);
    card.appendChild(head);

    const body = document.createElement('div');
    body.className = 'kg-body';
    for (const [title, keys] of KG_GROUPS) {
      const g = document.createElement('div');
      g.className = 'kg-group';
      const h = document.createElement('h4');
      h.textContent = title;
      g.appendChild(h);
      const row = document.createElement('div');
      row.className = 'kg-fields';
      for (const k of keys) row.appendChild(kgInput(kg, k, idx));
      g.appendChild(row);
      body.appendChild(g);
    }
    const blank = document.createElement('div');
    blank.className = 'hintline';
    blank.textContent = 'Blank means "leave the template value alone".';
    body.appendChild(blank);
    card.appendChild(body);
    host.appendChild(card);
  });
  el('btnProg').disabled = S.busy;
}

function addKg() {
  const sample = el('progSample').value;
  if (!sample) { err('pick a sample first'); return; }
  const lokey = parseInt(el('progLo').value, 10);
  const hikey = parseInt(el('progHi').value, 10);
  if (!(lokey >= 0 && hikey <= 127 && lokey <= hikey)) {
    err(`bad key range ${lokey}..${hikey}`); return;
  }
  S.kgs = S.kgs || [];
  S.kgs.push({ sample, lokey, hikey });
  S.openKg = S.kgs.length - 1;
  renderKgs();
  log(`keygroup ${S.kgs.length}: ${sample} over ${lokey}-${hikey}`);
}

function bulkApply() {
  const key = el('bulkField').value;
  const raw = el('bulkValue').value;
  if (!key || !(S.kgs || []).length) return;
  const v = raw === '' ? null : parseInt(raw, 10);
  for (const kg of S.kgs) { if (v === null) delete kg[key]; else kg[key] = v; }
  renderKgs();
  log(`bulk: ${key} = ${v === null ? 'template' : v} on `
    + `${S.kgs.length} keygroups`);
}

function fillBulkFields() {
  const sel = el('bulkField');
  sel.textContent = '';
  for (const [title, keys] of KG_GROUPS) {
    for (const k of keys) {
      if (k === 'sample' || k === 'sample_loud') continue;
      const o = document.createElement('option');
      o.value = k;
      o.textContent = `${title}: ${fieldMeta(k).label || k}`;
      sel.appendChild(o);
    }
  }
}

// Audition straight off the card: the point is to hear what is STORED,
// including the 8-bit second half the format keeps, rather than the audio
// that was sent.
async function audition(name, btn) {
  try {
    if (S.audio && S.audio.src) { S.audio.pause(); }
    const was = btn ? btn.textContent : '';
    if (btn) btn.textContent = '\u25b6';
    const url = tokenised('/api/wav?' + new URLSearchParams({ name }));
    const a = S.audio || (S.audio = new Audio());
    a.src = url;
    a.onended = a.onerror = () => { if (btn) btn.textContent = was; };
    await a.play();
    log(`auditioning ${name} from the card`);
  } catch (e) {
    err(`could not play ${name}: ${e.message}`);
    if (btn) btn.textContent = 'play';
  }
}

// Audition a queued import BEFORE it is sent, from the PCM the browser
// already decoded - so a bad trim or rate is heard before it costs a push.
function auditionQueued(item, btn) {
  try {
    const rate = item.rate || RATE;
    const ctx = S.ctx || (S.ctx = new (window.AudioContext
      || window.webkitAudioContext)());
    const buf = ctx.createBuffer(1, item.pcm.length, rate);
    const ch = buf.getChannelData(0);
    for (let i = 0; i < item.pcm.length; i++) ch[i] = item.pcm[i] / 32768;
    const src = ctx.createBufferSource();
    src.buffer = buf;
    src.connect(ctx.destination);
    src.start();
    log(`auditioning ${item.name} (queued, ${rate} Hz)`);
  } catch (e) {
    err(`could not play ${item.name}: ${e.message}`);
  }
}

async function editProgram(name) {
  if (S.busy) return;
  S.busy = true;
  state(`reading ${name}\u2026`, 'busy');
  try {
    const p = await api('/api/program?' + new URLSearchParams({ name }));
    el('progName').value = p.name;
    // parse_program fills every field, so what is shown is what is
    // stored - not a template with the differences guessed at
    S.kgs = p.keygroups.map(k => {
      const kg = {};
      for (const [key, v] of Object.entries(k)) {
        if (v !== null && v !== '') kg[key] = v;
      }
      return kg;
    });
    S.openKg = 0;
    renderKgs();
    log(`loaded program ${p.name}: ${p.keygroups.length} keygroups, `
      + `${p.bytes} B - edit and create to write it back`);
    state(`${p.name} loaded for editing`, '');
  } catch (e) {
    err(e.message);
    state('failed', 'error');
  } finally {
    S.busy = false;
  }
}

async function makeProgram() {
  if (S.busy) return;
  const name = (el('progName').value || '').toUpperCase().trim();
  const sample = el('progSample').value;
  if (!name) { err('the program needs a name'); return; }
  if (!sample) { err('pick a sample for the program'); return; }
  const lokey = parseInt(el('progLo').value, 10);
  const hikey = parseInt(el('progHi').value, 10);
  if (!(lokey >= 0 && hikey <= 127 && lokey <= hikey)) {
    err(`bad key range ${lokey}..${hikey}`); return;
  }
  S.busy = true;
  state(`building ${name}\u2026`, 'busy');
  try {
    const q = new URLSearchParams({
      name, sample, lokey: String(lokey), hikey: String(hikey),
      load: el('progLoad').checked ? '1' : '0',
    });
    // two dozen fields per keygroup do not fit a query string
    const opts = { method: 'POST' };
    if ((S.kgs || []).length) {
      opts.headers = { 'Content-Type': 'application/json' };
      opts.body = JSON.stringify({ name, keygroups: S.kgs });
    }
    const r = await api('/api/program?' + q, opts);
    const what = r.keygroups && r.keygroups.length
      ? `${r.keygroups.length} keygroups (`
        + r.keygroups.map(k => `${k.sample} ${k.lokey ?? 0}-${k.hikey ?? 127}`)
          .join(', ') + ')'
      : `${r.sample} over keys ${r.lokey}-${r.hikey}`;
    log(`program ${r.name}: ${what}, ${r.bytes} B`
      + (r.loaded
        ? (r.sample_resident ? ' \u2014 sample in RAM, program loading'
                             : ' \u2014 loading')
        : ' \u2014 on the card'));
    state(`${r.name} created`, '');
    S.kgs = [];
    S.openKg = -1;
    renderKgs();
    if (el('progLoad').checked) { S.busy = false; await readState(); }
  } catch (e) {
    err(e.message);
    state('failed', 'error');
  } finally {
    S.busy = false;
    refresh();
  }
}

async function unload(name, type, points) {
  if (S.busy) return;
  const sz = points == null ? '' : ` (${points.toLocaleString()} points)`;
  if (!await ask('Unload from the sampler',
      `Remove ${name}${sz} from the sampler\u2019s memory?\n\nThis frees `
      + 'its space. The copy on the card is not touched, so you can load '
      + 'it again later.', 'Unload')) return;
  S.busy = true;
  state(`unloading ${name}\u2026`, 'busy');
  try {
    const st = await api('/api/unload?' + new URLSearchParams({ name, type }),
                         { method: 'POST' });
    S.ram = st;
    renderRam(st);
    log(`unloaded ${name}: ${st.used_words} words used, `
      + `${st.free_words} free (${st.seconds}s)`);
    state(`${name} unloaded`, '');
    refresh();                      // keep the drop-box panel in step
  } catch (e) {
    err(e.message);
    state('failed', 'error');
  } finally {
    S.busy = false;
  }
}

async function loadSelected() {
  if (S.busy) return;
  const picks = picked('#diskTbl .pick');
  if (!picks.length) return;
  S.busy = true;
  let done = 0;
  try {
    // One at a time and in order: the mailbox holds a single request, so
    // announcing two files inside one poll interval loses the first.
    // /api/load waits for each to actually arrive, which is both the
    // confirmation and the pacing - and it means the LAST item is in
    // memory before the panel is drawn, which announcing did not.
    for (const p of picks) {
      progress('loading', done, picks.length);
      state(`loading ${p.name} (${done + 1}/${picks.length})\u2026`, 'busy');
      const st = await api('/api/load?'
        + new URLSearchParams({ name: p.name, type: p.type }),
        { method: 'POST' });
      done++;
      S.ram = st;
      renderRam(st);
      progress('loading', done, picks.length);
      log(`${p.name}: in memory, ${st.free_words} words free`);
    }
    state(`${done} of ${picks.length} loaded`, '');
  } catch (e) {
    err(`after ${done} of ${picks.length}: ${e.message}`);
    state('failed', 'error');
  } finally {
    progress('', 0, null);
    S.busy = false;
    refresh();
  }
}

async function unloadSelected() {
  if (S.busy) return;
  const picks = picked('#ramTbl .pick');
  if (!picks.length) return;
  const names = picks.map(p => p.name).join(', ');
  if (!await ask('Unload from the sampler',
      `Remove ${picks.length} item${picks.length > 1 ? 's' : ''} from the `
      + `sampler\u2019s memory: ${names}.\n\nThis frees their space. The `
      + 'copies on the card are not touched.', 'Unload')) return;
  S.busy = true;
  let done = 0;
  try {
    for (const p of picks) {
      progress('unloading', done, picks.length);
      state(`unloading ${p.name} (${done + 1}/${picks.length})\u2026`, 'busy');
      const st = await api('/api/unload?'
        + new URLSearchParams({ name: p.name, type: p.type }),
        { method: 'POST' });
      done++;
      S.ram = st;
      renderRam(st);
      progress('unloading', done, picks.length);
      log(`${p.name}: unloaded, ${st.free_words} words free`);
    }
    state(`${done} unloaded`, '');
  } catch (e) {
    err(`after ${done} of ${picks.length}: ${e.message}`);
    state('failed', 'error');
  } finally {
    progress('', 0, null);
    S.busy = false;
    refresh();
  }
}

async function clearBox() {
  if (S.busy) return;
  const n = (S.files || []).length;
  if (!n) { log('drop-box is already empty'); return; }
  if (!await ask('Empty the drop-box',
      `Delete all ${n} files from the drop-box volume on the card?\n\n`
      + 'This frees their blocks and wipes the directory. It does NOT '
      + 'touch what is already loaded in the sampler\u2019s memory.',
      'Delete all')) return;
  S.busy = true;
  state('emptying the drop-box\u2026', 'busy');
  try {
    const r = await api('/api/clear', { method: 'POST' });
    log(`emptied the drop-box: ${r.files} files, ${r.pushed_bytes} B pushed`);
    state(`${r.files} files deleted`, '');
  } catch (e) {
    err(e.message);
    state('failed', 'error');
  } finally {
    S.busy = false;
    refresh();
  }
}

async function readState() {
  if (S.busy) return;
  S.busy = true;
  state('asking the S950\u2026', 'busy');
  try {
    // The sampler only answers on an idle poll, so this is not instant:
    // a sounding voice or the DISK/RECORD section holds it off.
    const st = await api('/api/state', { method: 'POST' });
    S.ram = st;
    renderRam(st);
    log(`S950 memory: ${st.items.length} resident, ${st.used_words} words `
      + `used, ${st.free_words} free (${st.seconds}s)`);
    state('memory read', '');
    return st;
  } catch (e) {
    err(e.message);
    state('failed', 'error');
  } finally {
    S.busy = false;
  }
}

async function remove(name, type, size) {
  if (S.busy) return;
  if (!await ask('Delete from the card',
      `Delete ${name} (${size} bytes) from the drop-box volume?\n\n`
      + 'Its blocks are freed and its directory entry wiped on the card. '
      + 'This cannot be undone.', 'Delete')) return;
  S.busy = true;
  state(`deleting ${name}\u2026`, 'busy');
  try {
    const r = await api('/api/delete?' + new URLSearchParams({ name, type }),
                        { method: 'POST' });
    log(`deleted ${r.name} type ${r.type} (${r.size} B), `
      + `${r.ranges} ranges, ${r.pushed_bytes} B pushed`);
    state(`${name} deleted`, '');
  } catch (e) {
    err(e.message);
    state('failed', 'error');
  } finally {
    S.busy = false;
    refresh();
  }
}

async function announce(name, type) {
  if (S.busy) return;
  S.busy = true;
  state(`announcing ${name}…`, 'busy');
  try {
    // /api/load waits until the sampler actually holds it, so the state
    // it returns is true when it returns.  Announcing and then reading
    // reported the state from BEFORE the load and showed stale numbers.
    const st = await api('/api/load?' + new URLSearchParams({ name, type }),
                         { method: 'POST' });
    S.ram = st;
    renderRam(st);
    log(`loaded ${name}: ${st.used_words} words used, `
      + `${st.free_words} free (${st.seconds}s)`);
    state(`${name} loaded`, '');
  } catch (e) {
    err(e.message);
    state('failed', 'error');
  } finally {
    S.busy = false;
    refresh();
  }
}

async function pushPending() {
  S.busy = true;
  state('pushing…', 'busy');
  try {
    const r = await api('/api/push', { method: 'POST' });
    log(`pushed ${r.ranges} ranges, ${r.bytes} B in ${r.seconds.toFixed(1)} s`
      + ` (${r.kbs} KB/s)`);
    state('pushed', '');
  } catch (e) {
    err(e.message);
    state('failed', 'error');
  } finally {
    S.busy = false;
    refresh();
  }
}

async function verifyCard() {
  S.busy = true;
  state('verifying the whole image…', 'busy');
  try {
    const r = await api('/api/verify', { method: 'POST' });
    log(r.differing
      ? `VERIFY FAILED: ${r.differing} bytes differ from the mirror`
      : `verified: the served image matches the mirror `
        + `(${r.bytes} B in ${r.seconds.toFixed(1)} s)`,
      r.differing ? 'err' : 'host');
    state(r.differing ? 'mismatch' : 'card verified',
          r.differing ? 'error' : '');
  } catch (e) {
    err(e.message);
    state('failed', 'error');
  } finally {
    S.busy = false;
    refresh();
  }
}

/* ---- wiring ----------------------------------------------------------- */

function init() {
  const drop = el('drop');
  for (const ev of ['dragenter', 'dragover']) {
    drop.addEventListener(ev, (e) => {
      e.preventDefault();
      drop.classList.add('over');
    });
  }
  for (const ev of ['dragleave', 'drop']) {
    drop.addEventListener(ev, (e) => {
      e.preventDefault();
      drop.classList.remove('over');
    });
  }
  drop.addEventListener('drop', (e) => addFiles(e.dataTransfer.files));
  el('btnPick').onclick = () => el('filePick').click();
  el('filePick').onchange = (e) => {
    addFiles(e.target.files);
    e.target.value = '';
  };
  el('btnSendAll').onclick = sendAll;
  el('btnClear').onclick = () => { S.queue = []; renderQueue(); };
  el('btnPush').onclick = pushPending;
  el('btnVerify').onclick = verifyCard;
  el('btnRefresh').onclick = refresh;
  el('btnState').onclick = readState;
  el('btnClearBox').onclick = clearBox;
  el('btnProg').onclick = makeProgram;
  el('btnLoadSel').onclick = loadSelected;
  el('btnAddKg').onclick = addKg;
  el('btnUnloadSel').onclick = unloadSelected;
  el('btnBulk').onclick = bulkApply;
  el('btnKgReset').onclick = () => {
    for (const kg of S.kgs || []) {
      for (const k of Object.keys(kg)) {
        if (!['sample', 'sample_loud', 'lokey', 'hikey'].includes(k)) delete kg[k];
      }
    }
    renderKgs();
    log('keygroups reset to the template values');
  };
  api('/api/fields').then((f) => {
    S.fields = f;
    fillBulkFields();
    renderKgs();
  }).catch(() => {});
  el('btnLogToggle').onclick = () => {
    const b = el('logBody');
    b.classList.toggle('hidden');
    el('btnLogToggle').textContent = b.classList.contains('hidden')
      ? 'Show' : 'Hide';
  };
  el('selMode').onchange = () => {
    for (const q of S.queue) if (q.state !== 'done') q.mode = el('selMode').value;
    renderQueue();
  };
  refresh();
  setInterval(() => { if (!S.busy) refresh(); }, 5000);
}

init();
