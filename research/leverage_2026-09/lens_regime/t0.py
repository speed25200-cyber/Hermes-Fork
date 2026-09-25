import sys, time
sys.path.insert(0, '/tmp/claude-0/-home-user/f7826394-e5e3-5463-a537-6672931e9cc4/scratchpad/newlisting')
from sim import *
t=time.time(); Dt=Data('hybrid'); print('load',time.time()-t, Dt.o.shape)
cfg=dict(d0=72,d1=168,side=-1,beta=1.0,stop=0.5,uni='newtok',K=5)
t=time.time(); a=simulate(Dt,cfg,IS_START,OOS_START); b=simulate(Dt,cfg,OOS_START,OOS_END); print('sim',time.time()-t)
print(summarize(a)); print(summarize(b))
print('newtok',Dt.newtok.sum(),'events',len(Dt.newtok))
