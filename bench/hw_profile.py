"""Hardware profiles: bench/profiles/<name>/{profile.json, response.npz, raw/, report.md, *.png}."""

import datetime
import json
import os
import subprocess

import numpy as np

PROFILES_DIR = os.environ.get('BENCH_PROFILES_DIR',
                              os.path.join(os.path.dirname(os.path.abspath(__file__)), 'profiles'))


def profile_dir(name):
    return os.path.join(PROFILES_DIR, name)


def raw_path(name, test):
    return os.path.join(profile_dir(name), 'raw', f'{test}.csv')


def _git_commit():
    try:
        return subprocess.check_output(['git', 'rev-parse', '--short', 'HEAD'],
                                       cwd=os.path.dirname(os.path.abspath(__file__)),
                                       text=True, stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def load(name):
    path = os.path.join(profile_dir(name), 'profile.json')
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {'name': name, 'created': datetime.datetime.now().isoformat(timespec='seconds'),
            'hardware': {}, 'setup': {}, 'tests': {}}


def save(name, prof):
    os.makedirs(profile_dir(name), exist_ok=True)
    prof['updated'] = datetime.datetime.now().isoformat(timespec='seconds')
    prof['git_commit'] = _git_commit()
    with open(os.path.join(profile_dir(name), 'profile.json'), 'w') as f:
        json.dump(prof, f, indent=2)


def store_result(name, prof, test, result, conditions):
    """Record one test's scalar results in the profile and its arrays in response.npz."""
    arrays = result.pop('_arrays', {})
    prof['tests'][test] = {'conditions': conditions, 'result': result,
                           'run_at': datetime.datetime.now().isoformat(timespec='seconds')}
    if arrays:
        npz = os.path.join(profile_dir(name), 'response.npz')
        existing = dict(np.load(npz)) if os.path.exists(npz) else {}
        existing.update(arrays)
        os.makedirs(profile_dir(name), exist_ok=True)
        np.savez(npz, **existing)
    save(name, prof)


def _fmt(v, nd=3):
    if v is None:
        return 'n/a'
    if isinstance(v, float):
        return f'{v:.{nd}g}'
    return str(v)


def render_report(name):
    """Regenerate report.md and plots from profile.json + response.npz."""
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt

    prof = load(name)
    d = profile_dir(name)
    npz_path = os.path.join(d, 'response.npz')
    arr = dict(np.load(npz_path)) if os.path.exists(npz_path) else {}
    tests = prof.get('tests', {})
    lines = [f"# Signal-chain profile: {name}", '',
             f"Updated {prof.get('updated')} · git {prof.get('git_commit')}", '']
    if prof.get('hardware'):
        lines += ['## Hardware', ''] + [f"- **{k}:** {v}" for k, v in prof['hardware'].items()] + ['']
    if prof.get('setup'):
        lines += ['## Setup', ''] + [f"- **{k}:** {v}" for k, v in prof['setup'].items()] + ['']

    if 'noise' in tests:
        r = tests['noise']['result']
        lines += ['## T0 Noise (input terminated)', '',
                  f"- Per-ping noise: {_fmt(r['rms_counts'])} counts RMS ({_fmt(r['rms_counts_band_2k_6k'])} in 2-6 kHz)",
                  f"- Ping-locked (burst-synchronous) signal: {_fmt(r['ping_locked_rms_counts'])} counts RMS",
                  f"- Floor: {_fmt(r['floor_asd_counts_per_rthz'])} counts/√Hz; DC {_fmt(r['dc_counts'], 5)} counts"]
        if r.get('input_referred_uv_rms') is not None:
            lines.append(f"- Input-referred: {_fmt(r['input_referred_uv_rms'])} µV RMS (using gain at the reference frequency)")
        if r['spurs']:
            lines.append('- Spurs: ' + ', '.join(f"{s['hz']:.0f} Hz" for s in r['spurs']))
        lines.append('')
        if 'noise_f' in arr:
            fig, ax = plt.subplots(figsize=(8, 3.5))
            ax.semilogy(arr['noise_f'], arr['noise_asd'])
            ax.set_xlabel('Hz'); ax.set_ylabel('counts/√Hz'); ax.set_title('T0 noise spectrum')
            fig.tight_layout(); fig.savefig(os.path.join(d, 'noise.png'), dpi=90); plt.close(fig)
            lines += ['![noise](noise.png)', '']

    if 'step' in tests:
        r = tests['step']['result']
        lines += ['## T1 Step response', '',
                  f"- {r['edges']} edges averaged; peak gain {_fmt(r['peak_gain_counts_per_mv'])} counts/mV "
                  f"(≈{_fmt(r['peak_gain_db_nominal'])} dB at the nominal ADC scale) at {_fmt(r['peak_freq_hz'])} Hz",
                  f"- −3 dB corners: low {_fmt(r['low_corner_hz'])} Hz, high {_fmt(r['high_corner_hz'])} Hz "
                  f"(frequency resolution {_fmt(r['freq_resolution_hz'])} Hz, so the low corner is coarse)",
                  f"- Rise 10-90%: {_fmt(r['rise_10_90_us'])} µs; settles to 2% {_fmt(r['settle_2pct_ms_after_peak'])} ms after peak", '']
        if 'step_t_ms' in arr:
            fig, ax = plt.subplots(1, 2, figsize=(11, 3.8))
            ax[0].plot(arr['step_t_ms'], arr['step_counts_per_mv'])
            ax[0].set_xlabel('ms from edge'); ax[0].set_ylabel('counts per mV step'); ax[0].set_title('Step response')
            ax[1].semilogx(arr['resp_f'], 20 * np.log10(np.maximum(arr['resp_mag'], 1e-9)), label='from step')
            if 'sine' in tests:
                pts = tests['sine']['result']['points']
                ax[1].semilogx([p['f_generator_hz'] for p in pts],
                               [20 * np.log10(max(p['gain_counts_per_mv'], 1e-9)) for p in pts], 'o', label='stepped sine')
            ax[1].set_xlabel('Hz'); ax[1].set_ylabel('dB re 1 count/mV'); ax[1].set_title('Frequency response')
            ax[1].legend(); ax[1].grid(True, which='both', alpha=.3)
            fig.tight_layout(); fig.savefig(os.path.join(d, 'step_response.png'), dpi=90); plt.close(fig)
            lines += ['![step](step_response.png)', '']

    if 'sine' in tests:
        pts = tests['sine']['result']['points']
        lines += ['## T2 Stepped sine', '', '| f gen (Hz) | f seen (Hz) | gain (counts/mV) | dB nominal | THD % | clipped |',
                  '|---|---|---|---|---|---|']
        for p in sorted(pts, key=lambda p: p['f_generator_hz']):
            lines.append(f"| {p['f_generator_hz']:.0f} | {p['f_observed_hz']:.0f}{' (alias)' if p['aliased'] else ''} | "
                         f"{_fmt(p['gain_counts_per_mv'])} | {_fmt(p['gain_db_nominal'])} | {_fmt(p['thd_pct'])} | "
                         f"{_fmt(p['clip_fraction'])} |")
        lines.append('')

    if 'linearity' in tests:
        pts = tests['linearity']['result']['points']
        lines += ['## T3 Linearity', '', '| input mV pk | output counts pk | gain (counts/mV) | THD % | clipped |',
                  '|---|---|---|---|---|']
        for p in sorted(pts, key=lambda p: p['input_mv_pk']):
            lines.append(f"| {_fmt(p['input_mv_pk'])} | {_fmt(p['output_counts_pk'])} | {_fmt(p['gain_counts_per_mv'])} | "
                         f"{_fmt(p['thd_pct'])} | {_fmt(p['clip_fraction'])} |")
        lines.append('')

    if 'crosstalk' in tests:
        r = tests['crosstalk']['result']
        lines += ['## T4 Burst crosstalk (input terminated, amp connected)', '',
                  f"- Ping-locked signal: {_fmt(r['rms_counts'])} counts RMS ({_fmt(r['rms_counts_below_300hz'])} below 300 Hz, "
                  f"{_fmt(r['rms_counts_in_band'])} in {r['band_hz'][0]}-{r['band_hz'][1]} Hz)",
                  f"- In-band crosstalk falls below 3× the per-ping noise {_fmt(r['in_band_below_floor_after_ms'])} ms after the burst "
                  f"(first sample at {_fmt(r['first_sample_ms_after_burst'])} ms)", '']
        if 'xtalk_t_ms' in arr:
            fig, ax = plt.subplots(figsize=(8, 3.5))
            ax.plot(arr['xtalk_t_ms'], arr['xtalk_mean'], lw=.7, label='ping-locked mean')
            ax.plot(arr['xtalk_t_ms'], arr['xtalk_env'], 'r', label='in-band envelope')
            ax.set_xlabel('ms after burst start'); ax.set_ylabel('counts'); ax.legend(); ax.set_title('T4 crosstalk')
            fig.tight_layout(); fig.savefig(os.path.join(d, 'crosstalk.png'), dpi=90); plt.close(fig)
            lines += ['![crosstalk](crosstalk.png)', '']

    with open(os.path.join(d, 'report.md'), 'w') as f:
        f.write('\n'.join(lines) + '\n')
    return os.path.join(d, 'report.md')
