"""Real OmniEdu API tests for user-supplied tasks across subjects.
Reference response for generated checks is a pipeline fixture, NOT learning evidence.
"""
import json
from pathlib import Path
import time
import uuid
import httpx
ROOT=Path(__file__).resolve().parents[1]
CASES=[
 {'name':'english','task':'将句子 She go to school every day. 改为正确的一般现在时句子，并说明修改原因。',
  'wrong':'She go to school every day. 因为每天都去所以用go。',
  'reply':'我以为主语是谁都可以直接用动词原形。',
  'correct':'She goes to school every day. 主语She是第三人称单数，一般现在时中动词要用第三人称单数形式goes。'},
 {'name':'physics','task':'一个质量为2 kg的物体，所受合外力为6 N。求其加速度并说明依据。',
  'wrong':'加速度是12 m/s²，因为把力和质量相乘。',
  'reply':'我把力等于质量乘加速度记成了加速度等于力乘质量。',
  'correct':'根据牛顿第二定律F=ma，a=F/m=6/2=3 m/s²。'},
 {'name':'biology_material','task':'根据材料说明植物光合作用需要哪些原料，释放什么气体。',
  'material':'绿色植物利用光能，把二氧化碳和水转化为储存能量的有机物，同时释放氧气。叶绿体是光合作用的重要场所。',
  'wrong':'植物吸收氧气和水，释放二氧化碳。','reply':'我把光合作用和呼吸作用混淆了。',
  'correct':'根据材料，原料是二氧化碳和水，释放的气体是氧气。'},
 {'name':'computer_science','task':'用一句话解释哈希表发生哈希冲突是什么意思。',
  'wrong':'同一个键重复查询就是哈希冲突。','reply':'我以为冲突是查询了同一个键两次。',
  'correct':'哈希冲突是不同的键经过哈希函数映射到了同一个哈希值或存储位置。'},
]

def main():
 report={'kind':'real_cross_subject_api','cases':[], 'limitations':['scripted examples, not learner study',
   'generated check reference used only to exercise grading/persistence','model rubric judgments are not deterministic proofs']}
 with httpx.Client(base_url='http://127.0.0.1:7860',timeout=600,trust_env=False) as c:
  def save(): (ROOT/'reports/open-task-e2e.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
  def post(path,body):
   r=c.post(path,json=body)
   if r.status_code!=200: raise RuntimeError(f'{path}: {r.status_code} {r.text}')
   return r.json()
  for case in CASES:
   record={'name':case['name'],'started':time.time(),'steps':[]};report['cases'].append(record);save()
   try:
    s=post('/api/sessions',{'learner':'open-eval-'+uuid.uuid4().hex[:8],'task':case['task'],
       'material':case.get('material',''),'request_id':uuid.uuid4().hex})
    sid=s['session_id'];record['session_id']=sid;record['task']=s['task']
    def act(action,text=''):
     result=post(f'/api/sessions/{sid}/{action}',{'request_id':uuid.uuid4().hex,'text':text})
     record['steps'].append({'action':action,'response':result});save();return result
    wrong=act('attempt',case['wrong']);assert wrong['status']=='incorrect',wrong
    reply=act('reply',case['reply']);assert reply['evidence']=='dialogue_only'
    corrected=act('attempt',case['correct']);assert corrected['status']=='correct',corrected
    assert corrected['evidence']=='assisted_complete'
    check=act('check');assert check['session']['task']['question']!=s['task']['question']
    traces=c.get(f'/api/teacher/sessions/{sid}/trace').json()
    generated=traces[-1]['generated_task']
    assert 'reference' not in check['session']['task']
    completed=act('attempt',generated['reference'])
    record['generated_check_fixture_status']=completed['status']
    assert completed['status']=='correct',completed
    assert completed['evidence'] in ('independent_verified','independent_model_assessed')
    replay=c.get(f'/api/teacher/sessions/{sid}/replay').json();assert replay['passed']
    record.update(passed=True,replay=replay,traces=c.get(f'/api/teacher/sessions/{sid}/trace').json())
   except Exception as e:
    record.update(passed=False,error=str(e))
   record['seconds']=time.time()-record['started'];save()
   print(case['name'],record['passed'],record.get('error',''),round(record['seconds'],1),flush=True)
 report['passed']=all(x['passed'] for x in report['cases']);save()
 if not report['passed']: raise SystemExit('Some real cross-subject cases failed; see report')
if __name__=='__main__':main()
