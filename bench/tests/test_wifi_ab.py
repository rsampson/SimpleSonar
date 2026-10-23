"""Synthetic Wi-Fi A/B: injected interference must show up, and clean data must not."""

import numpy as np
import pytest

import wifi_ab

FS = 200_000.0
N = 4096
BIAS = 1810.0
NOISE = 2.0


def capture(seed, bursts=0, spur_hz=None, spur_amp=0.0, n=60):
    """Ping-locked echo + white noise; optional short bursts in some pings and a steady spur."""
    rng = np.random.default_rng(seed)
    t = np.arange(N) / FS
    echo = 50 * np.exp(-((t - 8e-3) / 3e-4) ** 2) * np.sin(2 * np.pi * 40e3 * t)
    x = BIAS + echo + rng.normal(0, NOISE, (n, N))
    for i in rng.choice(n, size=bursts, replace=False):
        k = rng.integers(0, N - 20)
        x[i, k:k + 20] += rng.normal(0, 40, 20)       # a transmit burst's worth of hash
    if spur_hz:
        phases = rng.uniform(0, 2 * np.pi, (n, 1))     # not ping-locked
        x += spur_amp * np.sin(2 * np.pi * spur_hz * t + phases)
    return np.round(x)


def test_clean_off_and_on_match():
    off = [wifi_ab.state_metrics(capture(s), FS) for s in (1, 2)]
    on = [wifi_ab.state_metrics(capture(s), FS) for s in (3, 4)]
    c = wifi_ab.compare(off, on)
    assert abs(c['oob_rms_counts_change_db']) < 0.2
    assert c['spike_pings_on'] <= 2 and c['spike_pings_off'] <= 2
    assert c['new_spurs_hz'] == []


def test_interference_bursts_and_spur_are_caught():
    off = [wifi_ab.state_metrics(capture(s), FS) for s in (1, 2)]
    on = [wifi_ab.state_metrics(capture(s, bursts=12, spur_hz=62_500.0, spur_amp=3.0), FS) for s in (3, 4)]
    c = wifi_ab.compare(off, on)
    assert c['spike_pings_on'] >= 20                 # 12 per capture, two captures
    assert c['oob_rms_counts_change_db'] > 1.0
    assert any(abs(f - 62_500) < 200 for f in c['new_spurs_hz'])


def test_varying_echoes_in_band_are_not_interference():
    # echo amplitude changing ping to ping (people, air) only moves the in-band figures
    off = [wifi_ab.state_metrics(capture(s), FS) for s in (1, 2)]
    on = []
    for s in (3, 4):
        x = capture(s)
        t = np.arange(N) / FS
        x += np.random.default_rng(s).normal(0, 20, (len(x), 1)) * np.sin(2 * np.pi * 40e3 * t) \
             * np.exp(-((t - 8e-3) / 3e-4) ** 2)
        on.append(wifi_ab.state_metrics(x, FS))
    c = wifi_ab.compare(off, on)
    assert c['rms_counts_in_band_change_db'] > 1.0
    assert abs(c['oob_rms_counts_change_db']) < 0.3
    assert c['spike_pings_on'] <= 2 and c['new_spurs_hz'] == []


def test_ping_locked_echo_is_not_counted_as_noise():
    m = wifi_ab.state_metrics(capture(5), FS)
    assert m['rms_counts'] == pytest.approx(NOISE, rel=0.1)
    assert m['ping_locked_rms_counts'] > 3
