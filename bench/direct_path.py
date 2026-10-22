"""Analyse a speaker->mic direct-path capture (mic facing the speaker, fixed).

    python bench/capture.py DIR/raw/direct.csv -n 80
    python bench/direct_path.py DIR --distance-m 2.54

Reports the first-arrival time against distance / speed of sound, per-ping
signal-to-noise, arrival jitter, in-band pre-arrival crosstalk, and saves the
averaged pulse (direct_pulse.npy, usable as a matched-filter template) and a
plot. The first arrival is read from the raw coherent mean (a zero-phase
band-pass would smear it earlier); strongest returns can be later than the
first because of room reflections, so don't use the envelope peak.

Reflecting surfaces near the mic or speaker put copies of the pulse into the
averaged waveform: a surface d behind the mic adds 2d of path, i.e. a copy
2d / c later (c = 345.8 m/s). Only use the first ~2d/c after the arrival as a
pulse template, and measure the distance accurately: a tape tolerance of 1 mm
is ~3 us of timing. The quoted jitter is real, but an approximate distance only
checks the speed of sound to within its own error.
"""

import argparse
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import analyze  # noqa: E402
from plot_echogram import hilbert_envelope  # noqa: E402

SPEED_OF_SOUND_M_S = 331.3 + 0.606 * 23.9


def first_arrival_ms(t_ms, w, expected_ms, sigma=8.0):
    """First time the coherent mean leaves its pre-arrival level by `sigma` std."""
    pre = (t_ms > expected_ms - 1.8) & (t_ms < expected_ms - 0.3)
    if pre.sum() < 20:
        raise ValueError(f"too little pre-arrival data ({pre.sum()} samples) before the expected "
                         f"{expected_ms:.2f} ms arrival: sampling starts at {t_ms[0]:.2f} ms; move further away")
    base, s = w[pre].mean(), w[pre].std()
    idx = np.where((t_ms > expected_ms - 0.4) & (np.abs(w - base) > sigma * s))[0]
    return float(t_ms[idx[0]]) if len(idx) else None


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('profile_dir', help='directory holding raw/direct.csv')
    ap.add_argument('--distance-m', type=float, required=True)
    ap.add_argument('--band', type=float, nargs=2, default=list(analyze.SONAR_BAND_HZ))
    args = ap.parse_args()

    d = args.profile_dir
    metas, x, fs, t0 = analyze.load_capture(os.path.join(d, 'raw', 'direct.csv'))
    t = t0 + np.arange(x.shape[1]) / fs * 1e3
    locked, res = analyze.split_ping_locked(x)
    w = locked - locked.mean()
    expected = args.distance_m / SPEED_OF_SOUND_M_S * 1e3
    lo, hi = args.band

    arr = first_arrival_ms(t, w, expected)
    print(f"{len(x)} pings; DC {x.mean():.0f}; raw {x.min():.0f}..{x.max():.0f}; railed {analyze.clip_fraction(x):.4f}")
    if arr is None:
        print("No arrival found - is the speaker driven and the mic facing it?")
        return
    print(f"first arrival {arr:.3f} ms vs {expected:.3f} ms expected at {args.distance_m:.2f} m "
          f"({(arr - expected) * 1e3:+.0f} us, {(arr - expected) * SPEED_OF_SOUND_M_S * 0.1:+.1f} cm of path)")

    B = np.array([analyze.bandpass(v, fs, lo, hi) for v in x])
    noise = np.mean([v[(t > t0 + 0.8) & (t < arr - 0.5)].std() for v in B - B.mean(0)])
    sig = np.mean([v[(t > arr) & (t < arr + 1.85)].std() for v in B])
    print(f"in-band {lo:.0f}-{hi:.0f} Hz: pulse {sig:.1f} counts RMS (first 1.85 ms) vs noise {noise:.2f} per ping -> "
          f"{20 * np.log10(sig / noise):.0f} dB single-ping, +{10 * np.log10(len(x)):.0f} dB averaged over {len(x)}")

    sel = (t > arr) & (t < arr + 2.5)
    ref = B.mean(0)[sel]
    shifts = []
    for v in B:
        c = np.correlate(v[(t > arr - 0.4) & (t < arr + 2.8)], ref, 'valid')
        k = int(np.argmax(c))
        den = c[k - 1] - 2 * c[k] + c[k + 1] if 0 < k < len(c) - 1 else 0
        shifts.append((k + (0.5 * (c[k - 1] - c[k + 1]) / den if den else 0)) * 1e6 / fs)
    shifts = np.array(shifts) - np.median(shifts)
    print(f"arrival jitter {shifts.std():.1f} us std ({shifts.std() * SPEED_OF_SOUND_M_S * 1e-3:.2f} mm); "
          f"drift first/last 10 pings {shifts[:10].mean():.1f}/{shifts[-10:].mean():.1f} us")

    bw = analyze.bandpass(w, fs, lo, hi)
    env = hilbert_envelope(bw)
    pre_cross = [(round(float(t[i]), 1), round(float(bw[i:i + 50].std()), 1)) for i in range(0, int((arr - 0.5 - t[0]) * fs / 1e3), 100)]
    print("ping-locked in-band crosstalk before the arrival (ms: counts RMS per 0.5 ms):", pre_cross)
    peaks = [(round(float(t[i]), 1), int(env[i])) for i in range(len(t))
             if t[i] > arr and env[i] == env[max(i - 80, 0):i + 80].max() and env[i] > 0.25 * env[t > arr].max()]
    print("strongest in-band returns after the first arrival (ms: counts):", peaks[:8])

    np.save(os.path.join(d, 'direct_pulse.npy'), w[(t >= arr - 0.2) & (t < arr + 4)])
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(2, 1, figsize=(9, 6))
    ax[0].plot(t, w, lw=.6); ax[0].plot(t, env, 'r'); ax[0].axvline(expected, c='k', ls='--')
    ax[0].set_title(f'direct path {args.distance_m * 100:.0f} cm: coherent mean, {lo:.0f}-{hi:.0f} Hz envelope (red)')
    ax[1].plot(t, w, lw=.7); ax[1].set_xlim(expected - 1.5, expected + 4.5); ax[1].axvline(expected, c='k', ls='--')
    ax[1].set_xlabel('ms after burst start')
    fig.tight_layout(); fig.savefig(os.path.join(d, 'direct.png'), dpi=80)


if __name__ == '__main__':
    main()
