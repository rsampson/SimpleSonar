"""Signal-chain characterization tests (see bench/README.md).

    python bench/run_test.py --profile NAME setup --description "..." --r-series 47000 --r-shunt 47
    python bench/run_test.py --profile NAME noise
    python bench/run_test.py --profile NAME step  --vpp 5.0
    python bench/run_test.py --profile NAME sine  --vpp 5.0
    python bench/run_test.py --profile NAME linearity --freq 3300
    python bench/run_test.py --profile NAME crosstalk
    python bench/run_test.py --profile NAME reanalyze   # rerun analysis from raw/ captures
    python bench/run_test.py --profile NAME report
"""

import argparse
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import analyze  # noqa: E402
import hw_profile  # noqa: E402

DEFAULT_SINE_FREQS = [100, 200, 500, 1000, 2000, 2500, 3000, 3300, 3600, 4000, 4400, 5000, 6000,
                      7000, 8000, 10000, 15000, 20000, 30000, 40000, 45000,
                      55000, 60000, 70000, 90000, 150000]
GAIN_REF_HZ = 1000
SUGGESTED_VPP = {100: '~1 V', 1000: '~5-10 V'}   # gives ~10 mV p-p at the node


def pad_ratio(prof, tap):
    s = prof.get('setup', {})
    key = 'r_series_div100' if tap == 100 else 'r_series_div1000'
    if key not in s or 'r_shunt' not in s:
        sys.exit(f"Pad resistors not recorded - run 'setup' with --r-shunt and "
                 f"{'--r-series-div100' if tap == 100 else '--r-series'} first.")
    return s['r_shunt'] / (s[key] + s['r_shunt'])


def ask(prompt):
    try:
        return input(prompt)
    except EOFError:
        return ''


def ask_float(prompt, default=None):
    while True:
        txt = ask(prompt + (f" [{default}]" if default is not None else '') + ': ').strip()
        if not txt and default is not None:
            return float(default)
        try:
            return float(txt)
        except ValueError:
            print("  enter a number")


class Session:
    """Opens the sonar once per invocation and reuses the port."""

    def __init__(self):
        self.ser = None

    def capture(self, n, path):
        import capture as cap
        if self.ser is None:
            self.ser = cap.open_sonar()
        got = cap.capture(self.ser, n, path)
        print(f"  captured {got} pings -> {os.path.relpath(path)}")
        return got

    def close(self):
        if self.ser is not None:
            self.ser.close()


def gain_at(prof, f_hz):
    """Best available gain (counts/mV) near f_hz: stepped sine, else step response."""
    t = prof['tests']
    if 'sine' in t:
        pts = [p for p in t['sine']['result']['points'] if not p['aliased']]
        if pts:
            p = min(pts, key=lambda p: abs(np.log(p['f_generator_hz'] / f_hz)))
            return p['gain_counts_per_mv']
    if 'step' in t:
        return t['step']['result']['peak_gain_counts_per_mv']
    return None


# --- tests ---------------------------------------------------------------------

def run_noise(args, prof, sess, reanalyze=False):
    path = hw_profile.raw_path(args.profile, 'noise')
    if not reanalyze:
        print("T0 noise: mic removed, pad installed, generator OUTPUT OFF (or 0 V), speaker amp input DISCONNECTED.")
        ask("Press Enter to capture...")
        sess.capture(args.pings, path)
    _, x, fs, _ = analyze.load_capture(path)
    r = analyze.analyze_noise(x, fs)
    g = gain_at(prof, GAIN_REF_HZ)
    r['input_referred_uv_rms'] = r['rms_counts'] / g * 1000 if g else None
    hw_profile.store_result(args.profile, prof, 'noise', r, {'pings': int(len(x))})
    print(f"  noise {r['rms_counts']:.2f} counts RMS ({r['rms_counts_band_2k_6k']:.2f} in 2-6 kHz); "
          f"ping-locked {r['ping_locked_rms_counts']:.1f} counts RMS")


def run_step(args, prof, sess, reanalyze=False):
    path = hw_profile.raw_path(args.profile, 'step')
    if reanalyze:
        c = prof['tests']['step']['conditions']
        vpp, tap, freq = c['generator_vpp'], c['tap'], c['square_hz']
    else:
        freq, tap = args.freq or 50, args.tap
        print(f"T1 step: generator SQUARE, {freq:g} Hz, {SUGGESTED_VPP[tap]} p-p, into the ÷{tap} tap. Speaker amp disconnected.")
        vpp = args.vpp or ask_float("Generator p-p volts as measured on the scope (before the pad)")
        ask("Press Enter to capture...")
        sess.capture(args.pings, path)
    step_mv = vpp * pad_ratio(prof, tap) * 1000
    _, x, fs, _ = analyze.load_capture(path)
    r = analyze.analyze_step(x, fs, step_mv)
    r['clip_fraction'] = analyze.clip_fraction(x)
    hw_profile.store_result(args.profile, prof, 'step', r,
                            {'generator_vpp': vpp, 'tap': tap, 'square_hz': freq, 'input_step_mv': step_mv,
                             'pings': int(len(x))})
    print(f"  {r['edges']} edges; peak gain {r['peak_gain_counts_per_mv']:.1f} counts/mV at {r['peak_freq_hz']:.0f} Hz; "
          f"corners {r['low_corner_hz']} / {r['high_corner_hz']} Hz; clipped {r['clip_fraction']:.4f}")


def _sine_point(args, prof, sess, f, vpp, tap, path, reanalyze):
    if not reanalyze:
        sess.capture(args.pings, path)
    _, x, fs, _ = analyze.load_capture(path)
    amp_mv_pk = vpp * pad_ratio(prof, tap) * 1000 / 2
    p = analyze.analyze_sine(x, fs, f, amp_mv_pk)
    p.update({'generator_vpp': vpp, 'tap': tap, 'raw': os.path.basename(path)})
    return p


def run_sine(args, prof, sess, reanalyze=False):
    old = prof['tests'].get('sine', {}).get('result', {}).get('points', [])
    if reanalyze:
        pts = [_sine_point(args, prof, sess, p['f_generator_hz'], p['generator_vpp'], p['tap'],
                           os.path.join(hw_profile.profile_dir(args.profile), 'raw', p['raw']), True) for p in old]
    else:
        freqs = args.freqs or DEFAULT_SINE_FREQS
        tap = args.tap
        print(f"T2 stepped sine: generator SINE into the ÷{tap} tap, fixed amplitude ({SUGGESTED_VPP[tap]} p-p). Speaker amp disconnected.")
        vpp = args.vpp or ask_float("Generator p-p volts as measured on the scope (before the pad)")
        pts = {p['f_generator_hz']: p for p in old}
        for f in freqs:
            cmd = ask(f"Set generator to {f:g} Hz then Enter (s = skip, q = stop, or type a new p-p volts): ").strip().lower()
            if cmd == 'q':
                break
            if cmd == 's':
                continue
            v = float(cmd) if cmd else vpp
            path = hw_profile.raw_path(args.profile, f'sine_{int(f)}')
            p = _sine_point(args, prof, sess, float(f), v, tap, path, False)
            pts[float(f)] = p
            print(f"  {f:g} Hz -> seen at {p['f_observed_hz']:.0f} Hz: gain {p['gain_counts_per_mv']:.2f} counts/mV "
                  f"({p['gain_db_nominal']:.1f} dB nominal), THD {p['thd_pct'] if p['thd_pct'] is None else round(p['thd_pct'], 2)} %, "
                  f"clipped {p['clip_fraction']:.4f}")
            # keep progress if the session is interrupted
            hw_profile.store_result(args.profile, prof, 'sine', {'points': sorted(pts.values(), key=lambda p: p['f_generator_hz'])},
                                    {'pings_per_point': args.pings})
        pts = sorted(pts.values(), key=lambda p: p['f_generator_hz'])
    r = {'points': pts}
    good = [p for p in pts if not p['aliased']]
    if len(good) >= 3:
        lo, hi, peak, fpk = analyze.corners_3db([p['f_generator_hz'] for p in good], [p['gain_counts_per_mv'] for p in good])
        r.update({'low_corner_hz': lo, 'high_corner_hz': hi, 'peak_gain_counts_per_mv': peak, 'peak_freq_hz': fpk})
    hw_profile.store_result(args.profile, prof, 'sine', r, {'pings_per_point': args.pings})
    if len(good) >= 3:
        print(f"  sine corners {r['low_corner_hz']} / {r['high_corner_hz']} Hz, peak {r['peak_gain_counts_per_mv']:.1f} counts/mV "
              f"at {r['peak_freq_hz']:.0f} Hz")


def run_linearity(args, prof, sess, reanalyze=False):
    old = prof['tests'].get('linearity', {}).get('result', {}).get('points', [])
    f = args.freq or 3300
    if reanalyze:
        pts = [_sine_point(args, prof, sess, p['f_generator_hz'], p['generator_vpp'], p['tap'],
                           os.path.join(hw_profile.profile_dir(args.profile), 'raw', p['raw']), True) for p in old]
    else:
        print(f"T3 linearity: generator SINE {f:g} Hz. Step the level up (e.g. 1, 2, 5, 10 V p-p on the ÷1000 tap, "
              "then the ÷100 tap) until it clearly clips. Blank Enter at the volts prompt ends the run.")
        pts = list(old)
        while True:
            txt = ask("Generator p-p volts on the scope (blank = done): ").strip()
            if not txt:
                break
            vpp = float(txt)
            tap = int(ask_float("Tap (1000 or 100)", 1000))
            path = hw_profile.raw_path(args.profile, f'linearity_{len(pts):02d}')
            p = _sine_point(args, prof, sess, float(f), vpp, tap, path, False)
            pts.append(p)
            print(f"  in {p['input_mv_pk']:.2f} mV pk -> {p['output_counts_pk']:.0f} counts pk, "
                  f"gain {p['gain_counts_per_mv']:.2f}, THD {p['thd_pct']}, clipped {p['clip_fraction']:.4f}")
            hw_profile.store_result(args.profile, prof, 'linearity', {'points': pts}, {'freq_hz': f, 'pings_per_point': args.pings})
    r = {'points': pts}
    clean = [p for p in pts if p['clip_fraction'] == 0]
    if clean:
        r['max_unclipped_input_mv_pk'] = max(p['input_mv_pk'] for p in clean)
    hw_profile.store_result(args.profile, prof, 'linearity', r, {'freq_hz': f, 'pings_per_point': args.pings})


def run_crosstalk(args, prof, sess, reanalyze=False):
    path = hw_profile.raw_path(args.profile, 'crosstalk')
    if not reanalyze:
        print("T4 crosstalk: mic removed, pad installed, generator OFF, speaker amp input RECONNECTED (normal pinging).")
        ask("Press Enter to capture...")
        sess.capture(args.pings, path)
    _, x, fs, t0 = analyze.load_capture(path)
    floor = prof['tests'].get('noise', {}).get('result', {}).get('rms_counts_band_2k_6k')
    r = analyze.analyze_crosstalk(x, fs, t0, noise_band_rms=floor)
    hw_profile.store_result(args.profile, prof, 'crosstalk', r, {'pings': int(len(x)), 'floor_from_T0': floor is not None})
    print(f"  ping-locked {r['rms_counts']:.1f} counts RMS ({r['rms_counts_below_300hz']:.1f} <300 Hz, "
          f"{r['rms_counts_in_band']:.1f} in band); in-band settles {r['in_band_below_floor_after_ms']} ms after burst")


TESTS = {'noise': run_noise, 'step': run_step, 'sine': run_sine, 'linearity': run_linearity, 'crosstalk': run_crosstalk}


def main():
    ap = argparse.ArgumentParser(description='Signal-chain characterization bench')
    ap.add_argument('--profile', required=True, help='profile name, e.g. ad828-electret-2026-10')
    ap.add_argument('command', choices=['setup', *TESTS, 'reanalyze', 'report'])
    ap.add_argument('--pings', type=int, default=None, help='pings per capture (default 60; 20 per sine point)')
    ap.add_argument('--vpp', type=float, help='generator p-p volts measured on the scope (before the pad)')
    ap.add_argument('--tap', type=int, default=1000, choices=[1000, 100], help='pad tap used')
    ap.add_argument('--freq', type=float, help='square frequency (step) or sine frequency (linearity)')
    ap.add_argument('--freqs', type=lambda s: [float(v) for v in s.split(',')], help='comma list for sine')
    ap.add_argument('--description', help='setup: free-text hardware description')
    ap.add_argument('--r-series', type=float, help='setup: measured ÷1000 series resistor (ohms)')
    ap.add_argument('--r-series-div100', type=float, help='setup: measured ÷100 series resistor (ohms)')
    ap.add_argument('--r-shunt', type=float, help='setup: measured shunt resistor (ohms)')
    args = ap.parse_args()
    if args.pings is None:
        args.pings = 20 if args.command in ('sine', 'linearity') else 60

    prof = hw_profile.load(args.profile)
    if args.command == 'setup':
        if args.description:
            prof['hardware']['description'] = args.description
        for k, v in (('r_series_div1000', args.r_series), ('r_series_div100', args.r_series_div100),
                     ('r_shunt', args.r_shunt)):
            if v is not None:
                prof['setup'][k] = v
        if 'r_shunt' in prof['setup'] and 'r_series_div1000' in prof['setup']:
            prof['setup']['pad_ratio_div1000'] = pad_ratio(prof, 1000)
        if 'r_shunt' in prof['setup'] and 'r_series_div100' in prof['setup']:
            prof['setup']['pad_ratio_div100'] = pad_ratio(prof, 100)
        hw_profile.save(args.profile, prof)
        print(f"Profile {args.profile}: {prof['hardware']} {prof['setup']}")
        return
    if args.command == 'report':
        print(hw_profile.render_report(args.profile))
        return

    sess = Session()
    try:
        if args.command == 'reanalyze':
            # step/sine first so noise gets its input-referred figure
            for name in ('step', 'sine', 'linearity', 'noise', 'crosstalk'):
                if name in prof['tests']:
                    print(f"reanalyzing {name}")
                    TESTS[name](args, prof, sess, reanalyze=True)
        else:
            TESTS[args.command](args, prof, sess)
    finally:
        sess.close()
    print(hw_profile.render_report(args.profile))


if __name__ == '__main__':
    main()
