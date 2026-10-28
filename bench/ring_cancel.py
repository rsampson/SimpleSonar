"""Cancel the array's high-Q rings with KICK pulses inside the post-burst brake.

The ring after each burst is several sharp lines at 45-48 kHz (one spurious
mode per element, Q in the hundreds) that the burst excites and the brake
doesn't touch. The drive-to-receive path is linear and repeats every ping,
so a narrow pulse at delay d and width w adds a fixed waveform: the measured
response to one probe pulse, shifted by the difference in delay and scaled
by width. This measures that response once, then picks a train of pulses
(matching pursuit on a 0.5 us delay grid, least squares on their widths)
that cancels the ring band over the capture, measures the result, and
repeats on what is left. The 40 kHz band (echoes) is not part of the fit.

    python bench/ring_cancel.py [--pings 40] [--rounds 3] [--save kicks.txt]

Leaves the firmware on RESET defaults; the KICK commands of the best round
are printed (and saved with --save) for applying by hand with BRAKE.
"""

import argparse
import csv
import os
import sys
import tempfile

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, '..', 'pc'))
import analyze  # noqa: E402
import capture as cap  # noqa: E402
from plot_echogram import hilbert_envelope  # noqa: E402

GRID_S = 0.5e-6        # candidate kick delays
MIN_WIDTH_S = 0.4e-6   # narrower pulses are dropped (the DRV8871 needs ~0.5 us)
MAX_WIDTH_S = 5e-6     # keeps the linear-in-width model honest (~1/4 period)
SPACING_S = 6e-6       # minimum distance between kick starts (no overlaps)


def load(path):
    rows = list(csv.reader(open(path)))[1:]
    x = np.array([list(map(float, r[6:])) for r in rows])
    dt = float(rows[0][5]) * 1e-6
    t = (int(rows[0][3]) - int(rows[0][2])) * 1e-6 + np.arange(x.shape[1]) * dt
    return x, t, 1 / dt


class Model:
    """Band-limited response of a kick, from the probe's measured response."""

    def __init__(self, resp, fs, band, probe_delay, probe_width):
        self.n = len(resp)
        self.N = 2 * self.n                       # zero padding: advancing must not wrap
        self.f = np.fft.rfftfreq(self.N, 1 / fs)
        self.mask = (self.f >= band[0]) & (self.f <= band[1])
        self.R = np.fft.rfft(resp, self.N) * self.mask
        self.d_p, self.w_p = probe_delay, probe_width
        self.scale = 1.0

    def response(self, delays, widths):
        """Summed response (len n) of kicks at delays with signed widths."""
        S = np.zeros_like(self.R)
        for d, w in zip(delays, widths):
            S += (w / self.w_p) * np.exp(2j * np.pi * self.f * (self.d_p - d))
        return self.scale * np.fft.irfft(self.R * S, self.N)[:self.n]

    def dictionary(self, delays, sel):
        F = np.exp(2j * np.pi * np.outer(self.d_p - delays, self.f[self.mask]))
        full = np.zeros((len(delays), len(self.f)), complex)
        full[:, self.mask] = F * self.R[self.mask]
        return self.scale * np.fft.irfft(full, self.N, axis=1)[:, :self.n][:, sel] / self.w_p


def pursue(target, model, sel, kicks, d_min, d_max, n_new):
    """Add up to n_new kicks (or widen existing ones) to cancel `target`.

    kicks: dict delay -> signed width. Greedy choice of delays, then a joint
    least-squares solve for all widths, clipped to the allowed range.
    """
    grid = np.arange(d_min, d_max, GRID_S)
    D = model.dictionary(grid, sel)                         # response per second of width
    resid = target[sel] - model.response(list(kicks), list(kicks.values()))[sel]
    chosen = list(kicks)
    for _ in range(n_new):
        free = np.ones(len(grid), bool)
        for c in chosen:
            free &= np.abs(grid - c) >= SPACING_S
        if not free.any():
            break
        score = np.abs(D @ resid) / np.linalg.norm(D, axis=1)
        score[~free] = 0
        j = int(np.argmax(score))
        chosen.append(grid[j])
        A = model.dictionary(np.array(chosen), sel)
        w, *_ = np.linalg.lstsq(A.T, target[sel], rcond=None)
        resid = target[sel] - A.T @ w
    A = model.dictionary(np.array(chosen), sel)
    w, *_ = np.linalg.lstsq(A.T, target[sel], rcond=None)
    w = np.clip(w, -MAX_WIDTH_S, MAX_WIDTH_S)
    return {d: wi for d, wi in zip(chosen, w) if abs(wi) >= MIN_WIDTH_S}


def send_kicks(ser, kicks):
    cap.send_command(ser, 'KICK CLEAR', 'KICK cleared')
    for d, w in sorted(kicks.items()):
        cap.send_command(ser, kick_cmd(d, w), 'KICK set')


def kick_cmd(d, w):
    return f"KICK {int(round(d * 1e9))} {int(round(abs(w) * 1e9))} {'+' if w > 0 else '-'}"


def run(ser, kicks, n, path):
    send_kicks(ser, kicks)
    cap.capture(ser, n, path, log=lambda *a: None)
    return load(path)


def report(label, x, t, fs, band, win, dc):
    m = x.mean(0) - dc
    eb = hilbert_envelope(analyze.bandpass(m, fs, *band))
    e40 = hilbert_envelope(analyze.bandpass(m, fs, 38e3, 42.5e3))
    r = lambda e, a, b: np.sqrt((e[(t >= a * 1e-3) & (t < b * 1e-3)] ** 2).mean())
    jit = x - x.mean(0)
    sel = (t >= win[0]) & (t < win[1])
    energy = np.sqrt((analyze.bandpass(m, fs, *band)[sel] ** 2).mean())
    print(f"{label:10s} ring RMS {energy:6.1f} | ring env 1.6-2.5 {r(eb, 1.6, 2.5):5.0f} 2.5-5 {r(eb, 2.5, 5):4.0f} "
          f"5-12 {r(eb, 5, 12):5.1f} | 40k 1.6-2.5 {r(e40, 1.6, 2.5):4.0f} | per-ping "
          f"{np.sqrt((jit[:, sel] ** 2).mean()):4.1f}")
    return energy


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--pings', type=int, default=40)
    ap.add_argument('--brake', type=int, default=950, help='brake after the burst, us (kicks fit inside)')
    ap.add_argument('--band', type=float, nargs=2, default=[43.5e3, 50e3])
    ap.add_argument('--window', type=float, nargs=2, default=[1.6e-3, 12e-3])
    ap.add_argument('--probe', type=float, nargs=2, default=[900e-6, 4e-6], metavar=('DELAY_S', 'WIDTH_S'))
    ap.add_argument('--rounds', type=int, default=3)
    ap.add_argument('--kicks-per-round', type=int, default=12)
    ap.add_argument('--save')
    args = ap.parse_args()
    tmp = tempfile.mkdtemp(prefix='ring_cancel_')
    ser = cap.open_sonar()
    try:
        print(cap.send_command(ser, 'RESET', 'RESET done'))
        print(cap.send_command(ser, f'BRAKE {args.brake}', 'BRAKE set'))
        x0, t, fs = run(ser, {}, args.pings, os.path.join(tmp, 'base.csv'))
        dc = x0.mean()
        sel = (t >= args.window[0]) & (t < args.window[1])
        base = report('baseline', x0, t, fs, args.band, args.window, dc)

        probe = {args.probe[0]: args.probe[1]}
        xp, *_ = run(ser, probe, args.pings, os.path.join(tmp, 'probe.csv'))
        report('probe', xp, t, fs, args.band, args.window, dc)
        model = Model(xp.mean(0) - x0.mean(0), fs, args.band, *args.probe)
        m0 = analyze.bandpass(x0.mean(0) - dc, fs, *args.band)
        target = -m0

        kicks, best = {}, (base, {})
        d_max = min(args.probe[0], args.brake * 1e-6 - MAX_WIDTH_S - 1e-6)
        for rnd in range(args.rounds):
            kicks = pursue(target, model, sel, kicks, 15e-6, d_max, args.kicks_per_round)
            if len(kicks) > 32:
                kicks = dict(sorted(kicks.items(), key=lambda kv: -abs(kv[1]))[:32])
            pred = model.response(list(kicks), list(kicks.values()))
            x, *_ = run(ser, kicks, args.pings, os.path.join(tmp, f'round{rnd}.csv'))
            e = report(f'round {rnd} ({len(kicks)})', x, t, fs, args.band, args.window, dc)
            if e < best[0]:
                best = (e, dict(kicks))
            actual = analyze.bandpass(x.mean(0) - x0.mean(0), fs, *args.band)
            # Model gain correction from what the kicks actually did.
            k = np.dot(actual[sel], pred[sel]) / np.dot(pred[sel], pred[sel])
            print(f"           model gain x{k:.2f}, predicted ring RMS "
                  f"{np.sqrt(((m0 + pred)[sel] ** 2).mean()):.1f}, {20 * np.log10(e / base):+.1f} dB vs baseline")
            model.scale *= k
        print(f"best: {20 * np.log10(best[0] / base):+.1f} dB with BRAKE {args.brake} and:")
        lines = [kick_cmd(d, w) for d, w in sorted(best[1].items())]
        print('\n'.join('  ' + s for s in lines))
        if args.save:
            with open(args.save, 'w') as f:
                f.write(f'BRAKE {args.brake}\n' + '\n'.join(lines) + '\n')
    finally:
        cap.send_command(ser, 'RESET', 'RESET done')
        ser.close()


if __name__ == '__main__':
    main()
