"""Optional API-only classroom mode. Local Gradio remains a trusted demo.
Accounts JSON: {opaque_token: {role: teacher|student, class_id: ..., learner: ...}}.
Fail closed when configured; Gradio callbacks are disabled to prevent API bypass.
"""
import hmac
import json
import os
from urllib.parse import unquote
from pathlib import Path
from starlette.responses import JSONResponse


async def authorize(request, call_next, gateway):
    filename=os.getenv('HARNESS_ACCOUNTS_FILE')
    if not filename: return await call_next(request)
    path=unquote(request.url.path)
    if path=='/health': return JSONResponse({'ready':True,'mode':'classroom-api'})
    if not path.startswith('/api/'):
        return JSONResponse({'detail':'班级模式仅开放认证API；本地演示界面已禁用。'},403)
    try:
        accounts=json.loads(Path(filename).read_text())
        token=request.headers.get('authorization','').removeprefix('Bearer ')
        account=next((v for k,v in accounts.items() if hmac.compare_digest(k,token)),None)
    except (OSError,ValueError,TypeError):
        return JSONResponse({'detail':'认证配置不可用'},503)
    if not token or not account or account.get('role') not in ('teacher','student') or not account.get('class_id'):
        return JSONResponse({'detail':'需要有效账号'},401)
    role=account['role'];cls=account['class_id'];learner=account.get('learner')
    allowed=False
    try:
        if path in ('/api/teacher/packs','/api/teacher/packs/generate') and request.method=='POST':
            body=await request.json()
            allowed=role=='teacher' and body.get('class_id')==cls
        elif path=='/api/teacher/packs' and request.method=='GET':
            allowed=role=='teacher' and request.query_params.get('class_id')==cls
        elif path.startswith('/api/teacher/packs/'):
            key=path.split('/')[4]
            pack=gateway.open.load_pack(key)
            allowed=role=='teacher' and pack['class_id']==cls
            if request.method=='PUT':
                body=await request.json()
                allowed=allowed and body.get('class_id')==cls
        elif path=='/api/learning/inbox' and request.method=='GET':
            allowed=request.query_params.get('class_id')==cls and (role=='teacher' or request.query_params.get('learner')==learner)
        elif path=='/api/sessions' and request.method=='POST':
            body=await request.json()
            with gateway.open.connect() as c:
                from .teacher import load
                aid=body.get('assignment_id','')
                if body.get('followup_id'):
                    from .planning import load_followup
                    followup=load_followup(c,body['followup_id'])
                    if followup['learner']!=body.get('learner'):raise ValueError('任务不属于该学生')
                    aid=followup['body']['assignment_id']
                assignment=load(c,aid)
            allowed=assignment['class_id']==cls and (role=='teacher' or body.get('learner')==learner)
            allowed=allowed and not body.get('skill')
        elif path in ('/api/teacher/assignments','/api/teacher/paths','/api/teacher/followups') and request.method=='POST':
            body=await request.json()
            allowed=role=='teacher' and body.get('class_id')==cls
        elif path=='/api/learning/next' and request.method=='GET':
            from .planning import load_path
            with gateway.open.connect() as c:path_body=load_path(c,request.query_params.get('path_id',''))
            allowed=path_body['class_id']==cls and (role=='teacher' or request.query_params.get('learner')==learner)
        elif path=='/api/learning/followups' and request.method=='GET':
            allowed=request.query_params.get('class_id')==cls and (role=='teacher' or request.query_params.get('learner')==learner)
        elif path.startswith('/api/teacher/followups/') and request.method=='PATCH':
            from .planning import load_followup
            with gateway.open.connect() as c:f=load_followup(c,path.rsplit('/',1)[-1])
            allowed=role=='teacher' and f['class_id']==cls
        elif path=='/api/teacher/dashboard' or path.startswith('/api/teacher/memory/'):
            allowed=role=='teacher' and request.query_params.get('class_id')==cls
        else:
            parts=path.split('/')
            # All session-based APIs, including trace, images, corrections.
            sid=next((part for part in parts if part.startswith('open_')),None)
            if sid:
                with gateway.open.connect() as c:
                    owner,state=gateway.open.load(c,sid)
                allowed=state.get('scope',{}).get('class_id')==cls and (role=='teacher' or owner==learner)
                if '/teacher/' in path: allowed=allowed and role=='teacher'
    except (ValueError,KeyError,TypeError):
        allowed=False
    if not allowed: return JSONResponse({'detail':'账号无权访问此班级或学习记录'},403)
    return await call_next(request)
