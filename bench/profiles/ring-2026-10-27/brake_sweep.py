import sys, os, numpy as np, csv
sys.path.insert(0,'bench'); sys.path.insert(0,'pc')
import capture as cap
D=os.path.join(os.path.dirname(os.path.abspath(__file__)), 'raw') + '/'
ser=cap.open_sonar()
print(cap.send_command(ser,'BRAKE 2000','BRAKE set') if False else '', end='')
try: cap.send_command(ser,'BRAKE 2000','BRAKE set'); print('BRAKE 2000 accepted?!')
except RuntimeError as e: print('limit check:',e)
for b in (100,300,500,700,900):
    print(cap.send_command(ser,f'BRAKE {b}','BRAKE set'))
    p=D+f'brake{b}.csv'; cap.capture(ser,20,p,log=lambda *a:None)
    r=list(csv.reader(open(p)));rows=r[1:]
    x=np.array([list(map(float,v[6:])) for v in rows]); t0=(int(rows[0][3])-int(rows[0][2]))*1e-3
    t=t0+np.arange(x.shape[1])/200.0
    clip=(x<=5)|(x>=4090); last=[t[np.where(c)[0].max()] if c.any() else t0 for c in clip]
    m=x.mean(0); m=m-np.median(m)
    w=lambda a,b_: np.sqrt((m[(t>=a)&(t<b_)]**2).mean())
    print(f'  brake {b}: sample start {t0:.3f} ms, clipped until {np.median(last):.2f} ms ({(np.median(last)*0.1729):.0f} cm), ping-locked RMS 1.25-1.75 {w(1.25,1.75):5.0f} 1.75-2.25 {w(1.75,2.25):5.0f} 2.25-3 {w(2.25,3):5.0f} 3-5 {w(3,5):4.0f} 10-12 {w(10,12):3.0f}')
print(cap.send_command(ser,'RESET','RESET done'))
ser.close()
