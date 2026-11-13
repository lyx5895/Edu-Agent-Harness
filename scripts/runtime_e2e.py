"""Real-model, API-only dialogue tests. This is not a learning-outcome study."""
import json
from pathlib import Path
import time
import uuid
import httpx

ROOT=Path(__file__).resolve().parents[1]


def main():
    report={'kind':'real_model_api_multiturn','started':time.time(),'cases':[],
            'limitations':['scripted student replies, not real learners','lexical output guard, not semantic proof',
                          'policy replay does not regenerate model output']}
    with httpx.Client(base_url='http://127.0.0.1:7860',timeout=600,trust_env=False) as c:
        def post(path,data):
            r=c.post(path,json=data);r.raise_for_status();return r.json()
        for name,reply,expected in [('slip','我知道括号里的每一项都要乘，我只是漏写了。','self_correct'),
                                    ('misconception','我认为括号外的数只乘第一项，后面的常数不需要乘。','prerequisite_review'),
                                    ('not_helped','我还是不明白，刚才的提示没有帮到我。','prerequisite_review')]:
            started=time.monotonic()
            s=post('/api/sessions',{'learner':f'runtime-eval-{name}-{uuid.uuid4().hex[:8]}','skill':'k01'})
            sid=s['session_id'];path=f'/api/sessions/{sid}'
            def act(kind,text): return post(path+'/'+kind,{'request_id':uuid.uuid4().hex,'text':text})
            wrong=act('attempt','2(x+3)=2x+3')
            follow=act('reply',reply)
            fixed=act('attempt','2x+6')
            independent=act('attempt','5x+30')
            traces=c.get(f'/api/teacher/sessions/{sid}/trace').json()
            replay_result=c.get(f'/api/teacher/sessions/{sid}/replay').json()
            case={'name':name,'session_id':sid,'expected_action':expected,
                  'actual_action':follow['teaching_action'],'feedback':follow['feedback'],
                  'wrong_action':wrong['teaching_action'], 'reply_evidence':follow['evidence'],
                  'practice_evidence':fixed['evidence'],'check_evidence':independent['evidence'],
                  'replay':replay_result,'traces':traces,'seconds':time.monotonic()-started}
            case['passed']=(case['actual_action']==expected and case['wrong_action']=='probe'
                and case['reply_evidence']=='dialogue_only' and case['practice_evidence']=='assisted_complete'
                and case['check_evidence']=='independent_check_passed' and replay_result['passed'])
            report['cases'].append(case)
            (ROOT/'reports/runtime-e2e.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
            print(name,case['actual_action'],case['passed'],round(case['seconds'],2),flush=True)
    report['passed']=all(x['passed'] for x in report['cases'])
    report['ended']=time.time()
    (ROOT/'reports/runtime-e2e.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    if not report['passed']: raise SystemExit('One or more runtime cases failed; inspect report')


if __name__=='__main__':main()
