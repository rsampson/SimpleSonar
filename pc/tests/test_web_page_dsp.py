"""The web page's signal processing (web_page.h, <script id="dsp">) against
the Python tools, run under Node. Skipped if Node isn't installed."""

import json
import re
import shutil
import subprocess

import numpy as np
import pytest

from plot_echogram import MIN_DISPLAY_RANGE_CM, hilbert_envelope
from plot_echogram_live import (MIN_DISPLAY_RANGE_RING_CANCEL_CM, cancel_ring, ring_cancel_weights,
                                ring_gate, ring_residual_rms, update_reference, update_ring_estimate)
from plot_matched_filter import US_TO_CM, make_receive_filter, matched_filter
from plot_sensor_stream import range_axis_cm
from web_preview import load_page, packet_bytes

NODE = shutil.which('node')
pytestmark = pytest.mark.skipif(NODE is None, reason='node not installed')

HARNESS = """
const input = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const bytes = Uint8Array.from(Buffer.from(input.packet, 'base64'));
const pkt = parsePacket(bytes.buffer);
const tpl = makeTemplate(pkt.dtUs, input.freq, input.cycles);
const fix = (a) => Array.from(a, (v) => Number.isNaN(v) ? null : v);
console.log(JSON.stringify({
  header: [pkt.pingId, pkt.tPingUs, pkt.tSampleUs, pkt.n, pkt.dtUs],
  values: Array.from(pkt.values.slice(0, 5)),
  tpl: fix(tpl),
  mf: fix(matchedFilter(pkt.values, tpl)),
  col: fix(processPing(pkt, tpl)),
  ref: [updateReference(100, 2500, 1), updateReference(1000, 10, 1)],
  ...ring(pkt, tpl),
}));

// Ring cancellation over three pings: the packet's samples shifted by 0, 7
// and -3 counts.
function ring(pkt, tpl) {
  const w = ringCancelWeights(rangeAxisCm(pkt), 90, 20);
  let est = null;
  for (let k = 0; k < 3; k++) est = updateRingEstimate(est, pkt.values.map((v) => v + [0, 7, -3][k]), k, 2);
  const cleaned = cancelRing(pkt.values, est, w);
  const stats = [ringResidualRms(pkt.values, est, w)];
  let g = [true, null, 0];
  const gates = [];
  for (const s of [10, 11, 9, 50, 50, 10, 200, 200, 200]) {
    g = ringGate(s, g[1], g[2], 16, 3.0, 2);
    gates.push(g.slice());
  }
  return { ringStat: stats[0], ringGates: gates, ringW: Array.from(w), ringEst: Array.from(est), ringCleaned: Array.from(cleaned),
           ringCol: fix(processPing(pkt, tpl, cleaned, 30)) };
}
"""


def dsp_source():
    return re.search(r'<script id="dsp">(.*?)</script>', load_page(), re.S).group(1)


def run_page_dsp(meta, values, freq, cycles):
    import base64
    payload = {'packet': base64.b64encode(packet_bytes(meta, values)).decode(), 'freq': freq, 'cycles': cycles}
    out = subprocess.run([NODE, '-e', dsp_source() + HARNESS], input=json.dumps(payload),
                         capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


@pytest.fixture
def ping():
    rng = np.random.default_rng(3)
    n, dt = 4096, 5
    t = np.arange(n) * dt * 1e-6
    echo = 300 * np.exp(-((t - 8e-3) / 4e-4) ** 2) * np.sin(2 * np.pi * 40e3 * t)
    values = [int(v) for v in np.round(1810 + echo + rng.normal(0, 5, n))]
    meta = {'PingID': 123456, 'T_ping_us': 98_765_432_100, 'T_sample_us': 98_765_433_343,
            'NumSamples': n, 'SampleDt_us': dt}
    return meta, values


@pytest.mark.parametrize('freq, cycles', [(40000, 10), (40250, 10), (40000, 50)])
def test_page_matches_python(ping, freq, cycles):
    meta, values = ping
    js = run_page_dsp(meta, values, freq, cycles)

    assert js['header'] == [meta['PingID'], meta['T_ping_us'], meta['T_sample_us'],
                            meta['NumSamples'], meta['SampleDt_us']]
    assert js['values'] == values[:5]

    tpl = make_receive_filter(meta['SampleDt_us'], freq_hz=freq, cycles=cycles)
    np.testing.assert_allclose(js['tpl'], tpl, atol=1e-12)

    mf = matched_filter(values, tpl)
    np.testing.assert_allclose(js['mf'], mf, rtol=1e-9, atol=1e-6)

    env = hilbert_envelope(mf)
    blank = range_axis_cm(meta, len(values)) < max(len(tpl) * meta['SampleDt_us'] * US_TO_CM,
                                                   MIN_DISPLAY_RANGE_CM)
    blank[len(values) - len(tpl) + 1:] = True
    col = np.array([np.nan if v is None else v for v in js['col']])
    assert np.array_equal(np.isnan(col), blank)
    np.testing.assert_allclose(col[~blank], env[~blank], rtol=1e-6, atol=1e-6)


def test_page_reference_matches_python(ping):
    meta, values = ping
    js = run_page_dsp(meta, values, 40000, 10)
    assert js['ref'] == pytest.approx([update_reference(100, 2500, 1), update_reference(1000, 10, 1)])


def test_page_ring_cancel_matches_python(ping):
    meta, values = ping
    js = run_page_dsp(meta, values, 40000, 20)
    w = ring_cancel_weights(range_axis_cm(meta, len(values)), 90, 20)
    np.testing.assert_allclose(js['ringW'], w, atol=1e-12)
    est = None
    for k, shift in enumerate([0, 7, -3]):
        est = update_ring_estimate(est, np.array(values) + shift, k, 2)
    np.testing.assert_allclose(js['ringEst'], est, atol=1e-9)
    cleaned = cancel_ring(values, est, w)
    np.testing.assert_allclose(js['ringCleaned'], cleaned, atol=1e-9)
    assert js['ringStat'] == pytest.approx(ring_residual_rms(values, est, w))

    typical, skips, expected = None, 0, []
    for s in [10, 11, 9, 50, 50, 10, 200, 200, 200]:
        accept, typical, skips = ring_gate(s, typical, skips, 16, 2)
        expected.append([accept, typical, skips])
    assert [g[0] for g in js['ringGates']] == [e[0] for e in expected]
    assert [g[2] for g in js['ringGates']] == [e[2] for e in expected]
    assert [g[1] for g in js['ringGates']] == pytest.approx([e[1] for e in expected])

    tpl = make_receive_filter(meta['SampleDt_us'], cycles=20)
    env = hilbert_envelope(matched_filter(cleaned, tpl))
    blank = range_axis_cm(meta, len(values)) < max(len(tpl) * meta['SampleDt_us'] * US_TO_CM,
                                                   MIN_DISPLAY_RANGE_RING_CANCEL_CM)
    blank[len(values) - len(tpl) + 1:] = True
    col = np.array([np.nan if v is None else v for v in js['ringCol']])
    assert np.array_equal(np.isnan(col), blank)
    np.testing.assert_allclose(col[~blank], env[~blank], rtol=1e-6, atol=1e-6)
