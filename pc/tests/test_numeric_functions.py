import numpy as np
import pytest

from deconvolve import (
    deconvolved_response,
    make_impulse_response,
    wiener_deconvolve,
)
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


# --- Wiener ringdown deconvolution -------------------------------------------

from deconvolve import (  # noqa: E402  - imported here too for the constants
    IMPULSE_RESPONSE_SPAN_CYCLES,
    RINGDOWN_FREQUENCY_HZ,
)


def test_make_impulse_response_shape_and_decay():
    sample_dt_us = 10
    dt_s = sample_dt_us * 1e-6

    bare = make_impulse_response(sample_dt_us, convolve_with_template=False)
    expected_n = max(int(round(IMPULSE_RESPONSE_SPAN_CYCLES / RINGDOWN_FREQUENCY_HZ / dt_s)), 1)
    assert len(bare) == expected_n
    assert np.isfinite(bare).all()
    assert np.abs(bare).max() == pytest.approx(1.0)

    template = make_ping_template(sample_dt_us)
    shaped = make_impulse_response(sample_dt_us, convolve_with_template=True)
    assert len(shaped) == expected_n + len(template) - 1
    assert np.abs(shaped).max() == pytest.approx(1.0)

    # The ringdown is a decaying sinusoid: sampling its peak-to-peak swing
    # over successive thirds of the record, each third must be quieter than
    # the one before (Hilbert-envelope FFT edge ripple makes a strict
    # per-sample monotonic check unreliable - see the trimming in
    # test_hilbert_envelope_constant_for_pure_sinusoid).
    thirds = np.array_split(bare, 3)
    swings = [t.max() - t.min() for t in thirds]
    assert swings[0] > swings[1] > swings[2]


def test_wiener_deconvolve_output_length_matches_input():
    x = np.zeros(300)
    h = np.ones(9)

    result = wiener_deconvolve(x, h)

    assert len(result) == 300


def test_wiener_deconvolve_recovers_two_target_lags():
    # A short synthetic ringdown (12 cycles) so the kernel comfortably fits
    # the test trace; the axis/resolution behaviour is span-independent.
    h = make_impulse_response(sample_dt_us=10, span_cycles=12.0,
                              convolve_with_template=False)
    rng = np.random.default_rng(seed=0)

    r = np.zeros(600)
    r[120] = 1.0
    r[210] = 0.8  # 90 samples apart - far shorter than the full ring, so a
                  # plain matched filter would merge the two into one lobe
    y = np.convolve(r, h, mode='full')[:600] + rng.normal(scale=1e-4, size=600)

    out = wiener_deconvolve(y, h, lam=1e-3)
    env = np.abs(out)

    # the two strongest local maxima land on the two target lags
    peak1 = int(np.argmax(env))
    guard = env.copy()
    guard[max(0, peak1 - 20):peak1 + 20] = 0.0
    peak2 = int(np.argmax(guard))
    assert {abs(min(peak1, peak2) - 120) <= 2, abs(max(peak1, peak2) - 210) <= 2} == {True}


def test_wiener_deconvolve_reduces_post_lobe_ring_vs_matched_filter():
    sample_dt_us = 10
    h = make_impulse_response(sample_dt_us, convolve_with_template=False)
    rng = np.random.default_rng(seed=0)

    k = 200
    r = np.zeros(500)
    r[k] = 1.0
    y = np.convolve(r, h, mode='full')[:500] + rng.normal(scale=1e-4, size=500)

    ring_period = int(round((1.0 / RINGDOWN_FREQUENCY_HZ) / (sample_dt_us * 1e-6)))
    win = slice(k + 3 * ring_period, k + 12 * ring_period)

    mf = matched_filter(y, h)
    mf = mf / np.abs(mf).max()
    deconv = wiener_deconvolve(y, h, lam=1e-3)
    deconv = deconv / np.abs(deconv).max()

    mf_ring = np.sum(mf[win] ** 2)
    deconv_ring = np.sum(deconv[win] ** 2)
    assert deconv_ring < 0.5 * mf_ring


def test_wiener_deconvolve_output_on_same_axis_as_matched_filter():
    h = make_impulse_response(sample_dt_us=10, span_cycles=8.0,
                              convolve_with_template=False)
    k = 200
    values = np.zeros(1024)
    values[k:k + len(h)] += h  # a lone echo (impulse convolved with h) at k

    result = wiener_deconvolve(values, h)

    # deconvolution recovers the reflector at its true lag, within h's
    # few-sample onset rise time
    assert abs(int(np.argmax(np.abs(result))) - k) <= 3


def test_wiener_deconvolve_handles_spectral_null():
    h = np.array([1.0, 0.0, -1.0])  # rfft has an exact zero at DC
    rng = np.random.default_rng(seed=0)
    y = rng.normal(size=200)

    out = wiener_deconvolve(y, h, lam=1e-2)

    assert np.all(np.isfinite(out))
    assert np.abs(out).max() < 1e6 * np.abs(y).max()


def test_deconvolved_response_length_and_axis():
    # 2048-sample trace like a real packet, so the matched-filter-of-h
    # kernel (~1600 samples) is not longer than the signal.
    sample_dt_us = 10
    template = make_ping_template(sample_dt_us)
    h = make_impulse_response(sample_dt_us)
    rng = np.random.default_rng(seed=0)

    n, k = 2048, 600
    values = rng.normal(scale=0.5, size=n)
    seg = h[:n - k]
    values[k:k + len(seg)] += 50.0 * seg  # one strong reflector at sample k

    out = deconvolved_response(values, sample_dt_us, template=template, domain='mf')

    assert len(out) == n
    # deconvolution collapses the ring back onto the true reflector lag,
    # undoing the group delay the raw matched filter would leave in
    assert abs(int(np.argmax(np.abs(out))) - k) <= 15


def test_deconvolved_response_raw_domain_smoke():
    sample_dt_us = 10
    template = make_ping_template(sample_dt_us)
    rng = np.random.default_rng(seed=0)
    values = rng.normal(scale=0.01, size=2048)
    values[600:600 + len(template)] += template

    out = deconvolved_response(values, sample_dt_us, template=template, domain='raw')

    assert len(out) == 2048
    assert np.all(np.isfinite(out))
