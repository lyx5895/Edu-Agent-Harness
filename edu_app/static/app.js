'use strict';

const S = { meta: null, pid: null, overview: null, tab: 'prep', subject: null, view: 'home', course: null, offering: null, aid: null, home: null };
const store = {  // per-browser conveniences only; the page works without them
  get(k) { try { return localStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { if (v == null) localStorage.removeItem(k); else localStorage.setItem(k, v); } catch { /* private mode */ } },
};
const newKey = () => (crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random().toString(16).slice(2)}`);
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
const TYPE_LABEL = { objective: '教学目标', question: '题目', rubric: '评分标准', submission: '学生作答', grading: '批改', analysis: '学情分析', problem_pattern: '问题模式', comparison: '两轮对比', lesson_plan: '教学安排', review_plan: '讲评稿' };
const RISK_LABEL = {
  undetermined_point: '有未判定要点', review_disagreement: '复核模型意见不同', audit_disagreement: '抽检发现分歧',
  writing_needs_confirmation: '写作建议分待确认', ungrounded_quote: '引用无法核对', low_confidence: '模型没把握',
};
const STATUS_LABEL = { earned: '得分', partial: '部分得分', missed: '失分', undetermined: '待判定' };

async function api(method, path, body) {
  const opts = { method, headers: {} };
  // A retried POST with the same key returns the first result instead of doing the work twice.
  if (method === 'POST') opts.headers['Idempotency-Key'] = newKey();
  if (body instanceof FormData) opts.body = body;
  else if (body !== undefined) { opts.headers['Content-Type'] = 'application/json'; opts.body = JSON.stringify(body); }
  const res = await fetch(path, opts);
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = { detail: text }; }
  if (!res.ok) throw new Error((data && data.detail) ? (typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail)) : `请求失败（${res.status}）`);
  return data;
}

let toastTimer;
function toast(msg, error = false) {
  const t = $('#toast');
  t.textContent = msg;
  t.className = 'toast' + (error ? ' error' : '');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.add('hidden'), error ? 7000 : 3500);
}

async function busy(btn, label, fn) {
  const old = btn ? btn.textContent : '';
  if (btn) { btn.disabled = true; btn.textContent = label; }
  try { return await fn(); } catch (e) { toast(e.message, true); return null; } finally { if (btn) { btn.disabled = false; btn.textContent = old; } }
}

function showNote(note) {
  const box = $('#context-note');
  if (!note) { box.classList.add('hidden'); return; }
  box.textContent = note;
  box.classList.remove('hidden');
}

function statusPill(a) {
  if (!a) return '';
  const pills = [];
  if (a.status === 'confirmed') pills.push(`<span class="pill ok">已确认 · 第 ${a.version} 版</span>`);
  else if (a.status === 'candidate') pills.push('<span class="pill info">A/B 候选</span>');
  else pills.push(`<span class="pill warn">草稿 · 第 ${a.version} 版</span>`);
  if (a.status !== 'confirmed' && a.confirmed_version) pills.push(`<span class="pill">已确认版本：第 ${a.confirmed_version} 版</span>`);
  if (a.stale) pills.push('<span class="pill bad">依据已变化</span>');
  if (a.origin === 'imported') pills.push('<span class="pill">导入</span>');
  if (a.origin === 'edited') pills.push('<span class="pill">老师修改</span>');
  if (a.origin === 'teacher') pills.push(`<span class="pill">${a.type === 'grading' ? '老师改判' : '老师编写'}</span>`);
  return pills.join(' ');
}

function changeKindSelect(needed) {
  if (!needed) return '';
  return `<select class="change-kind" title="这次修改的性质"><option value="substantive">实质修改（下游需复核）</option><option value="cosmetic">文字修改（不影响下游）</option></select>`;
}

function rounds() {
  const r = new Set([...(S.overview?.rounds || [1])]);
  return [...r].sort((a, b) => a - b);
}

function fillRoundSelect(sel, keep = true) {
  const el = $(sel);
  const prev = el.value;
  el.innerHTML = rounds().map((r) => `<option value="${r}">第 ${r} 轮</option>`).join('');
  if (keep && prev && rounds().includes(Number(prev))) el.value = prev;
}

// ---------- startup ----------

async function init() {
  S.meta = await api('GET', '/api/meta');
  $('#subject-select').innerHTML = '<option value="" disabled selected>请选择学科</option>'
    + S.meta.subjects.map((s) => `<option value="${s.id}">${esc(s.name)}</option>`).join('');
  $('#nc-subject').innerHTML = '<option value="" disabled selected>请选择学科</option>'
    + S.meta.subjects.map((s) => `<option value="${s.id}">${esc(s.name)}</option>`).join('')
    + '<option value="__custom">其他（自己填写）</option>';
  S.course = store.get('edu.course');
  S.offering = store.get('edu.offering');
  $('#model-status').textContent = S.meta.model_ready
    ? `当前模型：出题与批改 ${S.meta.models.grader}，复核 ${S.meta.models.reviewer}`
    : '未配置模型：只能查看已有内容（设置 EDU_LLM_API_KEY 后重启服务）';
  bindEvents();
  bindHomeEvents();
  bindExtras();
  const pid = store.get('edu.pid');
  const projects = await loadProjects(pid, false);
  if (store.get('edu.view') === 'project' && projects.some((p) => p.id === pid)) await selectProject(pid);
  else await showHome();
}

async function loadProjects(selectId, open = true) {
  const projects = await api('GET', '/api/projects');
  const sel = $('#project-select');
  sel.innerHTML = '<option value="">所有工作区…</option>'
    + projects.map((p) => `<option value="${p.id}">${esc(p.title)}（${esc(p.subject_name)}）</option>`).join('');
  const target = projects.find((p) => p.id === selectId) ? selectId : '';
  sel.value = S.view === 'project' ? target : '';
  if (open && target) await selectProject(target);
  return projects;
}

function showWelcome(on) {
  $('#welcome').classList.toggle('hidden', !on);
  $('#home').classList.add('hidden');
  $('#workspace').classList.toggle('hidden', on || !S.pid || S.view !== 'project');
  if (on) $('#home').classList.add('hidden');
}

async function selectProject(pid, opts = {}) {
  if (pid !== S.pid) { S.aid = null; S.pendingParent = null; }
  S.pid = pid;
  S.view = 'project';
  if (opts.aid !== undefined) S.aid = opts.aid;
  if (opts.tab) S.tab = opts.tab;
  store.set('edu.pid', pid);
  store.set('edu.view', 'project');
  $('#project-select').value = pid;
  $('#home').classList.add('hidden');
  $('#welcome').classList.add('hidden');
  $('#workspace').classList.remove('hidden');
  showNote('');
  await refresh();
  window.scrollTo({ top: 0 });
}

async function refresh() {
  if (!S.pid || S.view !== 'project') return;
  S.overview = await api('GET', `/api/projects/${S.pid}${S.aid ? `?assignment_id=${encodeURIComponent(S.aid)}` : ''}`);
  S.aid = S.overview.assignment_id || null;
  const p = S.overview.project;
  S.subject = S.meta.subjects.find((s) => s.id === p.subject)
    || { id: p.subject, name: p.subject_name, types: p.question_types, tags: S.meta.common_tags };
  renderOverview();
  await renderTab();
}

// ---------- 布置记录 (assignment scope) ----------

const scopeQS = () => (S.aid ? `&assignment_id=${encodeURIComponent(S.aid)}` : '');
const scoped = (body) => (S.aid ? { ...body, assignment_id: S.aid } : body);
const currentAssignment = () => (S.overview?.assignments || []).find((a) => a.id === S.aid) || null;

function renderScope() {
  const o = S.overview;
  const show = ['grading', 'analysis', 'review'].includes(S.tab) && (o.assignments.length > 0);
  $('#scope-bar').classList.toggle('hidden', !show);
  const a = currentAssignment();
  if (show) {
    $('#scope-assignment').innerHTML = '<option value="">本项目（未关联班级）</option>'
      + o.assignments.map((x) => `<option value="${x.id}">${esc(x.class_name)} · ${esc(x.title)}${x.round > 1 ? `（第 ${x.round} 轮）` : ''}</option>`).join('');
    $('#scope-assignment').value = S.aid || '';
    $('#scope-info').textContent = a
      ? `${a.status_label} · 名单 ${a.roster} 人，收到 ${a.received} 人 · ${a.questions} 道题（版本已固定）`
      : '不属于任何班级的作答';
  }
  const updates = show && a ? a.updates : [];
  $('#scope-updates').classList.toggle('hidden', !updates.length);
  $('#scope-updates').innerHTML = updates.map((u) => `<div>第 ${u.no} 题的${u.changed.map((k) => (k === 'rubric' ? '评分标准' : '题目')).join('和')}有新版本，这次布置仍使用原版本。
    <button class="small secondary" data-repin="${u.question_lineage}">本班改用新版本并重新批改</button></div>`).join('');
  // A class assignment fixes its round.
  ['#g-round', '#a-round', '#r-round'].forEach((sel) => {
    const el = $(sel);
    if (a) { if (![...el.options].some((x) => x.value === String(a.round))) el.insertAdjacentHTML('beforeend', `<option value="${a.round}">第 ${a.round} 轮</option>`); el.value = String(a.round); }
    el.disabled = !!a;
  });
}

const STAGES = ['prep', 'questions', 'grading', 'analysis', 'review'];
const STAGE_NAME = { prep: '备课设计', questions: '出题改题', grading: '作业批改', analysis: '学情分析', review: '讲题答疑' };
// 下一步 = the arrow to the next big stage of the teaching loop, never a step inside the current page.
const CONTINUE = {
  prep: [{ label: '按本课目标出题', tab: 'questions' }],
  questions: [{ label: '准备批改', tab: 'grading' }],
  grading: [{ label: '汇总学情', tab: 'analysis' }],
  analysis: [{ label: '准备针对性讲评，定位讲解重点', tab: 'review' }],
  review: [
    { label: '生成对应的巩固练习', tab: 'review', target: '#practice' },
    { label: '进入下一课时 · 优化后续备课', action: 'next-lesson' },
  ],
};

function currentStage() {
  const step = S.overview.next.find((x) => STAGES.includes(x.tab) && !x.optional);
  return step ? step.tab : null;
}

function renderOverview() {
  const o = S.overview;
  $('#project-crumbs').innerHTML = o.course
    ? `<a data-home="1">我的教学</a> › ${esc(o.course.title)}${o.class_name ? ' · ' + esc(o.class_name) : ''}${o.task ? ` · ${o.task.kind === 'legacy' ? '旧课时项目' : '任务'}` : ''}`
    : '<a data-home="1">我的教学</a> › 单独项目（不属于课程）';
  $('#project-title').textContent = o.project.title;
  $('#project-meta').textContent = `${o.project.subject_name}${o.project.grade ? ' · ' + o.project.grade : ''}${o.latest_round > 1 ? ` · 第 ${o.latest_round} 轮` : ''}`;
  const cur = currentStage();
  $$('#tabs button[data-tab]').forEach((b) => {
    const tab = b.dataset.tab;
    if (!STAGES.includes(tab)) return;
    b.classList.toggle('done', !!o.stages[tab]);
    b.classList.toggle('current', tab === cur);
    $('.mark', b).textContent = o.stages[tab] ? '✓' : String(STAGES.indexOf(tab) + 1).padStart(2, '0');
  });
  const badge = $('#records-badge');
  badge.textContent = o.stale.length;
  badge.classList.toggle('hidden', !o.stale.length);
  if (!$('#project-edit').classList.contains('hidden')) renderProjectEdit();
}

function renderContinue() {
  const box = $(`#tab-${S.tab} .continue`);
  if (!box) return;
  const o = S.overview;
  const done = !!o.stages[S.tab];
  // A later lesson only needs this lesson's 学情 (in any round), so it stays open after 再练 starts round 2.
  const hasStatus = o.counts.problem_pattern.confirmed > 0 || !!o.next_lesson;
  const buttons = CONTINUE[S.tab]
    .map((g) => (g.action === 'next-lesson' && o.next_lesson ? { ...g, label: `进入下一课时（${o.next_lesson.title}）` } : g))
    .map((g, i) => `<button class="continue-btn${i ? ' ghost' : ''}" data-continue="${i}"${done || (g.action === 'next-lesson' && hasStatus) ? '' : ' disabled'}>下一步：${esc(g.label)} →</button>`)
    .join('');
  let hint = '';
  if (!done) {
    const earlier = STAGES.slice(0, STAGES.indexOf(S.tab)).find((t) => !o.stages[t]);
    hint = earlier && S.tab !== 'review'
      ? `先完成「${STAGE_NAME[earlier]}」`
      : (() => { const here = o.next.find((x) => x.tab === S.tab && !x.optional); return here ? `完成「${here.label}」后可进入` : '本页完成后可进入'; })();
  }
  box.innerHTML = `<div class="row-btns">${buttons}</div>${hint ? `<span class="muted hint">${esc(hint)}</span>` : ''}`;
}

async function renderTab() {
  $$('#tabs button').forEach((b) => b.classList.toggle('active', b.dataset.tab === S.tab));
  $$('.tab').forEach((t) => t.classList.toggle('hidden', t.id !== `tab-${S.tab}`));
  const fn = { prep: renderPrep, questions: renderQuestions, grading: renderGrading, analysis: renderAnalysis, review: renderReview, records: renderRecords }[S.tab];
  renderScope();
  try { await fn(); } catch (e) { toast(e.message, true); }
  renderScope();
  renderContinue();
}

async function goNext(step) {
  S.tab = step.tab;
  showNote('');
  await renderTab();
  const el = $(step.target);
  if (!el) return;
  const details = el.closest('details');
  if (details) details.open = true;
  el.scrollIntoView({ behavior: 'smooth', block: 'center' });
  el.classList.remove('flash');
  void el.offsetWidth;  // restart the animation
  el.classList.add('flash');
  setTimeout(() => el.classList.remove('flash'), 1700);
  if (el.matches('input, textarea')) el.focus({ preventScroll: true });
}

function renderProjectEdit() {
  const p = S.overview.project;
  const locked = S.overview.counts.question.total > 0;
  $('#project-edit').innerHTML = `<h3>编辑项目</h3><div class="grid-form">
    <label>项目名称<input class="pe-title" value="${esc(p.title)}"></label>
    <label>学科<select class="pe-subject" ${locked ? 'disabled' : ''}>${S.meta.subjects.some((s) => s.id === p.subject) ? '' : `<option value="${esc(p.subject)}" selected>${esc(p.subject_name)}</option>`}${S.meta.subjects.map((s) => `<option value="${s.id}" ${s.id === p.subject ? 'selected' : ''}>${esc(s.name)}</option>`).join('')}</select></label>
    ${p.course_id ? `<label>主题<select class="pe-unit"><option value="">（不归入主题）</option>${S.overview.units.map((u) => `<option value="${u.id}" ${u.id === p.unit_id ? 'selected' : ''}>${esc(u.title)}</option>`).join('')}</select></label>` : ''}
    <label>年级<input class="pe-grade" value="${esc(p.grade)}"></label>
    <label class="wide">老师说明<textarea class="pe-notes" rows="2">${esc(p.notes)}</textarea></label>
    ${locked ? '<div class="wide muted">项目里已经有题目，学科不能再改；需要换学科请新建项目。</div>' : ''}
    <div class="wide"><button data-act="save-project">保存</button> <button class="secondary" data-act="close-project-edit">取消</button></div></div>`;
}

// ---------- 01 备课设计 ----------

async function renderPrep() {
  const p = S.overview.project;
  $('#course-module').value = p.knowledge_module || '';
  $('#course-lesson').value = p.lesson || '';
  $('#course-profile').value = p.learner_profile || '';
  $('#course-notes').value = p.notes || '';
  $('#course-scope').value = p.lesson_scope || '';
  await renderCurriculum();
  if (p.carryover) renderCarryover(p.carryover);
  else if (p.offering_id) renderClassStatus(await api('GET', `/api/projects/${S.pid}/class-status`));
  else renderCarryover(null);
  const [mats, plans] = await Promise.all([
    api('GET', `/api/projects/${S.pid}/materials`),
    api('GET', `/api/projects/${S.pid}/lesson-plans`),
  ]);
  const mp = p.material_prefs || {};
  const off = new Set(mp.exclude || []);
  $('#materials').innerHTML = mats.length
    ? mats.map((m) => `<div class="item" data-mat="${m.id}"><div class="item-head"><label class="inline"><input type="checkbox" class="mat-use" ${off.has(m.id) ? '' : 'checked'}> <b>${esc(m.filename)}</b></label>
        <span><span class="pill">${m.chars} 字</span>${m.scope === 'course' ? ' <span class="pill info">课程资料</span>' : ''}</span></div>
        <div class="kv">${esc(m.text)}${m.chars > 400 ? '…' : ''}</div>
        <input class="mat-focus" placeholder="只用某章节或关键词（选填），例如：第二章 对数" value="${esc((mp.focus || {})[m.id] || '')}"></div>`).join('')
    : '<div class="empty">没有材料也可以生成备课方案；有教材或讲义时添加进来，内容范围会更准。</div>';
  $('#material-prefs-row').classList.toggle('hidden', !mats.length);
  $('#lesson-plan').innerHTML = plans.length
    ? lessonPlanItem(plans[0])
    : '<div class="empty">填写课时信息后点“生成备课方案”：教学目标、重点难点和课堂活动会一起生成，你可以修改后整体确认。</div>';
  $('#draft-lesson-plan').textContent = plans.length ? '重新生成' : '生成备课方案';
  $('#revise-row').classList.toggle('hidden', !plans.length);
  if (plans.length) {
    const pc = plans[0].content;
    $('#revise-target').innerHTML = [
      ...pc.objectives.map((o, i) => `<option value="objective:${o.id}">目标 ${i + 1}</option>`),
      ...(pc.activities || []).map((x, i) => `<option value="activity:${i + 1}">活动 ${i + 1}：${esc(x.name)}</option>`),
      '<option value="key_points">重点</option>', '<option value="difficulties">难点</option>', '<option value="notes">备注</option>',
    ].join('');
  }
  const others = S.overview.offerings.filter((o) => o.id !== p.offering_id);
  const adopted = plans.length && plans[0].confirmed_version;
  $('#use-plan-row').classList.toggle('hidden', !(p.course_id && others.length && adopted));
  $('#use-plan-class').innerHTML = others.map((o) => `<option value="${o.id}">${esc(o.class_name)}</option>`).join('');
}

function renderCarryover(c) {
  const box = $('#carryover-card');
  box.classList.toggle('hidden', !c);
  if (!c) return;
  const pats = c.patterns.map((x) => `<li><b>${esc(x.name)}</b> <span class="pill">${esc(x.affected)} 人</span> <span class="pill ${x.after_practice.includes('仍有') ? 'warn' : ''}">${esc(x.after_practice)}</span><div class="muted">${esc(x.description)}</div></li>`).join('');
  box.innerHTML = `<div class="card-head"><h3>上一课时的学情（已带入）</h3><button class="link" data-open-project="${esc(c.from_project)}">回到上一课时</button></div>
    <p class="muted">来自「${esc(c.from_title)}」。生成备课方案时会参考这些问题，安排简短的回顾或巩固。</p>
    ${c.summary ? `<p>${esc(c.summary)}</p>` : ''}
    ${pats ? `<ul class="carry-list">${pats}</ul>` : ''}
    ${c.review_focus.length ? `<p class="muted">上一课时讲评重点：${c.review_focus.map(esc).join('；')}</p>` : ''}`;
}

// 本班学情 for a task of a class: every item can be left out of what the model reads, and put back.
function renderClassStatus(st) {
  const box = $('#carryover-card');
  box.classList.toggle('hidden', !st);
  if (!st) return;
  const pats = st.patterns.map((x) => `<li><label class="inline"><input type="checkbox" class="cs-pattern" value="${esc(x.lineage)}" ${x.excluded ? '' : 'checked'}>
    <b>${esc(x.name)}</b></label> <span class="pill">${esc(x.affected)} 人</span> <span class="pill ${x.after_practice.includes('仍有') ? 'warn' : ''}">${esc(x.after_practice)}</span>
    <div class="muted">${esc(x.description)}</div></li>`).join('');
  box.innerHTML = `<div class="card-head"><h3>本班学情（${esc(st.from_title)}）</h3></div>
    <p class="muted">备课和出题时会参考勾选的内容；取消勾选的不会发给模型，可以随时勾回来。只来自本班已确认的分析和问题模式。</p>
    ${st.summary ? `<label class="inline"><input type="checkbox" id="cs-summary" ${st.summary_excluded ? '' : 'checked'}> 学情概述：${esc(st.summary)}</label>` : ''}
    ${st.note ? `<p class="kv flag">${esc(st.note)}</p>` : ''}
    ${pats ? `<ul class="carry-list">${pats}</ul>` : ''}`;
}

function nextLessonName(lesson) {
  const m = /第\s*(\d+)\s*课时/.exec(lesson || '');
  return m ? lesson.replace(/第\s*\d+\s*课时/, `第 ${Number(m[1]) + 1} 课时`) : '';
}

async function openNextLesson() {
  const o = S.overview;
  if (o.next_lesson) { S.tab = 'prep'; await loadProjects(o.next_lesson.id); return; }
  const card = $('#next-lesson-card');
  $('#nl-module').value = o.project.knowledge_module || '';
  $('#nl-lesson').value = nextLessonName(o.project.lesson);
  const pats = (await api('GET', `/api/projects/${S.pid}/patterns`)).filter((x) => x.confirmed_version);
  $('#nl-carry').innerHTML = `<div class="muted">将带入：已确认的问题模式 ${pats.length} 个${pats.length ? `（${pats.map((x) => esc(x.content.name)).join('、')}）` : ''}、学情概述、讲评重点和本课目标。</div>`;
  card.classList.remove('hidden');
  card.scrollIntoView({ behavior: 'smooth', block: 'center' });
}

function courseInfo() {
  const info = {
    knowledge_module: $('#course-module').value, lesson: $('#course-lesson').value,
    learner_profile: $('#course-profile').value, notes: $('#course-notes').value, lesson_scope: $('#course-scope').value,
  };
  if (S.curTouched) {
    info.curriculum_ids = $$('#curriculum-list input:checked').map((i) => i.value);
    info.curriculum_off = $('#curriculum-off').checked;
  }
  return info;
}

// 对应课标: suggested items for the course info; the teacher's ticks are saved with the course info.
async function renderCurriculum(q) {
  const box = $('#curriculum-box');
  const p = S.overview.project;
  box.classList.toggle('hidden', !p.has_curriculum);
  if (!p.has_curriculum) return;
  const keep = q !== undefined ? $$('#curriculum-list input:checked').map((i) => ({ id: i.value, html: i.closest('.cur-item').outerHTML })) : [];
  const r = await api('GET', `/api/projects/${S.pid}/curriculum${q !== undefined ? `?q=${encodeURIComponent(q)}` : ''}`);
  if (q === undefined) S.curTouched = false;
  $('#curriculum-src').innerHTML = `依据<a href="${esc(r.source_url)}" target="_blank" rel="noopener">${esc(r.title)}</a>（${esc(r.scope)}）`;
  $('#curriculum-note').textContent = q !== undefined
    ? `“${q}”的搜索结果，勾选后保存课时信息。`
    : r.weak ? '没有找到明确对应的课标条目：请从下面勾选或换个说法搜索；都不勾选时，备课方案不受课标约束。'
    : r.chosen_by === 'teacher' ? '按你勾选的条目生成。目标会标出依据哪一条，要求不能高于课标用词。'
      : '按课时信息自动匹配，生成备课方案时使用勾选的条目；可以增减，保存课时信息后生效。';
  const selected = new Set(q !== undefined ? keep.map((k) => k.id) : r.selected);
  const row = (it) => `<label class="cur-item">
      <input type="checkbox" value="${esc(it.id)}" ${selected.has(it.id) ? 'checked' : ''}>
      <span><b>${esc(it.label)}</b>${it.optional ? ' <span class="pill">选学</span>' : ''}<span class="muted"> · 第 ${it.page} 页</span><br>${esc(it.text)}</span></label>`;
  const rest = r.items.filter((it) => !keep.some((k) => k.id === it.id));
  const picked = rest.filter((it) => selected.has(it.id)).map(row);
  const others = rest.filter((it) => !selected.has(it.id)).map(row);
  // Only the ticked items are shown; other candidates stay folded so the page stays calm.
  $('#curriculum-list').innerHTML = keep.map((k) => k.html).join('') + picked.join('')
    + (others.length ? `<details class="cur-more" ${q !== undefined && !picked.length ? 'open' : ''}><summary>其他可能相关的条目（${others.length}）</summary>${others.join('')}</details>` : '')
    + (!keep.length && !rest.length ? '<div class="empty">没有匹配的课标条目，换个说法搜索，或勾选“不参考课标”。</div>' : '');
  $('#curriculum-off').checked = r.off;
  box.classList.toggle('off', r.off);
}

async function saveCourseInfo() {
  const info = courseInfo();
  const p = S.overview.project;
  const changed = info.knowledge_module !== (p.knowledge_module || '') || info.lesson !== (p.lesson || '')
    || info.learner_profile !== (p.learner_profile || '') || info.notes !== (p.notes || '')
    || info.lesson_scope !== (p.lesson_scope || '') || !!S.curTouched;
  if (changed) { await api('PATCH', `/api/projects/${S.pid}`, info); S.curTouched = false; }
  return changed;
}

// ---------- 02 出题改题 ----------

async function renderQuestions() {
  fillRoundSelect('#q-round');
  if (S.showRound) { $('#q-round').value = String(S.showRound); S.showRound = null; }
  const types = S.overview.project.question_types;
  const box = $('#type-boxes');
  if (box.dataset.pid !== S.pid) {
    box.dataset.pid = S.pid;
    box.innerHTML = types.map((t) => `<label><input type="checkbox" value="${t}" ${t !== 'writing' ? 'checked' : ''}> ${esc(S.meta.question_types[t])}</label>`).join('');
  }
  const round = Number($('#q-round').value || 1);
  const [qs, cov] = await Promise.all([
    api('GET', `/api/projects/${S.pid}/questions?round=${round}`),
    api('GET', `/api/projects/${S.pid}/coverage?round=${round}`),
  ]);
  S.goals = Object.fromEntries(cov.objectives.map((o, i) => [o.id, { no: i + 1, text: o.text }]));
  renderCoverage(cov);
  const candidates = qs.filter((q) => q.status === 'candidate' && q.candidate && S.overview.pending_ab.includes(q.candidate.candidate_group));
  const groups = {};
  candidates.forEach((q) => { (groups[q.candidate.candidate_group] ||= { A: [], B: [] })[q.candidate.position].push(q); });
  $('#ab-panel').innerHTML = Object.entries(groups).map(([g, sides]) => `<div class="card" data-group="${g}">
    <h3>A/B 两版候选：请选择更合适的一版</h3>
    <p class="muted">两版只在出题提示上有一处不同；左右位置是随机的。选择后被采用的一版进入草稿，未采用的保留为研究记录。</p>
    <div class="ab">${['A', 'B'].map((pos) => `<div><h4>候选 ${pos}</h4>${sides[pos].map((q) => questionBody(q)).join('') || '<div class="empty">（无）</div>'}</div>`).join('')}</div>
    <div class="row"><button data-verdict="A">采用 A</button><button data-verdict="B">采用 B</button>
      <button class="secondary" data-verdict="both_ok" data-work="A">两份都可用，用 A 继续</button><button class="secondary" data-verdict="both_ok" data-work="B">两份都可用，用 B 继续</button>
      <button class="secondary" data-verdict="neither">都不合适</button><button class="link" data-verdict="skip">暂不比较</button></div>
    <details><summary>补充一句（选填）</summary>
      <div class="row"><select class="ab-pref"><option value="none">不评价</option><option value="better">采用的这份更适合本次任务</option><option value="work_only">只是选它继续修改</option></select>
        <input class="ab-reason" placeholder="理由（选填）"></div>
      <div class="checks">${['更贴合目标', '难度更合适', '更清楚', '有事实错误', '依据不足', '其他'].map((t) => `<label><input type="checkbox" class="ab-tag" value="${t}"> ${t}</label>`).join('')}</div>
    </details>
  </div>`).join('');
  const offerings = S.overview.offerings;
  const confirmedHere = qs.some((q) => q.confirmed_version);
  $('#assign-row').classList.toggle('hidden', !(S.overview.project.course_id && offerings.length && confirmedHere));
  const prevClass = $('#assign-class').value;
  $('#assign-class').innerHTML = offerings.map((o) => `<option value="${o.id}">${esc(o.class_name)}</option>`).join('');
  const parent = S.pendingParent && S.pendingParent.round === round ? S.pendingParent : null;
  if (parent) $('#assign-class').value = parent.offering_id;
  else if (prevClass && offerings.some((o) => o.id === prevClass)) $('#assign-class').value = prevClass;
  else if (S.overview.project.offering_id) $('#assign-class').value = S.overview.project.offering_id;
  $('#assign-btn').textContent = parent ? `布置给本班作为讲评后再练（第 ${round} 轮）` : `布置第 ${round} 轮已确认的题目`;
  const shown = qs.filter((q) => q.status !== 'candidate');
  $('#questions').innerHTML = (shown.length ? shown.map(questionItem).join('') : '<div class="empty">这一轮还没有题目。</div>')
    + `<div class="row"><button class="secondary" data-act="new-question">手工添加题目</button></div><div class="new-question-area"></div>`;
}

// 目标覆盖: one line per objective; an objective without questions gets a one-click 补题.
function renderCoverage(cov) {
  const box = $('#coverage');
  if (!cov.objectives.length) { box.innerHTML = ''; return; }
  box.innerHTML = `<div class="coverage"><div class="label">目标覆盖（第 ${cov.round} 轮）</div>${cov.objectives.map((o, i) => `<div class="cov-row${o.total ? '' : ' none'}">
      <span class="cov-no">目标 ${i + 1}</span><span class="cov-text">${esc(o.text)}</span>
      <span class="cov-count">${o.total ? `${o.total} 道 · 已确认 ${o.confirmed}` : '<span class="pill bad">还没有题</span>'}</span>
      ${o.flagged.length ? `<span class="pill warn">第 ${o.flagged.join('、')} 题待核对</span>` : ''}
      ${cov.round === 1 ? `<button class="small secondary" data-fill="${esc(o.id)}">补 1 道</button>` : ''}</div>`).join('')}
    ${cov.unassigned.length ? `<div class="muted">第 ${cov.unassigned.join('、')} 题没有对应目标</div>` : ''}</div>`;
}

function questionBody(q) {
  const c = q.content;
  const r = q.rubric ? q.rubric.content : { points: [], total: 0 };
  const checks = c.checks || {};
  const warn = [];
  if (checks.answer_suspect) warn.push('<span class="pill bad">答案存疑</span>');
  if ((checks.flags || []).length) warn.push('<span class="pill warn">需核对</span>');
  if (checks.solver === 'agree' && !(checks.flags || []).length) warn.push('<span class="pill ok">自检通过</span>');
  if (checks.difficulty) warn.push(`<span class="pill">难度 ${esc(checks.difficulty)}</span>`);
  const notes = [...(checks.flags || []), ...(checks.notes || [])].map((n) => `<div class="kv flag">⚠ ${esc(n)}</div>`).join('');
  const goal = (c.objective_ids || []).map((id) => S.goals && S.goals[id]).filter(Boolean)[0];
  const options = (c.options || []).map((o, i) => `<div>${'ABCDEFGH'[i]}. ${esc(o)}</div>`).join('');
  return `<div class="item-head"><div><b>第 ${c.no} 题</b> · ${esc(S.meta.question_types[c.type] || c.type)} · 满分 ${r.total}
      ${(c.target_patterns || []).length ? '<span class="pill info">巩固练习</span>' : ''}</div><div>${warn.join(' ')}</div></div>
    ${goal ? `<div class="kv">考查目标 ${goal.no}：${esc(goal.text)}${c.difficulty_requested ? ` · 要求难度 ${esc(c.difficulty_requested)}` : ''}${c.instruction ? ` · 换题要求：${esc(c.instruction)}` : ''}</div>` : ''}
    <div class="stem">${esc(c.stem)}</div>${options}
    ${c.answer_key ? `<div class="kv">答案：${esc(c.answer_key)}${(c.accepted || []).length ? '（也接受：' + esc(c.accepted.join('、')) + '）' : ''}</div>` : ''}
    <div class="kv">参考答案：${esc(c.reference)}</div>${notes}
    ${checks.solver_answer ? `<details><summary>查看独立解题的过程（${esc(checks.solver_score || '')}）</summary><div class="answer">${esc(checks.solver_answer)}</div></details>` : ''}
    <table><tr><th>要点</th><th>评分要求</th><th>分值</th><th>自动核对</th></tr>
    ${r.points.map((p) => `<tr><td>${esc(p.id)}</td><td>${esc(p.description)}</td><td>${p.score}</td><td>${p.check ? esc(p.check.kind + '：' + p.check.expected) : ''}</td></tr>`).join('')}</table>`;
}

function questionItem(q) {
  const qDraft = q.status === 'draft';
  const rDraft = q.rubric && q.rubric.status === 'draft';
  const needsKind = (qDraft && q.confirmed_version) || (rDraft && q.rubric.confirmed_version);
  return `<div class="item" data-id="${q.id}" data-rubric="${q.rubric ? q.rubric.id : ''}">
    <div class="item-head"><div></div><div>题目 ${statusPill(q)} · 评分标准 ${statusPill(q.rubric)}</div></div>
    ${questionBody(q)}
    <div class="item-actions" style="margin-top:8px">
      <button class="small secondary" data-act="edit-question">编辑</button>
      <button class="small secondary" data-act="regen-open">换一道</button>
      ${(q.content.checks || {}).answer_suspect && (q.content.checks || {}).solver_answer ? '<button class="small secondary" data-act="adopt-solver">采用独立解题的答案</button>' : ''}
      ${qDraft || rDraft ? `${changeKindSelect(needsKind)}<button class="small" data-act="confirm-question">确认题目与评分标准</button>` : ''}
    </div><div class="edit-area"></div></div>`;
}

function questionForm(q) {
  const c = q ? q.content : { type: S.overview.project.question_types[0], stem: '', options: [], answer_key: '', accepted: [], reference: '' };
  const pts = q && q.rubric ? q.rubric.content.points : [{ id: 'P1', description: '', score: 2 }];
  return `<div class="edit-grid">
    <label>题型<select class="f-type">${S.overview.project.question_types.map((t) => `<option value="${t}" ${t === c.type ? 'selected' : ''}>${esc(S.meta.question_types[t])}</option>`).join('')}</select></label>
    <label>题干<textarea class="f-stem" rows="3">${esc(c.stem)}</textarea></label>
    <label>选项（选择题，每行一个，不写字母）<textarea class="f-options" rows="2">${esc((c.options || []).join('\n'))}</textarea></label>
    <label>答案（选择/判断/填空）<input class="f-key" value="${esc(c.answer_key)}"></label>
    <label>其他可接受答案（填空题，每行一个）<textarea class="f-accepted" rows="2">${esc((c.accepted || []).join('\n'))}</textarea></label>
    <label>参考答案<textarea class="f-ref" rows="3">${esc(c.reference)}</textarea></label>
    <span class="label">评分要点</span><div class="f-points">${pts.map(pointRow).join('')}</div>
    <div class="item-actions"><button class="small secondary" data-act="add-point">添加要点</button>
      <button class="small" data-act="${q ? 'save-question' : 'create-question'}">${q ? '保存为新版本' : '添加题目'}</button>
      <button class="small secondary" data-act="cancel-edit">取消</button></div></div>`;
}

function pointRow(p) {
  return `<div class="point-row" data-pid="${esc(p.id || '')}" data-check='${esc(JSON.stringify(p.check || null))}' data-target="${esc(p.target_pattern || '')}">
    <input class="p-desc" value="${esc(p.description)}" placeholder="评分要求"><input class="p-score" type="number" step="0.5" min="0.5" value="${p.score}">
    <button class="small danger" data-act="remove-point" title="删除">×</button></div>`;
}

function readQuestionForm(area) {
  const lines = (v) => v.split('\n').map((s) => s.trim()).filter(Boolean);
  const question = {
    type: $('.f-type', area).value, stem: $('.f-stem', area).value.trim(), options: lines($('.f-options', area).value),
    answer_key: $('.f-key', area).value.trim(), accepted: lines($('.f-accepted', area).value), reference: $('.f-ref', area).value.trim(),
  };
  const points = $$('.point-row', area).map((row, i) => {
    const p = { id: row.dataset.pid || `P${i + 1}`, description: $('.p-desc', row).value.trim(), score: Number($('.p-score', row).value) };
    const check = JSON.parse(row.dataset.check || 'null');
    if (check) p.check = check;
    if (row.dataset.target) p.target_pattern = row.dataset.target;
    return p;
  });
  return { question, rubric: { points } };
}

// ---------- 03 作业批改 ----------

async function renderGrading() {
  fillRoundSelect('#g-round');
  const round = Number($('#g-round').value || 1);
  const a = S.aid ? await api('GET', `/api/assignments/${S.aid}`) : null;
  const [learners, queue, gradings] = await Promise.all([
    a ? Promise.resolve(a.members) : api('GET', `/api/projects/${S.pid}/learners`),
    api('GET', `/api/projects/${S.pid}/review-queue?round=${round}${scopeQS()}`),
    api('GET', `/api/projects/${S.pid}/gradings?round=${round}${scopeQS()}`),
  ]);
  $('#ph-learners').innerHTML = learners.map((l) => `<option value="${esc(l.alias)}">`).join('');
  [$('#roster'), $('#add-roster'), $('#roster-box > .label')].forEach((el) => el && el.classList.toggle('hidden', !!a));
  $('#roster-list').textContent = a
    ? `${a.class_name}的名单（布置时 ${learners.length} 人）：${learners.map((l) => l.alias).join('、') || '空'}。名单在“我的教学”的班级里维护；名单外的学生可勾选“自动加入”。`
    : (learners.length ? `名单（${learners.length} 人）：${learners.map((l) => l.alias).join('、')}` : '名单为空');
  $('#correction-box').innerHTML = a ? `<details><summary>本次评分依据（${a.pinned.length} 道题，版本已固定）</summary>
    ${a.pinned.map((x) => `<div class="row" data-qlin="${x.question_lineage}">第 ${x.no} 题 · 题目第 ${x.question_version} 版 · 评分标准第 ${x.rubric_version} 版
      <button class="small secondary" data-act="correct-open">更正本次评分依据</button></div><div class="correction-area" data-for="${x.question_lineage}"></div>`).join('')}</details>` : '';
  $('#queue-count').textContent = queue.length;
  $('#queue').innerHTML = queue.length ? queue.map(queueItem).join('') : '<div class="empty">复核队列为空。</div>';
  $('#gradings').innerHTML = gradings.length ? `<table><tr><th>学生</th><th>题号</th><th>得分</th><th>状态</th><th>说明</th></tr>
    ${gradings.sort((a, b) => a.content.question_no - b.content.question_no || a.content.learner_alias.localeCompare(b.content.learner_alias)).map(gradingRow).join('')}</table>` : '<div class="empty">这一轮还没有批改。</div>';
}

function gradingRow(g) {
  const c = g.content;
  const state = g.status === 'confirmed' ? '<span class="pill ok">已确认</span>' : (c.needs_teacher ? '<span class="pill warn">需复核</span>' : '<span class="pill">待确认</span>');
  const lost = c.points.filter((p) => p.status !== 'earned').map((p) => `${p.id}${p.error_tag ? '：' + p.error_tag : ''}`).join('；');
  return `<tr><td>${esc(c.learner_alias)}</td><td>${c.question_no}</td><td>${c.total}/${c.max_total}${c.undetermined ? '（' + c.undetermined + ' 个要点待判）' : ''}</td>
    <td>${state} ${g.stale ? '<span class="pill bad">依据已变化</span>' : ''} ${g.origin === 'teacher' ? '<span class="pill">老师改判</span>' : ''}</td>
    <td>${esc(lost)}<details><summary>详情</summary>${pointsDetail(c.points)}</details></td></tr>`;
}

function pointsDetail(points) {
  return points.map((p) => `<div class="kv"><b>${esc(p.id)}</b> ${esc(STATUS_LABEL[p.status] || p.status)} ${p.score ?? '-'}/${p.max} · 来源：${esc({ key: '答案比对', tool: '工具核对', model: '模型', teacher: '老师', rule: '规则' }[p.source] || p.source)}
    ${p.evidence_quote ? `<div class="quote">${esc(p.evidence_quote)}</div>` : ''}${esc(p.reason)}${p.error_note ? '（' + esc(p.error_note) + '）' : ''}
    ${p.review ? `<br>复核：${p.review.agreed ? '同意' : '不同意'}（${esc(p.review.status)} ${p.review.score ?? ''}）${esc(p.review.reason || '')}` : ''}
    ${p.audit ? `<br>抽检意见：${esc(p.audit.status)} ${esc(p.audit.reason || '')}` : ''}</div>`).join('');
}

const photoUrl = (name) => `/api/photos/${encodeURIComponent(name)}`;

function photoThumbs(names) {
  return `<div class="ph-thumbs">${names.map((n, i) => `<a href="${photoUrl(n)}" target="_blank" rel="noopener" title="第 ${i + 1} 张，点击看原图"><img src="${photoUrl(n)}" alt="第 ${i + 1} 张照片" loading="lazy"></a>`).join('')}</div>`;
}

function renderPicked() {
  const files = S.photoFiles || [];
  $$('#ph-picked img').forEach((img) => URL.revokeObjectURL(img.src));
  $('#ph-picked').innerHTML = files.length
    ? `<div class="ph-thumbs">${files.map((f, i) => `<figure><img src="${URL.createObjectURL(f)}" alt="第 ${i + 1} 张"><button class="small secondary" data-remove="${i}">移除</button></figure>`).join('')}</div>
       <div class="muted">已选 ${files.length} 张（最多 8 张），请按试卷顺序添加</div>`
    : '';
}

function renderPhotoDraft() {
  const d = S.photoDraft;
  if (!d) { $('#ph-draft').innerHTML = ''; return; }
  const unsure = d.answers.filter((a) => a.uncertain).length;
  $('#ph-draft').innerHTML = `<h4>${esc(d.learner)} 的识别结果（第 ${d.round} 轮）${unsure ? ` <span class="pill warn">${unsure} 题需要对照照片核对</span>` : ''}</h4>
    ${d.note ? `<div class="kv flag">${esc(d.note)}</div>` : ''}
    ${(d.dropped || []).length ? `<div class="kv flag">未采用：${esc(d.dropped.join('；'))}</div>` : ''}
    <div class="ph-review">
      <div>${photoThumbs(d.photos)}</div>
      <div>${d.answers.map((a) => `<div class="ph-answer"><div class="kv"><b>第 ${a.question_no} 题</b> ${esc(a.stem || '')}</div>
        ${a.missing ? '<span class="pill">照片中没找到</span> ' : ''}${a.uncertain ? '<span class="pill warn">看不清，请核对</span> ' : ''}${a.note && !a.missing ? `<span class="kv flag">${esc(a.note)}</span>` : ''}
        <textarea data-no="${a.question_no}" rows="${Math.min(8, Math.max(2, (a.text.match(/\n/g) || []).length + 2))}" placeholder="没有作答就留空">${esc(a.text)}</textarea></div>`).join('')}</div>
    </div>
    <div class="row"><label class="inline"><input type="checkbox" id="ph-add-missing"> 名单外的学生自动加入</label>
      <button id="ph-import">确认导入</button> <button id="ph-discard" class="secondary">放弃</button></div>
    <p class="muted">导入的是上面框里的文字，空着的题不导入；照片会附在作答上，复核时可以对照。</p>`;
}

function queueItem(g) {
  const c = g.content;
  const tags = S.subject.tags;
  return `<div class="item" data-id="${g.id}">
    <div class="item-head"><div><b>${esc(c.learner_alias)}</b> · 第 ${c.question_no} 题 · 当前 ${c.total}/${c.max_total}</div>
      <div>${c.risks.map((r) => `<span class="pill warn">${esc(RISK_LABEL[r] || r)}</span>`).join(' ')}</div></div>
    ${g.question ? `<details><summary>题目</summary><div class="stem">${esc(g.question.stem)}</div><div class="kv">参考答案：${esc(g.question.reference)}</div></details>` : ''}
    <div class="answer">${esc(g.answer)}</div>
    ${(g.photos || []).length ? `<details><summary>对照试卷照片（${g.photos.length} 张）</summary>${photoThumbs(g.photos)}</details>` : ''}
    <table><tr><th>要点</th><th>判定与依据</th><th>老师给分</th><th>错误标签</th></tr>
    ${c.points.map((p) => {
      const suggested = p.suggested || {};
      const value = p.score ?? suggested.score ?? '';
      const tag = p.error_tag || suggested.error_tag || '';
      return `<tr data-point="${esc(p.id)}" data-max="${p.max}"><td>${esc(p.id)}<div class="kv">${esc(p.description)}</div></td>
        <td>${esc(STATUS_LABEL[p.status] || p.status)}${p.status === 'undetermined' && suggested.status ? `（模型建议：${esc(STATUS_LABEL[suggested.status] || suggested.status)} ${suggested.score ?? ''}）` : ''}
          ${p.evidence_quote ? `<div class="quote">${esc(p.evidence_quote)}</div>` : ''}<div class="kv">${esc(p.reason)}${suggested.reason && p.status === 'undetermined' ? '；' + esc(suggested.reason) : ''}</div>
          ${p.review ? `<div class="kv">复核模型：${esc(STATUS_LABEL[p.review.status] || p.review.status)} ${p.review.score ?? ''} · ${esc(p.review.reason || '')}</div>` : ''}
          ${p.audit ? `<div class="kv">抽检：${esc(STATUS_LABEL[p.audit.status] || p.audit.status)} · ${esc(p.audit.reason || '')}</div>` : ''}</td>
        <td><input class="o-score" type="number" min="0" max="${p.max}" step="0.5" value="${value}"> / ${p.max}</td>
        <td><select class="o-tag"><option value="">（得满分不填）</option>${tags.map((t) => `<option ${t === tag ? 'selected' : ''}>${esc(t)}</option>`).join('')}</select>
          <input class="o-note" placeholder="备注" value="${esc(p.error_note || suggested.error_note || '')}"></td></tr>`;
    }).join('')}</table>
    <div class="row"><input class="o-comment" placeholder="复核说明（选填）"><button class="small" data-act="override">提交老师判定</button></div>
  </div>`;
}

// ---------- 04 学情分析 ----------

async function renderAnalysis() {
  fillRoundSelect('#a-round');
  const round = Number($('#a-round').value || 1);
  const qs = S.aid ? `?assignment_id=${encodeURIComponent(S.aid)}` : '';
  const [analyses, patterns] = await Promise.all([api('GET', `/api/projects/${S.pid}/analyses${qs}`), api('GET', `/api/projects/${S.pid}/patterns${qs}`)]);
  const mine = analyses.filter((a) => (a.content.round || 1) === round);
  const latest = mine[mine.length - 1];
  if (!latest) { $('#analysis').innerHTML = '<div class="empty">这一轮还没有学情分析。</div>'; }
  else {
    const detail = await api('GET', `/api/artifacts/${latest.id}`);
    $('#analysis').innerHTML = analysisView(latest, detail.context_note);
  }
  $('#patterns').innerHTML = patterns.length ? patterns.map(patternItem).join('') : '<div class="empty">还没有问题模式。</div>';
  const comps = await api('GET', `/api/projects/${S.pid}/comparisons${qs}`);
  const latestCmp = comps[comps.length - 1];
  $('#comparison').innerHTML = latestCmp ? comparisonView(latestCmp) : '<div class="empty">还没有对比结果。需要先有“讲评后再练”的一轮批改。</div>';
  const r = S.overview.latest_round || 1;
  if (r > 1) { $('#cmp-a').value = r - 1; $('#cmp-b').value = r; }
  const cur = currentAssignment();
  [$('#cmp-a'), $('#cmp-b')].forEach((el) => { el.disabled = !!cur; });
  if (cur && !cur.parent_id) $('#comparison').insertAdjacentHTML('afterbegin', '<div class="kv">这次布置不是讲评后再练；对比要在再练那次布置下进行。</div>');
}

function analysisView(a, note) {
  const c = a.content;
  const head = `<div class="item-head"><div>${statusPill(a)}</div><div class="muted">${esc(note || '')}</div></div>`;
  if (c.mode === 'pitfalls') {
    return `${head}<div class="banner warn">${esc(c.note)}</div>${c.pitfalls.map((p) => `<div class="item"><b>${esc(p.name)}</b>（第 ${p.question_nos.join('、')} 题）<div class="kv">${esc(p.description)}</div></div>`).join('')}`;
  }
  const stats = Object.entries(c.stats || {}).map(([no, s]) => `<tr><td>第 ${no} 题</td><td>${s['人数']}</td><td>${s['得分率'] == null ? '-' : Math.round(s['得分率'] * 100) + '%'}</td>
    <td>${Object.entries(s['要点得分率']).map(([k, v]) => `${k}：${v == null ? '-' : Math.round(v * 100) + '%'}`).join('；')}</td></tr>`).join('');
  const tags = Object.entries(c.tag_counts || {}).sort((a, b) => b[1] - a[1]).map(([t, n]) => `<span class="pill">${esc(t)} · ${n} 人</span>`).join(' ');
  const pct = (v) => (v == null ? '-' : Math.round(v * 100) + '%');
  const objs = (c.objective_stats || []).map((o) => `<tr><td>${esc(o.id)} ${esc(o['目标'])}</td><td>${o['题号'].length ? '第 ' + o['题号'].join('、') + ' 题' : '<span class="muted">没有题目</span>'}</td>
    <td>${pct(o['得分率'])}</td><td>${o['人数'] ? `${o['全部得分人数']}/${o['人数']}` : '-'}</td></tr>`).join('');
  const cov = c.coverage ? `<div class="banner ${c.partial ? 'warn' : 'info'}">${c.partial ? '部分分析，不是全班结论：' : ''}${esc(c.coverage.label)}</div>` : '';
  const src = c.summary_source === 'server' ? '<div class="kv">概述由系统根据通过核对的证据写出（模型的概述没有全部通过证据核对）。</div>' : '';
  const adopt = a.status !== 'confirmed' ? `<div class="item-actions" data-id="${a.id}"><button class="small" data-act="confirm">确认这份分析</button><span class="muted">确认后，才会作为本班学情提供给之后的备课和出题</span></div>` : '';
  return `${head}${cov}<p>${esc(c.summary)}</p>${src}${adopt}
    ${objs ? `<h4>目标达成</h4><table><tr><th>教学目标</th><th>对应题目</th><th>得分率</th><th>全部得分</th></tr>${objs}</table><h4>各题得分</h4>` : ''}
    <table><tr><th>题目</th><th>人数</th><th>得分率</th><th>各要点得分率</th></tr>${stats}</table>
    ${tags ? `<h4>失分标签（按涉及学生数）</h4>${tags}` : ''}
    ${(c.insufficient || []).length ? `<h4>证据不足，建议补查</h4>${c.insufficient.map((x) => `<div class="item"><b>${esc(x.question)}</b><div class="kv">${esc(x.why)}</div></div>`).join('')}` : ''}
    ${(c.dropped_patterns || []).length ? `<div class="kv">已丢弃没有可核对证据的模式：${esc(c.dropped_patterns.join('、'))}</div>` : ''}`;
}

function patternItem(p) {
  const c = p.content;
  const needsKind = p.status === 'draft' && p.confirmed_version;
  return `<div class="item" data-id="${p.id}">
    <div class="item-head"><div><b>${esc(c.name)}</b> <span class="pill">第 ${c.round} 轮 · ${c.affected_learners.length}/${c.total_learners} 人</span></div><div>${statusPill(p)}</div></div>
    <div class="kv">${esc(c.description)}</div>
    ${c.tags.length ? `<div>${c.tags.map((t) => `<span class="pill">${esc(t)}</span>`).join(' ')}</div>` : ''}
    <details><summary>证据（${c.evidence.length} 条）与涉及学生</summary>
      ${c.evidence.map((e) => `<div class="kv">${esc(e.learner)} · 第 ${e.question_no} 题 ${esc(e.point_id)}<div class="quote">${esc(e.quote)}</div></div>`).join('')}
      <div class="kv">涉及学生：${esc(c.affected_learners.join('、'))}</div></details>
    <div class="item-actions"><button class="small secondary" data-act="edit-pattern">改名称/描述</button>
      ${p.status !== 'confirmed' ? `${changeKindSelect(needsKind)}<button class="small" data-act="confirm">确认</button>` : ''}</div>
    <div class="edit-area"></div></div>`;
}

// ---------- 01 lesson plan ----------

const lines = (xs) => (xs && xs.length ? `<ul class="lines">${xs.map((x) => `<li>${esc(x)}</li>`).join('')}</ul>` : '<div class="kv">（无）</div>');

function lessonPlanItem(a) {
  const c = a.content;
  const total = (c.activities || []).reduce((n, x) => n + (Number(x.minutes) || 0), 0);
  const goalNo = Object.fromEntries((c.objectives || []).map((o, i) => [o.id, i + 1]));
  return `<div class="item" data-id="${a.id}">
    <div class="item-head"><div>${statusPill(a)}</div></div>
    <h4>教学目标</h4>
    <ol class="lines">${(c.objectives || []).map((o) => `<li><b>${esc(o.text)}</b> ${o.inferred ? '<span class="pill">由导入试卷反推</span>' : ''}
      ${(o.checks || []).map((x) => `<span class="pill warn">${esc(x)}</span>`).join(' ')}
      ${o.beyond_reason ? `<span class="pill warn" title="${esc(o.beyond_reason)}">超出课标：${esc(o.beyond_reason)}</span>` : ''}
      <div class="kv">达成标准：${esc(o.success_criterion || '（未填写）')}</div>
      ${(o.standard_refs || []).length ? `<div class="std-refs">依据课标：${o.standard_refs.map((s) => `<span class="std" tabindex="0" title="${esc(s.text)}（课标第 ${s.page} 页）">${esc(s.label)}</span>`).join('')}</div>` : ''}</li>`).join('')}</ol>
    <h4>重点</h4>${lines(c.key_points)}<h4>难点</h4>${lines(c.difficulties)}
    <h4>课堂活动（共 ${total} 分钟）</h4>
    <table><tr><th>活动</th><th>用时</th><th>做法</th><th>对应目标</th></tr>${(c.activities || []).map((x) => `<tr><td>${esc(x.name)}</td><td>${x.minutes} 分钟</td><td>${esc(x.description)}</td>
      <td>${(x.objective_ids || []).map((id) => (goalNo[id] ? `目标 ${goalNo[id]}` : '')).join('、')}</td></tr>`).join('')}</table>
    ${c.notes ? `<div class="kv">提醒：${esc(c.notes)}</div>` : ''}
    ${c.revision ? `<div class="kv flag">这一版只改了：${esc(c.revision.target)}（${esc(c.revision.instruction)}）</div>` : ''}
    <div class="item-actions"><button class="small secondary" data-act="edit-lesson-plan">修改</button>
      ${a.status !== 'confirmed' ? `${changeKindSelect(a.confirmed_version)}<button class="small" data-act="confirm">确认备课方案</button>` : ''}</div>
    <div class="edit-area"></div></div>`;
}

function objectiveRow(o = {}) {
  return `<div class="obj-row" data-oid="${esc(o.id || '')}" data-inferred="${o.inferred ? '1' : ''}">
    <input class="o-text" placeholder="教学目标" value="${esc(o.text || '')}">
    <input class="o-crit" placeholder="达成标准：怎样从作答判断达成" value="${esc(o.success_criterion || '')}">
    <button class="small danger" data-act="remove-objective-row" title="删除">×</button></div>`;
}

function lessonPlanForm(c) {
  const goalNo = Object.fromEntries((c.objectives || []).map((o, i) => [o.id, i + 1]));
  const acts = (c.activities || []).map((x) => [x.name, x.minutes, x.description, (x.objective_ids || []).map((id) => goalNo[id]).filter(Boolean).join(',')].join('｜')).join('\n');
  return `<div class="edit-grid">
    <span class="label">教学目标</span><div class="f-objectives">${(c.objectives || []).map(objectiveRow).join('')}</div>
    <div><button class="small secondary" data-act="add-objective-row">添加目标</button></div>
    <label>重点（每行一条）<textarea class="f-key" rows="3">${esc((c.key_points || []).join('\n'))}</textarea></label>
    <label>难点（每行一条）<textarea class="f-diff" rows="2">${esc((c.difficulties || []).join('\n'))}</textarea></label>
    <label>课堂活动（每行：名称｜分钟｜做法｜对应目标序号，如 1,2）<textarea class="f-acts" rows="5">${esc(acts)}</textarea></label>
    <label>提醒<input class="f-notes" value="${esc(c.notes || '')}"></label>
    <div class="item-actions"><button class="small" data-act="save-lesson-plan">保存修改</button><button class="small secondary" data-act="cancel-edit">取消</button></div></div>`;
}

// ---------- 05 讲题答疑 ----------

async function renderReview() {
  fillRoundSelect('#r-round');
  const round = Number($('#r-round').value || S.overview.latest_round || 1);
  const [patterns, plans] = await Promise.all([
    api('GET', `/api/projects/${S.pid}/patterns${S.aid ? `?assignment_id=${encodeURIComponent(S.aid)}` : ''}`),
    api('GET', `/api/projects/${S.pid}/review-plans?round=${round}${scopeQS()}`),
  ]);
  const confirmed = patterns.filter((p) => p.confirmed_version && (p.content.round || 1) === round);
  $('#review-patterns').innerHTML = confirmed.length
    ? '<span class="label">针对这些问题模式：</span>' + confirmed.map((p) => `<label><input type="checkbox" value="${p.lineage_id}" checked> ${esc(p.content.name)}（${p.content.affected_learners.length}/${p.content.total_learners} 人）</label>`).join('')
    : '<div class="empty">这一轮还没有已确认的问题模式（在“04 学情分析”中确认）。</div>';
  $('#review-plans').innerHTML = plans.length ? plans.map(reviewItem).join('') : '<div class="empty">这一轮还没有讲评稿。</div>';
  const exps = await api('GET', `/api/projects/${S.pid}/explanations`);
  $('#explanations').innerHTML = exps.map(explanationItem).join('');
}

function explanationItem(a) {
  const c = a.content;
  return `<div class="item" data-id="${a.id}">
    <div class="item-head"><div><b>讲解草稿</b> <span class="pill ${c.mode === 'answer' ? 'info' : ''}">${esc(c.scope_note)}</span></div><div>${statusPill(a)}</div></div>
    <div class="kv">题目：${esc(c.question)}</div>
    ${c.student_answer ? `<div class="kv">学生作答：<span class="quote">${esc(c.student_answer)}</span></div>` : ''}
    <h4>讲解重点</h4>${lines(c.focus)}<h4>关键步骤</h4>${lines(c.steps)}
    ${c.pitfalls.length ? `<h4>可能的易错点（预判）</h4>${lines(c.pitfalls)}` : ''}
    ${c.answer_comment ? `<h4>针对这份作答</h4><p>${esc(c.answer_comment)}</p>${c.quotes.map((q) => `<div class="quote">${esc(q)}</div>`).join('')}` : ''}
    ${c.quotes_rejected ? `<div class="kv flag">${c.quotes_rejected} 处引用在学生作答中找不到，已去掉。</div>` : ''}
    <h4>课堂追问</h4>${lines(c.follow_up)}
    ${a.status !== 'confirmed' ? '<div class="item-actions"><button class="small" data-act="confirm">确认</button></div>' : ''}</div>`;
}

function reviewItem(a) {
  const c = a.content;
  return `<div class="item" data-id="${a.id}">
    <div class="item-head"><div><b>第 ${c.round} 轮讲评稿</b></div><div>${statusPill(a)}</div></div>
    <h4>讲解重点</h4>${lines(c.focus)}
    ${c.sections.map((x, i) => `<div class="item">
      <b>${i + 1}. ${esc(x.title)}</b> <span class="pill">${esc(x.pattern_name)}</span>
      <h4>关键步骤</h4>${lines(x.key_steps)}
      <h4>典型错误辨析</h4>
      ${x.typical_quote ? `<div class="quote">${esc(x.typical_quote)}</div><div class="kv">学生原文（第 ${x.typical_source.question_no} 题），已核对</div>` : (x.quote_rejected ? '<div class="kv">模型给的“典型错误”不是学生原文，已去掉</div>' : '')}
      <div class="kv">${esc(x.error_analysis)}</div>
      <h4>课堂追问</h4>${lines(x.follow_up)}</div>`).join('')}
    ${c.closing ? `<div class="kv">${esc(c.closing)}</div>` : ''}
    <div class="item-actions"><button class="small secondary" data-act="edit-review">编辑</button>
      ${a.status !== 'confirmed' ? `${changeKindSelect(a.confirmed_version)}<button class="small" data-act="confirm">确认</button>` : ''}</div>
    <div class="edit-area"></div></div>`;
}

function reviewForm(c) {
  return `<div class="edit-grid">
    <label>讲解重点（每行一条）<textarea class="f-focus" rows="3">${esc((c.focus || []).join('\n'))}</textarea></label>
    ${c.sections.map((x, i) => `<div class="item f-section" data-i="${i}"><b>${i + 1}. ${esc(x.pattern_name)}</b>
      <label>标题<input class="f-title" value="${esc(x.title)}"></label>
      <label>关键步骤（每行一条）<textarea class="f-steps" rows="3">${esc(x.key_steps.join('\n'))}</textarea></label>
      <label>典型错误辨析<textarea class="f-analysis" rows="3">${esc(x.error_analysis)}</textarea></label>
      <label>课堂追问（每行一条）<textarea class="f-follow" rows="2">${esc(x.follow_up.join('\n'))}</textarea></label></div>`).join('')}
    <label>总结<textarea class="f-closing" rows="2">${esc(c.closing || '')}</textarea></label>
    <div class="item-actions"><button class="small" data-act="save-review">保存为新版本</button><button class="small secondary" data-act="cancel-edit">取消</button></div></div>`;
}

function comparisonView(a) {
  const c = a.content;
  const list = (xs) => xs.length ? esc(xs.join('、')) : '—';
  return `<p class="muted">第 ${c.round_a} 轮 → 第 ${c.round_b} 轮 ${a.stale ? '<span class="pill bad">依据已变化</span>' : ''}</p>
    <table><tr><th>问题模式</th><th>前一轮涉及</th><th>复测</th><th>已改善</th><th>仍存在</th><th>新出现</th><th>未复测</th></tr>
    ${c.patterns.map((r) => `<tr><td>${esc(r.name)}${r.enough_evidence ? '' : '<div class="kv">没有针对它的复测题，证据不足</div>'}
        ${(r.same_tag_elsewhere || []).length ? `<div class="kv flag">其他题有同类失分：${list(r.same_tag_elsewhere)}（不计入“仍存在”，请自行判断）</div>` : ''}</td><td>${list(r.before)}</td><td>${list(r.retested)}</td>
      <td>${list(r.improved)}</td><td>${list(r.persisting)}</td><td>${list(r.new)}</td><td>${list(r.not_retested)}</td></tr>`).join('')}</table>
    <p class="muted">复测、已改善、仍存在、新出现都只按针对这个问题模式出的再练题统计。</p>`;
}

// ---------- 记录与导出 ----------

async function renderRecords() {
  const stale = await api('GET', `/api/projects/${S.pid}/stale`);
  const graded = stale.filter((m) => m.type === 'grading');
  $('#stale-list').innerHTML = (stale.length ? stale.map((m) => `<div class="item" data-mark="${m.id}">
      <div class="item-head"><div><b>${esc(TYPE_LABEL[m.type])}</b> 第 ${m.version} 版 · 第 ${m.round} 轮</div>
        <div class="muted">原因：${m.cause ? esc(TYPE_LABEL[m.cause.type]) + '更新到第 ' + m.cause.version + ' 版（实质修改）' : '上游变化'}</div></div>
      <div class="item-actions"><button class="small secondary" data-resolve="kept">保留原结果</button><button class="small secondary" data-resolve="redone">已重新处理</button></div></div>`).join('')
    : '<div class="empty">没有依据已变化的产物。</div>')
    + (graded.length ? `<div class="row"><button data-act="regrade-stale">重新批改这 ${graded.length} 条批改</button></div>` : '');
  $('#export-link').href = `/api/projects/${S.pid}/export`;
  const q = `round=${S.overview.latest_round || 1}${S.aid ? `&assignment_id=${encodeURIComponent(S.aid)}` : ''}`;
  $('#deliverables').innerHTML = [['lesson_plan', '教案'], ['student_paper', '学生练习（不含答案）'], ['teacher_key', '教师答案与评分标准'], ['review_outline', '讲评提纲']]
    .map(([k, label]) => `<div class="row"><span class="s-title">${label}</span>
      <a class="button small secondary" target="_blank" href="/api/projects/${S.pid}/deliverables/${k}?fmt=html&${q}">打印版</a>
      <a class="button small secondary" href="/api/projects/${S.pid}/deliverables/${k}?fmt=docx&${q}">Word</a></div>`).join('')
    + (S.aid ? '<p class="muted">学生练习和答案按当前布置记录固定的版本导出。</p>' : '');
  const cws = await api('GET', `/api/projects/${S.pid}/artifacts?type=courseware`);
  $('#courseware-list').innerHTML = cws.map((x) => `<div class="kv">${esc(x.content.kind)} · 交换包 ${esc(x.content.package_id)} ${x.stale ? '<span class="pill bad">依据已变化</span>' : ''}
    ${x.content.files.map((f) => `<a href="${esc(f.url)}" target="_blank" rel="noopener">${esc(f.name)}</a>`).join(' ')}</div>`).join('');
}

// ---------- events ----------

function bindEvents() {
  $('#project-select').addEventListener('change', (e) => (e.target.value ? selectProject(e.target.value) : showHome()));
  $('#new-project-btn').addEventListener('click', () => showWelcome(true));
  $('#cancel-project').addEventListener('click', () => (S.pid && S.view === 'project' ? showWelcome(false) : showHome()));
  $('#home-btn').addEventListener('click', () => showHome());
  $('#scope-assignment').addEventListener('change', async (e) => { S.aid = e.target.value || null; await refresh(); });
  $('#assign-btn').addEventListener('click', (e) => busy(e.target, '布置中…', async () => {
    const round = Number($('#q-round').value || 1);
    const parent = S.pendingParent && S.pendingParent.round === round ? S.pendingParent.aid : '';
    const a = await api('POST', `/api/projects/${S.pid}/assignments`, { offering_id: $('#assign-class').value, round, title: $('#assign-title').value, parent_id: parent });
    $('#assign-title').value = ''; S.pendingParent = null; S.aid = a.id;
    toast(`已布置给${a.class_name}：${a.questions} 道题，名单 ${a.roster} 人；接下来在“作业批改”里导入这个班的作答`);
    await refresh();
  }));
  $('#derive-btn').addEventListener('click', (e) => busy(e.target, '复制中…', async () => {
    const r = await api('POST', `/api/projects/${S.pid}/derive`, { offering_id: $('#use-plan-class').value });
    await loadProjects(r.project.id, false);
    await selectProject(r.project.id, { tab: 'prep' });
    toast(`已建立本班调整版：复制了${r.copied.plan ? '备课方案和' : ''} ${r.copied.questions} 道题（都是草稿），修改后确认即可，原任务不受影响`);
  }));
  $('#ex-go').addEventListener('click', (e) => busy(e.target, '生成中…', async () => {
    const r = await api('POST', `/api/projects/${S.pid}/explain`, { question: $('#ex-question').value, ask: $('#ex-ask').value, student_answer: $('#ex-answer').value });
    showNote(r.context_note); toast('讲解草稿已生成，请核对后确认'); await refresh();
  }));
  $('#carryover-card').addEventListener('change', (e) => {
    if (!e.target.matches('.cs-pattern, #cs-summary')) return;
    const exclude = [...document.querySelectorAll('.cs-pattern')].filter((x) => !x.checked).map((x) => x.value);
    const sum = $('#cs-summary');
    busy(null, '', async () => {
      await api('PATCH', `/api/projects/${S.pid}/class-status`, { exclude, exclude_summary: sum ? !sum.checked : null });
      toast('已更新：之后的生成按勾选的本班学情');
    });
  });
  $('#use-plan').addEventListener('click', (e) => busy(e.target, '处理中…', async () => {
    const s = await api('POST', `/api/projects/${S.pid}/use-plan`, { offering_id: $('#use-plan-class').value });
    toast(`已加入该班课时安排：${s.title}（计划中）`);
  }));
  // Pick the subject from the project name ("高一数学…") unless the teacher chose one by hand.
  const subjectSel = $('#subject-select');
  subjectSel.addEventListener('change', () => { subjectSel.dataset.manual = '1'; });
  $('#project-form [name=title]').addEventListener('input', (e) => {
    if (subjectSel.dataset.manual) return;
    const hit = S.meta.subjects.find((s) => e.target.value.includes(s.name) || (s.id === 'ai' && /AI|人工智能/i.test(e.target.value)));
    if (hit) subjectSel.value = hit.id;
  });
  document.querySelector('#workspace').addEventListener('click', (e) => {
    const go = e.target.closest('[data-goto]');
    if (go) { goNext(S.overview.next[Number(go.dataset.goto)]); return; }
    if (e.target.closest('[data-continue]')) {
      const btn = e.target.closest('[data-continue]');
      if (btn.disabled) return;
      const c = CONTINUE[S.tab][Number(btn.dataset.continue)];
      if (c.action === 'next-lesson') { openNextLesson(); return; }
      goNext({ tab: c.tab, target: c.target || `#tab-${c.tab} .stage-head` });
    }
  });
  $('#edit-project').addEventListener('click', () => {
    const box = $('#project-edit');
    box.classList.toggle('hidden');
    if (!box.classList.contains('hidden')) renderProjectEdit();
  });
  $('#project-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    const p = await busy(e.submitter, '创建中…', () => api('POST', '/api/projects', Object.fromEntries(f.entries())));
    if (p) { e.target.reset(); delete subjectSel.dataset.manual; await loadProjects(p.id); toast('项目已创建，先添加课程材料'); }
  });
  $('#tabs').addEventListener('click', (e) => {
    const b = e.target.closest('button[data-tab]');
    if (b) { S.tab = b.dataset.tab; showNote(''); renderTab(); window.scrollTo({ top: 0 }); }
  });
  ['#q-round', '#g-round', '#a-round'].forEach((sel) => $(sel).addEventListener('change', renderTab));

  $('#add-material').addEventListener('click', (e) => busy(e.target, '添加中…', async () => {
    const text = $('#material-text').value;
    await api('POST', `/api/projects/${S.pid}/materials`, { text, filename: $('#material-name').value || '粘贴文本' });
    $('#material-text').value = ''; $('#material-name').value = '';
    toast('材料已添加'); await refresh();
  }));
  $('#material-file').addEventListener('change', async (e) => {
    const file = e.target.files[0];
    if (!file) return;
    const fd = new FormData(); fd.append('file', file);
    await busy(null, '', async () => { await api('POST', `/api/projects/${S.pid}/materials/upload`, fd); toast('文件已导入'); await refresh(); });
    e.target.value = '';
  });
  $('#nl-cancel').addEventListener('click', () => $('#next-lesson-card').classList.add('hidden'));
  $('#nl-create').addEventListener('click', (e) => busy(e.target, '创建中…', async () => {
    const p = await api('POST', `/api/projects/${S.pid}/next-lesson`, { knowledge_module: $('#nl-module').value, lesson: $('#nl-lesson').value });
    $('#next-lesson-card').classList.add('hidden');
    S.tab = 'prep';
    await loadProjects(p.id);
    toast('已新建下一课时，上一课时的学情已带入；核对课时信息后生成备课方案');
  }));
  document.querySelector('#workspace').addEventListener('click', (e) => {
    if (e.target.closest('[data-home]')) { showHome(); return; }
    const rp = e.target.closest('[data-repin]');
    if (rp) {
      busy(rp, '处理中…', async () => {
        const r = await api('POST', `/api/assignments/${S.aid}/repin`, { question_lineage: rp.dataset.repin });
        toast(r.changed ? `本班已改用新版本，${r.regrade} 份批改需要重新批改（在“作业批改”里点“批改本轮作答”）` : '已经是最新版本');
        await refresh();
      });
      return;
    }
    const b = e.target.closest('[data-open-project]');
    if (b) { S.tab = 'prep'; loadProjects(b.dataset.openProject); }
  });
  $('#save-course').addEventListener('click', (e) => busy(e.target, '保存中…', async () => {
    const changed = await saveCourseInfo();
    toast(changed ? '课时信息已保存' : '没有改动'); await refresh();
  }));
  $('#generate-questions').addEventListener('click', (e) => busy(e.target, '出题与自检中…（约 1 分钟）', async () => {
    const types = $$('#type-boxes input:checked').map((i) => i.value);
    const r = await api('POST', `/api/projects/${S.pid}/questions/generate`, {
      types, count: Number($('#q-count').value), difficulty: $('#q-difficulty').value, purpose: $('#q-purpose').value, notes: $('#q-notes').value, ab: $('#q-ab').checked ? true : false,
    });
    showNote(r.context_note);
    const dropped = r.candidates.flatMap((c) => c.dropped || []);
    toast((r.candidate_group ? '生成了 A/B 两版，请先选择' : `生成了 ${r.candidates[0].questions.length} 道题，请核对后确认`) + (dropped.length ? `；丢弃 ${dropped.length} 道不完整的题：${dropped.join('；')}` : ''), dropped.length > 0);
    S.showRound = 1; await refresh();
  }));
  $('#coverage').addEventListener('click', (e) => {
    const b = e.target.closest('[data-fill]');
    if (!b) return;
    busy(b, '出题中…', async () => {
      const types = $$('#type-boxes input:checked').map((i) => i.value);
      const r = await api('POST', `/api/projects/${S.pid}/questions/generate`, {
        types, count: 1, difficulty: $('#q-difficulty').value, purpose: $('#q-purpose').value, notes: $('#q-notes').value, ab: false, objective_ids: [b.dataset.fill],
      });
      showNote(r.context_note); toast('已为这条目标补了 1 道题，请核对后确认'); S.showRound = 1; await refresh();
    });
  });
  $('#import-paper').addEventListener('click', (e) => busy(e.target, '整理中…', async () => {
    const r = await api('POST', `/api/projects/${S.pid}/import-paper`, { text: $('#paper-text').value, filename: '导入试卷' });
    $('#paper-text').value = ''; showNote(r.context_note);
    toast(`导入了 ${r.questions.length} 道题，备课方案已补充反推的目标（草稿），请核对确认` + ((r.dropped || []).length ? `；${r.dropped.length} 道无法整理：${r.dropped.join('；')}` : ''), (r.dropped || []).length > 0); await refresh();
  }));
  $('#add-roster').addEventListener('click', (e) => busy(e.target, '加入中…', async () => {
    const aliases = $('#roster').value.split(/[\n,，]/).map((s) => s.trim()).filter(Boolean);
    const r = await api('POST', `/api/projects/${S.pid}/learners`, { aliases });
    $('#roster').value = ''; toast(`加入 ${r.added.length} 人`); await refresh();
  }));
  $('#import-subs').addEventListener('click', (e) => busy(e.target, '导入中…', async () => {
    const r = await api('POST', `/api/projects/${S.pid}/submissions`, scoped({ csv: $('#sub-csv').value, round: Number($('#g-round').value || 1), add_missing: $('#sub-add-missing').checked }));
    const msgs = [`导入 ${r.created} 份作答`];
    if (r.unmatched_learners.length) msgs.push(`名单中没有：${r.unmatched_learners.join('、')}（未导入）`);
    if (r.problems.length) msgs.push(r.problems.join('；'));
    $('#import-result').textContent = msgs.join('。');
    if (r.created) $('#sub-csv').value = '';
    await refresh();
  }));
  const pickPhotos = (e) => {
    const added = [...e.target.files];
    S.photoFiles = [...(S.photoFiles || []), ...added].slice(0, 8);
    if (S.photoFiles.length === 8 && added.length) toast('一次最多 8 张照片');
    e.target.value = '';
    renderPicked();
  };
  $('#ph-camera').addEventListener('change', pickPhotos);
  $('#ph-files').addEventListener('change', pickPhotos);
  $('#ph-picked').addEventListener('click', (e) => {
    const i = e.target.dataset.remove;
    if (i === undefined) return;
    S.photoFiles.splice(Number(i), 1);
    renderPicked();
  });
  $('#ph-transcribe').addEventListener('click', (e) => busy(e.target, '识别中…', async () => {
    const learner = $('#ph-learner').value.trim();
    if (!learner) throw new Error('请先填写学生代号');
    if (!(S.photoFiles || []).length) throw new Error('请先拍照或选择照片');
    const fd = new FormData();
    fd.append('learner', learner);
    fd.append('round', String(Number($('#g-round').value || 1)));
    if (S.aid) fd.append('assignment_id', S.aid);
    S.photoFiles.forEach((f) => fd.append('files', f));
    S.photoDraft = await api('POST', `/api/projects/${S.pid}/photo-answers/transcribe`, fd);
    showNote(S.photoDraft.context_note);
    renderPhotoDraft();
  }));
  $('#ph-draft').addEventListener('click', (e) => {
    if (e.target.id === 'ph-discard') { S.photoDraft = null; renderPhotoDraft(); return; }
    if (e.target.id !== 'ph-import') return;
    busy(e.target, '导入中…', async () => {
      const d = S.photoDraft;
      const answers = $$('#ph-draft textarea[data-no]').map((t) => ({ question_no: Number(t.dataset.no), text: t.value }));
      const r = await api('POST', `/api/projects/${S.pid}/photo-answers`, scoped({
        learner: d.learner, round: d.round, answers, photos: d.photos, add_missing: $('#ph-add-missing').checked, generation_id: d.generation_id,
      }));
      toast(`导入 ${d.learner} 的 ${r.created} 道作答` + (r.problems.length ? `；${r.problems.join('；')}` : ''), r.problems.length > 0);
      S.photoDraft = null; S.photoFiles = []; $('#ph-learner').value = '';
      renderPicked(); renderPhotoDraft(); await refresh();
    });
  });
  $('#grade-round').addEventListener('click', (e) => busy(e.target, '批改中…', async () => {
    const r = await api('POST', `/api/projects/${S.pid}/grade`, scoped({ round: Number($('#g-round').value || 1) }));
    const msgs = [`批改 ${r.graded} 份`, `进入复核队列 ${r.review_queue} 份`];
    if (r.skipped.length) msgs.push(`跳过 ${r.skipped.length} 份（题目或评分标准未确认）`);
    if (r.failed.length) msgs.push(`失败 ${r.failed.length} 份：${r.failed[0].error}`);
    toast(msgs.join('，'), r.failed.length > 0); await refresh();
  }));
  $('#confirm-auto').addEventListener('click', (e) => busy(e.target, '确认中…', async () => {
    const r = await api('POST', `/api/projects/${S.pid}/gradings/confirm`, scoped({ round: Number($('#g-round').value || 1) }));
    toast(`确认了 ${r.confirmed} 份批改` + (r.held ? `；${r.held} 份需要先复核或处理依据变化，未包含在批量确认中` : '')); await refresh();
  }));
  $('#analyze').addEventListener('click', (e) => busy(e.target, '分析中…', async () => {
    const r = await api('POST', `/api/projects/${S.pid}/analyze`, scoped({ round: Number($('#a-round').value || 1), partial: $('#a-partial').checked }));
    showNote(r.context_note); toast(r.patterns.length ? `归纳出 ${r.patterns.length} 个问题模式，请核对确认` : '分析完成'); await refresh();
  }));
  $('#practice').addEventListener('click', (e) => busy(e.target, '出题与自检中…', async () => {
    const ids = $$('#review-patterns input:checked').map((i) => i.value);
    const r = await api('POST', `/api/projects/${S.pid}/practice`, scoped({ pattern_ids: ids, count: Number($('#p-count').value), round: Number($('#r-round').value || 1) }));
    showNote(r.context_note);
    const cur = currentAssignment();
    if (r.parent_assignment_id && cur) S.pendingParent = { aid: r.parent_assignment_id, offering_id: cur.offering_id, round: r.round };
    toast(`第 ${r.round} 轮再练题：${r.questions.length} 道，请在“02 出题改题”中确认` + (cur ? `，确认后布置给${cur.class_name}` : '') + ((r.dropped || []).length ? `；丢弃 ${r.dropped.length} 道：${r.dropped.join('；')}` : ''), (r.dropped || []).length > 0);
    S.showRound = r.round; S.tab = 'questions'; await refresh();
  }));
  $('#curriculum-list').addEventListener('change', () => { S.curTouched = true; });
  $('#curriculum-off').addEventListener('change', (e) => { S.curTouched = true; $('#curriculum-box').classList.toggle('off', e.target.checked); });
  $('#curriculum-search').addEventListener('click', (e) => busy(e.target, '搜索中…', () => renderCurriculum($('#curriculum-q').value.trim())));
  $('#curriculum-q').addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); $('#curriculum-search').click(); } });
  $('#draft-lesson-plan').addEventListener('click', (e) => {
    if (e.target.textContent === '重新生成' && !window.confirm('重新生成会产生一个新版本的草稿，当前方案保留在历史里。继续吗？')) return;
    busy(e.target, '生成中…（约半分钟）', async () => {
      await saveCourseInfo();  // what the teacher typed is what the plan is based on
      const r = await api('POST', `/api/projects/${S.pid}/lesson-plan/draft`);
      showNote(r.context_note); toast('备课方案已生成，请核对修改后确认'); await refresh();
    });
  });
  $('#draft-review').addEventListener('click', (e) => busy(e.target, '生成中…', async () => {
    const ids = $$('#review-patterns input:checked').map((i) => i.value);
    const r = await api('POST', `/api/projects/${S.pid}/review/draft`, scoped({ round: Number($('#r-round').value || 1), pattern_ids: ids }));
    showNote(r.context_note); toast('讲评稿已生成，请核对后确认'); await refresh();
  }));
  $('#r-round').addEventListener('change', renderTab);
  $('#compare').addEventListener('click', (e) => busy(e.target, '对比中…', async () => {
    const cur = currentAssignment();
    await api('POST', `/api/projects/${S.pid}/compare`, cur && cur.parent_id ? { assignment_id: cur.id } : { round_a: Number($('#cmp-a').value), round_b: Number($('#cmp-b').value) });
    await refresh();
  }));

  document.body.addEventListener('click', onAction);
}

async function onAction(e) {
  const btn = e.target.closest('button');
  if (!btn) return;
  const item = btn.closest('[data-id]');
  const act = btn.dataset.act;
  if (btn.dataset.verdict) {
    const card = btn.closest('[data-group]');
    await busy(btn, '提交中…', async () => {
      const verdict = btn.dataset.verdict;
      const pref = $('.ab-pref', card).value;
      await api('POST', `/api/projects/${S.pid}/choices`, {
        group: card.dataset.group, verdict, reason: $('.ab-reason', card).value,
        work_choice: btn.dataset.work || null,
        preference: ['A', 'B'].includes(verdict) ? pref : 'none',
        reason_tags: [...card.querySelectorAll('.ab-tag:checked')].map((x) => x.value),
      });
      toast(verdict === 'skip' ? '已跳过，这一组没有比较标签' : '已记录'); await refresh();
    });
    return;
  }
  if (btn.dataset.resolve) {
    const mark = btn.closest('[data-mark]').dataset.mark;
    await busy(btn, '…', async () => { await api('POST', `/api/stale/${mark}/resolve`, { resolution: btn.dataset.resolve }); await refresh(); });
    return;
  }
  const kind = () => { const sel = item && $('.change-kind', item); return sel ? sel.value : 'substantive'; };
  switch (act) {
    case 'confirm':
      await busy(btn, '确认中…', async () => { await api('POST', `/api/artifacts/${item.dataset.id}/confirm`, { change_kind: kind() }); toast('已确认'); await refresh(); });
      break;
    case 'cancel-edit': {
      const box = btn.closest('.new-objective');
      if (box) box.remove(); else { const area = btn.closest('.edit-area') || btn.closest('.new-question-area'); if (area) area.innerHTML = ''; }
      break;
    }
    case 'regen-open':
      $('.edit-area', item).innerHTML = `<div class="regen">
        <select class="regen-kind"><option value="">换一道同类题</option><option value="更简单">更简单</option><option value="更难">更难</option><option value="换个真实情境">换个真实情境</option></select>
        <input class="regen-note" placeholder="其他要求（选填），例如：不要用分数">
        <button class="small" data-act="regen-go">生成</button><button class="small secondary" data-act="cancel-edit">取消</button></div>`;
      break;
    case 'regen-go': {
      const kind = $('.regen-kind', item).value;
      const note = $('.regen-note', item).value.trim();
      const levels = ['基础', '中等', '较难'];
      const now = (await api('GET', `/api/projects/${S.pid}/questions?round=${$('#q-round').value || 1}`)).find((x) => x.id === item.dataset.id);
      const cur = levels.indexOf((now && now.content.difficulty_requested) || '中等');
      const difficulty = kind === '更简单' ? levels[Math.max(0, cur - 1)] : kind === '更难' ? levels[Math.min(2, cur + 1)] : null;
      await busy(btn, '换题与自检中…', async () => {
        const r = await api('POST', `/api/questions/${item.dataset.id}/regenerate`, { instruction: [kind, note].filter(Boolean).join('；'), difficulty });
        showNote(r.context_note); toast('已换成新的一版，请核对后确认'); S.showRound = Number($('#q-round').value || 1); await refresh();
      });
      break;
    }
    case 'adopt-solver':
      await busy(btn, '处理中…', async () => {
        await api('POST', `/api/questions/${item.dataset.id}/adopt-solver`);
        toast('已采用独立解题的答案，存为新版本，请核对后确认'); S.showRound = Number($('#q-round').value || 1); await refresh();
      });
      break;
    case 'edit-question': {
      const qs = await api('GET', `/api/projects/${S.pid}/questions?round=${$('#q-round').value || 1}`);
      const q = qs.find((x) => x.id === item.dataset.id);
      $('.edit-area', item).innerHTML = questionForm(q);
      break;
    }
    case 'new-question':
      $('.new-question-area').innerHTML = `<div class="item">${questionForm(null)}</div>`;
      break;
    case 'add-point': {
      const list = $('.f-points', btn.closest('.edit-grid, .correction'));
      list.insertAdjacentHTML('beforeend', pointRow({ id: '', description: '', score: 1 }));
      break;
    }
    case 'remove-point':
      btn.closest('.point-row').remove();
      break;
    case 'save-question': {
      const form = readQuestionForm(btn.closest('.edit-grid'));
      await busy(btn, '保存中…', async () => {
        const qs = await api('GET', `/api/projects/${S.pid}/questions?round=${$('#q-round').value || 1}`);
        const q = qs.find((x) => x.id === item.dataset.id);
        const changedQ = JSON.stringify(pick(q.content, Object.keys(form.question))) !== JSON.stringify(form.question);
        const changedR = JSON.stringify(q.rubric.content.points.map((p) => pick(p, ['id', 'description', 'score']))) !== JSON.stringify(form.rubric.points.map((p) => pick(p, ['id', 'description', 'score'])));
        if (changedQ) await api('PUT', `/api/artifacts/${q.id}`, { content: form.question });
        if (changedR) await api('PUT', `/api/artifacts/${q.rubric.id}`, { content: form.rubric });
        toast(changedQ || changedR ? '已保存为草稿，确认后生效' : '没有改动'); await refresh();
      });
      break;
    }
    case 'create-question': {
      const form = readQuestionForm(btn.closest('.edit-grid'));
      await busy(btn, '添加中…', async () => {
        await api('POST', `/api/projects/${S.pid}/questions`, { ...form, round: Number($('#q-round').value || 1) });
        toast('题目已添加为草稿'); await refresh();
      });
      break;
    }
    case 'confirm-question':
      await busy(btn, '确认中…', async () => {
        const qs = await api('GET', `/api/projects/${S.pid}/questions?round=${$('#q-round').value || 1}`);
        const q = qs.find((x) => x.id === item.dataset.id);
        if (q.status !== 'confirmed') await api('POST', `/api/artifacts/${q.id}/confirm`, { change_kind: kind() });
        else if (q.rubric && q.rubric.status !== 'confirmed') await api('POST', `/api/artifacts/${q.rubric.id}/confirm`, { change_kind: kind() });
        toast('题目与评分标准已确认'); await refresh();
      });
      break;
    case 'override': {
      const points = $$('tr[data-point]', item).map((tr) => ({
        id: tr.dataset.point, score: $('.o-score', tr).value === '' ? null : Number($('.o-score', tr).value),
        error_tag: $('.o-tag', tr).value, error_note: $('.o-note', tr).value,
      }));
      if (points.some((p) => p.score === null || Number.isNaN(p.score))) { toast('请为每个要点填写分数', true); return; }
      await busy(btn, '提交中…', async () => {
        await api('POST', `/api/gradings/${item.dataset.id}/override`, { points, note: $('.o-comment', item).value });
        toast('已按老师判定保存'); await refresh();
      });
      break;
    }
    case 'edit-pattern': {
      const a = await api('GET', `/api/artifacts/${item.dataset.id}`);
      $('.edit-area', item).innerHTML = `<div class="edit-grid"><label>名称<input class="f-name" value="${esc(a.content.name)}"></label>
        <label>描述<textarea class="f-desc" rows="2">${esc(a.content.description)}</textarea></label>
        <div class="item-actions"><button class="small" data-act="save-pattern">保存为新版本</button><button class="small secondary" data-act="cancel-edit">取消</button></div></div>`;
      break;
    }
    case 'save-pattern':
      await busy(btn, '保存中…', async () => {
        await api('PUT', `/api/artifacts/${item.dataset.id}`, { content: { name: $('.f-name', item).value, description: $('.f-desc', item).value } });
        toast('已保存为草稿'); await refresh();
      });
      break;
    case 'edit-lesson-plan':
    case 'edit-review': {
      const a = await api('GET', `/api/artifacts/${item.dataset.id}`);
      $('.edit-area', item).innerHTML = act === 'edit-review' ? reviewForm(a.content) : lessonPlanForm(a.content);
      break;
    }
    case 'add-objective-row':
      $('.f-objectives', item).insertAdjacentHTML('beforeend', objectiveRow());
      break;
    case 'remove-objective-row':
      btn.closest('.obj-row').remove();
      break;
    case 'save-lesson-plan': {
      const split = (v) => v.split('\n').map((x) => x.trim()).filter(Boolean);
      const rows = $$('.obj-row', item);
      const objectives = rows.map((r) => ({
        id: r.dataset.oid || undefined, text: $('.o-text', r).value, success_criterion: $('.o-crit', r).value,
        inferred: r.dataset.inferred === '1' || undefined,
      })).filter((o) => o.text.trim());
      const activities = split($('.f-acts', item).value).map((line) => {
        const [name, minutes, description, goals] = line.split(/[｜|]/);
        const ids = (goals || '').split(/[,，、\s]+/).map((n) => rows[Number(n) - 1]).filter(Boolean).map((r) => r.dataset.oid).filter(Boolean);
        return { name: (name || '').trim(), minutes: Number(minutes) || 0, description: (description || '').trim(), objective_ids: ids };
      });
      await busy(btn, '保存中…', async () => {
        await api('PUT', `/api/artifacts/${item.dataset.id}`, { content: { objectives, key_points: split($('.f-key', item).value), difficulties: split($('.f-diff', item).value), activities, notes: $('.f-notes', item).value } });
        toast('已保存修改，确认后生效'); await refresh();
      });
      break;
    }
    case 'save-review': {
      const split = (v) => v.split('\n').map((x) => x.trim()).filter(Boolean);
      await busy(btn, '保存中…', async () => {
        const a = await api('GET', `/api/artifacts/${item.dataset.id}`);
        const sections = a.content.sections.map((x, i) => {
          const box = $(`.f-section[data-i="${i}"]`, item);
          return { ...x, title: $('.f-title', box).value, key_steps: split($('.f-steps', box).value), error_analysis: $('.f-analysis', box).value, follow_up: split($('.f-follow', box).value) };
        });
        await api('PUT', `/api/artifacts/${item.dataset.id}`, { content: { focus: split($('.f-focus', item).value), sections, closing: $('.f-closing', item).value } });
        toast('已保存为草稿，确认后生效'); await refresh();
      });
      break;
    }
    case 'save-project':
      await busy(btn, '保存中…', async () => {
        const box = $('#project-edit');
        const body = { title: $('.pe-title', box).value, grade: $('.pe-grade', box).value, notes: $('.pe-notes', box).value };
        const sel = $('.pe-subject', box);
        if (!sel.disabled) body.subject = sel.value;
        await api('PATCH', `/api/projects/${S.pid}`, body);
        const unit = $('.pe-unit', box);
        if (unit && unit.value !== (S.overview.project.unit_id || '')) await api('PUT', `/api/projects/${S.pid}/unit`, { unit_id: unit.value });
        box.classList.add('hidden');
        toast('项目已更新');
        await loadProjects(S.pid);
      });
      break;
    case 'correct-open': {
      const qlin = btn.closest('[data-qlin]').dataset.qlin;
      const box = $(`.correction-area[data-for="${qlin}"]`);
      const imp = await api('GET', `/api/assignments/${S.aid}/correction?question_lineage=${encodeURIComponent(qlin)}`);
      box.innerHTML = `<div class="correction" data-qlin="${qlin}">
        <div class="kv">更正后，只有这次布置改用新的评分标准；本班这道题的 ${imp.gradings} 份批改会标为“依据已变化”，重新批改并确认后才成为有效成绩，旧结果保留。</div>
        ${imp.other_assignments.length ? `<div class="kv flag">这些布置也在用同一版评分标准，不会被自动修改：${imp.other_assignments.map((o) => esc(`${o.class_name}·${o.title}`)).join('、')}。需要时到对应布置里分别更正。</div>` : ''}
        <div class="f-points">${imp.rubric.content.points.map(pointRow).join('')}</div>
        <div class="row"><button class="small secondary" data-act="add-point">加一个要点</button></div>
        <div class="row"><input class="c-note" placeholder="更正理由（选填）"><button class="small" data-act="correct-go">确认更正本次评分依据</button><button class="small secondary" data-act="correct-cancel">取消</button></div></div>`;
      break;
    }
    case 'correct-cancel':
      btn.closest('.correction-area').innerHTML = '';
      break;
    case 'correct-go': {
      const box = btn.closest('.correction');
      const points = $$('.point-row', box).map((r, i) => {
        const pt = { id: r.dataset.pid || `P${i + 1}`, description: $('.p-desc', r).value.trim(), score: Number($('.p-score', r).value) };
        const check = JSON.parse(r.dataset.check || 'null');
        if (check) pt.check = check;
        if (r.dataset.target) pt.target_pattern = r.dataset.target;
        return pt;
      });
      await busy(btn, '更正中…', async () => {
        const r = await api('POST', `/api/assignments/${S.aid}/correct-rubric`, { question_lineage: box.dataset.qlin, content: { points }, note: $('.c-note', box).value });
        toast(`已更正：${r.regrade} 份批改待重新批改` + (r.other_assignments.length ? `；${r.other_assignments.length} 个其他布置仍用原版本` : ''));
        await refresh();
      });
      break;
    }
    case 'close-project-edit':
      $('#project-edit').classList.add('hidden');
      break;
    case 'regrade-stale':
      await busy(btn, '重新批改中…', async () => {
        let n = 0;
        if (S.aid) n = (await api('POST', `/api/projects/${S.pid}/grade`, { assignment_id: S.aid })).graded;
        else for (const r of rounds()) n += (await api('POST', `/api/projects/${S.pid}/grade`, { round: r })).graded;
        toast(`重新批改了 ${n} 份`); await refresh();
      });
      break;
    default:
  }
}

function pick(obj, keys) { const o = {}; keys.forEach((k) => { o[k] = obj[k] ?? (Array.isArray(obj[k]) ? [] : obj[k]); }); return o; }

// ---------- 我的教学 (home: courses, classes, tasks) ----------

const START_LABEL = { prep: '准备下一节课', practice: '出一份练习', grading: '批改作业', explain: '准备讲解' };

async function showHome() {
  S.view = 'home';
  store.set('edu.view', 'home');
  $('#project-select').value = '';
  await renderHome();  // fill first, so the page never shows stale lists
  $('#workspace').classList.add('hidden');
  $('#welcome').classList.add('hidden');
  $('#home').classList.remove('hidden');
  window.scrollTo({ top: 0 });
}

async function renderHome() {
  const h = await api('GET', `/api/home${S.course ? `?course_id=${encodeURIComponent(S.course)}` : ''}`);
  S.home = h;
  S.course = h.course ? h.course.id : null;
  store.set('edu.course', S.course);
  $('#home-course').innerHTML = h.courses.map((c) => `<option value="${c.id}">${esc(c.title)}（${esc(c.subject_name)}）</option>`).join('') || '<option value="">（还没有课程）</option>';
  $('#home-course').value = S.course || '';
  if (!h.offerings.some((o) => o.id === S.offering)) S.offering = h.offerings.length === 1 ? h.offerings[0].id : null;
  store.set('edu.offering', S.offering);
  $('#home-class').innerHTML = '<option value="">（不指定班级）</option>' + h.offerings.map((o) => `<option value="${o.id}">${esc(o.class_name)}</option>`).join('');
  $('#home-class').value = S.offering || '';
  $('#home-body').classList.toggle('hidden', !h.course);
  if (!h.courses.length) $('#new-course').classList.remove('hidden');
  syncEmptyCard();

  $('#home-attention').innerHTML = h.attention.length
    ? h.attention.map((a, i) => `<div class="task-row"><div class="task-main">${esc(a.text)}</div>
        ${a.project_id ? `<button class="small secondary" data-attn="${i}">去处理</button>` : ''}</div>`).join('')
    : '<div class="empty">没有需要马上处理的事项。</div>';
  $('#home-tasks').innerHTML = h.tasks.length
    ? h.tasks.map((t) => `<div class="task-row"><div class="task-main"><b>${esc(t.title)}</b>
        <span class="pill">${esc(t.kind_label)}</span>${t.class_name ? ` <span class="pill info">${esc(t.class_name)}</span>` : ''}
        <div class="task-pending">${esc(t.pending.join('；'))}</div></div>
        <div><button class="small" data-open-task="${t.project_id}" data-tab="${t.tab}">继续</button>
        <button class="small link" data-done-task="${t.id}">标记完成</button></div></div>`).join('')
    : '<div class="empty">还没有进行中的工作。从下面“开始工作”选一项。</div>';
  $('#home-start').innerHTML = Object.entries(START_LABEL).map(([k, v]) => `<button class="secondary" data-start="${k}">${esc(v)}</button>`).join('');

  $('#home-units').innerHTML = h.units.length
    ? h.units.map((u) => `<button class="secondary small" data-unit="${u.id}">${esc(u.title)} · ${u.tasks} 项工作${u.assignments ? ` · ${u.assignments} 次布置` : ''}</button>`).join(' ')
    : '<div class="empty">还没有主题。开始工作时填写知识模块，会自动建立。</div>';
  if (S.unit && h.units.some((u) => u.id === S.unit)) await renderUnit(S.unit); else { S.unit = null; $('#unit-view').innerHTML = ''; }
  $('#home-classes').innerHTML = h.offerings.length
    ? h.offerings.map((o) => `<div class="offering" data-offering="${o.id}"><h4>${esc(o.class_name)} <span class="pill">${o.members} 人</span>
        ${o.progress ? `<span class="pill ok">已上到：${esc(o.progress)}</span>` : ''}</h4>
        ${o.sessions.length ? o.sessions.map((s) => `<div class="session-row${s.overdue ? ' overdue' : ''}" data-session="${s.id}">
          <span class="s-title">${esc(s.title)}${s.plan_version ? `（教案第 ${s.plan_version} 版）` : ''}</span>
          <span class="pill ${s.status === 'taught' ? 'ok' : s.status === 'cancelled' ? '' : 'warn'}">${esc(s.status_label)}</span>
          ${s.status === 'planned' ? `<input type="date" class="s-date" value="${esc(s.planned_date)}">
            <button class="small secondary" data-session-status="taught">已上</button><button class="small link" data-session-status="cancelled">取消</button>` : ''}
          ${s.project_id ? `<button class="small link" data-open-task="${s.project_id}" data-tab="prep">打开</button>` : ''}</div>`).join('')
          : '<div class="kv">还没有课时安排。备课方案确认后会自动出现在这里。</div>'}
        <div class="row"><button class="small link" data-unit-review="${o.id}">单元讲评（合并几份作业）</button></div><div class="ur-box"></div></div>`).join('')
    : '<div class="empty">本课程还没有关联班级。布置作业、导入作答和查看学情需要班级。</div>';
  const classes = await api('GET', '/api/classes');
  const linked = new Set(h.offerings.map((o) => o.class_id));
  $('#cls-existing').innerHTML = '<option value="">（新建班级）</option>' + classes.filter((c) => !linked.has(c.id)).map((c) => `<option value="${c.id}">${esc(c.name)}（${c.members} 人）</option>`).join('');

  if (h.course) {
    const detail = await api('GET', `/api/courses/${h.course.id}`);
    $('#course-materials').innerHTML = detail.materials.length
      ? detail.materials.map((m) => `<div class="kv">${esc(m.filename)} <span class="pill">${m.chars} 字</span></div>`).join('')
      : '<div class="empty">还没有课程资料。</div>';
  }
  await renderResearch();
  $('#legacy-card').classList.toggle('hidden', !h.legacy_projects);
  if (h.legacy_projects) await renderLegacy();
}

// The "先建一门课程" card only shows when there is no course and the form is closed: the form says the same.
function syncEmptyCard() {
  const formOpen = !$('#new-course').classList.contains('hidden');
  $('#home-empty').classList.toggle('hidden', !!(S.home && S.home.course) || formOpen);
}

const pct = (v) => (v == null ? '-' : `${Math.round(v * 100)}%`);

async function renderUnit(uid) {
  const v = await api('GET', `/api/units/${uid}`);
  S.unit = uid;
  const objs = v.objectives.length ? `<ul class="lines">${v.objectives.map((o) => `<li><b>${esc(o.code)}</b> ${esc(o.text)} ${o.version > 1 ? `<span class="pill">描述第 ${o.version} 版</span>` : ''}
      ${o.needs_review ? '<span class="pill warn">表述改过，请核对题目对应</span>' : ''} <span class="muted">（${esc(o.from)}）</span>
      <button class="link small" data-objective="${o.id}">按目标查看</button></li>`).join('')}</ul><div id="objective-view"></div>` : '<div class="kv">还没有已确认的备课方案。</div>';
  const classes = v.classes.map((c) => `<div class="offering"><h4>${esc(c.class_name)}</h4>
    <div class="kv">课时：${c.sessions.map((s) => `${esc(s.title)}（${esc(s.status_label)}）`).join('、') || '无'}</div>
    <div class="kv">布置：${c.assignments.map((a) => `${esc(a.title)}（${esc(a.status_label)}，收到 ${a.received}/${a.roster}）`).join('、') || '无'}</div>
    ${c.evidence.map((e) => `<div class="kv"><b>${esc(e.assignment)}</b>${e.partial ? ' <span class="pill warn">部分分析</span>' : ''} <span class="muted">${esc(e.coverage)}</span>
      <table><tr><th>目标</th><th>题号</th><th>得分率</th><th>全部得分</th></tr>${e.objectives.map((o) => `<tr><td>${esc(o['目标'])}</td><td>${o['题号'].length ? o['题号'].join('、') : '<span class="muted">没有题目</span>'}</td><td>${pct(o['得分率'])}</td><td>${o['人数'] ? `${o['全部得分人数']}/${o['人数']}` : '-'}</td></tr>`).join('')}</table></div>`).join('') || '<div class="kv muted">还没有已确认的学情分析。</div>'}
    ${c.patterns.length ? `<div class="kv">问题模式：${c.patterns.map((x) => `${esc(x.name)}（${esc(x.affected)}，${esc(x.assignment)}）`).join('；')}</div>` : ''}</div>`).join('');
  const open = v.tasks.filter((t) => t.status === 'open');
  $('#unit-view').innerHTML = `<div class="offering"><div class="card-head"><h4>${esc(v.unit.title)}</h4><button class="link small" data-unit-close="1">收起</button></div>
    <h4>教学目标</h4>${objs}
    ${classes || '<div class="kv">这个主题还没有布置给任何班级。</div>'}
    ${open.length ? `<h4>进行中的工作</h4>${open.map((t) => `<div class="kv">${esc(t.title)} <button class="small link" data-open-task="${t.project_id}" data-tab="${t.tab}">打开</button></div>`).join('')}` : ''}
    <p class="muted">得分率只来自已确认的学情分析，不合成单一的掌握分数。</p></div>`;
}

async function renderObjective(oid) {
  const v = await api('GET', `/api/objectives/${oid}`);
  const cls = v.classes.map((c) => `<div class="kv"><b>${esc(c.class_name)}</b>：${esc(c.state)}
    ${c.assignments.map((a) => `<div class="muted">· ${esc(a.assignment)}（${esc(a.status)}）${a.tested ? '' : '，没有考查这条目标'}
      ${a.evidence ? `：得分率 ${pct(a.evidence.rate)}，全部得分 ${a.evidence.full}/${a.evidence.learners}${a.evidence.partial ? '（部分分析）' : ''}，帮助条件：${esc(a.evidence.help)}` : ''}</div>`).join('')}</div>`).join('');
  $('#objective-view').innerHTML = `<div class="offering"><div class="card-head"><h4>${esc(v.code)} ${esc(v.text)}</h4>
      ${v.needs_review ? `<button class="small secondary" data-objective-ok="${v.id}">已核对题目对应</button>` : ''}</div>
    ${v.versions.length > 1 ? `<div class="kv">描述版本：${v.versions.map((x) => `第 ${x.version} 版「${esc(x.text)}」`).join(' → ')}</div>` : ''}
    <div class="kv">教过：${v.plans.map((x) => `${esc(x.title)}（教案第 ${x.plan_version} 版）`).join('、') || '无'}</div>
    <div class="kv">考查的题目：${v.questions.map((x) => `第 ${x.no} 题${x.round > 1 ? `（第 ${x.round} 轮）` : ''}`).join('、') || '还没有题目'}</div>
    ${cls || '<div class="kv">还没有布置给班级。</div>'}<p class="muted">${esc(v.note)}</p></div>`;
}

async function renderUnitReview(oid, box) {
  const list = await api('GET', `/api/offerings/${oid}/assignments`);
  if (list.length < 2) { box.innerHTML = '<div class="kv">这个班至少要有两份作业才能做单元讲评；只有一份时在那份作业里准备讲评。</div>'; return; }
  box.innerHTML = `<div class="kv">选择要合在一起讲评的作业：</div>${list.map((a) => `<label class="inline"><input type="checkbox" class="ur-pick" value="${a.id}" checked> ${esc(a.title)}（${esc(a.status_label)}）</label>`).join('')}
    <div class="row"><button class="small secondary" data-ur-preview="${oid}">查看各份情况</button><button class="small" data-ur-go="${oid}">生成单元讲评</button></div><div class="ur-preview"></div>`;
}

async function renderResearch() {
  if (!S.course) return;
  const [sum, cases, consents] = await Promise.all([
    api('GET', `/api/research/summary?course_id=${encodeURIComponent(S.course)}`),
    api('GET', `/api/research/cases?course_id=${encodeURIComponent(S.course)}`),
    api('GET', `/api/courses/${S.course}/consents`),
  ]);
  const c = sum.counts || {};
  const label = { teacher_tasks: '老师任务', runs: '运行', model_calls: '模型调用', research_cases: '研究案例', reviewed_cases: '已审定案例', source_groups: '来源组', contributors: '贡献者' };
  $('#research-summary').innerHTML = Object.entries(label).filter(([k]) => k in c)
    .map(([k, l]) => `<span class="pill" title="${esc((sum.definitions || {})[k] || '')}">${l} ${c[k] == null ? '未统计' : c[k]}</span>`).join(' ');
  const stageName = { lesson_plan: '备课', questions: '出题', grading: '批改', analysis: '学情', review: '讲评', explanation: '讲解' };
  const stateName = { pending: '待审定', approved: '已通过', rejected: '未通过', disputed: '有分歧', changed: '内容有变化，需重审' };
  $('#research-cases').innerHTML = cases.length ? cases.map((x) => `<div class="task-row" data-case="${x.id}"><div class="task-main">${esc(stageName[x.stage] || x.stage)}
      <span class="pill">${esc(stateName[x.status] || x.status)}</span>${x.split ? ` <span class="pill">${x.split === 'test' ? '测试集' : '开发集'}</span>` : ''}
      <span class="muted">${x.artifact_ids.length} 个产物 · 来源组 ${esc(x.source_group.slice(-6))} · 审定 ${x.reviews.length} 次</span></div>
      <div class="row"><input class="cr-reviewer" placeholder="审定人" style="width:7em"><input class="cr-standard" placeholder="审定标准">
      <button class="small secondary" data-case-verdict="approve">通过</button><button class="small link" data-case-verdict="reject">不通过</button></div></div>`).join('')
    : '<div class="kv muted">点“整理研究案例”按任务阶段生成；重试和内部调用不会增加案例。</div>';
  $('#consent-list').innerHTML = consents.map((x) => `<div class="kv">${esc(x.purposes.join('、'))} · 版本 ${esc(x.version)} · ${esc(x.granted_by)}
    ${x.revoked_at ? '<span class="pill">已撤回</span>' : `<button class="small link" data-consent-revoke="${x.id}">撤回</button>`}</div>`).join('') || '<div class="kv muted">还没有授权：训练数据导出不会包含本课程的内容。</div>';
}

function downloadJson(data, name) {
  const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 1)], { type: 'application/json' }));
  const a = document.createElement('a');
  a.href = url; a.download = name; a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function bindExtras() {
  $('#material-prefs-save').addEventListener('click', (e) => busy(e.target, '保存中…', async () => {
    const rows = [...document.querySelectorAll('#materials [data-mat]')];
    await api('PUT', `/api/projects/${S.pid}/material-prefs`, {
      exclude: rows.filter((r) => !$('.mat-use', r).checked).map((r) => r.dataset.mat),
      focus: Object.fromEntries(rows.map((r) => [r.dataset.mat, $('.mat-focus', r).value.trim()]).filter(([, v]) => v)),
    });
    toast('材料选择已保存，之后的生成按这里的选择'); await refresh();
  }));
  $('#revise-go').addEventListener('click', (e) => busy(e.target, '修改中…', async () => {
    const plans = await api('GET', `/api/projects/${S.pid}/lesson-plans`);
    const r = await api('POST', `/api/artifacts/${plans[0].id}/revise`, { target: $('#revise-target').value, instruction: $('#revise-instruction').value });
    $('#revise-instruction').value = '';
    showNote(r.context_note); toast(`只改了：${r.changed.join('、')}；存为新草稿，确认后生效`); await refresh();
  }));
  $('#package-btn').addEventListener('click', (e) => busy(e.target, '打包中…', async () => {
    const pkg = await api('POST', `/api/projects/${S.pid}/coursemaker-package`);
    downloadJson(pkg, `${pkg.package_id}.json`);
    toast('交换包已生成并下载；课件做好后可回传，系统会记录它依据的版本');
  }));
  $('#cases-build').addEventListener('click', (e) => busy(e.target, '整理中…', async () => {
    const r = await api('POST', `/api/research/cases/build?course_id=${encodeURIComponent(S.course)}`);
    toast(`新增 ${r.created} 个研究案例`); await renderResearch();
  }));
  $('#dataset-export').addEventListener('click', (e) => busy(e.target, '导出中…', async () => {
    const res = await fetch(`/api/datasets/export?course_id=${encodeURIComponent(S.course)}`, { method: 'POST', headers: { 'Idempotency-Key': newKey() } });
    if (!res.ok) throw new Error((await res.json()).detail || '导出失败');
    const data = await res.json();
    downloadJson(data, `${data.manifest.version}.json`);
    const ex = Object.entries(data.manifest.excluded).map(([k, n]) => `${k} ${n}`).join('，');
    toast(`导出 ${data.records.length} 条；排除：${ex || '无'}`);
  }));
  $('#consent-grant').addEventListener('click', (e) => busy(e.target, '记录中…', async () => {
    await api('POST', `/api/courses/${S.course}/consents`, { purposes: ['training'], version: $('#consent-version').value.trim(), granted_by: $('#consent-by').value.trim() });
    toast('已记录授权'); await renderResearch();
  }));
  $('#home').addEventListener('click', async (e) => {
    const ob = e.target.closest('[data-objective]');
    if (ob) { await busy(ob, '…', () => renderObjective(ob.dataset.objective)); return; }
    const ok = e.target.closest('[data-objective-ok]');
    if (ok) { await busy(ok, '…', async () => { await api('POST', `/api/objectives/${ok.dataset.objectiveOk}/reviewed`); await renderObjective(ok.dataset.objectiveOk); }); return; }
    const ur = e.target.closest('[data-unit-review]');
    if (ur) { await busy(ur, '…', () => renderUnitReview(ur.dataset.unitReview, ur.closest('.offering').querySelector('.ur-box'))); return; }
    const pv = e.target.closest('[data-ur-preview], [data-ur-go]');
    if (pv) {
      const box = pv.closest('.ur-box');
      const ids = [...box.querySelectorAll('.ur-pick:checked')].map((x) => x.value);
      const oid = pv.dataset.urPreview || pv.dataset.urGo;
      if (pv.dataset.urPreview) {
        await busy(pv, '…', async () => {
          const rows = await api('POST', `/api/offerings/${oid}/unit-review/preview`, { offering_id: oid, assignment_ids: ids });
          box.querySelector('.ur-preview').innerHTML = rows.map((r) => `<div class="kv">${esc(r.title)}：收到 ${r.received}/${r.roster}，${esc(r.coverage.label)}；未处理 ${r.open_items} 项；问题模式 ${r.patterns.length} 个</div>`).join('');
        });
      } else {
        await busy(pv, '生成中…', async () => {
          const r = await api('POST', `/api/offerings/${oid}/unit-review`, { offering_id: oid, assignment_ids: ids });
          await loadProjects(r.project.id, false);
          await selectProject(r.project.id, { tab: 'review' });
          showNote(r.context_note); toast('单元讲评稿已生成（新任务），请核对后确认');
        });
      }
      return;
    }
    const cv = e.target.closest('[data-case-verdict]');
    if (cv) {
      const row = cv.closest('[data-case]');
      await busy(cv, '…', async () => {
        const r = await api('POST', `/api/research/cases/${row.dataset.case}/review`, { reviewer: $('.cr-reviewer', row).value.trim(), standard: $('.cr-standard', row).value.trim(), verdict: cv.dataset.caseVerdict });
        toast(`已记录审定：${r.status}`); await renderResearch();
      });
      return;
    }
    const rv = e.target.closest('[data-consent-revoke]');
    if (rv) { await busy(rv, '…', async () => { const r = await api('POST', `/api/consents/${rv.dataset.consentRevoke}/revoke`); toast(`已撤回；标记了 ${r.datasets_marked.length} 个已导出的数据集`); await renderResearch(); }); }
  });
}

async function renderLegacy() {
  const prev = await api('GET', '/api/migration/preview');
  $('#legacy-count').textContent = prev.groups.reduce((n, g) => n + g.projects.length, 0);
  S.legacy = prev.groups;
  $('#legacy-list').innerHTML = prev.groups.map((g, i) => `<div class="legacy-group" data-legacy="${i}">
    <div class="muted">${esc(g.basis)}</div>
    ${g.projects.map((p) => `<div class="kv"><b>${esc(p.title)}</b> · ${p.artifacts} 个产物 · 名单 ${p.learners.length} 人${p.rounds_with_student_work.length ? ` · 有作答的轮次：${p.rounds_with_student_work.join('、')}` : ''}</div>`).join('')}
    <div class="grid-form">
      <label>放进课程<select class="lg-course">${g.existing_courses.map((c) => `<option value="${c.id}">${esc(c.title)}</option>`).join('')}<option value="">新建课程…</option></select></label>
      <label>新课程名称<input class="lg-title" value="${esc(g.suggested_course.title)}"></label>
      <label class="wide">这些学生代号属于哪个班（不填则只放进课程，作答仍留在项目里）<input class="lg-class" placeholder="例如：高一（3）班"></label>
      <div class="wide"><button class="small" data-map="${i}">确认整理</button></div>
    </div></div>`).join('');
}

function bindHomeEvents() {
  $('#home-course').addEventListener('change', async (e) => { S.course = e.target.value || null; S.offering = null; await renderHome(); });
  $('#home-class').addEventListener('change', (e) => { S.offering = e.target.value || null; store.set('edu.offering', S.offering); });
  $('#new-course-btn').addEventListener('click', () => { $('#new-course').classList.toggle('hidden'); syncEmptyCard(); });
  $('#nc-cancel').addEventListener('click', () => { $('#new-course').classList.add('hidden'); syncEmptyCard(); });
  $('#nc-subject').addEventListener('change', (e) => $('#nc-custom-wrap').classList.toggle('hidden', e.target.value !== '__custom'));
  $('#nc-create').addEventListener('click', (e) => busy(e.target, '创建中…', async () => {
    const subject = $('#nc-subject').value === '__custom' ? $('#nc-custom').value.trim() : $('#nc-subject').value;
    const c = await api('POST', '/api/courses', { title: $('#nc-title').value.trim(), subject, grade: $('#nc-grade').value.trim() });
    $('#new-course').classList.add('hidden'); $('#nc-title').value = '';
    S.course = c.id; S.offering = null;
    toast('课程已创建：可以直接开始工作，班级以后再加'); await renderHome();
  }));
  $('#cls-add').addEventListener('click', (e) => busy(e.target, '处理中…', async () => {
    let classId = $('#cls-existing').value;
    const aliases = $('#cls-roster').value.split(/[\n,，]/).map((x) => x.trim()).filter(Boolean);
    if (!classId) {
      const name = $('#cls-name').value.trim();
      if (!name) throw new Error('请填写班级名称，或选择已有班级');
      classId = (await api('POST', '/api/classes', { name, aliases })).id;
    } else if (aliases.length) await api('POST', `/api/classes/${classId}/members`, { aliases });
    const o = await api('POST', `/api/courses/${S.course}/classes`, { class_id: classId });
    $('#cls-name').value = ''; $('#cls-roster').value = '';
    S.offering = o.id; toast(`${o.class_name}已关联到本课程`); await renderHome();
  }));
  $('#cm-add').addEventListener('click', (e) => busy(e.target, '添加中…', async () => {
    await api('POST', `/api/courses/${S.course}/materials`, { text: $('#cm-text').value, filename: $('#cm-name').value || '课程资料' });
    $('#cm-text').value = ''; $('#cm-name').value = ''; toast('课程资料已添加'); await renderHome();
  }));
  $('#sf-cancel').addEventListener('click', () => $('#start-form').classList.add('hidden'));
  $('#sf-go').addEventListener('click', (e) => busy(e.target, '准备中…', async () => {
    const r = await api('POST', `/api/courses/${S.course}/tasks`, {
      kind: S.startKind, offering_id: S.offering || '', knowledge_module: $('#sf-module').value.trim(), lesson: $('#sf-lesson').value.trim(), request: $('#sf-request').value.trim(),
    });
    $('#start-form').classList.add('hidden');
    await loadProjects(r.project.id, false);
    await selectProject(r.project.id, { tab: r.tab });
    toast(`已开始：${r.task.title}`);
  }));
  $('#unit-add').addEventListener('click', (e) => busy(e.target, '添加中…', async () => {
    await api('POST', `/api/courses/${S.course}/units`, { title: $('#unit-title').value.trim() });
    $('#unit-title').value = ''; await renderHome();
  }));
  $('#home').addEventListener('click', async (e) => {
    const unit = e.target.closest('[data-unit]');
    if (unit) { await busy(unit, '…', () => renderUnit(unit.dataset.unit)); return; }
    if (e.target.closest('[data-unit-close]')) { S.unit = null; $('#unit-view').innerHTML = ''; return; }
    const start = e.target.closest('[data-start]');
    if (start) {
      S.startKind = start.dataset.start;
      $('#start-form').classList.remove('hidden');
      $('#sf-go').textContent = `开始：${START_LABEL[S.startKind]}`;
      const hint = $('#start-hint');
      hint.classList.add('hidden');
      if (S.startKind === 'prep' && S.offering) {
        const sug = await api('GET', `/api/offerings/${S.offering}/next-lesson`);
        // A suggestion fills empty fields only: it never overwrites what the teacher already typed.
        if (!$('#sf-module').value.trim() && sug.knowledge_module) $('#sf-module').value = sug.knowledge_module;
        if (!$('#sf-lesson').value.trim() && sug.lesson) $('#sf-lesson').value = sug.lesson;
        const parts = [`${sug.class_name}${sug.progress ? `已上到：${sug.progress}` : ''}`];
        if (sug.unfinished.length) parts.push(`${sug.unfinished.length} 节计划课还没标记是否已上`);
        if (sug.class_status) parts.push(`将参考本班已确认的学情：${sug.class_status.patterns.map((x) => x.name).join('、') || sug.class_status.summary}`);
        parts.push(sug.note);
        hint.textContent = parts.join('；');
        hint.classList.remove('hidden');
      } else if (!S.offering && S.startKind !== 'explain') {
        hint.textContent = '没有指定班级：可以先备课或出题，布置和批改时再选班级。';
        hint.classList.remove('hidden');
      }
      return;
    }
    const open = e.target.closest('[data-open-task]');
    if (open) { await loadProjects(open.dataset.openTask, false); await selectProject(open.dataset.openTask, { tab: open.dataset.tab, aid: null }); return; }
    const done = e.target.closest('[data-done-task]');
    if (done) { await busy(done, '…', async () => { await api('PATCH', `/api/tasks/${done.dataset.doneTask}`, { status: 'done' }); await renderHome(); }); return; }
    const attn = e.target.closest('[data-attn]');
    if (attn) {
      const a = S.home.attention[Number(attn.dataset.attn)];
      await loadProjects(a.project_id, false);
      await selectProject(a.project_id, { tab: 'grading', aid: a.assignment ? a.assignment.id : null });
      return;
    }
    const st = e.target.closest('[data-session-status]');
    if (st) {
      const sid = st.closest('[data-session]').dataset.session;
      await busy(st, '…', async () => { await api('PATCH', `/api/sessions/${sid}`, { status: st.dataset.sessionStatus }); await renderHome(); });
      return;
    }
    const map = e.target.closest('[data-map]');
    if (map) {
      const box = map.closest('[data-legacy]');
      const g = S.legacy[Number(map.dataset.map)];
      const courseId = $('.lg-course', box).value;
      const className = $('.lg-class', box).value.trim();
      const body = { project_ids: g.projects.map((p) => p.id), course_id: courseId || null, course: courseId ? null : { title: $('.lg-title', box).value.trim(), subject: g.subject } };
      if (className) body.class_name = className;
      if (!window.confirm(`把 ${g.projects.length} 个项目整理进课程${className ? `，学生代号归入“${className}”` : ''}？整理前会自动备份数据库。`)) return;
      await busy(map, '整理中…', async () => {
        const r = await api('POST', '/api/migration/map', body);
        S.course = r.course_id;
        toast(`已整理：产物 ${r.checked.artifacts} 个、引用 ${r.checked.refs} 条，前后一致${r.assignments.length ? `；生成 ${r.assignments.length} 条布置记录` : ''}`);
        await renderHome();
      });
    }
  });
  $('#home').addEventListener('change', async (e) => {
    if (!e.target.classList.contains('s-date')) return;
    const sid = e.target.closest('[data-session]').dataset.session;
    await busy(null, '', async () => { await api('PATCH', `/api/sessions/${sid}`, { planned_date: e.target.value }); await renderHome(); });
  });
}

init().catch((e) => toast(e.message, true));
