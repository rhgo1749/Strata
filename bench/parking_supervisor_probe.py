#!/usr/bin/env python3
import argparse,concurrent.futures,json,statistics,time,urllib.request
from pathlib import Path
CORP=("Independent GPU serving lanes preserve failure isolation, session affinity, shared host expert memory, bounded admission, queueing, KV locality, and predictable latency. "
      "Conversation parking stores same-lane running state in host RAM so an alternating session can resume without rereading its full prefix. ")
def post(port,sid,msgs,max_tokens=96):
 b={'model':'bench','messages':msgs,'max_tokens':max_tokens,'temperature':0,'seed':1234,'chat_template_kwargs':{'enable_thinking':False}}
 req=urllib.request.Request(f'http://127.0.0.1:{port}/v1/chat/completions',data=json.dumps(b).encode(),headers={'Content-Type':'application/json','X-Strata-Session-Id':sid})
 t=time.perf_counter()
 with urllib.request.urlopen(req,timeout=900) as r:
  h=dict(r.headers); d=json.loads(r.read())
 return d,time.perf_counter()-t,float(h.get('X-Strata-Queue-Wait-Ms','nan')),h.get('X-Strata-Lane')
def main():
 ap=argparse.ArgumentParser(); ap.add_argument('--port',type=int,required=True); ap.add_argument('--arm',required=True); ap.add_argument('--out',required=True); a=ap.parse_args()
 out={'arm':a.arm,'bench_start_epoch':time.time(),'scenarios':{}}
 for n in (4,6,9):
  ids=[f'{a.arm}-n{n}-s{i}' for i in range(n)]; sess={}
  for i,sid in enumerate(ids): sess[sid]=[{'role':'system','content':f'UNIQUE_{sid}. '+CORP*20}]
  turns=[]
  for turn in (1,2):
   def one(sid):
    m=sess[sid]; m.append({'role':'user','content':f'Turn {turn}. '+CORP*2+' Give a concise continuity-aware systems analysis.'})
    d,e,q,l=post(a.port,sid,m); ans=d['choices'][0]['message']['content']; m.append({'role':'assistant','content':ans})
    return {'sid':sid,'client_s':e,'queue_ms':q,'lane':l,'prompt_tokens':d.get('usage',{}).get('prompt_tokens'),'completion_tokens':d.get('usage',{}).get('completion_tokens')}
   t0=time.perf_counter()
   with concurrent.futures.ThreadPoolExecutor(max_workers=n) as ex: rows=list(ex.map(one,ids))
   wall=time.perf_counter()-t0; toks=sum(x['completion_tokens'] or 0 for x in rows)
   turns.append({'turn':turn,'wall_s':wall,'aggregate_completion_tps':toks/wall,'rows':rows})
  out['scenarios'][str(n)]={'turns':turns}
 out['bench_end_epoch']=time.time(); Path(a.out).write_text(json.dumps(out,indent=2)+'\n')
 for n,v in out['scenarios'].items():
  for t in v['turns']:
   print(a.arm,'n',n,'turn',t['turn'],'wall',round(t['wall_s'],3),'e2e',round(statistics.mean(x['client_s'] for x in t['rows']),3),'q95',round(sorted(x['queue_ms'] for x in t['rows'])[max(0,int(.95*len(t['rows']))-1)],1),'tps',round(t['aggregate_completion_tps'],2),'lanes',[x['lane'] for x in t['rows']])
if __name__=='__main__': main()
