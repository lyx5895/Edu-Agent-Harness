"""Look and feel of the Gradio workbench: theme, CSS and HTML renderers for cards, chat and dashboards."""
from html import escape

import gradio as gr

FONT = ['PingFang SC', 'Microsoft YaHei', 'Noto Sans SC', 'Inter', 'system-ui', 'sans-serif']

THEME = gr.themes.Base(
    primary_hue=gr.themes.colors.indigo, secondary_hue=gr.themes.colors.violet, neutral_hue=gr.themes.colors.slate,
    radius_size=gr.themes.sizes.radius_lg, font=FONT, font_mono=['JetBrains Mono', 'Menlo', 'monospace'],
).set(
    body_background_fill='#f4f6fb',
    block_background_fill='#ffffff',
    block_border_width='1px',
    block_border_color='#e6e9f2',
    block_shadow='0 1px 2px rgba(16,24,40,.04), 0 4px 16px rgba(16,24,40,.04)',
    block_label_background_fill='transparent',
    block_label_text_color='#475467',
    block_label_text_weight='600',
    block_title_text_weight='600',
    input_background_fill='#f9fafc',
    input_border_color='#e4e7ec',
    input_shadow='none',
    button_primary_background_fill='linear-gradient(135deg,#4f46e5 0%,#7c3aed 100%)',
    button_primary_background_fill_hover='linear-gradient(135deg,#4338ca 0%,#6d28d9 100%)',
    button_primary_text_color='#ffffff',
    button_primary_border_color='transparent',
    button_secondary_background_fill='#ffffff',
    button_secondary_background_fill_hover='#f2f4f7',
    button_secondary_border_color='#d0d5dd',
    button_secondary_text_color='#344054',
)

CSS = """
.gradio-container{max-width:1360px !important;width:100% !important;margin:0 auto !important;padding-top:20px !important}
main.fillable{max-width:1360px !important}
footer{display:none !important}
.hero{display:flex;align-items:center;justify-content:space-between;gap:24px;padding:22px 28px;border-radius:18px;
  background:radial-gradient(1200px 300px at 10% -40%,rgba(255,255,255,.25),transparent),linear-gradient(120deg,#312e81 0%,#4f46e5 45%,#7c3aed 100%);
  color:#fff;box-shadow:0 12px 32px rgba(79,70,229,.25)}
.hero .brand{display:flex;align-items:center;gap:14px}
.hero .logo{width:44px;height:44px;border-radius:12px;background:rgba(255,255,255,.18);display:grid;place-items:center;
  font-weight:800;font-size:20px;letter-spacing:-.5px;border:1px solid rgba(255,255,255,.3)}
.hero h1{margin:0;font-size:22px;font-weight:700;color:#fff}
.hero p{margin:2px 0 0;opacity:.85;font-size:13.5px;color:#fff}
.hero .meta{display:flex;gap:8px;flex-wrap:wrap;justify-content:flex-end}
.pill{display:inline-flex;align-items:center;gap:6px;padding:4px 10px;border-radius:999px;font-size:12px;font-weight:600;
  background:rgba(255,255,255,.14);border:1px solid rgba(255,255,255,.25);color:#fff}
.pill .dot{width:7px;height:7px;border-radius:50%;background:#34d399;box-shadow:0 0 0 3px rgba(52,211,153,.25)}
.role-tabs > .tab-wrapper{border:none !important;margin:16px 0 4px !important}
.role-tabs > .tab-wrapper .tab-container[role=tablist]{display:inline-flex !important;gap:4px;background:#e8ebf4;padding:4px;border-radius:12px;border:none !important;height:auto !important}
.role-tabs > .tab-wrapper button[role=tab]{font-size:15px !important;font-weight:600 !important;padding:8px 26px !important;border-radius:9px !important;border:none !important;color:#475467 !important;height:auto !important}
.role-tabs > .tab-wrapper button[role=tab].selected{background:#fff !important;color:#4338ca !important;box-shadow:0 1px 3px rgba(16,24,40,.14)}
.role-tabs > .tab-wrapper button[role=tab].selected::after{display:none !important}
.html-flat{background:transparent !important;border:none !important;box-shadow:none !important;padding:0 !important}
.section-title{font-size:15px;font-weight:700;color:#101828;margin:2px 0 2px}
.section-sub{font-size:12.5px;color:#667085;margin:0 0 4px}
.card{background:#fff;border:1px solid #e6e9f2;border-radius:16px;padding:18px 20px;box-shadow:0 1px 2px rgba(16,24,40,.04)}
.task-card{position:relative;overflow:hidden}
.task-card:before{content:"";position:absolute;inset:0 auto 0 0;width:4px;background:linear-gradient(#4f46e5,#7c3aed)}
.task-card .head{display:flex;align-items:center;gap:8px;margin-bottom:10px;flex-wrap:wrap}
.task-card .q{font-size:16px;line-height:1.75;color:#101828;white-space:pre-wrap}
.task-card .goal{margin-top:12px;padding-top:12px;border-top:1px dashed #e4e7ec;font-size:13px;color:#475467}
.task-card.empty{text-align:center;padding:34px 20px;color:#667085}
.task-card.empty .big{font-size:17px;font-weight:700;color:#344054;margin-bottom:6px}
.tag{display:inline-flex;align-items:center;padding:3px 10px;border-radius:999px;font-size:12px;font-weight:600;line-height:18px}
.tag.indigo{background:#eef2ff;color:#4338ca}.tag.violet{background:#f5f3ff;color:#6d28d9}
.tag.green{background:#ecfdf3;color:#067647}.tag.red{background:#fef3f2;color:#b42318}
.tag.amber{background:#fffaeb;color:#b54708}.tag.gray{background:#f2f4f7;color:#475467}.tag.blue{background:#eff8ff;color:#175cd3}
.status-card .row{display:flex;justify-content:space-between;align-items:center;padding:9px 0;border-bottom:1px solid #f2f4f7;font-size:13.5px;color:#475467}
.status-card .row:last-child{border-bottom:none}
.status-card .row b{color:#101828;font-weight:600}
.status-card .tip{margin-top:10px;padding:10px 12px;border-radius:12px;background:#f8f9ff;color:#3538cd;font-size:13px;line-height:1.6}
.kpis{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:14px}
.kpi{background:#fff;border:1px solid #e6e9f2;border-radius:16px;padding:16px 18px;box-shadow:0 1px 2px rgba(16,24,40,.04)}
.kpi .label{font-size:12.5px;color:#667085;font-weight:600}
.kpi .value{font-size:28px;font-weight:700;color:#101828;margin-top:4px;letter-spacing:-.5px}
.kpi .hint{font-size:12px;color:#98a2b3;margin-top:2px}
.kpi.accent{background:linear-gradient(135deg,#eef2ff,#f5f3ff);border-color:#e0e7ff}
.notice{padding:12px 14px;border-radius:12px;font-size:13px;line-height:1.65}
.notice.ok{background:#ecfdf3;color:#067647;border:1px solid #abefc6}
.notice.info{background:#f8f9ff;color:#3538cd;border:1px solid #e0e7ff}
.notice code{background:rgba(255,255,255,.7);padding:1px 6px;border-radius:6px}
.chat-panel .message-wrap{padding:6px 4px !important}
.chat-panel .message{font-size:14.5px !important;line-height:1.7 !important}
.chat-panel .user .message, .chat-panel .message.user{background:linear-gradient(135deg,#4f46e5,#6d5cf0) !important;color:#fff !important;border:none !important}
.chat-panel .user .message *, .chat-panel .message.user *{color:#fff !important}
.chat-panel .bot .message, .chat-panel .message.bot{background:#f6f7fb !important;border:none !important;box-shadow:none !important}
.chat-panel .message.bot .prose, .chat-panel .bot .message .prose{background:transparent !important;border:none !important}
.composer textarea{font-size:14.5px !important;line-height:1.7 !important}
.actions button{min-width:0 !important}
.muted{color:#667085;font-size:12.5px}
.align-end{align-items:flex-end !important}
.align-end button{height:42px !important;margin-bottom:10px}
.start-panel{background:#fff !important}
.start-panel > .html-flat, .start-panel .html-flat{padding:14px 14px 6px !important;background:#fff !important}
"""

ACTION_LABELS = {
    'locate': ('定位问题', 'amber'), 'probe': ('追问依据', 'blue'), 'clarify': ('请补充', 'gray'),
    'self_correct': ('自行修正', 'blue'), 'change_explanation': ('换个讲法', 'violet'), 'prerequisite': ('先修知识', 'violet'),
    'scaffold': ('提示', 'indigo'), 'scaffold_concept': ('提示 · 概念', 'indigo'), 'scaffold_step': ('提示 · 步骤', 'indigo'),
    'scaffold_example': ('提示 · 例子', 'indigo'), 'explain': ('完整讲解', 'violet'), 'offer_check': ('完成', 'green'),
    'create_check': ('独立检查', 'indigo'), 'assess_only': ('独立检查结果', 'gray'), 'teacher_review': ('已转教师', 'red'),
    'resume_practice': ('回到原任务', 'gray'),
}
STATUS_LABELS = {'correct': ('判定 · 正确', 'green'), 'incorrect': ('判定 · 有错', 'red'),
                 'incomplete': ('判定 · 未完成', 'amber'), 'unknown': ('判定 · 待教师核对', 'gray')}
STATE_LABELS = {'insufficient_evidence': '证据不足', 'needs_recheck': '需重新检查', 'needs_support': '需要帮助',
                'independent_observed': '独立完成', 'assisted_complete': '辅助完成', 'practice_complete': '练习完成'}


def tag(text, color='gray'):
    return f'<span class="tag {color}">{escape(text)}</span>'


def hero(policy, model):
    return f"""<div class="hero"><div class="brand"><div class="logo">Ω</div><div>
<h1>OmniEdu 教学工作台</h1><p>判定有依据、提示不给答案、学习证据可追溯</p></div></div>
<div class="meta"><span class="pill"><span class="dot"></span>模型 {escape(model)}</span>
<span class="pill">教学策略 {escape(policy.rsplit('-', 1)[-1])}</span><span class="pill">本地演示 · 学生与教师共享数据</span></div></div>"""


def section(title, sub=''):
    return f'<div class="section-title">{escape(title)}</div>' + (f'<div class="section-sub">{escape(sub)}</div>' if sub else '')


def task_card(s):
    if not s:
        return ('<div class="card task-card empty"><div class="big">还没有开始任务</div>'
                '在右侧选择老师布置的任务，或者自己输入一道题开始。</div>')
    if s.get('engine') != 'open-task-v1':
        item = s['item']
        phase = tag('独立检查', 'violet') if item['phase'] == 'check' else tag('教学练习', 'indigo')
        return (f'<div class="card task-card"><div class="head">{phase}{tag("代数示例", "gray")}</div>'
                f'<div class="q">{escape(item["prompt"])}</div><div class="goal">课程依据：{escape(item["source"])}</div></div>')
    if s.get('needs_review'):
        return '<div class="card task-card empty"><div class="big">教师已修订这次记录</div>请新建任务重新检查。</div>'
    t = s.get('task')
    if not t:
        return '<div class="card task-card empty"><div class="big">请确认题目图片</div>在下方“照片作答”中核对识别出的题目文字，再确认。</div>'
    phase = tag('独立检查 · 请独立完成', 'violet') if t['phase'] == 'check' else tag('当前任务', 'indigo')
    pages = t.get('source_pages') or []
    source = f'　·　材料出处：第 {"、".join(map(str, pages))} 页' if pages else ''
    return (f'<div class="card task-card"><div class="head">{phase}{tag(t.get("domain") or "自定义", "gray")}</div>'
            f'<div class="q">{escape(t["question"])}</div>'
            f'<div class="goal">学习目标：{escape(t.get("objective", ""))}{escape(source)}</div></div>')


def _criteria(rows):
    if not rows:
        return ''
    names = {'correct': '通过', 'incorrect': '有错', 'incomplete': '未完成', 'unknown': '待核对'}
    return '\n\n' + '　'.join(f'`{escape(x["description"])}：{names.get(x["status"], x["status"])}`' for x in rows)


def chat_messages(s):
    if not s:
        return []
    messages = []
    for e in s.get('history', []):
        if e.get('type') == 'correction':
            messages.append({'role': 'assistant', 'content': '*教师修订了这次判定，相关学习记录需要重新检查。*'})
            continue
        if e.get('text'):
            messages.append({'role': 'user', 'content': e['text']})
        if e.get('hint'):
            messages.append({'role': 'assistant', 'content': f'**第 {e.get("level", "")} 级提示**\n\n{e["hint"]}'})
        if e.get('feedback'):
            label, _ = ACTION_LABELS.get(e.get('action'), ('', 'gray'))
            status = STATUS_LABELS.get(e.get('status'), ('', ''))[0] if e.get('status') else ''
            head = '　·　'.join(x for x in (label, status) if x)
            messages.append({'role': 'assistant',
                             'content': (f'**{head}**\n\n' if head else '') + e['feedback'] + _criteria(e.get('criteria_results'))})
    if not messages and s.get('task'):
        first = '这是一次独立检查，请不借助提示完成。' if s['task']['phase'] == 'check' else '先试着写出你的解答和理由。卡住了可以随时要提示。'
        messages.append({'role': 'assistant', 'content': first})
    if s.get('pending_image'):
        messages.append({'role': 'assistant', 'content': '我识别了你上传的图片，请在“照片作答”里核对文字后确认。'})
    return messages


def status_card(s):
    if not s:
        return ('<div class="card status-card"><div class="section-title">学习状态</div>'
                '<div class="muted" style="margin-top:6px">开始任务后，这里显示进度和下一步。</div></div>')
    if s.get('engine') != 'open-task-v1':
        return (f'<div class="card status-card"><div class="section-title">学习状态</div>'
                f'<div class="row"><span>会话</span><b>{escape(s["session_id"][:14])}</b></div></div>')
    t = s.get('task') or {}
    attempts = sum(1 for e in s.get('history', []) if e.get('text'))
    hints = sum(1 for e in s.get('history', []) if str(e.get('action', '')).startswith('scaffold'))
    if s.get('completed'):
        state, tip = tag('已完成', 'green'), '做得好！可以开始一道没见过的新题做独立检查，确认真正掌握。'
    elif s.get('pending_question'):
        state, tip = tag('等待你的回答', 'amber'), '老师在等你回答追问。直接在下方输入，点“回答追问”。'
    elif t.get('phase') == 'check':
        state, tip = tag('独立检查中', 'violet'), '独立检查不提供提示，请独立完成。'
    else:
        state, tip = tag('进行中', 'indigo'), '写出完整的步骤和理由，判定会更准确。'
    scope = s.get('scope') or {}
    rows = [('状态', state), ('阶段', escape('独立检查' if t.get('phase') == 'check' else '练习')),
            ('已作答', f'{attempts} 次'), ('已用提示', f'{hints} 次')]
    if scope.get('class_id'):
        rows.append(('班级', escape(scope['class_id'])))
    body = ''.join(f'<div class="row"><span>{k}</span><b>{v}</b></div>' for k, v in rows)
    return (f'<div class="card status-card"><div class="section-title">学习状态</div>{body}'
            f'<div class="tip">{escape(tip)}</div>'
            f'<div class="muted" style="margin-top:10px">会话编号 {escape(s["session_id"])}（可在“恢复会话”中继续）</div></div>')


def kpis(report):
    learning = report.get('learning', [])
    learners = {r['learner'] for r in learning}
    support = sum(1 for r in learning if r['state'] in ('needs_support', 'needs_recheck'))
    independent = sum(1 for r in learning if r['state'] == 'independent_observed')
    queue = report.get('review_queue', [])
    urgent = sum(1 for r in queue if r.get('priority') == 'urgent')
    cards = [('学生', len(learners), f'{len(learning)} 个学习目标', 'accent'),
             ('需要帮助', support, '需要支持或重新检查', ''),
             ('独立完成', independent, '无提示新题已通过', ''),
             ('待复核', len(queue), f'其中紧急 {urgent} 条', '')]
    return '<div class="kpis">' + ''.join(
        f'<div class="kpi {cls}"><div class="label">{label}</div><div class="value">{value}</div><div class="hint">{hint}</div></div>'
        for label, value, hint, cls in cards) + '</div>'


PRIORITY = {'urgent': '紧急', 'risk': '风险', 'sample': '抽检'}
RISKS = {'negative_judgment': '负面判定', 'low_confidence': '低置信', 'independent_by_model_only': '仅模型判定',
         'model_reference': '模型参考答案', 'criterion_undetermined': '分项未定'}


def learning_rows(report):
    return [[r['learner'], r['objective'], STATE_LABELS.get(r['state'], r['state']), r['independent_count'], r['next_step']]
            for r in report.get('learning', [])]


def queue_rows(report):
    status = {'correct': '正确', 'incorrect': '有错', 'incomplete': '未完成', 'unknown': '待核对', None: '反馈待审'}
    return [[PRIORITY.get(r.get('priority'), r.get('priority', '')), r['learner'], (r.get('text') or '')[:80],
             status.get(r.get('status'), r.get('status')), '、'.join(RISKS.get(x, x) for x in r.get('risks', [])),
             r['session_id'], r['event_id']] for r in report.get('review_queue', [])]


def groups_html(report):
    groups = report.get('support_groups', [])
    if not groups:
        return '<div class="muted">暂无需要分组补救的知识点。</div>'
    return ''.join(f'<div class="notice info" style="margin-bottom:8px"><b>{escape(g["objective"])}</b>'
                   f'{"　·　" + escape(g.get("criterion", "")) if g.get("criterion") else ""}<br>'
                   f'{escape("、".join(g["learners"]))}</div>' for g in groups)


def notice(text, kind='ok'):
    return f'<div class="notice {kind}">{text}</div>'
