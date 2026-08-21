import numpy as np
import pytest

from plot_echogram import hilbert_envelope, to_db
from plot_matched_filter import PING_CYCLES, PING_FREQUENCY_HZ, make_ping_template, matched_filter


def test_matched_filter_peak_at_expected_lag():
    template = make_ping_template(sample_dt_us=10)
    rng = np.random.default_rng(seed=0)
    values = rng.normal(scale=0.01, size=400)
    k = 50
    values[k:k + len(template)] += template

    result = matched_filter(values, template)

    assert np.argmax(np.abs(result)) == k


def test_matched_filter_output_length_matches_input():
    values = np.zeros(100)
    template = np.ones(17)

    result = matched_filter(values, template)

    assert len(result) == 100


def test_hilbert_envelope_constant_for_pure_sinusoid():
    fs = 100_000
    n = 2048
    cycles = 20  # chosen so f*n/fs is a whole number of cycles - avoids
                 # FFT spectral-leakage edge artifacts that a non-period-
                 # aligned frequency would introduce near the array edges
    f = cycles * fs / n
    x = np.sin(2 * np.pi * f * np.arange(n) / fs)

    envelope = hilbert_envelope(x)

    trim = n // 20
    assert np.allclose(envelope[trim:-trim], 1.0, atol=0.01)


def test_hilbert_envelope_even_vs_odd_length_paths():
    fs = 100_000
    f = 1000

    for n in (100, 101):
        x = np.sin(2 * np.pi * f * np.arange(n) / fs)
        envelope = hilbert_envelope(x)
        assert len(envelope) == n
        assert np.all(np.isfinite(envelope))
        assert np.all(envelope >= 0)


def test_to_db_normalizes_against_known_peak():
    image = np.array([[10.0, 100.0], [50.0, 25.0]])

    db, peak = to_db(image)

    assert peak == 100.0
    assert db[0, 1] == pytest.approx(0.0)
    assert db[0, 0] == pytest.approx(20 * np.log10(10.0 / 100.0))
    assert db[1, 0] == pytest.approx(20 * np.log10(50.0 / 100.0))
    assert db[1, 1] == pytest.approx(20 * np.log10(25.0 / 100.0))


def test_to_db_handles_all_nan_image():
    image = np.full((3, 3), np.nan)

    db, peak = to_db(image)

    assert peak == 1.0
    assert np.all(np.isnan(db))


def test_make_ping_template_length_and_zero_mean():
    sample_dt_us = 10
    template = make_ping_template(sample_dt_us)

    burst_duration_s = PING_CYCLES / PING_FREQUENCY_HZ
    dt_s = sample_dt_us * 1e-6
    expected_n = max(int(round(burst_duration_s / dt_s)), 1)

    assert len(template) == expected_n
    assert np.isclose(template.mean(), 0.0, atol=1e-9)
    assert len(np.unique(np.round(template, 6))) == 2


def test_make_ping_template_minimum_length_floor():
    huge_sample_dt_us = 10 ** 9

    template = make_ping_template(huge_sample_dt_us)

    assert len(template) == 1
