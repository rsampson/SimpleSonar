"""Can the matched filter / Wiener deconvolution separate two targets?

Needs bench/profiles/two-target/raw/{none1,none2,A_sq,B_sq,AB_sq}.csv (gitignored).
Targets A=77 in and B=93 in (40.6 cm apart in range), side by side. Each
capture has the back-to-back no-target mean subtracted; the envelope is then
formed by the plain matched filter and by Wiener deconvolution (the branch's
deconvolve.py, our template). Reports peaks near each target, the valley
between them and whether a second peak shows up.

    python bench/two_target_eval.py
"""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import wiener_eval as we  # noqa: E402

RAW = os.path.join(we.PROFILES, 'two-target', 'raw')
RA, RB = 77 * 2.54, 93 * 2.54


def load(name):
    return we.analyze.load_capture(os.path.join(RAW, name + '.csv'))[1]


def local_peaks(e, r, lo, hi, rel=0.25):
    sel = (r > lo) & (r < hi)
    idx = np.where(sel)[0]
    pk = []
    for i in idx[1:-1]:
        if e[i] > e[i - 1] and e[i] >= e[i + 1] and e[i] > rel * e[idx].max():
            pk.append(i)
    return pk


def analyse(e, r, label):
    pks = local_peaks(e, r, RA - 20, RB + 60)
    s = f'{label:28s} peaks(cm vs A): ' + ', '.join(f'{r[i]-RA:+.0f}({e[i]/e.max():.2f})' for i in pks)
    if len(pks) >= 2:
        a, b = pks[0], pks[-1]
        v = e[a:b + 1].min()
        s += f'  valley {20*np.log10(v/min(e[a], e[b])):+.1f} dB'
    return s


def main():
    n1, n2 = load('none1'), load('none2')
    print(f'DC: none1 {n1.mean():.1f} none2 {n2.mean():.1f}, ' + ', '.join(f'{n} {load(n).mean():.1f}' for n in ('A_sq', 'B_sq', 'AB_sq')))
    bg = 0.5 * (n1.mean(0) + n2.mean(0))
    r = we.R_AXIS
    methods = [('matched filter', None)] + [(f'wiener lam={l}', (3300, 5, l)) for l in (0.003, 0.01, 0.03)]
    env = {}
    for n in ('A_sq', 'B_sq', 'AB_sq'):
        w = load(n).mean(0) - bg
        env[n] = {m: we.envelope(w, p) for m, p in methods}
    for m, _ in methods:
        print(f'--- {m}')
        for n in ('A_sq', 'B_sq', 'AB_sq'):
            print('  ' + analyse(env[n][m], r, n))
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        fig, ax = plt.subplots(len(methods), 1, figsize=(10, 9), sharex=True)
        for a, (m, _) in zip(ax, methods):
            for n in ('A_sq', 'B_sq', 'AB_sq'):
                a.plot(r, env[n][m] / env['AB_sq'][m].max(), label=n)
            a.axvline(RA, c='k', ls=':'); a.axvline(RB, c='k', ls=':')
            a.set_ylabel(m); a.set_xlim(RA - 60, RB + 100)
        ax[0].legend(); ax[-1].set_xlabel('one-way range (cm)')
        out = os.path.join(we.PROFILES, 'two-target', 'two_target.png')
        fig.savefig(out, dpi=110, bbox_inches='tight'); print('saved', out)
    except Exception as e:
        print('no plot:', e)


if __name__ == '__main__':
    main()
