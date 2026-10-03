#!/usr/bin/env python3
import argparse,json,time,urllib.request,statistics
from pathlib import Path
CORP=("Mixture-of-experts inference systems balance GPU kernels, CPU expert execution, KV state, PCIe transfers, scheduling, cache locality, queueing, failure isolation, and serving latency. "
      "This synthetic conversation is intentionally long so state restoration has measurable value. ")
def post(port,messages,max_tokens=128):
 body={'model':'bench','messages':messages,'max_tokens':max_tokens,'temperature':0,'seed':1234,'chat_template_kwargs':{'enable_thinking':False}}
 req=urllib.request.Request(f'http://127.0.0.1:{port}/v1/chat/completions',data=json.dumps(body).encode(),headers={'Content-Type':'application/json'})
 t0=time.perf_counter()
 with urllib.request.urlopen(req,timeout=900) as r: data=json.loads(r.read())
 return data,time.perf_counter()-t0
def metric(port):
 with urllib.request.urlopen(f'http://127.0.0.1:{port}/metrics',timeout=10) as r:return (json.loads(r.read()).get('requests') or [{}])[-1]
def main():
 ap=argparse.ArgumentParser(); ap.add_argument('--port',type=int,required=True); ap.add_argument('--arm',required=True); ap.add_argument('--out',required=True); ap.add_argument('--sessions',type=int,default=4); ap.add_argument('--turns',type=int,default=3); a=ap.parse_args()
 ids='ABCDEFGHIJKLMNOPQRSTUVWXYZ'[:a.sessions]
 sessions={}
 for sid in ids:
  system=f'SESSION_{sid}_UNIQUE. '+CORP*28
  sessions[sid]=[{'role':'system','content':system}]
 rows=[]; start=time.time()
 for turn in range(1,a.turns+1):
  for sid in ids:
   msgs=sessions[sid]
   msgs.append({'role':'user','content':f'Turn {turn} for session {sid}. '+CORP*3+' Summarize the engineering tradeoffs and preserve continuity with this conversation.'})
   data,e=post(a.port,msgs,128); m=metric(a.port)
   answer=data['choices'][0]['message']['content']; msgs.append({'role':'assistant','content':answer})
   offered=m.get('drafts_offered') or 0; accepted=m.get('drafts_accepted') or 0
   rows.append({'session':sid,'turn':turn,'client_s':e,'prompt_tokens':m.get('prompt_tokens'),'reused':m.get('reused'),'prompt_ms':m.get('prompt_ms'),'decode_ms':m.get('decode_ms'),'decode_tok_s':m.get('decode_tok_s'),'completion_tokens':data.get('usage',{}).get('completion_tokens'),'accept':accepted/offered if offered else None})
 out={'arm':a.arm,'bench_start_epoch':start,'bench_end_epoch':time.time(),'rows':rows}
 Path(a.out).write_text(json.dumps(out,indent=2,ensure_ascii=False)+'\n')
 for t in range(1,a.turns+1):
  rr=[x for x in rows if x['turn']==t]
  print(a.arm,'turn',t,'e2e',round(statistics.mean(x['client_s'] for x in rr),3),'prompt_ms',round(statistics.mean(x['prompt_ms'] or 0 for x in rr),1),'reused',[x['reused'] for x in rr])
if __name__=='__main__': main()
