"""Signal-chain analysis for bench captures (pure numpy).

Every capture is a set of pings recorded while the firmware pings normally
and an unsynchronised bench generator drives the preamp input. Anything
locked to the ping (burst feedthrough, preamp/ADC start-up transients) is
identical in every ping, while the generator's signal lands at a random
phase each time - so the ping-locked part is estimated as the mean across
pings and subtracted (`split_ping_locked`) before analysing the stimulus.
That mean is itself the crosstalk measurement (T4).
"""

import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'pc'))
from plot_sensor_stream import load_packets  # noqa: E402
from plot_echogram import hilbert_envelope  # noqa: E402

ADC_MAX = 4095
RAIL_MARGIN = 5          # counts from either rail treated as clipped
ADC_LSB_MV = 3300 / 4096  # nominal; ESP32 ADC scale is only approximate
# The sonar's working band: 40 kHz 40T/40R-12 transducers (+/-5 kHz covers
# the datasheet response plots).
SONAR_BAND_HZ = (35000, 45000)


# --- loading ----------------------------------------------------------------

def load_capture(path):
    """Return (metas, x, fs_hz, t0_ms) for a capture CSV.

    x is float (n_pings, n_samples). Packets with out-of-range samples (a
    corrupted packet, e.g. from a serial byte drop) are dropped. t0_ms is
    the time of the first sample after the burst start.
    """
    packets = load_packets(path)
    metas = [m for m, _ in packets]
    x = np.array([v for _, v in packets], dtype=float)
    ok = ~((x < 0) | (x > ADC_MAX)).any(axis=1)
    metas = [m for m, k in zip(metas, ok) if k]
    x = x[ok]
    if len(x) == 0:
        raise ValueError(f"{path}: no valid packets")
    dt_us = metas[0]['SampleDt_us']
    t0_ms = float(np.median([m['T_sample_us'] - m['T_ping_us'] for m in metas])) / 1e3
    return metas, x, 1e6 / dt_us, t0_ms


def split_ping_locked(x):
    """Split pings into (ping-locked mean, residual = x - mean)."""
    mean = x.mean(axis=0)
    return mean, x - mean


def clip_fraction(x):
    return float(((x <= RAIL_MARGIN) | (x >= ADC_MAX - RAIL_MARGIN)).mean())


# --- helpers ------------------------------------------------------------------

def fold_frequency(f_hz, fs_hz):
    """Frequency at which a tone at f_hz appears after sampling at fs_hz."""
    f = np.mod(f_hz, fs_hz)
    return float(min(f, fs_hz - f))


def bandpass(sig, fs_hz, lo_hz, hi_hz, taper_hz=300.0):
    """Zero-phase FFT band-pass with raised-cosine edges.

    The record is mirrored first so the FFT doesn't see a jump between its
    two ends (captures start large and end small), which a plain FFT filter
    would turn into in-band ringing at both ends.
    """
    sig = np.asarray(sig, dtype=float)
    n = len(sig)
    ext = np.concatenate([sig, sig[::-1]])
    ext = ext - ext.mean()
    f = np.fft.rfftfreq(2 * n, 1 / fs_hz)

    def edge(x):  # 0 -> 1 across one taper width
        return np.clip(0.5 - 0.5 * np.cos(np.pi * np.clip(x, 0, 1)), 0, 1)

    gain = np.ones_like(f)
    if lo_hz > 0:
        gain *= edge((f - (lo_hz - taper_hz / 2)) / taper_hz)
    if hi_hz < fs_hz / 2:
        gain *= 1 - edge((f - (hi_hz - taper_hz / 2)) / taper_hz)
    return np.fft.irfft(np.fft.rfft(ext) * gain, 2 * n)[:n]


def band_rms(sig, fs_hz, lo_hz, hi_hz):
    """RMS of sig restricted to [lo_hz, hi_hz]."""
    return float(bandpass(sig, fs_hz, lo_hz, hi_hz).std())


def _parabolic_peak(y, k):
    if 0 < k < len(y) - 1:
        a, b, c = y[k - 1], y[k], y[k + 1]
        den = a - 2 * b + c
        if den != 0:
            return 0.5 * (a - c) / den
    return 0.0


def corners_3db(freqs, mag):
    """(-3 dB low corner, -3 dB high corner, peak gain, peak freq) of mag(f).

    A corner is None if the response doesn't fall 3 dB on that side within
    the measured range.
    """
    freqs = np.asarray(freqs, dtype=float)
    mag = np.asarray(mag, dtype=float)
    k = int(np.argmax(mag))
    peak = mag[k]
    level = peak / np.sqrt(2)

    def crossing(idx):
        for a, b in zip(idx[:-1], idx[1:]):
            if mag[b] < level <= mag[a]:
                # interpolate in log-frequency
                fa, fb = np.log10(freqs[a]), np.log10(freqs[b])
                return float(10 ** (fa + (level - mag[a]) / (mag[b] - mag[a]) * (fb - fa)))
        return None

    lo = crossing(list(range(k, -1, -1))) if k > 0 else None
    hi = crossing(list(range(k, len(mag))))
    return lo, hi, float(peak), float(freqs[k])


# --- T0: noise -----------------------------------------------------------------

def analyze_noise(x, fs_hz, top_spurs=8, band=SONAR_BAND_HZ):
    """Noise floor with the input terminated."""
    locked, res = split_ping_locked(x)
    n = res.shape[1]
    win = np.hanning(n)
    psd = (np.abs(np.fft.rfft(res * win, axis=1)) ** 2).mean(axis=0)
    psd *= 2 / (fs_hz * (win ** 2).sum())          # one-sided, counts^2/Hz
    f = np.fft.rfftfreq(n, 1 / fs_hz)
    asd = np.sqrt(psd)
    # spurs: local maxima well above the local median floor
    floor = np.median(asd[f > 50])
    spur_idx = [i for i in range(2, len(asd) - 1)
                if asd[i] > asd[i - 1] and asd[i] >= asd[i + 1] and asd[i] > 1.8 * floor and f[i] > 200]
    spur_idx = sorted(spur_idx, key=lambda i: asd[i], reverse=True)[:top_spurs]
    return {
        'pings': int(len(x)),
        'rms_counts': float(res.std()),
        'rms_counts_in_band': float(np.mean([band_rms(r, fs_hz, *band) for r in res])),
        'band_hz': list(band),
        'ping_locked_rms_counts': float((locked - locked.mean()).std()),
        'dc_counts': float(x.mean()),
        'floor_asd_counts_per_rthz': float(floor),
        'spurs': [{'hz': float(f[i]), 'asd_counts_per_rthz': float(asd[i])} for i in sorted(spur_idx)],
        '_arrays': {'noise_f': f, 'noise_asd': asd},
    }


# --- T1: step ------------------------------------------------------------------

def find_edges(r, threshold, min_gap):
    """Return [(edge_position_samples, sign)] for steps in one ping."""
    d = np.diff(r)
    a = np.abs(d)
    edges = []
    k = 0
    while k < len(a):
        if a[k] >= threshold:
            j = k + int(np.argmax(a[k:k + min_gap]))
            pos = j + 0.5 + _parabolic_peak(a, j)   # diff sits between samples
            edges.append((pos, float(np.sign(d[j]))))
            k = j + min_gap
        else:
            k += 1
    return edges


def analyze_step(x, fs_hz, step_mv, pre_ms=1.0, post_ms=8.0):
    """Average the generator's edges into a step response, then derive the
    impulse and frequency response. step_mv: input step size at the node
    (the square wave's peak-to-peak)."""
    _, res = split_ping_locked(x)
    pre = int(round(pre_ms * 1e-3 * fs_hz))
    post = int(round(post_ms * 1e-3 * fs_hz))
    d_all = np.abs(np.diff(res, axis=1))
    threshold = 0.4 * np.percentile(d_all.max(axis=1), 50)
    min_gap = int(2e-3 * fs_hz)
    offsets = np.arange(-pre, post)
    windows = []
    per_ping = [find_edges(r, threshold, min_gap) for r in res]
    # A square wave gives edges of alternating sign at a regular spacing in
    # most pings; a lone glitch per ping (or noise) doesn't.
    paired = sum(1 for e in per_ping if len(e) >= 2 and all(a[1] != b[1] for a, b in zip(e, e[1:])))
    if paired < len(res) / 2:
        raise ValueError(f"no square wave found: only {paired} of {len(res)} pings have alternating edges "
                         "- is the generator signal reaching the input?")
    for r, edges in zip(res, per_ping):
        for pos, sign in edges:
            if pos - pre < 1 or pos + post > len(r) - 2:
                continue
            seg = np.interp(pos + offsets, np.arange(len(r)), r)
            seg = sign * (seg - seg[:pre - 2].mean())
            windows.append(seg)
    if len(windows) < 4:
        raise ValueError(f"only {len(windows)} usable edges found - is the square wave reaching the input?")
    w = np.array(windows)
    step = w.mean(axis=0) / step_mv                      # counts per mV of input step
    t_ms = offsets / fs_hz * 1e3

    impulse = np.diff(step)                              # counts per mV per sample
    nfft = 16 * len(impulse)
    H = np.fft.rfft(impulse, nfft)
    f = np.fft.rfftfreq(nfft, 1 / fs_hz)
    mag = np.abs(H)
    valid = f >= fs_hz / len(impulse)                    # below 1/window the estimate is meaningless
    lo, hi, peak, fpeak = corners_3db(f[valid], mag[valid])

    peak_i = int(np.argmax(step))
    final = step[-int(0.5e-3 * fs_hz):].mean()
    tol = 0.02 * abs(step[peak_i])
    outside = np.where(np.abs(step[peak_i:] - final) > tol)[0]
    settle_ms = float((outside[-1] + 1) / fs_hz * 1e3) if len(outside) else 0.0
    rise = np.where(step >= 0.1 * step[peak_i])[0]
    rise90 = np.where(step >= 0.9 * step[peak_i])[0]
    return {
        'edges': int(len(w)),
        'peak_gain_counts_per_mv': peak,
        'peak_gain_db_nominal': float(20 * np.log10(peak * ADC_LSB_MV)),
        'peak_freq_hz': fpeak,
        'low_corner_hz': lo,
        'high_corner_hz': hi,
        'step_peak_counts_per_mv': float(step[peak_i]),
        'rise_10_90_us': float((rise90[0] - rise[0]) / fs_hz * 1e6) if len(rise) and len(rise90) else None,
        'settle_2pct_ms_after_peak': settle_ms,
        'edge_noise_counts': float(w.std(axis=0).mean()),
        'freq_resolution_hz': float(fs_hz / len(impulse)),
        '_arrays': {'step_t_ms': t_ms, 'step_counts_per_mv': step,
                    'resp_f': f[valid], 'resp_mag': mag[valid], 'resp_phase_deg': np.degrees(np.angle(H[valid]))},
    }


# --- T2/T3: sine ----------------------------------------------------------------

def estimate_frequency(res, fs_hz, f_hint=None):
    """Refine the observed tone frequency from the ping-averaged power spectrum."""
    n = res.shape[1]
    nfft = 8 * n
    p = (np.abs(np.fft.rfft(res * np.hanning(n), nfft, axis=1)) ** 2).mean(axis=0)
    f = np.fft.rfftfreq(nfft, 1 / fs_hz)
    mask = f > 20
    if f_hint is not None:
        mask &= np.abs(f - f_hint) < max(0.05 * f_hint, 3 * fs_hz / n)
    k = int(np.argmax(np.where(mask, p, 0)))
    return float(f[k] + _parabolic_peak(p, k) * (f[1] - f[0]))


def fit_tones(r, fs_hz, freqs_hz):
    """Joint least-squares amplitudes of tones at freqs_hz in one ping.

    Fitting them together matters: a capture isn't a whole number of cycles,
    so tones fitted one at a time leak into each other.
    """
    t = np.arange(len(r)) / fs_hz
    cols = [np.ones_like(t)]
    for f in freqs_hz:
        cols += [np.cos(2 * np.pi * f * t), np.sin(2 * np.pi * f * t)]
    coef, *_ = np.linalg.lstsq(np.column_stack(cols), r, rcond=None)
    return [float(np.hypot(coef[1 + 2 * i], coef[2 + 2 * i])) for i in range(len(freqs_hz))]


def analyze_sine(x, fs_hz, f_true_hz, amp_mv_pk, n_harmonics=5):
    """Gain, THD and clipping for one stepped-sine point.

    f_true_hz is the generator frequency; above Nyquist the tone is measured
    where it folds to, which is exactly the residual-aliasing figure.
    amp_mv_pk: peak amplitude at the input node.
    """
    _, res = split_ping_locked(x)
    f_obs_nom = fold_frequency(f_true_hz, fs_hz)
    f_obs = estimate_frequency(res, fs_hz, f_hint=f_obs_nom)
    sep = 3 * fs_hz / res.shape[1]
    # The generator's dial is only approximately right: scale its frequency
    # by observed/nominal so harmonics are placed where they really land.
    # That ratio is only meaningful when the fundamental isn't aliased.
    f_scale = f_obs / f_obs_nom if f_true_hz <= fs_hz / 2 else 1.0
    hf = []
    for k in range(2, n_harmonics + 1):
        fk = fold_frequency(k * f_true_hz * f_scale, fs_hz)
        if fk < 50 or abs(fk - f_obs) < sep or any(abs(fk - g) < sep for g in hf):
            continue            # lands on DC, the fundamental or another harmonic: not separable
        hf.append(fk)
    fits = np.array([fit_tones(r, fs_hz, [f_obs] + hf) for r in res])
    amps = fits[:, 0]
    a1 = float(amps.mean())
    harm = [float(v) for v in fits[:, 1:].mean(axis=0)] if hf else []
    noise = float(np.sqrt(max(res.var() - a1 ** 2 / 2, 0)))
    return {
        'f_generator_hz': float(f_true_hz),
        'f_observed_hz': f_obs,
        'aliased': bool(f_true_hz > fs_hz / 2),
        'input_mv_pk': float(amp_mv_pk),
        'output_counts_pk': a1,
        'output_counts_pk_std': float(amps.std()),
        'gain_counts_per_mv': a1 / amp_mv_pk,
        'gain_db_nominal': float(20 * np.log10(a1 / amp_mv_pk * ADC_LSB_MV)),
        'thd_pct': float(100 * np.sqrt(np.sum(np.square(harm))) / a1) if harm and a1 > 0 else None,
        'residual_rms_counts': noise,
        'clip_fraction': clip_fraction(x),
        'pings': int(len(x)),
    }


# --- T4: crosstalk ----------------------------------------------------------------

def _settles_after_ms(t_ms, above):
    """Time after which the envelope stays below the floor: the first sample
    if it never exceeds it, None if it still exceeds it at the record's end."""
    if not len(above):
        return float(t_ms[0])
    first_quiet = above[-1] + 1
    return float(t_ms[first_quiet]) if first_quiet < len(t_ms) else None


def analyze_crosstalk(x, fs_hz, t0_ms, noise_band_rms=None, band=SONAR_BAND_HZ):
    """Ping-locked (burst-synchronous) signal with the input terminated.

    noise_band_rms: in-band per-ping noise RMS from T0, used as the floor the
    crosstalk must fall under; falls back to this capture's own residual.
    """
    locked, res = split_ping_locked(x)
    m = locked - locked.mean()
    env = hilbert_envelope(bandpass(m, fs_hz, *band))
    k = int(0.3e-3 * fs_hz)
    env_s = np.convolve(env, np.ones(k) / k, mode='same')
    floor = noise_band_rms if noise_band_rms else float(np.mean([band_rms(r, fs_hz, *band) for r in res]))
    # the mean of N pings carries noise of floor/sqrt(N); require 3x the per-ping floor
    above = np.where(env_s > 3 * floor * np.sqrt(2))[0]
    t_ms = t0_ms + np.arange(len(m)) / fs_hz * 1e3
    return {
        'pings': int(len(x)),
        'rms_counts': float(m.std()),
        'rms_counts_below_300hz': band_rms(m, fs_hz, 0, 300),
        'rms_counts_in_band': band_rms(m, fs_hz, *band),
        'band_hz': list(band),
        'floor_counts_rms_in_band': floor,
        'in_band_below_floor_after_ms': _settles_after_ms(t_ms, above),
        'first_sample_ms_after_burst': float(t0_ms),
        '_arrays': {'xtalk_t_ms': t_ms, 'xtalk_mean': m, 'xtalk_env': env_s},
    }
