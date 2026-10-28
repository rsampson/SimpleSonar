import sys, os, csv, numpy as np
sys.path.insert(0,'bench'); sys.path.insert(0,'pc')
import capture as cap, analyze
from plot_echogram import hilbert_envelope
D=os.path.join(os.path.dirname(os.path.abspath(__file__)), 'raw') + '/'
fs=200e3
ser=cap.open_sonar()
print(cap.send_command(ser,'RESET','RESET done'))
res={}
for n in (10,15,20,25,10):
    print(cap.send_command(ser,f'CYCLES {n}','CYCLES set'))
    p=D+f'cyc{n}.csv'; cap.capture(ser,25,p,log=lambda *a:None)
    r=list(csv.reader(open(p)));rows=r[1:]
    x=np.array([list(map(float,v[6:])) for v in rows]); t0=(int(rows[0][3])-int(rows[0][2]))*1e-3
    t=t0+np.arange(x.shape[1])/fs*1e3; m=x.mean(0)-x.mean()
    clip=((x<=5)|(x>=4090)).any(1).mean()
    e40=hilbert_envelope(analyze.bandpass(m,fs,38e3,42.5e3)); e46=hilbert_envelope(analyze.bandpass(m,fs,44.5e3,47.5e3))
    rms=lambda e,a,b: np.sqrt((e[(t>=a)&(t<b)]**2).mean())
    noise=np.sqrt(((x-x.mean(0))**2).mean())
    print(f'  {n:2d} cyc: t0 {t0:.3f} ms, pings clipped {clip:.0%} | 40k env 1.3-2 ms {rms(e40,1.3,2):5.0f}, 2-3 {rms(e40,2,3):4.0f} | 46k env 2-3 ms {rms(e46,2,3):4.0f}, 3-6 {rms(e46,3,6):4.0f}, 8-12 {rms(e46,8,12):3.0f} | per-ping noise {noise:4.1f}')
print(cap.send_command(ser,'RESET','RESET done'))
ser.close()
