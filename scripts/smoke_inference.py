"""20 real requests, including multi-turn and a synthetic homework image."""
import base64
import io
import json
import os
from pathlib import Path
import time

import httpx
from PIL import Image,ImageDraw,ImageFont

ROOT=Path(__file__).resolve().parents[1]
BASE_URL=os.getenv('OMNIEDU_SMOKE_BASE_URL','http://127.0.0.1:8000').rstrip('/')


def image_url():
    im=Image.new('RGB',(720,220),'white')
    draw=ImageDraw.Draw(im)
    font=ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',36)
    draw.text((25,30),'Expand: 2(x + 3)',fill='black',font=font)
    draw.text((25,110),'Student: 2x + 3',fill='black',font=font)
    im.save(ROOT/'reports/homework-fixture.png')
    b=io.BytesIO();im.save(b,format='PNG')
    return 'data:image/png;base64,'+base64.b64encode(b.getvalue()).decode()


def main():
    records=[]
    with httpx.Client(timeout=300,trust_env=False) as client:
        health=client.get(BASE_URL+'/health')
        health.raise_for_status()
        if health.content: assert health.json().get('ready',True)
        for i in range(20):
            if i==0:
                messages=[{'role':'user','content':'请用一句中文解释分配律。'}]
            elif i==1:
                messages=[{'role':'user','content':'2(x+3)=2x+3，对吗？'},
                    {'role':'assistant','content':'括号外的数需要乘到括号内每一项。'},
                    {'role':'user','content':'我现在改成2x+6。请检查，不要再重复提示。'}]
            elif i==2:
                messages=[{'role':'user','content':[{'type':'text','text':'请转写图中的学生作答，并指出是否漏乘。'},
                    {'type':'image_url','image_url':{'url':image_url()}}]}]
            else:
                messages=[{'role':'user','content':f'学生求解x+{i}={i+4}，请给一句简短提示，不直接给出答案。'}]
            started=time.monotonic()
            try:
                r=client.post(BASE_URL+'/v1/chat/completions',json={
                    'model':'omniedu-9b','messages':messages,'temperature':0,'max_tokens':96,
                    'chat_template_kwargs':{'enable_thinking':False}})
                r.raise_for_status();data=r.json()
                assert data['choices'][0]['message']['content'].strip()
                records.append({'index':i,'ok':True,'client_seconds':time.monotonic()-started,**data})
            except Exception as e:
                records.append({'index':i,'ok':False,'error':str(e)})
            (ROOT/'reports/deployment-smoke.json').write_text(json.dumps(records,ensure_ascii=False,indent=2))
            print(i,records[-1]['ok'],round(time.monotonic()-started,2),flush=True)
    assert all(x['ok'] for x in records)


if __name__=='__main__':main()
