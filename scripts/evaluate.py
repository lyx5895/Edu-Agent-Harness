"""Small independent fixtures; automatic proxy metrics, not learning gains."""
import json
from pathlib import Path
import re
import sys
import time
from typing import Literal
from pydantic import BaseModel,Field

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from harness.grading.algebra import verify
from harness.legacy.course import retrieve
from harness.llm.model import ModelClient, ModelUnavailable

ROOT=Path(__file__).resolve().parents[1]


class Direct(BaseModel):
    status:Literal['correct','incorrect','incomplete','unknown']
    first_error:int|None=None
    hint:str=Field(max_length=300)


def main():
    model=ModelClient();rows=[]
    cases=[(11,7,False),(12,8,False),(13,9,False),(14,10,True),(15,11,True),(16,12,False)]
    for a,b,correct in cases:
        item={'id':f'eval-{a}','skill':'k01','prompt':f'展开并合并：{a}(x+{b})','initial':f'{a}(x+{b})',
              'answer':f'{a}x+{a*b}','kind':'expression','rubric':'每步等价，完整展开。'}
        work=f'{a}x+{a*b if correct else b}'
        expected='correct' if correct else 'incorrect'
        for mode in ('direct','harness'):
            t=time.monotonic();row={'case':item['id'],'mode':mode,'expected':expected,'work':work}
            call_start=len(model.calls)
            try:
                if mode=='direct':
                    output=model.structured(Direct,'你是初中数学辅导教师。检查学生作答，定位第一处错误并给出一句概念提示，不直接给答案。',
                        json.dumps({'question':item['prompt'],'student_work':work,'reference':item['answer'],'course':retrieve('k01')},ensure_ascii=False)).model_dump()
                else:
                    v=verify(item,work)
                    d=model.diagnose(item,work,v,retrieve('k01'))
                    hint=model.hint(retrieve('k01'))
                    if re.search(r'[0-9a-zA-Z=+*/<>]',hint):hint=retrieve('k01')['text']
                    output={'status':v['status'],'first_error':v['first_error'],'hint':hint,'diagnosis':d}
                flat=re.sub(r'\s+','',output['hint'])
                row.update({'ok':True,'output':output,'status_correct':output['status']==expected,
                    'first_error_correct':output['first_error']==(None if correct else 1),
                    'answer_string_leak':item['answer'] in flat})
            except ModelUnavailable as e:row.update({'ok':False,'error':str(e)})
            calls=model.calls[call_start:]
            row['usage']={key:sum((c.get('usage') or {}).get(key,0) for c in calls)
                for key in ('prompt_tokens','completion_tokens','total_tokens')}
            row['model_calls']=len(calls)
            row['seconds']=time.monotonic()-t;rows.append(row)
            (ROOT/'reports/comparison.json').write_text(json.dumps({'limitations':
                'Six authored holdout cases, automated status/first-error/exact-answer-string proxies only. No human rating or learning-gain claims. Local API monetary price unknown; no fabricated cost.',
                'rows':rows},ensure_ascii=False,indent=2))
            print(item['id'],mode,row['ok'],round(row['seconds'],2),flush=True)


if __name__=='__main__':main()
