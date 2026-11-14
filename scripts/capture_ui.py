"""Drive the workspaces in Chromium, capture screenshots and fail on console errors.

Start the preview server first (scripted model, seeded class):

    .envs/harness/bin/python scripts/ui_preview.py --port 7862
    python scripts/capture_ui.py --url http://127.0.0.1:7862 --out docs/screenshots

Requires ``pip install playwright && playwright install chromium`` in the Python that runs it.
"""

import argparse
import json
import sys
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

DESKTOP = {'width': 1440, 'height': 960}
PHONE = {'width': 390, 'height': 844}


def run(url, out, learner):
    out.mkdir(parents=True, exist_ok=True)
    errors, steps = [], []

    def watch(page):
        page.on('console', lambda m: m.type == 'error' and errors.append(f'{page.url}: {m.text}'))
        page.on('pageerror', lambda e: errors.append(f'{page.url}: {e}'))

    def shot(page, name, full=False):
        page.screenshot(path=str(out / f'{name}.png'), full_page=full)
        steps.append(name)

    with sync_playwright() as p:
        browser = p.chromium.launch(args=['--no-sandbox'])
        context = browser.new_context(
            viewport=DESKTOP, device_scale_factor=2, locale='zh-CN', timezone_id='Asia/Shanghai'
        )
        page = context.new_page()
        watch(page)

        page.goto(url + '/')
        expect(page.get_by_role('heading', name='选择你的工作台')).to_be_visible()
        shot(page, 'workspaces')

        # ---- student: set identity, take a task, get it wrong, ask for a hint, fix it, start a check
        page.goto(url + '/student/')
        page.fill('#p-name', learner)
        page.fill('#p-class', '七年一班')
        page.click('#profile-form button[value=ok]')
        expect(page.locator('#inbox .list-item').first).to_be_visible()
        shot(page, 'student-start')
        page.locator('#inbox .list-item', has_text='平均速度').click()
        expect(page.locator('.task-card .question')).to_contain_text('小明骑车')
        page.fill('#work', '平均速度等于路程乘以时间，45 × 3 = 135，所以是 135 千米每小时。')
        page.click('#send')
        expect(page.locator('.msg.tutor .ask').last).to_be_visible()
        page.click('[data-act=hint]')
        expect(page.locator('.msg.tutor').last).to_contain_text('速度')
        shot(page, 'student')
        page.fill('#work', '速度 = 路程 ÷ 时间，45 ÷ 3 = 15，平均速度是 15 千米/小时。')
        page.check('#complete')
        page.click('#send')
        expect(page.locator('.banner.ok')).to_contain_text('借助帮助完成')
        shot(page, 'student-assisted')
        page.click('[data-act=check]')
        expect(page.locator('.banner.check')).to_be_visible()
        expect(page.locator('[data-act=hint]')).to_have_count(0)

        phone = browser.new_context(
            viewport=PHONE, device_scale_factor=3, locale='zh-CN', is_mobile=True, has_touch=True
        )
        mobile = phone.new_page()
        watch(mobile)
        mobile.goto(url + '/student/')
        mobile.fill('#p-name', '陈一鸣')
        mobile.fill('#p-class', '七年一班')
        mobile.click('#profile-form button[value=ok]')
        mobile.locator('#inbox .list-item', has_text='平均速度').click()
        expect(mobile.locator('.msg.tutor').first).to_be_visible()
        mobile.locator('#work').scroll_into_view_if_needed()
        shot(mobile, 'student-mobile')

        # ---- teacher
        page.goto(url + '/teacher/')
        page.fill('#class-id', '七年一班')
        page.press('#class-id', 'Enter')
        page.locator('#class-id').dispatch_event('change')
        expect(page.locator('.kpi').first).to_be_visible()
        expect(page.locator('tr[data-review]').first).to_be_visible()
        shot(page, 'teacher-dashboard', full=True)
        page.locator('tr[data-review]', has_text='陈一鸣').first.click()
        expect(page.locator('#review-detail .answer')).to_contain_text('45')
        page.locator('#trace-details summary').click()
        expect(page.locator('.timeline li').first).to_be_visible()
        shot(page, 'teacher-review', full=True)
        page.fill('#review-reason', '已对照题目核对：学生把路程和时间相乘，判定有错成立。')
        page.click('#confirm-review')
        expect(page.locator('.toast')).to_contain_text('已确认')
        page.locator('.toast').wait_for(state='detached')

        page.goto(url + '/teacher/#publish')
        expect(page.locator('#f-question')).to_be_visible()
        page.fill('#f-course_id', '七年级数学')
        page.fill('#f-domain', '数学')
        page.fill('#f-objective', '能用速度、时间求路程')
        page.fill('#f-question', '一辆车以 60 千米/小时的速度行驶 2.5 小时，行驶了多少千米？')
        page.fill('#f-concept_id', '路程')
        page.fill('#f-rubric', '列出路程 = 速度 × 时间，结果带单位。')
        page.fill('#f-reference', '60 × 2.5 = 150 千米')
        shot(page, 'teacher-publish')
        page.click('#publish-btn')
        expect(page.locator('#publish-result .banner')).to_contain_text('已发布')
        page.click('#publish-another')
        expect(page.locator('#f-question')).to_have_value('')

        page.goto(url + '/teacher/#packs')
        page.locator('.queue .list-item').first.click()
        expect(page.locator('.pack-item').first).to_be_visible()
        shot(page, 'teacher-packs')

        page.goto(url + '/teacher/#planning')
        expect(page.locator('#followup-table table')).to_be_visible()
        shot(page, 'teacher-planning', full=True)

        tablet = browser.new_context(
            viewport=PHONE, device_scale_factor=3, locale='zh-CN', is_mobile=True, has_touch=True
        )
        tpage = tablet.new_page()
        watch(tpage)
        tpage.goto(url + '/teacher/')
        tpage.fill('#class-id', '七年一班')
        tpage.locator('#class-id').dispatch_event('change')
        expect(tpage.locator('.kpi').first).to_be_visible()
        shot(tpage, 'teacher-mobile')
        overflow = tpage.evaluate('document.documentElement.scrollWidth > window.innerWidth')
        if overflow:
            errors.append('teacher dashboard scrolls horizontally on a phone')
        overflow = mobile.evaluate('document.documentElement.scrollWidth > window.innerWidth')
        if overflow:
            errors.append('student page scrolls horizontally on a phone')

        browser.close()
    return {'steps': steps, 'console_errors': errors, 'passed': not errors}


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--url', default='http://127.0.0.1:7862')
    parser.add_argument('--out', type=Path, default=Path('docs/screenshots'))
    parser.add_argument('--learner', default='周宁')
    args = parser.parse_args()
    report = run(args.url.rstrip('/'), args.out, args.learner)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    sys.exit(0 if report['passed'] else 1)


if __name__ == '__main__':
    main()
