"""Overlay several Wiener-deconvolution lambda values on one plot to pick WIENER_LAMBDA.

WIENER_LAMBDA in deconvolve.py trades ring-collapse against noise
amplification: too small and the between-target background goes grassy,
too large and the ringdown isn't undone. The right value depends on this
transducer and this room's noise/clutter level, so it has to be picked by
eye on a real capture.

Run this against a sensor_stream.csv recorded with the transducer aimed at
a known target (e.g. a wall at a measured range). It matched-filters +
Wiener-deconvolves every ping at each lambda in LAMBDAS, averages the
envelope over all pings (so stationary reflections reinforce and
non-stationary noise averages down), and plots one curve per lambda on a
shared dB range axis. A vertical marker shows the known target range.

    python plot_lambda_sweep.py
    python plot_lambda_sweep.py --target-cm 251.5 --domain mf
    python plot_lambda_sweep.py --lambdas 3e-4,1e-3,3e-3,1e-2

Read the plot: find the smallest lambda where the target peak is sharp AND
the background between/after peaks is still low and smooth. Put that value
(and a one-line why) in deconvolve.WIENER_LAMBDA.
"""

import argparse
import sys

import numpy as np
import matplotlib.pyplot as plt

import deconvolve
from plot_sensor_stream import load_packets
from plot_matched_filter import make_ping_template, matched_filter, US_TO_CM
from plot_echogram import hilbert_envelope

INPUT_FILE = 'sensor_stream.csv'

# Default target marker: slant range to the wall in the current test room
# (~275 cm; the transducer is tilted, so this is longer than the normal
# floor-plan distance). Override with --target-cm for a different setup.
DEFAULT_TARGET_CM = 275.0

DEFAULT_LAMBDAS = (3e-4, 1e-3, 3e-3, 1e-2, 3e-2, 1e-1)


def averaged_deconv_envelope(packets, lam, domain):
    """Mean Hilbert envelope of the deconvolved response over all pings."""
    sample_dt_us = packets[0][0]['SampleDt_us']
    template = make_ping_template(sample_dt_us)
    h = deconvolve.make_impulse_response(sample_dt_us)

    acc = None
    for meta, values in packets:
        resp = deconvolve.deconvolved_response(
            values, meta['SampleDt_us'], template=template, h=h,
            domain=domain, lam=lam)
        env = hilbert_envelope(resp)
        acc = env if acc is None else acc + env
    return acc / len(packets)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--target-cm', type=float, default=DEFAULT_TARGET_CM,
                        help="known one-way target range in cm (marker on the plot); "
                             f"default {DEFAULT_TARGET_CM:.0f} (tilted wall, slant range)")
    parser.add_argument('--domain', choices=('mf', 'raw'), default='mf',
                        help="'mf' = matched-filter then Wiener (default), 'raw' = Wiener only")
    parser.add_argument('--lambdas', type=str, default=None,
                        help="comma-separated lambda values; default "
                             + ",".join(f"{v:g}" for v in DEFAULT_LAMBDAS))
    parser.add_argument('--db-range', type=float, default=50.0,
                        help="dB below each curve's own peak to display (default 50)")
    args = parser.parse_args()

    lambdas = ([float(s) for s in args.lambdas.split(',')]
               if args.lambdas else list(DEFAULT_LAMBDAS))

    packets = load_packets(INPUT_FILE)
    if not packets:
        print(f"No packets found in {INPUT_FILE}.")
        sys.exit(1)

    sample_dt_us = packets[0][0]['SampleDt_us']
    n_samples = len(packets[0][1])
    range_cm = np.arange(n_samples) * sample_dt_us * US_TO_CM

    # Blank the transmit-burst ringdown region, same as the echogram.
    blank_samples = len(make_ping_template(sample_dt_us))

    print(f"{len(packets)} pings, {n_samples} samples/ping, dt={sample_dt_us} us. "
          f"domain={args.domain}. Target marker at {args.target_cm:.1f} cm.")

    fig, ax = plt.subplots(figsize=(12, 7))
    cmap = plt.get_cmap('viridis')

    for i, lam in enumerate(lambdas):
        env = averaged_deconv_envelope(packets, lam, args.domain)
        env[:blank_samples] = np.nan
        peak = np.nanmax(env)
        db = 20 * np.log10(env / peak)
        ax.plot(range_cm, db, color=cmap(i / max(len(lambdas) - 1, 1)),
                label=f"lambda = {lam:g}", lw=1.3)
        # report where the biggest peak landed vs the known target
        pk_idx = int(np.nanargmax(env))
        print(f"  lambda={lam:<8g} strongest peak at {range_cm[pk_idx]:6.1f} cm  "
              f"(target {args.target_cm:.1f} cm)")

    ax.axvline(args.target_cm, color='crimson', ls='--', lw=1,
               label=f"known target {args.target_cm:.0f} cm")
    ax.set_xlim(0, range_cm[-1])
    ax.set_ylim(-args.db_range, 2)
    ax.set_xlabel("One-way range (cm)")
    ax.set_ylabel("Averaged deconvolved envelope (dB re: each curve's peak)")
    ax.set_title(f"WIENER_LAMBDA sweep — {len(packets)} pings averaged, domain={args.domain}\n"
                 f"pick the smallest lambda with a sharp target peak and a clean background")
    ax.legend(loc='upper right', fontsize=9)
    ax.grid(alpha=0.25)
    fig.tight_layout()
    plt.show()


if __name__ == "__main__":
    main()
