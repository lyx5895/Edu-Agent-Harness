"""Gradio page construction and callbacks; the gateway is supplied by the app."""
import base64
import os
from pathlib import Path
from types import SimpleNamespace
import uuid
import json
import gradio as gr
from harness.legacy.course import SKILLS
from harness.errors import Conflict
from harness.resources.images import file_to_data_url
from harness.resources.materials import read_material
from harness.llm.model import ModelUnavailable
from harness.teaching.preparation import Distribution
from harness.core.pedagogy import PROFILES
from harness.interfaces.ui import CSS, THEME, chat_messages, groups_html, hero, kpis, learning_rows, notice, queue_rows, section, status_card, task_card


PACK_COLUMNS=['课程','知识点','学科','教学类型','学习目标','题目','评分要求','参考答案','独立检查要求（每行一项）']

PACK_FIELDS=['course_id','concept_id','domain','task_type','objective','question','rubric','reference','check_requirements']


def create_ui(get_engine, endpoints):
    """Build a page and its callbacks without importing the application entrypoint."""
    def student_view(s):
        pending=(s or {}).get('pending_image') or {}
        return task_card(s),chat_messages(s),status_card(s),pending.get('text','')

    def start(learner,skill):
        try:
            s=get_engine().create(learner,skill)
            return (s['session_id'],s,*student_view(s))
        except Exception as e: raise gr.Error(str(e))

    def resume(sid):
        try:
            s=get_engine().session(sid.strip())
            return (sid.strip(),s,*student_view(s))
        except Exception as e: raise gr.Error(str(e))

    def operate(action,s,text='',image=None,request_id=None):
        if not s: raise gr.Error('请先开始或恢复会话')
        if action == 'confirm' and s.get('pending_image',{}):
            if s['pending_image'].get('purpose') == 'task': action = 'confirm_task'
        body={}
        if action in ('attempt','confirm','reply','confirm_task'):
            if not (text or '').strip(): raise gr.Error('请先写下你的作答或回答')
            body={'text':text}
        if action=='image':
            if not image: raise gr.Error('请先选择图片')
            try: body={'image_url':image_data(image)}
            except ValueError as e: raise gr.Error(str(e))
        try:
            # Same UI submission/state/body has the same key, including after a retry.
            import hashlib
            key=request_id or hashlib.sha256(json.dumps([s['session_id'],s['version'],action,body],sort_keys=True).encode()).hexdigest()
            get_engine().operation(s['session_id'],key,action,body,s['version'])
            new=get_engine().session(s['session_id'])
            return (new,*student_view(new),'')
        except (ValueError,ModelUnavailable,Conflict) as e: raise gr.Error(str(e))

    def teacher(learner):
        return get_engine().report(learner.strip() or None)

    def revision(event_id,status,reason):
        try:
            get_engine().correct(int(event_id),status,reason,uuid.uuid4().hex)
            return '修正已记录。原始作答保留，相关会话需重新检查。',get_engine().report()
        except ValueError as e: raise gr.Error(str(e))

    def image_data(path):
        if not path: return ''
        path=Path(path)
        if path.stat().st_size > 30*1024**2: raise ValueError('图片需小于30MB')
        return file_to_data_url(path)

    def start_task(learner,task,material,mode,task_image,material_file,assignment_id="",followup_id=""):
        try:
            if material_file:
                uploaded=read_material(material_file)
                material = uploaded if not material.strip() else material+'\n'+uploaded
            s=get_engine().create_task(learner=learner,request=task,material=material,mode=mode,image_url=image_data(task_image),assignment_id=assignment_id.strip() or None,followup_id=followup_id.strip() or None)
            return (s['session_id'],s,*student_view(s))
        except Exception as e: raise gr.Error(str(e))

    def inbox_options(cls,learner):
        items=get_engine().open.inbox(cls.strip(),learner.strip())
        choices=[(f"{x['course_id']} · {x['objective']} · {x['status']}",x['followup_id'])
                 for x in items if x['status'] not in ('cancelled','needs_recheck','completed')]
        return gr.Dropdown(choices=choices,value=choices[0][1] if choices else None)

    def start_inbox(learner,followup):
        if not followup:raise gr.Error('请先选择任务')
        return start_task(learner,'','','problem',None,None,'',followup)

    def pack_display(pack):
        rows=[]
        for item in pack['body']['items']:
            rows.append(["\n".join(item.get(k,[])) if k=='check_requirements' else item.get(k,'') for k in PACK_FIELDS])
        criteria=[[str(i+1),c['id'],c['kind'],c['description'],c.get('expected',''),c.get('concept_id','')]
                  for i,item in enumerate(pack['body']['items']) for c in item.get('criteria',[])]
        return pack,pack['pack_id'],rows,f"{pack['body']['title']} · {pack['status']} · 版本 {pack['version']}",criteria,False

    def generate_pack_ui(cls,title,material,goals):
        try:
            values=[dict(zip(['course_id','concept_id','domain','task_type','objective'],r)) for r in goals if any(r)]
            for value in values:value['task_type']=value.get('task_type') or 'open'
            return pack_display(get_engine().open.generate_pack(dict(class_id=cls,title=title,material=material,goals=values),uuid.uuid4().hex))
        except (ValueError,ModelUnavailable,Conflict) as e:raise gr.Error(str(e))

    def edited_pack_body(pack,rows,criteria_rows=None):
        if not pack:raise gr.Error('请先生成或加载备课包')
        if len(rows)!=len(pack['body']['items']):raise gr.Error('请保持行数；增加任务请新建备课包')
        from copy import deepcopy
        body=deepcopy(pack['body'])
        for item,row in zip(body['items'],rows):
            for key,value in zip(PACK_FIELDS,row):
                item[key]=[v.strip() for v in str(value or '').splitlines() if v.strip()] if key=='check_requirements' else str(value or '')
        if criteria_rows is not None:
            for item in body['items']:item['criteria']=[]
            for row in criteria_rows:
                if not any(row):continue
                try:
                    position=int(row[0])-1
                    if not 0<=position<len(body['items']):raise ValueError('题号不存在')
                    body['items'][position]['criteria'].append(dict(zip(['id','kind','description','expected','concept_id'],row[1:])))
                except (ValueError,TypeError,IndexError) as e:raise gr.Error('分项标准请填写有效题号：'+str(e))
        return body

    def save_pack_ui(pack,rows,criteria_rows=None):
        body=edited_pack_body(pack,rows,criteria_rows)
        try:return pack_display(get_engine().open.save_pack(body,uuid.uuid4().hex,pack['pack_id'],pack['version']))
        except (ValueError,Conflict) as e:raise gr.Error(str(e))

    def publish_pack_ui(pack,rows,criteria_rows,learners,reviewed,note):
        if not pack:raise gr.Error('请先生成或加载备课包')
        try:
            Distribution(learners=[x.strip() for x in learners.splitlines() if x.strip()],reviewed=reviewed,review_note=note)
        except ValueError as e:raise gr.Error(str(e))
        body=edited_pack_body(pack,rows,criteria_rows)
        try:
            result=get_engine().open.publish_pack(pack['pack_id'],pack['version'],dict(
                learners=[x.strip() for x in learners.splitlines() if x.strip()],
                reviewed=reviewed,review_note=note,due_at=0),uuid.uuid4().hex,edited_body=body)
            return pack_display(result)
        except (ValueError,Conflict) as e:raise gr.Error(str(e))

    def suggest_knowledge_ui(question,subject,stage):
        try:result=get_engine().open.suggest_knowledge(question,subject.strip(),stage or None)
        except ValueError as e:raise gr.Error(str(e))
        if not result['items']:return gr.update(),gr.update(),'知识点库中没有找到匹配的知识点。'
        top=result['items'][0]
        lines=['推荐知识点（第一项已填入，请核对后再发布）：']+[
            f"- `{i['id']}` {i['label']}"+(f"；先修：{'、'.join(p['name'] for p in i['prerequisites'])}" if i['prerequisites'] else '')
            for i in result['items']]+[f"来源：{result['license']}"]
        return top['id'],'、'.join(p['name'] for p in top['prerequisites']) or gr.update(),'\n'.join(lines)

    def fill_path_ui(rows):
        rows=[list(r) for r in rows or [] if r and r[0]]
        ids={r[0].strip() for r in rows}
        outside=[]
        for r in rows:
            node=get_engine().open.knowledge_node(r[0].strip())
            if not node:continue
            r[1]=r[1] or node['name']
            listed=[x.strip() for x in (r[3] or '').replace('，',',').split(',') if x.strip()]
            r[3]=','.join(dict.fromkeys(listed+[p['id'] for p in node['prerequisites'] if p['id'] in ids]))
            outside+=[f"{node['name']} ← {p['name']}（`{p['id']}`）" for p in node['prerequisites'] if p['id'] not in ids]
        note='知识点库中还有这些先修不在路径里，可考虑补充：\n'+'\n'.join(f'- {x}' for x in outside) if outside else '已按知识点库补全。'
        return rows,note

    def next_text(path_id,learner):
        try:
            result=get_engine().open.next_step(path_id.strip(),learner.strip())
            labels={'resume':'继续未完成作答','followup':'完成教师跟进任务','request_review':'请教师安排复习',
                    'check':'进行独立检查','remediate':'补救学习','diagnose':'先做诊断','complete':'本轮路径完成'}
            lines=[f"### {labels[result['action']]}",result['reason']]
            for key,label in [('session_id','会话编号'),('followup_id','跟进任务编号'),('assignment_id','教师任务编号'),('concept_id','知识点')]:
                if result.get(key):
                    node=get_engine().open.knowledge_node(result[key]) if key=='concept_id' else None
                    lines.append(f"{label}：`{result[key]}`"+(f" {node['label']}" if node else ''))
            return '\n\n'.join(lines)
        except ValueError as e:raise gr.Error(str(e))

    def followups_text(class_id,learner):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        rows=get_engine().open.followups(class_id.strip(),learner.strip())
        labels={'queued':'待开始','active':'进行中','completed':'独立检查通过','cancelled':'已撤销','needs_recheck':'证据失效，需重查'}
        return '\n\n'.join(f"- `{r['id']}` · {r['body']['objective']} · {labels[r['status']]} · {datetime.fromtimestamp(r['due_at'],ZoneInfo('Asia/Shanghai')).strftime('%m-%d %H:%M')}（北京时间）" for r in rows) or '暂无跟进任务。'

    def dashboard_ui(class_id):
        report=get_engine().open.dashboard(class_id.strip() or None)
        return kpis(report),learning_rows(report),queue_rows(report),groups_html(report)

    def pick_review(rows,evt:gr.SelectData):
        row=rows[evt.index[0]] if rows and evt.index is not None else None
        if not row: return gr.update(),gr.update()
        return row[5],int(row[6])

    with gr.Blocks(title='OmniEdu · 教学工作台',theme=THEME,css=CSS,fill_width=True) as demo:
        gr.HTML(hero(get_engine().open.policy,os.getenv('OMNIEDU_MODEL','omniedu-9b')),padding=False,elem_classes='html-flat')
        with gr.Tabs(elem_classes='role-tabs'):
            with gr.Tab('学生学习'):
                session_state=gr.State(None)
                with gr.Row(equal_height=False):
                    with gr.Column(scale=7):
                        task_view=gr.HTML(task_card(None),padding=False,elem_classes='html-flat')
                        chat=gr.Chatbot(type='messages',height=440,show_label=False,elem_classes='chat-panel',
                                        placeholder='开始任务后，你和老师的对话会显示在这里。',show_copy_button=False,layout='bubble')
                        work=gr.Textbox(show_label=False,lines=4,max_lines=12,elem_classes='composer',
                                        placeholder='写下你的解答和理由；如果老师在追问，直接在这里回答')
                        with gr.Row(elem_classes='actions'):
                            submit=gr.Button('提交作答',variant='primary',scale=2)
                            reply=gr.Button('回答追问',scale=1)
                            hint=gr.Button('给我一点提示',scale=1)
                            explain=gr.Button('请完整讲解',scale=1)
                        with gr.Accordion('照片作答 · 拍照上传手写过程',open=False):
                            with gr.Row():
                                image=gr.Image(type='filepath',label='作答照片（PNG / JPEG / WebP）',height=220)
                                with gr.Column():
                                    transcribe=gr.Button('识别照片')
                                    transcript=gr.Textbox(label='识别结果 · 核对或修改后确认',lines=6)
                                    confirm=gr.Button('确认并提交判定',variant='primary')
                    with gr.Column(scale=4):
                        with gr.Group(elem_classes='start-panel'):
                            gr.HTML(padding=False,elem_classes='html-flat',value=section('开始学习','选择老师布置的任务，或者自己出一道题'))
                            learner=gr.Textbox(label='我的名字',value='学生A')
                            with gr.Tabs():
                                with gr.Tab('老师布置的任务'):
                                    inbox_class=gr.Textbox(label='班级',placeholder='例如：七年一班')
                                    inbox_refresh=gr.Button('加载我的任务')
                                    inbox_choice=gr.Dropdown(label='选择任务',choices=[])
                                    inbox_begin=gr.Button('开始这个任务',variant='primary')
                                with gr.Tab('自己出题'):
                                    task_input=gr.Textbox(label='题目或学习目标',lines=4,placeholder='粘贴任何学科的题目，或说明你想学什么')
                                    mode=gr.Radio(label='方式',choices=[('辅导这道题','problem'),('根据目标或材料学习','learn')],value='problem')
                                    with gr.Accordion('附上题目图片或学习材料',open=False):
                                        task_image=gr.Image(type='filepath',label='题目图片（识别后需要确认）',height=160)
                                        material=gr.Textbox(label='参考材料',lines=4)
                                        material_file=gr.File(label='上传材料（文本或文字版 PDF）',file_types=['.txt','.md','.pdf'],type='filepath')
                                    begin_task=gr.Button('开始学习',variant='primary')
                        status_view=gr.HTML(status_card(None),padding=False,elem_classes='html-flat')
                        with gr.Row():
                            new_check=gr.Button('开始独立检查')
                            back=gr.Button('回到原任务')
                        with gr.Accordion('我的复习与学习路径',open=False):
                            student_class=gr.Textbox(label='班级')
                            list_followups=gr.Button('查看复习与补救任务',size='sm')
                            followup_list=gr.Markdown()
                            path_input=gr.Textbox(label='课程路径编号')
                            next_button=gr.Button('查看下一步',size='sm')
                            next_view=gr.Markdown()
                        with gr.Accordion('高级：编号、恢复会话与示例课程',open=False):
                            sid=gr.Textbox(label='会话编号')
                            restore=gr.Button('恢复会话',size='sm')
                            assignment_id=gr.Textbox(label='教师任务编号')
                            followup_id=gr.Textbox(label='跟进任务编号')
                            skill=gr.Dropdown(label='代数示例课程',choices=[(x['name'],x['id']) for x in SKILLS.values()],value='k01')
                            begin=gr.Button('打开示例课程',size='sm')
                            show_progress=gr.Button('查看历史进度',size='sm')
                            progress_json=gr.JSON(label='学习证据与可恢复会话')
                starts=[sid,session_state,task_view,chat,status_view,transcript]
                outs=[session_state,task_view,chat,status_view,transcript,work]
                inbox_refresh.click(inbox_options,[inbox_class,learner],[inbox_choice])
                inbox_begin.click(start_inbox,[learner,inbox_choice],starts)
                begin_task.click(start_task,[learner,task_input,material,mode,task_image,material_file,assignment_id,followup_id],starts)
                begin.click(start,[learner,skill],starts)
                restore.click(resume,[sid],starts)
                submit.click(lambda s,t:operate('attempt',s,t),[session_state,work],outs)
                work.submit(lambda s,t:operate('attempt',s,t),[session_state,work],outs)
                reply.click(lambda s,t:operate('reply',s,t),[session_state,work],outs)
                explain.click(lambda s:operate('explain',s),[session_state],outs)
                new_check.click(lambda s:operate('check',s),[session_state],outs)
                back.click(lambda s:operate('resume_practice',s),[session_state],outs)
                hint.click(lambda s:operate('hint',s),[session_state],outs)
                transcribe.click(lambda s,i:operate('image',s,image=i),[session_state,image],outs)
                confirm.click(lambda s,t:operate('confirm',s,t),[session_state,transcript],outs)
                list_followups.click(followups_text,[student_class,learner],[followup_list])
                next_button.click(next_text,[path_input,learner],[next_view])
                show_progress.click(get_engine().progress,[learner],[progress_json])

            with gr.Tab('教师工作台'):
                with gr.Tabs():
                    with gr.Tab('学情总览'):
                        with gr.Row():
                            class_filter=gr.Textbox(show_label=False,placeholder='班级（留空查看全部）',scale=4)
                            dashboard_button=gr.Button('刷新学情',variant='primary',scale=1)
                        kpi_view=gr.HTML(kpis({}),padding=False,elem_classes='html-flat')
                        with gr.Row(equal_height=False):
                            with gr.Column(scale=3):
                                gr.HTML(padding=False,elem_classes='html-flat',value=section('学生学习情况','按学习目标汇总的证据和建议的下一步'))
                                learning_table=gr.Dataframe(headers=['学生','学习目标','状态','独立检查','下一步'],datatype=['str','str','str','number','str'],
                                                            type='array',interactive=False,wrap=True,column_widths=['14%','34%','14%','12%','26%'])
                            with gr.Column(scale=2):
                                gr.HTML(padding=False,elem_classes='html-flat',value=section('建议分组补救','相同知识点、相同问题的学生'))
                                groups_view=gr.HTML('<div class="muted">刷新后显示。</div>',padding=False,elem_classes='html-flat')
                        gr.HTML(padding=False,elem_classes='html-flat',value=section('待复核作答','按风险排序；点击一行可直接到“复核与证据”处理'))
                        queue_table=gr.Dataframe(headers=['优先级','学生','作答','判定','风险','会话','作答编号'],type='array',interactive=False,wrap=True,
                                                 column_widths=['8%','9%','38%','8%','13%','16%','8%'])

                    with gr.Tab('发布任务'):
                        gr.HTML(padding=False,elem_classes='html-flat',value=section('发布一道教师任务','发布后版本不变；修改题目或规则请发布新版本。同一知识点用相同的课程和知识点编号，学习记录会跨题接续'))
                        with gr.Row(equal_height=False):
                            with gr.Column():
                                with gr.Row():
                                    cls=gr.Textbox(label='班级')
                                    course=gr.Textbox(label='课程')
                                    subject=gr.Textbox(label='学科')
                                task_type=gr.Dropdown(label='教学类型',choices=[(v['label'],k) for k,v in PROFILES.items()],value='open')
                                objective=gr.Textbox(label='学习目标（可观察）')
                                tq=gr.Textbox(label='题目',lines=5)
                                with gr.Row(equal_height=False,elem_classes='align-end'):
                                    kp_stage=gr.Dropdown(label='学段',choices=[('不限',''),('小学','小学'),('初中','初中'),('高中','高中')],value='',scale=1)
                                    kp_button=gr.Button('从知识点库推荐知识点',scale=2)
                                with gr.Row():
                                    concept=gr.Textbox(label='知识点编号')
                                    prerequisite=gr.Textbox(label='先修知识')
                                kp_view=gr.Markdown()
                            with gr.Column():
                                rubric=gr.Textbox(label='评分要求（注明解释、单位、形式等）',lines=3)
                                reference=gr.Textbox(label='参考答案（学生看不到）',lines=3)
                                tm=gr.Textbox(label='教学材料',lines=3)
                                guidance=gr.Textbox(label='教学指导（不要填写检查题答案）')
                                check_requirements=gr.Textbox(label='独立检查要求（每行一项，写可迁移的要求）',lines=2)
                                allow_explanation=gr.Checkbox(label='允许学生请求完整讲解',value=True)
                        with gr.Accordion('高级：分项评分、验证器与教师检查题',open=False):
                            gr.Markdown('类型：algebra 代数、numeric 数值、unit 指定单位、explanation 解释、factorization 因式分解、model 开放评分。数值、单位、因式分解需要填写预期值；学生只看到要求。')
                            criteria_table=gr.Dataframe(headers=['编号','类型','评分要求','预期值','关联知识点'],datatype=['str']*5,type='array',row_count=4,col_count=(5,'fixed'))
                            with gr.Row():
                                initial=gr.Textbox(label='验证器原式（可留空，自动识别单个一元方程）')
                                initial_kind=gr.Radio(choices=['none','equation','expression'],value='none',label='验证器类型')
                                preview_button=gr.Button('核对验证器绑定',size='sm')
                            preview=gr.JSON(label='验证器绑定结果')
                            check_bank=gr.Textbox(label='教师审核过的检查题编号（每行一个；留空则自动生成并审核新题）',lines=2)
                        publish=gr.Button('发布任务',variant='primary')
                        published_view=gr.HTML(padding=False,elem_classes='html-flat')

                    with gr.Tab('批量备课'):
                        pack_state=gr.State(None)
                        gr.HTML(padding=False,elem_classes='html-flat',value=section('第一步 · 列出学习目标','一次最多 6 个目标，可跨课程和学科；模型起草后由你逐题审核'))
                        with gr.Row():
                            pack_class=gr.Textbox(label='班级')
                            pack_title=gr.Textbox(label='备课包名称')
                        pack_material=gr.Textbox(label='参考材料',lines=4)
                        gr.Markdown('<span class="muted">教学类型：'+'；'.join(k+'（'+v['label']+'）' for k,v in PROFILES.items())+'</span>')
                        goals=gr.Dataframe(headers=['课程','知识点','学科','教学类型','学习目标'],datatype=['str']*5,type='array',row_count=3,col_count=(5,'fixed'))
                        with gr.Row():
                            pack_generate=gr.Button('生成待审核草稿',variant='primary')
                            pack_key=gr.Textbox(show_label=False,placeholder='或输入备课包编号继续编辑')
                            pack_load=gr.Button('加载备课包')
                        gr.HTML(padding=False,elem_classes='html-flat',value=section('第二步 · 逐题审核与编辑','题目、评分、参考答案和独立检查要求都可以直接改'))
                        pack_message=gr.Textbox(label='备课包状态',interactive=False)
                        pack_rows=gr.Dataframe(headers=PACK_COLUMNS,datatype=['str']*len(PACK_COLUMNS),type='array',row_count=1,col_count=(len(PACK_COLUMNS),'fixed'),wrap=True)
                        pack_criteria=gr.Dataframe(label='分项标准（题号从 1 开始，每题最多 4 项）',headers=['题号','编号','类型','评分要求','预期值','知识点'],datatype=['str']*6,type='array',row_count=1,col_count=(6,'fixed'),wrap=True)
                        pack_save=gr.Button('保存编辑')
                        gr.HTML(padding=False,elem_classes='html-flat',value=section('第三步 · 确认发布','发布在同一事务中完成，失败不会发布部分任务'))
                        with gr.Row():
                            pack_learners=gr.Textbox(label='分发给学生（每行一个；留空只放入题库）',lines=4)
                            with gr.Column():
                                pack_note=gr.Textbox(label='审核说明')
                                pack_review=gr.Checkbox(label='我已逐题核对题目、参考答案、评分和独立检查要求',value=False)
                                pack_publish=gr.Button('确认发布并分发',variant='primary')

                    with gr.Tab('路径与跟进'):
                        with gr.Row(equal_height=False):
                            with gr.Column():
                                gr.HTML(padding=False,elem_classes='html-flat',value=section('课程路径','用知识点和先修关系组织学习顺序，系统据此给每个学生推荐下一步'))
                                with gr.Row():
                                    plan_class=gr.Textbox(label='班级')
                                    plan_course=gr.Textbox(label='课程')
                                plan_title=gr.Textbox(label='路径名称')
                                plan_rows=gr.Dataframe(headers=['知识点编号','名称','教师任务编号','先修知识点（逗号分隔）'],datatype=['str']*4,type='array',row_count=3,col_count=(4,'fixed'))
                                with gr.Row():
                                    plan_fill=gr.Button('按知识点库补全')
                                    plan_publish=gr.Button('发布课程路径',variant='primary')
                                plan_note=gr.Markdown()
                                plan_result=gr.Textbox(label='课程路径编号',interactive=False)
                            with gr.Column():
                                gr.HTML(padding=False,elem_classes='html-flat',value=section('批量跟进','复习和诊断使用未见过的新题；补救可用提示，但仍需通过后续独立检查'))
                                group_names=gr.Textbox(label='学生名单（每行一人）',lines=3)
                                followup_assignment=gr.Textbox(label='教师任务编号')
                                followup_kind=gr.Radio(choices=[('复习','review'),('补救','remediation'),('诊断','diagnostic')],value='remediation',label='类型')
                                with gr.Row():
                                    due_hours=gr.Number(label='几小时后到期（0 为现在）',value=24,minimum=0,maximum=8760)
                                    followup_reason=gr.Textbox(label='安排依据')
                                assign_button=gr.Button('为名单中的学生布置',variant='primary')
                                with gr.Accordion('调整已布置的任务',open=False):
                                    revise_id=gr.Textbox(label='跟进任务编号')
                                    revise_kind=gr.Radio(choices=[('延期','postpone'),('撤销','cancel')],value='postpone',label='调整方式')
                                    revise_button=gr.Button('记录调整（使用上方小时数和依据）',size='sm')
                                assigned_results=gr.JSON(label='布置结果')

                    with gr.Tab('复核与证据'):
                        with gr.Row(equal_height=False):
                            with gr.Column():
                                gr.HTML(padding=False,elem_classes='html-flat',value=section('复核一次作答','从“学情总览”的待复核列表点选，会自动填入会话和作答编号'))
                                with gr.Row():
                                    open_sid=gr.Textbox(label='会话编号')
                                    event_id=gr.Number(label='作答编号',precision=0)
                                corrected=gr.Dropdown(label='改判为',choices=[('正确','correct'),('有错','incorrect'),('未完成','incomplete'),('待核对','unknown')],value='incorrect')
                                reason=gr.Textbox(label='依据',lines=2)
                                with gr.Row():
                                    confirm_review_button=gr.Button('确认原判定正确')
                                    open_fix=gr.Button('记录改判',variant='primary')
                                revision_msg=gr.HTML(padding=False,elem_classes='html-flat')
                                with gr.Accordion('旧示例课程的改判',open=False):
                                    fix=gr.Button('按作答编号改判示例课程',size='sm')
                            with gr.Column():
                                gr.HTML(padding=False,elem_classes='html-flat',value=section('教学轨迹','每一轮的输入、决策、候选回复与审核结果'))
                                trace_sid=gr.Textbox(label='会话编号')
                                with gr.Row():
                                    trace_button=gr.Button('查看轨迹')
                                    replay_button=gr.Button('回放策略（不调用模型）')
                                trace_json=gr.JSON(label='教学轨迹')
                                with gr.Accordion('原始图片',open=False):
                                    image_id=gr.Number(label='图片事件编号',precision=0)
                                    view_image=gr.Button('查看原始图片',size='sm')
                                    original=gr.Image(label='原始作业',interactive=False)
                        with gr.Accordion('全部学情与原始作答（JSON）',open=False):
                            with gr.Row():
                                filter_learner=gr.Textbox(show_label=False,placeholder='学生（留空查看全部）',scale=4)
                                refresh=gr.Button('查看',scale=1)
                            teacher_json=gr.JSON()

        dashboard_button.click(dashboard_ui,[class_filter],[kpi_view,learning_table,queue_table,groups_view])
        class_filter.submit(dashboard_ui,[class_filter],[kpi_view,learning_table,queue_table,groups_view])
        queue_table.select(pick_review,[queue_table],[open_sid,event_id])

        def preview_binding(question,initial,kind):
            from harness.grading.criteria import bind_algebra
            try:return bind_algebra(question,initial,kind)
            except ValueError as e:raise gr.Error(str(e))
        preview_button.click(preview_binding,[tq,initial,initial_kind],[preview])
        kp_button.click(suggest_knowledge_ui,[tq,subject,kp_stage],[concept,prerequisite,kp_view])

        def publish_ui(cls,course,concept,subject,objective,tq,rubric,reference,tm,prerequisite,guidance,allow,criteria_rows,initial,kind,task_type,requirements,check_bank):
            try:
                key=get_engine().open.publish_assignment(dict(class_id=cls,course_id=course,concept_id=concept,
                    domain=subject,objective=objective,question=tq,rubric=rubric,reference=reference,material=tm,
                    task_type=task_type,check_requirements=[x.strip() for x in requirements.splitlines() if x.strip()],
                    check_assignment_ids=[x.strip() for x in check_bank.splitlines() if x.strip()],
                    prerequisite=prerequisite,teaching_note=guidance,allow_explanation=allow,algebra_initial=initial,algebra_kind=kind,
                    criteria=[dict(id=r[0],kind=r[1] or 'model',description=r[2],expected=r[3] or '',concept_id=r[4] or '') for r in (criteria_rows or []) if r[0]]))['assignment_id']
            except ValueError as e: raise gr.Error(str(e))
            return notice(f'已发布。任务编号 <code>{key}</code>，学生在“老师布置的任务”里加载班级即可看到；也可以在“路径与跟进”里给指定学生布置。'),key
        publish.click(publish_ui,[cls,course,concept,subject,objective,tq,rubric,reference,tm,prerequisite,guidance,allow_explanation,criteria_table,initial,initial_kind,task_type,check_requirements,check_bank],[published_view,followup_assignment])

        pack_outputs=[pack_state,pack_key,pack_rows,pack_message,pack_criteria,pack_review]
        pack_generate.click(generate_pack_ui,[pack_class,pack_title,pack_material,goals],pack_outputs)
        pack_load.click(lambda key:pack_display(get_engine().open.load_pack(key.strip())),[pack_key],pack_outputs)
        pack_save.click(save_pack_ui,[pack_state,pack_rows,pack_criteria],pack_outputs)
        pack_publish.click(publish_pack_ui,[pack_state,pack_rows,pack_criteria,pack_learners,pack_review,pack_note],pack_outputs)

        plan_fill.click(fill_path_ui,[plan_rows],[plan_rows,plan_note])
        def publish_path_ui(cls,course,title,rows):
            try:
                return get_engine().open.publish_path(dict(class_id=cls,course_id=course,title=title,
                    nodes=[dict(concept_id=r[0],title=r[1],assignment_id=r[2],prerequisite_ids=[x.strip() for x in (r[3] or '').replace('，',',').split(',') if x.strip()]) for r in rows if r[0]]))['path_id']
            except ValueError as e:raise gr.Error(str(e))
        plan_publish.click(publish_path_ui,[plan_class,plan_course,plan_title,plan_rows],[plan_result])
        def assign_ui(cls,aid,names,kind,hours,reason):
            import time
            try:return get_engine().open.assign_followups(dict(class_id=cls,assignment_id=aid,
                learners=[x.strip() for x in names.splitlines() if x.strip()],kind=kind,due_at=time.time()+float(hours)*3600,reason=reason),uuid.uuid4().hex)
            except (ValueError,Conflict) as e:raise gr.Error(str(e))
        assign_button.click(assign_ui,[plan_class,followup_assignment,group_names,followup_kind,due_hours,followup_reason],[assigned_results])
        def revise_ui(key,kind,hours,reason):
            import time
            try:return get_engine().open.revise_followup(key,kind,time.time()+float(hours)*3600,reason,uuid.uuid4().hex)
            except (ValueError,Conflict) as e:raise gr.Error(str(e))
        revise_button.click(revise_ui,[revise_id,revise_kind,due_hours,followup_reason],[assigned_results])

        def confirm_review_ui(session,event,reason):
            try:
                get_engine().open.confirm_review(session.strip(),int(event),reason,uuid.uuid4().hex)
                return notice('已确认原判定，这条学习证据保持有效。'),get_engine().report()
            except (ValueError,TypeError,Conflict) as e:raise gr.Error(str(e))
        confirm_review_button.click(confirm_review_ui,[open_sid,event_id,reason],[revision_msg,teacher_json])
        def revise_open(session,event,status,reason):
            try:
                get_engine().open.correct(session.strip(),int(event),status,reason,uuid.uuid4().hex)
                return notice('改判已保存。原始作答保留，依赖这条证据的学习记录需要重新检查。'),get_engine().report()
            except (ValueError,TypeError,Conflict) as e: raise gr.Error(str(e))
        open_fix.click(revise_open,[open_sid,event_id,corrected,reason],[revision_msg,teacher_json])
        def revision_ui(event,status,reason):
            message,report=revision(event,status,reason)
            return notice(message),report
        fix.click(revision_ui,[event_id,corrected,reason],[revision_msg,teacher_json])
        trace_button.click(get_engine().traces,[trace_sid],[trace_json])
        replay_button.click(get_engine().replay,[trace_sid],[trace_json])
        refresh.click(teacher,[filter_learner],[teacher_json])
        def show_image(i,session):
            import io
            from PIL import Image
            payload=endpoints.open_image(session,int(i)) if session.startswith('open_') else endpoints.original_image(int(i))
            return Image.open(io.BytesIO(base64.b64decode(payload['image_url'].split(',',1)[1])))
        view_image.click(show_image,[image_id,open_sid],[original])

    demo.queue(default_concurrency_limit=4,max_size=16)
    return demo, SimpleNamespace(
        student_view=student_view,
        start=start,
        resume=resume,
        operate=operate,
        teacher=teacher,
        revision=revision,
        image_data=image_data,
        start_task=start_task,
        inbox_options=inbox_options,
        start_inbox=start_inbox,
        pack_display=pack_display,
        generate_pack_ui=generate_pack_ui,
        edited_pack_body=edited_pack_body,
        save_pack_ui=save_pack_ui,
        publish_pack_ui=publish_pack_ui,
        suggest_knowledge_ui=suggest_knowledge_ui,
        fill_path_ui=fill_path_ui,
        next_text=next_text,
        followups_text=followups_text,
        dashboard_ui=dashboard_ui,
        pick_review=pick_review,
        preview_binding=preview_binding,
        publish_ui=publish_ui,
        publish_path_ui=publish_path_ui,
        assign_ui=assign_ui,
        revise_ui=revise_ui,
        confirm_review_ui=confirm_review_ui,
        revise_open=revise_open,
        revision_ui=revision_ui,
        show_image=show_image,
    )
