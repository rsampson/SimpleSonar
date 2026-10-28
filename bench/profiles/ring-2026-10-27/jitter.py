import sys,os,numpy as np
sys.path.insert(0,'bench'); sys.path.insert(0,'pc')
import capture as cap
from ring_cancel import load
def offsets(x,t,f,a=1.6e-3,b=3.5e-3):
    sel=(t>=a)&(t<b); m=x.mean(0); ref=np.exp(-2j*np.pi*f*t[sel])
    zm=((m[sel]-m[sel].mean())*ref).sum(); zp=((x[:,sel]-x[:,sel].mean(1,keepdims=True))*ref).sum(1)
    return -np.angle(zp/zm)/(2*np.pi*f)*1e6
def report(p,label):
    x,t,fs=load(p); a=offsets(x,t,40e3); b=offsets(x,t,47.83e3)
    jit=x-x.mean(0); sel=(t>=1.6e-3)&(t<3.5e-3)
    print(f"{label}: timing std from 40k {a.std():.3f} us, 47.8k {b.std():.3f} us, corr {np.corrcoef(a,b)[0,1]:.2f}; per-ping residual 1.6-3.5 ms {np.sqrt((jit[:,sel]**2).mean()):.1f}, 10-20 ms {np.sqrt((jit[:,(t>10e-3)]**2).mean()):.1f}")
    return x,t,a,b
if __name__=='__main__':
    ser=cap.open_sonar(); cap.send_command(ser,'RESET','RESET done')
    p=sys.argv[1]; cap.capture(ser,int(sys.argv[2]) if len(sys.argv)>2 else 40,p,log=lambda *a:None); ser.close()
    report(p,os.path.basename(p))
