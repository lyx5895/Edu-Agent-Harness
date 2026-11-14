"""Real-model functional audit of the v0.10 workspaces, driven in Chromium.

Start an isolated app first (temporary database; never the production one):

    HARNESS_DB=/tmp/edu-audit/learning.sqlite OMNIEDU_REVIEW_BASE_URL=http://127.0.0.1:8002/v1 \
    .envs/harness/bin/python -m uvicorn harness.app:app --host 127.0.0.1 --port 7861

then, in a Python with Playwright and Chromium:

    python scripts/capture_functional_audit.py --url http://127.0.0.1:7861 \
        --out reports/functional-audit-v010 --shots docs/screenshots/functional-audit-v010

Each case records what the live models actually did. A case whose assertion fails is
reported as failed with its observations and a screenshot; it is never retried until it
passes. Model output varies between runs. Exit code is non-zero if any case failed.
"""

import argparse
import json
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import expect, sync_playwright

CLASS = '功能验收班'
MODEL_WAIT = 180_000  # ms; a turn can take several model calls
DESKTOP = {'width': 1440, 'height': 960}
PHONE = {'width': 390, 'height': 844}

CHECK_QUESTION = dict(
    objective='能根据路程和时间求平均速度，并写出计算过程和单位',
    question='小红步行 4 小时行了 20 千米，她的平均速度是多少千米每小时？请写出计算过程和单位。',
    rubric='列出“速度 = 路程 ÷ 时间”，代入数据计算，结果带单位。',
    reference='20 ÷ 4 = 5，平均速度是 5 千米/小时。',
)
MAIN_QUESTION = dict(
    objective='能根据路程和时间求平均速度，并写出计算过程和单位',
    question='小明骑车 3 小时行了 45 千米，他的平均速度是多少千米每小时？请写出计算过程和单位。',
    rubric='列出“速度 = 路程 ÷ 时间”，代入数据计算，结果带单位。',
    reference='45 ÷ 3 = 15，平均速度是 15 千米/小时。',
)
REQUIREMENT = '能在新的情境中用路程除以时间求速度，并写出单位'
WRONG = '平均速度等于路程乘以时间，45 × 3 = 135，所以平均速度是 135 千米每小时。'
RIGHT = '速度 = 路程 ÷ 时间，45 ÷ 3 = 15，平均速度是 15 千米/小时。'
CHECK_RIGHT = '速度 = 路程 ÷ 时间，20 ÷ 4 = 5，平均速度是 5 千米/小时。'


class Audit:
    def __init__(self, url, out, shots):
        self.url, self.out, self.shots = url, out, shots
        self.cases, self.console = [], []
        self.ctx = {}

    # ---- helpers ------------------------------------------------------------------
    def api(self, path, body=None, method=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method or ('POST' if data else 'GET'))
        req.add_header('Content-Type', 'application/json')
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(req, timeout=300) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b'null')

    def page(self, browser, viewport=DESKTOP, **kw):
        context = browser.new_context(
            viewport=viewport, device_scale_factor=2, locale='zh-CN', timezone_id='Asia/Shanghai', **kw
        )
        page = context.new_page()
        page.on('console', lambda m: m.type == 'error' and self.console.append(f'{page.url}: {m.text}'))
        page.on('pageerror', lambda e: self.console.append(f'{page.url}: {e}'))
        page.on('dialog', lambda d: d.accept())
        return page

    def shot(self, page, name, full=False):
        path = self.shots / f'{name}.png'
        page.screenshot(path=str(path), full_page=full)
        return str(path)

    def case(self, name, title, fn, page_for_failure=None):
        started = time.monotonic()
        row = {'name': name, 'title': title, 'observed': {}}
        try:
            fn(row['observed'])
            row['passed'] = True
        except Exception as error:
            row.update(passed=False, error=f'{type(error).__name__}: {error}', traceback=traceback.format_exc(limit=3))
            page = page_for_failure() if callable(page_for_failure) else page_for_failure
            if page is not None:
                try:
                    row['screenshot'] = self.shot(page, name + '-failed')
                except Exception as shot_error:  # a failed screenshot must not hide the case result
                    row['screenshot_error'] = str(shot_error)
        row['seconds'] = round(time.monotonic() - started, 1)
        self.cases.append(row)
        print(('PASS ' if row['passed'] else 'FAIL ') + name, row.get('error', ''), flush=True)
        return row['passed']

    def session_of(self, page, learner):
        sid = page.evaluate(f"JSON.parse(localStorage.getItem('omniedu.session:{learner}'))")
        return self.api(f'/api/sessions/{sid}')[1]

    def student(self, browser, learner, viewport=DESKTOP, **kw):
        page = self.page(browser, viewport, **kw)
        page.goto(self.url + '/student/')
        page.fill('#p-name', learner)
        page.fill('#p-class', CLASS)
        page.click('#profile-form button[value=ok]')
        page.audit_learner = learner
        return page

    def turn(self, page, trigger):
        """Run one learner action and wait until the server has recorded it (or reported an error)."""
        learner = page.audit_learner
        before = len(self.session_of(page, learner)['history'])
        trigger()
        # The pending bubble shows a spinner until the response has been rendered.
        page.wait_for_timeout(300)
        expect(page.locator('.msg.tutor .spinner')).to_have_count(0, timeout=MODEL_WAIT)
        history = self.session_of(page, learner)['history']
        if len(history) <= before:
            toast = page.locator('.toast').all_inner_texts()
            raise AssertionError(f'action not recorded; page said: {toast}')
        return history[-1]

    def send(self, page, text, complete=False):
        def trigger():
            page.fill('#work', text)
            if complete and page.locator('#complete').count():
                page.check('#complete')
            page.click('#send')

        return self.turn(page, trigger)

    def act(self, page, action):
        return self.turn(page, lambda: page.click(f'[data-act={action}]'))

    def fill_assignment(self, page, item):
        page.fill('#f-course_id', '七年级数学')
        page.fill('#f-domain', '数学')
        page.select_option('#f-task_type', 'problem_solving')
        page.fill('#f-objective', item['objective'])
        page.fill('#f-question', item['question'])
        page.fill('#f-concept_id', '平均速度')
        page.fill('#f-rubric', item['rubric'])
        page.fill('#f-reference', item['reference'])
        page.fill('#f-check_requirements', REQUIREMENT)

    # ---- run ----------------------------------------------------------------------
    def run(self):
        with sync_playwright() as p:
            browser = p.chromium.launch(args=['--no-sandbox'])
            teacher = self.page(browser)
            teacher.goto(self.url + '/teacher/')
            teacher.fill('#class-id', CLASS)
            teacher.locator('#class-id').dispatch_event('change')
            self.teacher_publish(teacher)
            self.teacher_assign(teacher)
            students = {}
            self.student_wrong_explain_correct_check(browser, students)
            self.student_hint_ladder(browser)
            self.student_photo(browser)
            self.student_pdf(browser)
            self.teacher_dashboard_review_correct(teacher)
            self.teacher_pack(teacher, browser)
            self.learning_path(teacher, students)
            self.mobile(browser)
            browser.close()

    def teacher_publish(self, t):
        def publish(o):
            t.goto(self.url + '/teacher/#publish')
            expect(t.locator('#f-question')).to_be_visible()
            self.fill_assignment(t, CHECK_QUESTION)
            t.click('#publish-btn')
            expect(t.locator('#publish-result .banner')).to_contain_text('已发布', timeout=60_000)
            o['check_assignment'] = t.locator('#publish-result code').inner_text()
            t.click('#publish-another')
            expect(t.locator('#f-question')).to_have_value('')  # a fresh, empty form
            self.fill_assignment(t, MAIN_QUESTION)
            t.locator('#advanced summary').click()
            t.check(f'#check-bank input[value="{o["check_assignment"]}"]')
            o['screenshot_form'] = self.shot(t, '02-publish-form')
            t.click('#publish-btn')
            expect(t.locator('#publish-result .banner')).to_contain_text('已发布', timeout=60_000)
            o['assignment'] = t.locator('#publish-result code').inner_text()
            self.ctx.update(o)
            listed = self.api('/api/teacher/assignments?' + urllib.parse.urlencode({'class_id': CLASS}))[1]
            assert len(listed) == 2 and all('reference' not in a for a in listed), listed
            o['screenshot'] = self.shot(t, '02-publish-result')

        t.goto(self.url + '/')
        self.shot(t, '01-entry')
        self.case('publish', '教师在页面上发布检查题和练习，并把检查题加入练习的题库', publish, t)

    def teacher_assign(self, t):
        def assign(o):
            t.goto(self.url + f'/teacher/#planning/{self.ctx["assignment"]}')
            expect(t.locator('#fu-learners')).to_be_visible()
            t.fill('#fu-learners', '验收学生A\n验收学生B\n验收学生C')
            t.fill('#fu-reason', '单元前测：速度公式')
            t.click('#fu-assign')
            expect(t.locator('#followup-table table')).to_contain_text('验收学生C', timeout=30_000)
            o['followups'] = t.locator('#followup-table tbody tr').count()
            assert o['followups'] == 3
            o['screenshot'] = self.shot(t, '03-assign', full=True)

        self.case('assign', '教师在“学习安排”中按名单布置补救任务', assign, t)

    def student_wrong_explain_correct_check(self, browser, students):
        s = self.student(browser, '验收学生B')
        students['B'] = s

        def inbox(o):
            expect(s.locator('#inbox .list-item').first).to_be_visible()
            o['inbox'] = s.locator('#inbox .list-item').all_inner_texts()
            s.locator('#inbox .list-item').first.click()
            expect(s.locator('.task-card .question')).to_contain_text('小明骑车')
            session = self.session_of(s, '验收学生B')
            o['followup_linked'] = bool(session['followup_id'])
            assert session['followup_id'] and 'reference' not in session['task']
            o['screenshot'] = self.shot(s, '04-student-inbox')

        def wrong(o):
            self.send(s, WRONG)
            last = self.session_of(s, '验收学生B')['history'][-1]
            o.update(status=last.get('status'), action=last.get('action'), feedback=last.get('feedback'))
            o['screenshot'] = self.shot(s, '05-wrong-answer')
            assert last.get('status') == 'incorrect', last

        def explain(o):
            self.act(s, 'explain')
            last = self.session_of(s, '验收学生B')['history'][-1]
            o.update(action=last.get('action'), feedback=last.get('feedback'))
            o['gives_result_15'] = '15' in (last.get('feedback') or '')
            o['screenshot'] = self.shot(s, '06-explanation')
            assert last.get('action') == 'explain', last

        def correct(o):
            self.send(s, RIGHT, complete=True)
            last = self.session_of(s, '验收学生B')['history'][-1]
            o.update(status=last.get('status'), evidence=last.get('evidence'))
            expect(s.locator('.banner.ok')).to_be_visible()
            o['banner'] = s.locator('.banner.ok').inner_text()
            o['screenshot'] = self.shot(s, '07-assisted-completion')
            assert last.get('evidence') == 'assisted_complete', last

        def check(o):
            self.act(s, 'check')
            expect(s.locator('.banner.check')).to_be_visible()
            session = self.session_of(s, '验收学生B')
            o['check_question'] = session['task']['question']
            o['from_teacher_bank'] = session['task']['question'] == CHECK_QUESTION['question']
            o['hint_buttons'] = s.locator('[data-act=hint]').count()
            status, _ = self.api(
                f'/api/sessions/{session["session_id"]}/hint',
                {'request_id': 'audit-hint', 'version': session['version']},
            )
            o['hint_api_status'] = status
            self.send(s, CHECK_RIGHT)
            last = self.session_of(s, '验收学生B')['history'][-1]
            o.update(status=last.get('status'), evidence=last.get('evidence'))
            o['screenshot'] = self.shot(s, '08-independent-check')
            assert o['from_teacher_bank'] and o['hint_buttons'] == 0 and status == 409
            assert last.get('evidence', '').startswith('independent'), last

        self.case('student-inbox', '学生领取老师布置的任务；学生端拿不到参考答案', inbox, s)
        self.case('wrong-answer', '提交错误作答，判定为有错，并指出问题', wrong, s)
        self.case('explanation', '学生请求完整讲解', explain, s)
        self.case('assisted-completion', '看过讲解后做对，记为借助帮助完成', correct, s)
        self.case('independent-check', '用老师题库里的新题做独立检查；检查中不提供提示', check, s)
        session = self.session_of(s, '验收学生B')
        self.ctx['sid_b'] = session.get('session_id')
        self.ctx['wrong_event_b'] = next(
            (e['id'] for e in session.get('history', []) if e.get('status') == 'incorrect'), None
        )

    def student_hint_ladder(self, browser):
        s = self.student(browser, '验收学生A')

        def ladder(o):
            s.locator('#inbox .list-item').first.click()
            expect(s.locator('#work')).to_be_visible()
            self.send(s, WRONG)
            actions = []
            for _ in range(3):
                if not s.locator('[data-act=hint]').count():
                    break
                self.act(s, 'hint')
                actions.append(self.session_of(s, '验收学生A')['history'][-1].get('action'))
            o['first_turn'] = self.session_of(s, '验收学生A')['history'][0].get('action')
            o['hint_actions'] = actions
            o['delivered'] = [e.get('delivered_action') for e in self.session_of(s, '验收学生A')['history'][1:]]
            o['feedbacks'] = [e.get('feedback') for e in self.session_of(s, '验收学生A')['history']]
            o['screenshot'] = self.shot(s, '09-hint-ladder', full=True)
            assert actions[:3] == ['scaffold_concept', 'scaffold_step', 'scaffold_example'], actions

        self.case('hint-ladder', '答错后连续请求三级提示：概念、步骤、例子', ladder, s)

    def student_photo(self, browser):
        s = self.student(browser, '验收学生C')

        def photo(o):
            s.locator('#inbox .list-item').first.click()
            expect(s.locator('#work')).to_be_visible()
            s.set_input_files('#photo', str(self.out / 'work-example.png'))
            expect(s.locator('#transcript')).to_be_visible(timeout=MODEL_WAIT)
            o['transcription'] = s.input_value('#transcript')
            o['screenshot_transcription'] = self.shot(s, '10-photo-transcription')
            pending = self.session_of(s, '验收学生C')['pending_image']
            assert pending and pending['purpose'] == 'work'
            s.click('#confirm-image')
            expect(s.locator('#transcript')).to_have_count(0, timeout=MODEL_WAIT)
            last = self.session_of(s, '验收学生C')['history'][-1]
            o.update(status=last.get('status'), action=last.get('action'))
            o['screenshot'] = self.shot(s, '11-photo-judged')
            assert last.get('status') in ('correct', 'incorrect', 'incomplete', 'unknown')

        self.case('photo-answer', '拍照作答：先核对识别文字，确认后再判定', photo, s)

    def student_pdf(self, browser):
        s = self.student(browser, '验收学生D')

        def pdf(o):
            s.click('#new-question')
            s.get_by_text('学习一个目标或材料').click()  # the radio itself is visually hidden
            s.fill('#q-text', '用牛顿第二定律解释：同样的力推质量更大的物体，加速度会怎样？')
            s.locator('#question-form details summary').click()
            s.set_input_files('#q-file', str(self.out / 'material.pdf'))
            s.click('#q-start')
            expect(s.locator('.task-card .question')).to_be_visible(timeout=MODEL_WAIT)
            session = self.session_of(s, '验收学生D')
            o['question'] = session['task']['question']
            o['source_pages'] = session['task'].get('source_pages')
            o['source_count'] = session['source_count']
            o['screenshot'] = self.shot(s, '12-pdf-material')
            assert session['source_count'] >= 2 and o['source_pages'] == [2], o

        self.case('pdf-material', '自己提问并上传两页文字版 PDF，任务引用正确的页码', pdf, s)

    def teacher_dashboard_review_correct(self, t):
        def dashboard(o):
            t.goto(self.url + '/teacher/#dashboard')
            expect(t.locator('.kpi').first).to_be_visible()
            o['kpis'] = t.locator('.kpi').all_inner_texts()
            o['learning_rows'] = t.locator('table.data').first.locator('tbody tr').count()
            o['queue_rows'] = t.locator('tr[data-review]').count()
            o['screenshot'] = self.shot(t, '13-teacher-dashboard', full=True)
            assert o['learning_rows'] >= 3

        def review(o):
            t.goto(self.url + f'/teacher/#review/{self.ctx["sid_b"]}/{self.ctx["wrong_event_b"]}')
            expect(t.locator('#review-detail .answer')).to_contain_text('135')
            t.locator('#trace-details summary').click()
            expect(t.locator('.timeline li').first).to_be_visible()
            o['replay_notice'] = t.locator('#trace-body .notice').inner_text()
            o['turns'] = t.locator('.timeline li').count()
            o['screenshot'] = self.shot(t, '14-review-replay', full=True)
            assert '一致' in o['replay_notice']

        def correct(o):
            t.fill('#review-reason', '验收：原题单位要求表述不清，改为待核对，请重新检查。')
            t.select_option('#new-status', 'unknown')
            t.click('#save-correction')
            expect(t.locator('#decision .notice')).to_contain_text('已改判', timeout=30_000)
            session = self.api(f'/api/sessions/{self.ctx["sid_b"]}')[1]
            o['needs_review'] = session['needs_review']
            o['screenshot'] = self.shot(t, '15-correction', full=True)
            assert session['needs_review']

        self.case('dashboard', '班级学情显示本次真实作答产生的记录和复核队列', dashboard, t)
        self.case('review-replay', '复核页查看作答、教学轨迹，策略回放一致（不调用模型）', review, t)
        self.case('correction', '教师改判后，原会话标记为需要重新检查', correct, t)

    def teacher_pack(self, t, browser):
        def pack(o):
            t.goto(self.url + '/teacher/#packs')
            expect(t.locator('#g-title')).to_be_visible()
            t.fill('#g-title', '验收练习包：蒸发与吸热')
            row = t.locator('#goals .editor-row').first
            for key, value in {
                'course_id': '八年级物理',
                'concept_id': '蒸发吸热',
                'domain': '物理',
                'objective': '能用蒸发吸热解释生活现象',
            }.items():
                row.locator(f'[data-k={key}]').fill(value)
            t.fill('#g-material', '蒸发是液体表面发生的汽化现象。蒸发时液体从周围吸收热量，所以有致冷作用。')
            t.click('#generate-btn')
            expect(t.locator('.pack-item').first).to_be_visible(timeout=MODEL_WAIT)
            o['draft_question'] = t.locator('.pack-item [data-k=question]').first.input_value()
            o['screenshot_draft'] = self.shot(t, '16-pack-draft', full=True)
            t.fill('#pk-learners', '验收学生E')
            t.fill('#pk-note', '验收：已逐题核对题目、参考答案与评分要求')
            t.check('#pk-reviewed')
            t.click('#pk-publish')
            expect(t.locator('.notice.ok')).to_contain_text('已发布', timeout=60_000)
            inbox = self.api(
                '/api/learning/inbox?' + urllib.parse.urlencode({'class_id': CLASS, 'learner': '验收学生E'})
            )[1]
            o['inbox_for_E'] = len(inbox)
            o['screenshot'] = self.shot(t, '17-pack-published')
            assert len(inbox) == 1

        self.case('pack', '批量出题：真实模型起草，教师审核后发布并分发', pack, t)

    def learning_path(self, t, students):
        def path(o):
            t.goto(self.url + '/teacher/#planning')
            expect(t.locator('#nodes .editor-row').first).to_be_visible()
            t.fill('#p-title', '速度单元')
            t.fill('#p-course', '七年级数学')
            row = t.locator('#nodes .editor-row').first
            row.locator('[data-k=concept_id]').fill('平均速度')
            row.locator('[data-k=title]').fill('平均速度')
            row.locator('[data-k=assignment_id]').select_option(self.ctx['assignment'])
            t.locator('#nodes .editor-row').nth(1).locator('.remove').click()
            t.click('#p-publish')
            expect(t.locator('#path-note code')).to_be_visible(timeout=30_000)
            path_id = t.locator('#path-note code').inner_text()
            s = students['B']
            s.click('#open-path')
            s.fill('#path-id', path_id)
            s.click('#path-go')
            expect(s.locator('#path-result .notice')).to_be_visible(timeout=30_000)
            o['next_step'] = s.locator('#path-result .notice').inner_text()
            o['screenshot'] = self.shot(s, '18-learning-path')
            assert o['next_step']

        self.case('learning-path', '教师发布学习顺序；学生按路径查看下一步（改判后的证据不再算数）', path, t)

    def mobile(self, browser):
        def layout(o):
            s = self.student(browser, '验收学生A', viewport=PHONE, is_mobile=True, has_touch=True)
            s.locator('#inbox .list-item').first.click()
            expect(s.locator('.msg.tutor').first).to_be_visible()
            o['student_overflow'] = s.evaluate('document.documentElement.scrollWidth > innerWidth')
            o['screenshot'] = self.shot(s, '19-mobile-student')
            t = self.page(browser, PHONE, is_mobile=True, has_touch=True)
            t.goto(self.url + '/teacher/')
            t.fill('#class-id', CLASS)
            t.locator('#class-id').dispatch_event('change')
            expect(t.locator('.kpi').first).to_be_visible()
            o['teacher_overflow'] = t.evaluate('document.documentElement.scrollWidth > innerWidth')
            o['screenshot_teacher'] = self.shot(t, '20-mobile-teacher')
            assert not o['student_overflow'] and not o['teacher_overflow']

        self.case('mobile', '手机宽度（390px）下学生端和教师端没有横向溢出', layout)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--url', default='http://127.0.0.1:7861')
    parser.add_argument('--out', type=Path, default=Path('reports/functional-audit-v010'))
    parser.add_argument('--shots', type=Path, default=Path('docs/screenshots/functional-audit-v010'))
    args = parser.parse_args()
    url = args.url.rstrip('/')
    if urlparse(url).hostname not in ('127.0.0.1', 'localhost') or urlparse(url).port in (7860, None):
        sys.exit('Run the audit against an isolated local app (not the production port 7860).')
    args.out.mkdir(parents=True, exist_ok=True)
    args.shots.mkdir(parents=True, exist_ok=True)
    audit = Audit(url, args.out, args.shots)
    started = time.time()
    audit.run()
    report = {
        'scope': 'real-model browser workflows on an isolated database; observations, not learning outcomes',
        'url': url,
        'started_at': started,
        'seconds': round(time.time() - started, 1),
        'passed': all(c['passed'] for c in audit.cases) and not audit.console,
        'summary': {'passed': sum(c['passed'] for c in audit.cases), 'total': len(audit.cases)},
        'console_errors': audit.console,
        'cases': audit.cases,
    }
    (args.out / 'browser-workflows.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report['summary'], ensure_ascii=False), 'console errors:', len(audit.console))
    sys.exit(0 if report['passed'] else 1)


if __name__ == '__main__':
    main()
