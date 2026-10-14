"""Compare the Wiener-deconvolution branch against the plain matched filter.

Needs the local captures bench/profiles/echo-77in/raw/target.csv and
echo-100in/raw/{target,none}.csv (raw captures are gitignored) and the branch
origin/add-wiener-deconvolution (its pc/deconvolve.py is read with `git show`,
and run with *our* tone template, not the branch's chirp template).

    python bench/wiener_eval.py

Scores each method on the coherent-mean waveform against the known target
range: peak offset, -6 dB width of the echo peak, peak / std of the quiet gap
("snr"), and the 5-sigma onset offset. See bench/WIENER_EVALUATION.md.
"""

import importlib.util
import itertools
import os
import subprocess
import sys
import tempfile

import numpy as np

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
sys.path.insert(0, os.path.join(ROOT, 'pc'))
sys.path.insert(0, os.path.join(ROOT, 'bench'))
import analyze  # noqa: E402
from plot_echogram import hilbert_envelope  # noqa: E402
from plot_matched_filter import make_ping_template, matched_filter  # noqa: E402
from plot_sensor_stream import range_axis_cm  # noqa: E402

BRANCH = 'origin/add-wiener-deconvolution'
PROFILES = os.path.join(ROOT, 'bench', 'profiles')


def load_branch_deconvolve():
    src = subprocess.check_output(['git', 'show', f'{BRANCH}:pc/deconvolve.py'], cwd=ROOT, text=True)
    with tempfile.NamedTemporaryFile('w', suffix='.py', delete=False) as f:
        f.write(src)
    spec = importlib.util.spec_from_file_location('branch_deconvolve', f.name)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    os.unlink(f.name)
    return mod


dc = load_branch_deconvolve()
TPL = make_ping_template(10)


def capture(path):
    return analyze.load_capture(os.path.join(PROFILES, path))


meta77, x77, _, _ = capture('echo-77in/raw/target.csv')
_, x100, _, _ = capture('echo-100in/raw/target.csv')
_, n100, _, _ = capture('echo-100in/raw/none.csv')
R_AXIS = range_axis_cm(meta77[0], 2048)
CASES = {'77in target alone': dict(x=x77, bg=None, R=195.6, gap=(150, 175)),
         '100in target-none': dict(x=x100, bg=n100.mean(0), R=254.0, gap=(150, 225))}


def envelope(w, params):
    if params is None:
        return hilbert_envelope(matched_filter(w, TPL))
    f0, q, lam = params
    h = dc.make_impulse_response(10, q=q, f0_hz=f0)
    return hilbert_envelope(dc.deconvolved_response(w, 10, template=TPL, h=h, lam=lam))


def metrics(e, R, gap):
    r = R_AXIS
    sel = (r > R - 30) & (r < R + 100)
    i = np.where(sel)[0][np.argmax(e[sel])]
    pk = e[i]
    lo = hi = i
    while lo > 0 and e[lo] >= 0.5 * pk:
        lo -= 1
    while hi < len(e) - 1 and e[hi] >= 0.5 * pk:
        hi += 1
    g = (r >= gap[0]) & (r <= gap[1])
    mu, sd = np.median(e[g]), e[g].std()
    idx = np.where((r > R - 40) & (e > mu + 5 * sd))[0]
    return dict(peak=r[i] - R, fwhm=r[hi] - r[lo], snr=pk / max(sd, 1e-9),
                onset=(r[idx[0]] - R) if len(idx) else float('nan'))


def evaluate(params, per_ping=False):
    out = {}
    for name, c in CASES.items():
        bg = 0 if c['bg'] is None else c['bg']
        if per_ping:
            e = np.mean([envelope(v - bg, params) for v in c['x']], axis=0)
        else:
            e = envelope(c['x'].mean(0) - bg, params)
        out[name] = metrics(e, c['R'], c['gap'])
    return out


def line(label, res):
    return f'{label:30s}' + ''.join(
        f" | peak {m['peak']:+4.0f} fwhm {m['fwhm']:3.0f} snr {m['snr']:5.1f} onset {m['onset']:+4.0f}"
        for m in res.values())


def main():
    print('columns per case: ' + ' | '.join(CASES) + '  (cm offsets vs known range)')
    print('--- coherent mean ---')
    print(line('plain matched filter', evaluate(None)))
    for p in itertools.product((3300, 4400), (5, 9), (0.001, 0.003, 0.01, 0.03, 0.1)):
        print(line('wiener f0=%d Q=%d lam=%g' % p, evaluate(p)))
    print('--- average of per-ping envelopes (echogram-like) ---')
    print(line('plain matched filter', evaluate(None, per_ping=True)))
    for p in ((3300, 5, 0.01), (3300, 5, 0.03), (4400, 9, 0.01), (4400, 9, 0.03)):
        print(line('wiener f0=%d Q=%d lam=%g' % p, evaluate(p, per_ping=True)))


if __name__ == '__main__':
    main()
