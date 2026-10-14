"""Find a target echo's leading edge in a capture (and optionally compare with a no-target run).

    python bench/echo_onset.py target.csv --distance-m 1.956 [--none none.csv]

Works on the coherent mean of the pings (the generator/burst is phase-locked,
see CLAUDE.md), band-passed to the sonar's band. The onset is the first time
the envelope exceeds the pre-echo level by k sigma, searched from just before
the expected echo time; the quiet stretch between the direct sound's ring-down
and the echo is the reference. Use the leading edge, not the envelope peak
(the peak is ~1.2 ms / ~20 cm later). A no-target run is only needed when the
echo is mixed with the direct-sound tail (targets closer than ~1.2 m); keep
everyone out of the beam while recording it - a person reflects strongly.
"""

import argparse
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import analyze  # noqa: E402
from plot_echogram import hilbert_envelope  # noqa: E402

C = 331.3 + 0.606 * 23.9


def onset(t, e, expected_ms, k):
    pre = (t > expected_ms - 5.0) & (t < expected_ms - 0.8)
    ref = e[pre]
    thr = np.median(ref) + k * ref.std()
    idx = np.where((t > expected_ms - 1.3) & (e > thr))[0]
    return float(t[idx[0]]) if len(idx) else None


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('target_csv')
    ap.add_argument('--distance-m', type=float, required=True, help='one-way distance to the target')
    ap.add_argument('--none', help='optional no-target capture to subtract (coherent means)')
    ap.add_argument('--band', type=float, nargs=2, default=[1500, 6000])
    args = ap.parse_args()

    _, x, fs, t0 = analyze.load_capture(args.target_csv)
    t = t0 + np.arange(x.shape[1]) / fs * 1e3
    locked, res = analyze.split_ping_locked(x)
    w = locked - locked.mean()
    label = 'target'
    if args.none:
        _, xn, _, _ = analyze.load_capture(args.none)
        ln, _ = analyze.split_ping_locked(xn)
        print(f"DC target {x.mean():.1f} vs none {xn.mean():.1f} (should match to a few counts)")
        w = locked - ln
        label = 'target - none'
    lo, hi = args.band
    e = hilbert_envelope(analyze.bandpass(w, fs, lo, hi))
    expected = 2 * args.distance_m / C * 1e3
    print(f"{len(x)} pings; {label}; expected echo start {expected:.2f} ms for {args.distance_m * 100:.1f} cm one-way")
    for k in (5, 8, 12):
        o = onset(t, e, expected, k)
        if o is None:
            print(f"  {k} sigma: no echo found")
        else:
            r = o * C / 2 * 0.1
            print(f"  {k} sigma: onset {o:.2f} ms -> {r:.1f} cm one-way ({r - args.distance_m * 100:+.1f} cm)")
    after = (t > expected - 1) & (t < expected + 4)
    print(f"  echo envelope max {e[after].max():.0f} counts at {t[after][np.argmax(e[after])]:.2f} ms; "
          f"direct-sound peak {e[(t < t0 + 1.5)].max():.0f}")


if __name__ == '__main__':
    main()
