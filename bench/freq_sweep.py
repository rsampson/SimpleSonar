"""Two-way frequency response of the transducers, from a reflector's echo.

    python bench/freq_sweep.py --profile NAME --target-m 2.0
    python bench/freq_sweep.py --profile NAME --target-m 2.0 --reanalyze
    python bench/freq_sweep.py --profile NAME --target-m 2.0 --cycles 50   # steady state

Steps the drive frequency (firmware FREQ command) across --start..--stop and
captures --pings pings at each step. Each step's echo amplitude is read in a
window around the target's round trip, so the room's direct sound and
reverberation are excluded. A large, flat, squared reflector has a flat
response over the band, so what's left is transmit x receive (plus a fraction
of a dB of air absorption). The direct sound's amplitude, read near range,
is a second, rougher estimate of the same response.

Amplitudes come from demodulating the coherent (ping-locked) mean at the
drive frequency: the burst is phase-locked to the capture, so averaging
pings keeps the echo and suppresses noise. Raw captures go to
profiles/NAME/raw/sweep_<hz>.csv (gitignored); results to profile.json,
response.npz and freq_sweep.png. The firmware is left at --stop afterwards;
reopen the port (which resets the ESP32) to return to PING_FREQUENCY.

The default 10-cycle burst spans ~3.6 kHz itself, so its sweep is the
response to the burst actually used, smoothed by that width. --cycles 50
(~0.7 kHz) measures the transducers' own, steady-state response.
"""

import argparse
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import analyze  # noqa: E402
import hw_profile  # noqa: E402
from echo_onset import C, onset  # noqa: E402
from plot_echogram import hilbert_envelope  # noqa: E402

ENVELOPE_SMOOTH_MS = 0.25   # demodulator low-pass: ~4 kHz, wider than the transducers
GATE_BEFORE_MS = 0.5        # echo window around the expected round trip
RING_MS = 1.75              # echo window continues this long past the burst's end
SETTLE_MS = 0.6             # ~4 ring-up time constants: past this a long burst's echo is steady
MIN_SETTLED_MS = 0.3        # bursts with less settled time than this are read at their peak
DIRECT_MS = 1.5             # near-range window for the direct sound


def demod_envelope(w, fs_hz, t_ms, f_hz):
    """Amplitude envelope (counts) of w's component near f_hz."""
    z = w * np.exp(-2j * np.pi * f_hz * t_ms * 1e-3)
    k = max(int(round(ENVELOPE_SMOOTH_MS * 1e-3 * fs_hz)), 1)
    return 2 * np.abs(np.convolve(z, np.ones(k) / k, mode='same'))


def sweep_point(x, fs_hz, t0_ms, f_hz, target_m, cycles=10):
    """Echo and direct-sound amplitude at one drive frequency.

    x: (n_pings, n_samples) raw counts; t0_ms: first sample after burst start.
    The echo window runs from GATE_BEFORE_MS before the expected round trip
    to RING_MS past the end of a `cycles`-long burst. A burst long enough to
    settle is read as its mean level from SETTLE_MS after the echo starts to
    the burst's end: its maximum can instead land on the start-up transient,
    which off resonance overshoots the steady state. Shorter bursts never
    settle and are read at their peak.
    """
    locked, _ = analyze.split_ping_locked(x)
    w = locked - locked.mean()
    t_ms = t0_ms + np.arange(len(w)) / fs_hz * 1e3
    env = demod_envelope(w, fs_hz, t_ms, f_hz)
    expected_ms = 2 * target_m / C * 1e3
    burst_ms = cycles / f_hz * 1e3
    gate = (t_ms > expected_ms - GATE_BEFORE_MS) & (t_ms < expected_ms + burst_ms + RING_MS)
    if not gate.any():
        raise ValueError(f"echo window {expected_ms:.2f} ms is outside the record "
                         f"({t_ms[0]:.2f}-{t_ms[-1]:.2f} ms)")
    k = int(np.argmax(np.where(gate, env, 0)))
    if burst_ms - SETTLE_MS >= MIN_SETTLED_MS:
        settled = (t_ms > expected_ms + SETTLE_MS) & (t_ms < expected_ms + burst_ms)
        echo_amp, method = float(env[settled].mean()), 'settled mean'
    else:
        echo_amp, method = float(env[k]), 'peak'
    return {
        'echo_amp_counts': echo_amp,
        'echo_method': method,
        'echo_peak_ms': float(t_ms[k]),
        'direct_amp_counts': float(env[t_ms < t0_ms + DIRECT_MS].max()),
        'clip_fraction': analyze.clip_fraction(x),
        'pings': int(len(x)),
    }


def summarize(points):
    """Peak and -3 dB band of the echo response."""
    f = [p['f_actual_hz'] for p in points]
    lo, hi, peak, f_peak = analyze.corners_3db(f, [p['echo_amp_counts'] for p in points])
    return {'peak_hz': f_peak, 'peak_echo_amp_counts': peak, 'low_3db_hz': lo, 'high_3db_hz': hi,
            'bandwidth_3db_hz': (hi - lo) if lo is not None and hi is not None else None}


def target_range_cm(x, fs_hz, t0_ms, target_m, k=8):
    """Leading-edge range of the echo (echo_onset's k-sigma method), or None."""
    locked, _ = analyze.split_ping_locked(x)
    w = locked - locked.mean()
    t_ms = t0_ms + np.arange(len(w)) / fs_hz * 1e3
    e = hilbert_envelope(analyze.bandpass(w, fs_hz, *analyze.SONAR_BAND_HZ))
    o = onset(t_ms, e, 2 * target_m / C * 1e3, k)
    return None if o is None else o * C / 2 * 0.1


def _command(ser, name, value, timeout_s=2.0):
    """Send '<name> <value>' and return the firmware's '<name> set...' reply."""
    ser.reset_input_buffer()
    ser.write(f'{name} {int(value)}\n'.encode())
    ser.flush()
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        line = ser.readline().decode(errors='replace').strip()
        if line.startswith(f'{name} set'):
            return line
        if line.startswith(name):
            raise RuntimeError(f"firmware: {line}")
    raise RuntimeError(f"no reply to {name} - is the firmware current?")


def set_frequency(ser, f_hz):
    """Send FREQ and return the firmware's actual frequency (Hz)."""
    return float(_command(ser, 'FREQ', f_hz).rsplit('actual', 1)[1].split()[0])


def set_cycles(ser, cycles):
    """Send CYCLES (burst length) and confirm it."""
    _command(ser, 'CYCLES', cycles)


def plot(path, points, summary):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    f = np.array([p['f_actual_hz'] for p in points]) / 1e3
    echo = np.array([p['echo_amp_counts'] for p in points])
    direct = np.array([p['direct_amp_counts'] for p in points])
    fig, ax = plt.subplots(figsize=(8, 4))
    ax.plot(f, 20 * np.log10(echo / echo.max()), 'o-', label='target echo')
    ax.plot(f, 20 * np.log10(direct / direct.max()), 's--', alpha=.6, label='direct sound (near range)')
    ax.axhline(-3, c='k', lw=.6, ls=':')
    ax.axvline(summary['peak_hz'] / 1e3, c='r', lw=.8, label=f"peak {summary['peak_hz'] / 1e3:.2f} kHz")
    ax.set_xlabel('drive frequency (kHz)'); ax.set_ylabel('dB re own peak')
    ax.set_title(f'Two-way frequency response ({summary["cycles"]}-cycle burst)'); ax.grid(True, alpha=.3); ax.legend()
    fig.tight_layout(); fig.savefig(path, dpi=90); plt.close(fig)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--profile', required=True)
    ap.add_argument('--target-m', type=float, required=True, help='one-way distance to the reflector face')
    ap.add_argument('--start', type=float, default=36000)
    ap.add_argument('--stop', type=float, default=44000)
    ap.add_argument('--step', type=float, default=250)
    ap.add_argument('--pings', type=int, default=20)
    ap.add_argument('--cycles', type=int, default=10, help='burst length (firmware CYCLES); 50 for steady state')
    ap.add_argument('--reanalyze', action='store_true', help='recompute from the saved raw captures')
    args = ap.parse_args()

    prof = hw_profile.load(args.profile)
    if args.reanalyze:
        sweep = prof['tests']['freq_sweep']
        steps = [(p['f_requested_hz'], p['f_actual_hz']) for p in sweep['result']['points']]
        args.cycles = sweep['conditions'].get('cycles', 10)
        ser = None
    else:
        import capture
        steps = [(f, None) for f in np.arange(args.start, args.stop + args.step / 2, args.step)]
        ser = capture.open_sonar()
        set_cycles(ser, args.cycles)

    points = []
    try:
        for f_req, f_act in steps:
            path = hw_profile.raw_path(args.profile, f'sweep_{int(f_req)}')
            if ser is not None:
                f_act = set_frequency(ser, f_req)
                capture.capture(ser, args.pings, path)
            _, x, fs, t0 = analyze.load_capture(path)
            p = sweep_point(x, fs, t0, f_act, args.target_m, args.cycles)
            p.update({'f_requested_hz': float(f_req), 'f_actual_hz': float(f_act)})
            points.append(p)
            print(f"  {f_act:8.0f} Hz: echo {p['echo_amp_counts']:7.1f} counts at {p['echo_peak_ms']:.2f} ms, "
                  f"direct {p['direct_amp_counts']:7.1f}, clipped {p['clip_fraction']:.4f}")
    finally:
        if ser is not None:
            ser.close()

    summary = summarize(points)
    summary['cycles'] = args.cycles
    best = min(points, key=lambda p: abs(p['f_actual_hz'] - summary['peak_hz']))
    _, x, fs, t0 = analyze.load_capture(hw_profile.raw_path(args.profile, f"sweep_{int(best['f_requested_hz'])}"))
    summary['range_cm_at_peak'] = target_range_cm(x, fs, t0, args.target_m)
    result = dict(summary, points=points, _arrays={
        'sweep_f_hz': np.array([p['f_actual_hz'] for p in points]),
        'sweep_echo_counts': np.array([p['echo_amp_counts'] for p in points]),
        'sweep_direct_counts': np.array([p['direct_amp_counts'] for p in points])})
    hw_profile.store_result(args.profile, prof, 'freq_sweep', result,
                            {'target_m': args.target_m, 'pings_per_step': args.pings, 'cycles': args.cycles})
    plot(os.path.join(hw_profile.profile_dir(args.profile), 'freq_sweep.png'), points, summary)

    bw = summary['bandwidth_3db_hz']
    print(f"\nPeak {summary['peak_hz']:.0f} Hz ({summary['peak_echo_amp_counts']:.1f} counts); "
          f"-3 dB band {summary['low_3db_hz']} - {summary['high_3db_hz']} Hz"
          + (f" ({bw:.0f} Hz wide)" if bw else " (not closed inside the sweep)"))
    rng = summary['range_cm_at_peak']
    print(f"Echo leading edge at the peak frequency: "
          + (f"{rng:.1f} cm vs {args.target_m * 100:.1f} cm measured ({rng - args.target_m * 100:+.1f} cm)"
             if rng is not None else "not found"))
    worst = max(p['clip_fraction'] for p in points)
    if worst > 0:
        print(f"WARNING: up to {worst:.4f} of samples clipped - amplitudes near the peak may be compressed.")


if __name__ == '__main__':
    main()
