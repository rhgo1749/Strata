#!/usr/bin/env python3
import argparse,json,statistics,sys,time,urllib.request
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'tools')]
import strata_tokenizer as ST
from serve.frontend import ChatTemplate, openai_to_messages
TOKDIR=Path('/home/gonus/projects/models/strata-qwen38-iq3-s/runtime-data/packs/iq3_s/tokenizer')
vocab=json.loads((TOKDIR/'vocab.json').read_text()); toks=[None]*len(vocab)
for t,i in vocab.items(): toks[i]=t
tok=ST.Tokenizer(toks,(TOKDIR/'merges.txt').read_text().split('\n'),json.loads((TOKDIR/'token_type.json').read_text()))
tpl=ChatTemplate(TOKDIR/'chat_template.jinja')
CORP='Analyze tradeoffs in mixture-of-experts inference involving memory bandwidth, PCIe transfers, expert routing, CUDA kernels, prompt processing, speculative decoding, cache locality, scheduling, tail latency, and failure isolation. Give concrete engineering implications. '
def count(content):
 req={'messages':[{'role':'user','content':content}],'chat_template_kwargs':{'enable_thinking':False}}
 m,t,k=openai_to_messages(req); return len(tok.encode(tpl.render(m,t,add_generation_prompt=True,**k),parse_special=True))
def make_prompt(target,nonce):
 pre=f'benchmark_nonce={nonce}.\n'; suf='\nContinue until the output limit; do not conclude early.'; lo,hi=0,max(1,target//20)
 while count(pre+CORP*hi+suf)<target: hi*=2
 best=None
 while lo<=hi:
  mid=(lo+hi)//2; c=pre+CORP*mid+suf; n=count(c)
  if best is None or abs(n-target)<abs(best[1]-target): best=(c,n)
  if n<target: lo=mid+1
  elif n>target: hi=mid-1
  else:return c,n
 return best
def post(port,content,max_tokens,session=None):
 body={'model':'bench','messages':[{'role':'user','content':content}],'max_tokens':max_tokens,'temperature':0,'seed':1234,'chat_template_kwargs':{'enable_thinking':False}}
 h={'Content-Type':'application/json'}
 if session:h['X-Strata-Session-Id']=session
 req=urllib.request.Request(f'http://127.0.0.1:{port}/v1/chat/completions',data=json.dumps(body).encode(),headers=h)
 t0=time.perf_counter()
 with urllib.request.urlopen(req,timeout=900) as r: data=json.loads(r.read())
 return data,time.perf_counter()-t0
def metric(port):
 with urllib.request.urlopen(f'http://127.0.0.1:{port}/metrics',timeout=10) as r:return (json.loads(r.read()).get('requests') or [{}])[0]
def one(port,content,max_tokens,session=None):
 data,e=post(port,content,max_tokens,session); m=metric(port)
 offered=m.get('drafts_offered') or 0; accepted=m.get('drafts_accepted') or 0
 return {'client_s':e,'completion_tokens':data['usage']['completion_tokens'],'prompt_tokens':m.get('prompt_tokens'),'reused':m.get('reused'),'prompt_ms':m.get('prompt_ms'),'pp':(m.get('prompt_tokens')/(m.get('prompt_ms')/1000)) if m.get('prompt_tokens') and m.get('prompt_ms') else None,'decode_ms':m.get('decode_ms'),'decode_tok_s':m.get('decode_tok_s'),'accept':accepted/offered if offered else None,'metric':m}
def main():
 ap=argparse.ArgumentParser(); ap.add_argument('--port',type=int,required=True); ap.add_argument('--arm',required=True); ap.add_argument('--out',required=True); ap.add_argument('--decode-only',action='store_true'); ap.add_argument('--warm-reps',type=int,default=5); a=ap.parse_args()
 out={'arm':a.arm,'port':a.port,'bench_start_epoch':time.time(),'cold':{},'warm':[]}
 if not a.decode_only:
  for target,label in [(1500,'short'),(15000,'medium')]:
   rows=[]
   for i in range(3):
    c,n=make_prompt(target,f'decode-assist-{label}-{i}')
    rows.append(one(a.port,c,16,f'{a.arm}-cold-{label}-{i}'))
   out['cold'][label]=rows
 warm,n=make_prompt(1500,'decode-assist-warm-fixed')
 one(a.port,warm,128,None); one(a.port,warm,128,None)
 for i in range(a.warm_reps): out['warm'].append(one(a.port,warm,768,None))
 out['bench_end_epoch']=time.time()
 Path(a.out).write_text(json.dumps(out,indent=2,ensure_ascii=False)+'\n')
 pp={k:statistics.mean([r['pp'] for r in v]) for k,v in out['cold'].items()}
 tg=statistics.mean([r['decode_tok_s'] for r in out['warm']]); acc=statistics.mean([r['accept'] for r in out['warm'] if r['accept'] is not None])
 print(json.dumps({'arm':a.arm,'pp':pp,'warm_tg_mean':tg,'warm_tg_sd':statistics.stdev([r['decode_tok_s'] for r in out['warm']]) if len(out['warm'])>1 else 0.0,'accept_mean':acc,'warm_client_s_mean':statistics.mean([r['client_s'] for r in out['warm']])},indent=2))
if __name__=='__main__': main()
