"""Does the ESP32's Wi-Fi add noise to the receive chain? Wi-Fi off/on A/B test.

    python bench/wifi_ab.py --profile wifi-YYYY-MM-DD
    python bench/wifi_ab.py --profile wifi-YYYY-MM-DD --reanalyze

Over the USB serial port, alternates WIFI OFF / WIFI ON (--rounds times) and
captures --pings pings in each state. While Wi-Fi is on, a client must be
joined to the open "SimpleSonar" network and reading the TCP bridge, so the
radio carries the real traffic (each 8 KB packet), not just beacons - e.g.
on the tablet, in Termux: `nc 192.168.4.1 3333 > /dev/null`. The script waits
for that client (WIFI STATUS) before each "on" capture. Android often won't
rejoin a network without internet on its own once it has dropped, so rejoin
the tablet by hand each time SimpleSonar reappears (once per round).

Each capture is split into its ping-locked mean and the residual (what
doesn't repeat ping to ping), as in analyze.py. Inside the sonar band the
residual is dominated by echoes varying with the room (air movement,
people), so the interference checks use the residual outside it (OOB_BANDS):
its RMS, its spurs, and spikes - pings whose largest out-of-band sample
exceeds SPIKE_FACTOR x the Wi-Fi-off captures' median per-ping peak (Wi-Fi
transmit bursts would land in some pings, not all; every ping has a peak of
its own, so a fixed sigma threshold flags them all). The in-band and
ping-locked levels are reported too, but they track the scene. Raw captures go to
profiles/NAME/raw/wifi_<state>_<round>.csv; results to profile.json,
response.npz and wifi_ab.png. Wi-Fi is left off at the end.
"""

import argparse
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import analyze  # noqa: E402
import hw_profile  # noqa: E402
from capture import send_command  # noqa: E402

OOB_BANDS = ((500, 30000), (50000, None))   # Hz; None = just below Nyquist
SPIKE_FACTOR = 1.5         # a ping's out-of-band peak above this x the off median is a spike
CLIENT_WAIT_S = 600        # default wait for the bridge client after WIFI ON (--client-wait)
SETTLE_S = 2.0             # after switching Wi-Fi, before capturing
SPUR_MATCH_HZ = 100.0      # spurs closer than this count as the same spur


def out_of_band(res, fs_hz):
    """res with the sonar band (and DC drift) removed, per ping."""
    return np.array([sum(analyze.bandpass(v, fs_hz, lo, hi if hi else 0.495 * fs_hz)
                         for lo, hi in OOB_BANDS) for v in res])


def state_metrics(x, fs_hz):
    """Noise figures for one capture (x: n_pings x n_samples raw counts)."""
    r = analyze.analyze_noise(x, fs_hz)
    _, res = analyze.split_ping_locked(x)
    oob = out_of_band(res, fs_hz)
    lo, hi = analyze.SONAR_BAND_HZ
    r.update({
        'oob_rms_counts': float(oob.std()),
        'oob_peaks_counts': [float(v) for v in np.abs(oob).max(axis=1)],
        # analyze_noise's spurs, minus the sonar band (+/-5 kHz), where echoes vary
        'oob_spurs': [sp for sp in r['spurs'] if not lo - 5000 <= sp['hz'] <= hi + 5000],
    })
    return r


def compare(off, on):
    """Summary of 'on' against 'off' (lists of state_metrics results)."""
    def mean(rs, key):
        return float(np.mean([r[key] for r in rs]))

    def db(a, b):
        return float(20 * np.log10(a / b)) if a > 0 and b > 0 else None

    off_spurs = [s['hz'] for r in off for s in r['oob_spurs']]
    new_spurs = sorted({round(s['hz']) for r in on for s in r['oob_spurs']
                        if all(abs(s['hz'] - f) > SPUR_MATCH_HZ for f in off_spurs)})
    off_peaks = np.concatenate([r['oob_peaks_counts'] for r in off])
    threshold = SPIKE_FACTOR * float(np.median(off_peaks))
    out = {'pings_off': sum(r['pings'] for r in off), 'pings_on': sum(r['pings'] for r in on),
           'spike_threshold_counts': threshold,
           'spike_pings_off': int(sum((np.array(r['oob_peaks_counts']) > threshold).sum() for r in off)),
           'spike_pings_on': int(sum((np.array(r['oob_peaks_counts']) > threshold).sum() for r in on)),
           'oob_peak_max_off': float(off_peaks.max()),
           'oob_peak_max_on': float(max(max(r['oob_peaks_counts']) for r in on)),
           'new_spurs_hz': new_spurs}
    for key in ('oob_rms_counts', 'rms_counts', 'rms_counts_in_band', 'ping_locked_rms_counts'):
        out[f'{key}_off'] = mean(off, key)
        out[f'{key}_on'] = mean(on, key)
        out[f'{key}_change_db'] = db(out[f'{key}_on'], out[f'{key}_off'])
    return out


def wait_for_client(ser, timeout_s=CLIENT_WAIT_S):
    """Poll WIFI STATUS until a bridge client is connected; return the status line."""
    deadline = time.monotonic() + timeout_s
    line = ''
    while time.monotonic() < deadline:
        line = send_command(ser, 'WIFI STATUS', 'WIFI status')
        if 'client connected' in line:
            return line
        time.sleep(2)
    raise RuntimeError(f"no bridge client after {timeout_s} s ({line}). Join the SimpleSonar "
                       "network and run e.g. `nc 192.168.4.1 3333 > /dev/null` on the tablet.")


def plot(path, off_arrays, on_arrays, f):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(9, 4))
    ax.semilogy(f / 1e3, np.mean(off_arrays, axis=0), label='Wi-Fi off', lw=.8)
    ax.semilogy(f / 1e3, np.mean(on_arrays, axis=0), label='Wi-Fi on + traffic', lw=.8, alpha=.8)
    lo, hi = analyze.SONAR_BAND_HZ
    ax.axvspan(lo / 1e3, hi / 1e3, color='k', alpha=.08, label='sonar band')
    ax.set_xlabel('kHz'); ax.set_ylabel('counts/√Hz'); ax.set_title('Residual noise spectrum')
    ax.grid(True, which='both', alpha=.3); ax.legend()
    fig.tight_layout(); fig.savefig(path, dpi=90); plt.close(fig)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--profile', required=True)
    ap.add_argument('--pings', type=int, default=60)
    ap.add_argument('--rounds', type=int, default=2, help='off/on pairs')
    ap.add_argument('--client-wait', type=float, default=CLIENT_WAIT_S,
                    help='seconds to wait for the bridge client after each WIFI ON')
    ap.add_argument('--reanalyze', action='store_true', help='recompute from the saved raw captures')
    args = ap.parse_args()

    prof = hw_profile.load(args.profile)
    if args.reanalyze:
        runs = prof['tests']['wifi_ab']['conditions']['runs']
    else:
        import capture
        runs = []
        ser = capture.open_sonar()
        try:
            for rnd in range(args.rounds):
                for state in ('off', 'on'):
                    print(f"round {rnd + 1}: {send_command(ser, f'WIFI {state.upper()}', f'WIFI {state}')}")
                    if state == 'on':
                        print("  waiting for the tablet: rejoin SimpleSonar and keep the nc loop running")
                        print(f"  {wait_for_client(ser, args.client_wait)}")
                    time.sleep(SETTLE_S)
                    name = f'wifi_{state}_{rnd}'
                    capture.capture(ser, args.pings, hw_profile.raw_path(args.profile, name))
                    runs.append({'state': state, 'round': rnd, 'raw': name})
        finally:
            print(send_command(ser, 'WIFI OFF', 'WIFI off'))
            ser.close()

    results = {'off': [], 'on': []}
    asd = {'off': [], 'on': []}
    f = None
    for run in runs:
        _, x, fs, _ = analyze.load_capture(hw_profile.raw_path(args.profile, run['raw']))
        m = state_metrics(x, fs)
        f, a = m['_arrays']['noise_f'], m['_arrays']['noise_asd']
        asd[run['state']].append(a)
        results[run['state']].append(m)
        print(f"  {run['raw']:12s}: out-of-band {m['oob_rms_counts']:.2f} counts RMS, peak per ping "
              f"{np.median(m['oob_peaks_counts']):.1f} median / {max(m['oob_peaks_counts']):.1f} worst; "
              f"in band {m['rms_counts_in_band']:.2f}, ping-locked {m['ping_locked_rms_counts']:.1f} (scene)")

    summary = compare(results['off'], results['on'])
    for rs in results.values():
        for m in rs:
            m.pop('_arrays', None)
    result = dict(summary, per_capture=results, _arrays={
        'wifi_f': f, 'wifi_asd_off': np.mean(asd['off'], axis=0), 'wifi_asd_on': np.mean(asd['on'], axis=0)})
    hw_profile.store_result(args.profile, prof, 'wifi_ab', result,
                            {'pings': args.pings, 'runs': runs})
    plot(os.path.join(hw_profile.profile_dir(args.profile), 'wifi_ab.png'), asd['off'], asd['on'], f)

    def fmt(v):
        return 'n/a' if v is None else f'{v:+.2f} dB'
    print(f"\nWi-Fi on vs off, out of band: noise {fmt(summary['oob_rms_counts_change_db'])}; "
          f"worst peak {summary['oob_peak_max_off']:.1f} off / {summary['oob_peak_max_on']:.1f} on")
    print(f"Spike pings (out-of-band peak > {summary['spike_threshold_counts']:.1f} counts): "
          f"{summary['spike_pings_off']}/{summary['pings_off']} off, "
          f"{summary['spike_pings_on']}/{summary['pings_on']} on")
    print("New out-of-band spurs with Wi-Fi on: " + (', '.join(f'{s} Hz' for s in summary['new_spurs_hz']) or 'none'))
    print(f"Scene (not interference): in band {fmt(summary['rms_counts_in_band_change_db'])}, "
          f"ping-locked {fmt(summary['ping_locked_rms_counts_change_db'])}")


if __name__ == '__main__':
    main()
