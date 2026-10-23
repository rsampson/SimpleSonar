"""The web page's signal processing (web_page.h, <script id="dsp">) against
the Python tools, run under Node. Skipped if Node isn't installed."""

import json
import re
import shutil
import subprocess

import numpy as np
import pytest

from plot_echogram import hilbert_envelope
from plot_echogram_live import update_reference
from plot_matched_filter import US_TO_CM, make_ping_template, matched_filter
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
}));
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

    tpl = make_ping_template(meta['SampleDt_us'], freq_hz=freq, cycles=cycles)
    np.testing.assert_allclose(js['tpl'], tpl, atol=1e-12)

    mf = matched_filter(values, tpl)
    np.testing.assert_allclose(js['mf'], mf, rtol=1e-9, atol=1e-6)

    env = hilbert_envelope(mf)
    blank = range_axis_cm(meta, len(values)) < len(tpl) * meta['SampleDt_us'] * US_TO_CM
    col = np.array([np.nan if v is None else v for v in js['col']])
    assert np.array_equal(np.isnan(col), blank)
    np.testing.assert_allclose(col[~blank], env[~blank], rtol=1e-6, atol=1e-6)


def test_page_reference_matches_python(ping):
    meta, values = ping
    js = run_page_dsp(meta, values, 40000, 10)
    assert js['ref'] == pytest.approx([update_reference(100, 2500, 1), update_reference(1000, 10, 1)])
