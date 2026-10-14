"""Offline end-to-end run of the CLI: the sonar is replaced by synthetic captures."""

import csv
import importlib
import json
import os
import re

import numpy as np
import pytest

from test_analyze import FS, N, OVERSAMPLE, make_pings, sine_pings

HEADER = ['Timestamp', 'PingID', 'T_ping_us', 'T_sample_us', 'NumSamples', 'SampleDt_us'] + [f'Int_{i}' for i in range(N)]
R_SERIES, R_SHUNT = 47000.0, 47.0
VPP = 5.0


def write_csv(path, x):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', newline='') as f:
        w = csv.writer(f)
        w.writerow(HEADER)
        for i, row in enumerate(x):
            w.writerow(['t', i, i * 300000, i * 300000 + 2672, N, 10] + [int(v) for v in row])


def fake_capture(n, path):
    rng = np.random.default_rng(abs(hash(os.path.basename(path))) % 2 ** 32)
    name = os.path.basename(path)
    node_mv_pp = VPP * R_SHUNT / (R_SERIES + R_SHUNT) * 1000
    if name.startswith('step'):
        period = int(FS * OVERSAMPLE / 50)
        sq = np.where(np.arange(period) < period // 2, node_mv_pp / 2, -node_mv_pp / 2)
        x = make_pings(1 / 50, sq, n, rng)
    elif name.startswith('sine_'):
        f = float(re.match(r'sine_(\d+)', name).group(1))
        x = sine_pings(f, node_mv_pp / 2, n, rng)
    else:
        x = sine_pings(1000.0, 0.0, n, rng)          # terminated input: noise + crosstalk
    write_csv(path, np.clip(x, 0, 4095))
    return n


@pytest.fixture
def cli(tmp_path, monkeypatch):
    monkeypatch.setenv('BENCH_PROFILES_DIR', str(tmp_path))
    import hw_profile
    import run_test
    importlib.reload(hw_profile)
    importlib.reload(run_test)
    monkeypatch.setattr(run_test.Session, 'capture', lambda self, n, path: fake_capture(n, path))
    monkeypatch.setattr('builtins.input', lambda prompt='': '')

    def run(*argv):
        monkeypatch.setattr('sys.argv', ['run_test.py', '--profile', 'synthetic', *argv])
        run_test.main()
    return run, tmp_path / 'synthetic'


def test_full_flow_and_reanalyze_is_reproducible(cli):
    run, d = cli
    run('setup', '--description', 'synthetic model', '--r-series', str(R_SERIES), '--r-shunt', str(R_SHUNT))
    run('step', '--vpp', str(VPP))
    run('sine', '--vpp', str(VPP), '--freqs', '1000,4000,60000')
    run('noise')
    run('crosstalk')
    first = json.loads((d / 'profile.json').read_text())['tests']
    assert first['step']['result']['edges'] > 50
    assert len(first['sine']['result']['points']) == 3
    assert first['noise']['result']['input_referred_uv_rms'] > 0
    assert (d / 'report.md').exists() and (d / 'step_response.png').exists()

    run('reanalyze')
    again = json.loads((d / 'profile.json').read_text())['tests']
    for test in ('step', 'noise', 'crosstalk'):
        assert again[test]['result'] == first[test]['result']
    assert again['sine']['result']['points'] == first['sine']['result']['points']
