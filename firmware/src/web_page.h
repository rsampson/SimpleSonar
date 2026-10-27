#pragma once

// The live echogram page served at / (see web_start() in main.cpp). It reads
// /config, opens the /ws WebSocket, and turns each binary ping (the serial
// packet, byte for byte) into an echogram column with the same processing
// as pc/plot_echogram_live.py. The <script id="dsp"> block holds only pure
// functions: pc/tests/test_web_page_dsp.py runs it under Node against the
// Python versions, and pc/web_preview.py serves this file's page on a PC.
// Both read the text between the R"HTML( and )HTML" delimiters.
static const char WEB_PAGE[] = R"HTML(<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SimpleSonar</title>
<style>
  :root { --bg: #101418; --panel: #1b2128; --text: #e6e9ec; --muted: #8b949e; --ok: #3fb950; --warn: #d29922; --bad: #f85149; }
  * { box-sizing: border-box; }
  html, body { margin: 0; height: 100%; background: var(--bg); color: var(--text);
               font: 14px/1.3 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
  body { display: flex; flex-direction: column; }
  header { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 16px; padding: 8px 12px; background: var(--panel); }
  h1 { font-size: 16px; margin: 0; font-weight: 600; }
  button { font: inherit; padding: 8px 18px; border-radius: 6px; border: 1px solid #3a434d; background: #262d35; color: var(--text); }
  button:active { background: #323b45; }
  #status { color: var(--muted); font-variant-numeric: tabular-nums; }
  .dot { display: inline-block; width: 9px; height: 9px; border-radius: 50%; margin-right: 6px; background: var(--bad); }
  .dot.ok { background: var(--ok); } .dot.wait { background: var(--warn); }
  #view { flex: 1; position: relative; min-height: 200px; }
  canvas { position: absolute; inset: 0; width: 100%; height: 100%; }
</style>
</head>
<body>
<header>
  <h1>SimpleSonar</h1>
  <button id="start">Start</button>
  <button id="stop">Stop</button>
  <span id="status"><span class="dot" id="dot"></span><span id="statustext">connecting...</span></span>
</header>
<div id="view"><canvas id="echo"></canvas></div>

<script id="dsp">
// Signal processing: pure functions, mirroring the Python tools.
const SPEED_OF_SOUND_M_S = 331.3 + 0.606 * 23.9;            // plot_matched_filter.py
const US_TO_CM = (SPEED_OF_SOUND_M_S * 100 / 1e6) / 2;      // one-way range per us
const MIN_DISPLAY_RANGE_CM = 40;                            // plot_echogram.py
const HEADER_BYTES = 28;                                    // sync + '<IqqHI' (receive.py)

// One packet (ArrayBuffer) -> {pingId, tPingUs, tSampleUs, n, dtUs, values}, or null.
function parsePacket(buf) {
  if (buf.byteLength < HEADER_BYTES) return null;
  const dv = new DataView(buf);
  if (dv.getUint8(0) !== 0xAA || dv.getUint8(1) !== 0xBB) return null;
  const n = dv.getUint16(22, true);
  if (buf.byteLength < HEADER_BYTES + 2 * n) return null;
  return {
    pingId: dv.getUint32(2, true),
    tPingUs: Number(dv.getBigInt64(6, true)),
    tSampleUs: Number(dv.getBigInt64(14, true)),
    n: n,
    dtUs: dv.getUint32(24, true),
    values: new Int16Array(buf, HEADER_BYTES, n),
  };
}

// make_ping_template(): the gated square-wave burst, zero-mean. Half-cycle
// index in exact integer steps, so edge samples don't flip on rounding.
function makeTemplate(dtUs, freqHz, cycles) {
  const n = Math.max(Math.round(cycles / freqHz / (dtUs * 1e-6)), 1);
  const t = new Float64Array(n);
  const step = Math.round(dtUs) * 2 * Math.round(freqHz);
  let sum = 0;
  for (let k = 0; k < n; k++) {
    t[k] = Math.floor(k * step / 1e6) % 2 === 0 ? 1 : -1;
    sum += t[k];
  }
  for (let k = 0; k < n; k++) t[k] -= sum / n;
  return t;
}

// matched_filter(): correlation with the template, lag 0 at index 0.
function matchedFilter(values, tpl) {
  const n = values.length, L = tpl.length;
  let mean = 0;
  for (let i = 0; i < n; i++) mean += values[i];
  mean /= n;
  const x = new Float64Array(n);
  for (let i = 0; i < n; i++) x[i] = values[i] - mean;
  const out = new Float64Array(n);
  for (let i = 0; i < n; i++) {
    let acc = 0;
    const m = Math.min(L, n - i);
    for (let j = 0; j < m; j++) acc += x[i + j] * tpl[j];
    out[i] = acc;
  }
  return out;
}

// In-place radix-2 FFT (n a power of two); inverse includes the 1/n.
function fft(re, im, inverse) {
  const n = re.length;
  for (let i = 1, j = 0; i < n; i++) {
    let bit = n >> 1;
    for (; j & bit; bit >>= 1) j ^= bit;
    j ^= bit;
    if (i < j) { let t = re[i]; re[i] = re[j]; re[j] = t; t = im[i]; im[i] = im[j]; im[j] = t; }
  }
  for (let len = 2; len <= n; len <<= 1) {
    const ang = (inverse ? 2 : -2) * Math.PI / len, half = len >> 1;
    for (let i = 0; i < n; i += len) {
      for (let k = 0; k < half; k++) {
        const wr = Math.cos(ang * k), wi = Math.sin(ang * k);
        const a = i + k, b = a + half;
        const vr = re[b] * wr - im[b] * wi, vi = re[b] * wi + im[b] * wr;
        re[b] = re[a] - vr; im[b] = im[a] - vi;
        re[a] += vr; im[a] += vi;
      }
    }
  }
  if (inverse) for (let i = 0; i < n; i++) { re[i] /= n; im[i] /= n; }
}

// hilbert_envelope(): |analytic signal| via the FFT method.
function hilbertEnvelope(x) {
  const n = x.length;
  if (n & (n - 1)) throw new Error('hilbertEnvelope needs a power-of-two length, got ' + n);
  const re = Float64Array.from(x), im = new Float64Array(n);
  fft(re, im, false);
  for (let i = 1; i < n / 2; i++) { re[i] *= 2; im[i] *= 2; }
  for (let i = n / 2 + 1; i < n; i++) { re[i] = 0; im[i] = 0; }
  fft(re, im, true);
  const env = new Float64Array(n);
  for (let i = 0; i < n; i++) env[i] = Math.hypot(re[i], im[i]);
  return env;
}

// range_axis_cm(): one-way range of each sample from the burst's start.
function rangeAxisCm(pkt) {
  const start = pkt.tSampleUs - pkt.tPingUs, r = new Float64Array(pkt.n);
  for (let i = 0; i < pkt.n; i++) r[i] = (start + i * pkt.dtUs) * US_TO_CM;
  return r;
}

// The samples [start, end) an echogram column shows. Blanked: below the
// burst's own length or MIN_DISPLAY_RANGE_CM, whichever is farther, and the
// last tpl.length - 1, where the matched filter runs out of samples.
function displayedSamples(pkt, tpl) {
  const blankCm = Math.max(tpl.length * pkt.dtUs * US_TO_CM, MIN_DISPLAY_RANGE_CM), r = rangeAxisCm(pkt);
  let start = 0;
  while (start < pkt.n && r[start] < blankCm) start++;
  return [start, Math.max(start, pkt.n - tpl.length + 1)];
}

// One echogram column: envelope, NaN outside displayedSamples().
function processPing(pkt, tpl) {
  const env = hilbertEnvelope(matchedFilter(pkt.values, tpl));
  const [start, end] = displayedSamples(pkt, tpl);
  env.fill(NaN, 0, start);
  env.fill(NaN, end);
  return env;
}

// update_reference(): fast attack, slow release (dB per ping).
function updateReference(ref, pingPeak, decayDb) {
  return Math.max(pingPeak, ref * Math.pow(10, -decayDb / 20));
}
</script>

<script>
// Display and connection.
const WINDOW_PINGS = 200;               // plot_echogram_live.py
const DYNAMIC_RANGE_DB = 40;
const REFERENCE_DECAY_DB_PER_PING = 1.0;
const ROWS = 512;                       // range bins shown (max-pooled from the samples)
const CM_PER_FOOT = 30.48;

const VIRIDIS = [[68, 1, 84], [71, 44, 122], [59, 81, 139], [44, 113, 142], [33, 144, 141],
                 [39, 173, 129], [92, 200, 99], [170, 220, 50], [253, 231, 37]];
const LUT = new Uint8ClampedArray(256 * 3);
for (let i = 0; i < 256; i++) {
  const p = i / 255 * (VIRIDIS.length - 1), k = Math.min(Math.floor(p), VIRIDIS.length - 2), f = p - k;
  for (let c = 0; c < 3; c++) LUT[i * 3 + c] = VIRIDIS[k][c] + f * (VIRIDIS[k + 1][c] - VIRIDIS[k][c]);
}

const state = {
  config: { freq_hz: 40000, cycles: 10 },
  tpl: null, tplKey: '',
  columns: new Float32Array(WINDOW_PINGS * ROWS).fill(NaN), next: 0, filled: 0,
  ref: 0, rangeCm: null, shown: null, lastId: null, received: 0, missed: 0, times: [],
};

const canvas = document.getElementById('echo');
const ctx = canvas.getContext('2d');
const image = document.createElement('canvas');
image.width = WINDOW_PINGS; image.height = ROWS;
const imageCtx = image.getContext('2d');
const pixels = imageCtx.createImageData(WINDOW_PINGS, ROWS);

function setStatus(kind, text) {
  document.getElementById('dot').className = 'dot ' + kind;
  document.getElementById('statustext').textContent = text;
}

async function loadConfig() {
  try {
    const r = await fetch('/config', { cache: 'no-store' });
    if (r.ok) state.config = await r.json();
  } catch (e) { /* keep the defaults */ }
}

function addPing(pkt) {
  const key = pkt.dtUs + '/' + state.config.freq_hz + '/' + state.config.cycles;
  if (key !== state.tplKey) {
    state.tpl = makeTemplate(pkt.dtUs, state.config.freq_hz, state.config.cycles);
    state.tplKey = key;
  }
  const env = processPing(pkt, state.tpl);
  state.rangeCm = rangeAxisCm(pkt);
  state.shown = displayedSamples(pkt, state.tpl);
  let peak = 0;
  const col = state.next * ROWS, per = pkt.n / ROWS;
  for (let row = 0; row < ROWS; row++) {
    let m = NaN;
    for (let i = Math.floor(row * per); i < Math.floor((row + 1) * per); i++) {
      if (!(env[i] <= m)) m = Number.isNaN(env[i]) ? m : env[i];
    }
    state.columns[col + row] = m;
    if (m > peak) peak = m;
  }
  state.ref = updateReference(state.ref, peak, REFERENCE_DECAY_DB_PER_PING);
  state.next = (state.next + 1) % WINDOW_PINGS;
  state.filled = Math.min(state.filled + 1, WINDOW_PINGS);

  if (state.lastId !== null && pkt.pingId > state.lastId + 1) state.missed += pkt.pingId - state.lastId - 1;
  state.lastId = pkt.pingId;
  state.received++;
  const now = performance.now();
  state.times.push(now);
  while (state.times.length > 11) state.times.shift();
  render();
}

function render() {
  const ref = state.ref > 0 ? state.ref : 1, d = pixels.data;
  for (let x = 0; x < WINDOW_PINGS; x++) {
    // oldest column on the left, newest on the right
    const c = (state.next + x) % WINDOW_PINGS;
    for (let row = 0; row < ROWS; row++) {
      const v = state.columns[c * ROWS + row], o = (row * WINDOW_PINGS + x) * 4;
      if (!(v > 0)) { d[o] = 16; d[o + 1] = 20; d[o + 2] = 24; d[o + 3] = 255; continue; }
      const db = Math.max(-DYNAMIC_RANGE_DB, Math.min(0, 20 * Math.log10(v / ref)));
      const li = Math.round((db + DYNAMIC_RANGE_DB) / DYNAMIC_RANGE_DB * 255) * 3;
      d[o] = LUT[li]; d[o + 1] = LUT[li + 1]; d[o + 2] = LUT[li + 2]; d[o + 3] = 255;
    }
  }
  imageCtx.putImageData(pixels, 0, 0);
  draw();
}

function draw() {
  const dpr = window.devicePixelRatio || 1;
  const w = Math.round(canvas.clientWidth * dpr), h = Math.round(canvas.clientHeight * dpr);
  if (canvas.width !== w || canvas.height !== h) { canvas.width = w; canvas.height = h; }
  const left = 54 * dpr, right = 46 * dpr, top = 10 * dpr, bottom = 26 * dpr;
  const pw = w - left - right, ph = h - top - bottom;
  ctx.fillStyle = '#101418'; ctx.fillRect(0, 0, w, h);
  ctx.imageSmoothingEnabled = false;
  // Only the rows inside displayedSamples(), so the range axis starts where
  // the blanking ends.
  const per = state.rangeCm ? state.rangeCm.length / ROWS : 1;
  const row0 = state.shown ? Math.ceil(state.shown[0] / per) : 0;
  const row1 = state.shown ? Math.max(row0 + 1, Math.floor(state.shown[1] / per)) : ROWS;
  ctx.drawImage(image, 0, row0, WINDOW_PINGS, row1 - row0, left, top, pw, ph);
  ctx.strokeStyle = 'rgba(230,233,236,0.18)'; ctx.lineWidth = dpr;
  ctx.strokeRect(left - 0.5 * dpr, top - 0.5 * dpr, pw + dpr, ph + dpr);  // outline, visible before the window fills

  ctx.fillStyle = '#8b949e'; ctx.strokeStyle = 'rgba(230,233,236,0.25)';
  ctx.font = (11 * dpr) + 'px system-ui, sans-serif';
  ctx.textBaseline = 'middle';
  const r = state.rangeCm;
  if (r) {
    const r0 = r[row0 * per], r1 = r[row1 * per - 1], y = (cm) => top + (cm - r0) / (r1 - r0) * ph;
    ctx.textAlign = 'right';
    for (let cm = Math.ceil(r0 / 50) * 50; cm <= r1; cm += 50) {
      ctx.fillText(cm + ' cm', left - 6 * dpr, y(cm));
      ctx.beginPath(); ctx.moveTo(left - 3 * dpr, y(cm)); ctx.lineTo(left, y(cm)); ctx.stroke();
    }
    ctx.textAlign = 'left';
    for (let ft = Math.ceil(r0 / CM_PER_FOOT); ft * CM_PER_FOOT <= r1; ft++) {
      ctx.fillText(ft + ' ft', left + pw + 6 * dpr, y(ft * CM_PER_FOOT));
    }
  }
  ctx.textAlign = 'center'; ctx.textBaseline = 'alphabetic';
  ctx.fillText('older  ←  ' + WINDOW_PINGS + ' pings  →  newest   (' + DYNAMIC_RANGE_DB +
               ' dB range)', left + pw / 2, h - 8 * dpr);
}

function statusLine() {
  const t = state.times, rate = t.length > 1 ? (t.length - 1) / ((t[t.length - 1] - t[0]) / 1000) : 0;
  return 'ping ' + (state.lastId === null ? '-' : state.lastId) + ' · ' + rate.toFixed(1) +
         '/s · missed ' + state.missed + ' · ' + (state.config.freq_hz / 1000) + ' kHz, ' +
         state.config.cycles + ' cycles';
}

let ws = null;
function connect() {
  setStatus('wait', 'connecting...');
  ws = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/ws');
  ws.binaryType = 'arraybuffer';
  ws.onopen = async () => { await loadConfig(); setStatus('wait', 'connected, waiting for pings...'); };
  ws.onmessage = (ev) => {
    if (typeof ev.data === 'string') return;
    const pkt = parsePacket(ev.data);
    if (pkt) { addPing(pkt); setStatus('ok', statusLine()); }
  };
  ws.onclose = () => { setStatus('', 'disconnected, retrying...'); setTimeout(connect, 2000); };
}

function send(cmd) {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(cmd);
  if (cmd === 'STOP') setStatus('wait', 'stopped');
}
document.getElementById('start').onclick = () => send('START');
document.getElementById('stop').onclick = () => send('STOP');
window.addEventListener('resize', draw);
draw();
connect();
</script>
</body>
</html>
)HTML";
