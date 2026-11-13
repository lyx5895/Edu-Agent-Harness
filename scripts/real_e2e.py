import json
from pathlib import Path
import time
import uuid

import httpx
from smoke_inference import image_url

ROOT=Path(__file__).resolve().parents[1]


def main():
    records=[]
    learner='验收学生-'+str(int(time.time()))
    with httpx.Client(base_url='http://127.0.0.1:7860',timeout=600,trust_env=False) as client:
        def post(path,body):
            r=client.post(path,json=body);r.raise_for_status();d=r.json()
            records.append({'path':path,'response':d})
            (ROOT/'reports/real-e2e.json').write_text(json.dumps(records,ensure_ascii=False,indent=2))
            return d
        s=post('/api/sessions',{'learner':learner,'skill':'k01'});sid=s['session_id']
        def act(kind,text=''):
            return post(f'/api/sessions/{sid}/{kind}',{'request_id':uuid.uuid4().hex,'text':text})
        assert act('attempt','2(x+3)=2x+3')['status']=='incorrect'
        act('hint')
        r=act('attempt','2x+6');assert r['evidence']=='assisted_complete'
        assert r['session']['item']['prompt']=='展开并合并：5(x+6)'
        r=act('attempt','5x+30');assert r['evidence']=='independent_check_passed'
        rep=client.get('/api/teacher/report',params={'learner':learner}).json()
        assert rep['summary'][0]['independent_check_passed']
        records.append({'teacher_report':rep})
        s=post('/api/sessions',{'learner':learner+'图片','skill':'k01'});sid=s['session_id']
        r=post(f'/api/sessions/{sid}/image',{'request_id':uuid.uuid4().hex,'image_url':image_url()})
        assert r['transcription']['text']
        assert act('confirm','2x+3')['status']=='incorrect'
        records.append({'image_confirmation':True})
        (ROOT/'reports/real-e2e.json').write_text(json.dumps(records,ensure_ascii=False,indent=2))
        print('Real text + image teaching loop passed:',learner)


if __name__=='__main__':main()
