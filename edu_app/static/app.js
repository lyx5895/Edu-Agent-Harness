'use strict';

const S = { meta: null, pid: null, overview: null, tab: 'objectives', subject: null };
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
const TYPE_LABEL = { objective: '教学目标', question: '题目', rubric: '评分标准', submission: '学生作答', grading: '批改', analysis: '学情分析', problem_pattern: '问题模式', comparison: '两轮对比' };
const RISK_LABEL = {
  undetermined_point: '有未判定要点', review_disagreement: '复核模型意见不同', audit_disagreement: '抽检发现分歧',
  writing_needs_confirmation: '写作建议分待确认', ungrounded_quote: '引用无法核对', low_confidence: '模型没把握',
};
const STATUS_LABEL = { earned: '得分', partial: '部分得分', missed: '失分', undetermined: '待判定' };

async function api(method, path, body) {
  const opts = { method, headers: {} };
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
  $('#subject-select').innerHTML = S.meta.subjects.map((s) => `<option value="${s.id}">${esc(s.name)}</option>`).join('');
  const ms = $('#model-status');
  ms.textContent = S.meta.model_ready ? `模型：${S.meta.models.grader} / 复核：${S.meta.models.reviewer}` : '未配置模型（只能查看）';
  ms.className = 'pill ' + (S.meta.model_ready ? 'ok' : 'bad');
  await loadProjects(localStorage.getItem('edu.pid'));
  bindEvents();
}

async function loadProjects(selectId) {
  const projects = await api('GET', '/api/projects');
  const sel = $('#project-select');
  sel.innerHTML = projects.map((p) => `<option value="${p.id}">${esc(p.title)}（${esc(p.subject_name)}）</option>`).join('');
  if (!projects.length) { showWelcome(true); return; }
  const target = projects.find((p) => p.id === selectId) ? selectId : projects[0].id;
  sel.value = target;
  await selectProject(target);
}

function showWelcome(on) {
  $('#welcome').classList.toggle('hidden', !on);
  $('#workspace').classList.toggle('hidden', on || !S.pid);
  $('#cancel-project').classList.toggle('hidden', !S.pid);
}

async function selectProject(pid) {
  S.pid = pid;
  localStorage.setItem('edu.pid', pid);
  showWelcome(false);
  $('#workspace').classList.remove('hidden');
  showNote('');
  await refresh();
}

async function refresh() {
  if (!S.pid) return;
  S.overview = await api('GET', `/api/projects/${S.pid}`);
  S.subject = S.meta.subjects.find((s) => s.id === S.overview.project.subject);
  renderOverview();
  await renderTab();
}

function renderOverview() {
  const o = S.overview;
  $('#project-title').textContent = o.project.title;
  $('#project-meta').textContent = `${o.project.subject_name}${o.project.grade ? ' · ' + o.project.grade : ''} · 材料 ${o.materials} 份 · 学生 ${o.learners} 人`;
  const c = o.counts;
  const stat = (label, k) => `<span class="stat"><b>${c[k].confirmed}</b>/${c[k].total} ${label}</span>`;
  $('#overview-stats').innerHTML = [
    stat('目标已确认', 'objective'), stat('题目已确认', 'question'), `<span class="stat"><b>${c.submission.total}</b> 份作答</span>`,
    stat('批改已确认', 'grading'), stat('问题模式已确认', 'problem_pattern'),
    `<span class="stat"><b>${o.review_queue}</b> 待复核</span>`,
  ].join('');
  $('#next-step').innerHTML = `下一步：<b>${esc(o.next.join('、'))}</b>`;
  const banner = $('#stale-banner');
  const parts = [];
  if (o.stale.length) parts.push(`有 ${o.stale.length} 个产物的依据已变化（上游有实质修改），请到“⑥ 过期与导出”处理。`);
  if (o.pending_ab.length) parts.push(`有 ${o.pending_ab.length} 组 A/B 候选题等待选择（在“② 出题改题”）。`);
  banner.innerHTML = parts.map(esc).join('<br>');
  banner.classList.toggle('hidden', !parts.length);
}

async function renderTab() {
  $$('#tabs button').forEach((b) => b.classList.toggle('active', b.dataset.tab === S.tab));
  $$('.tab').forEach((t) => t.classList.toggle('hidden', t.id !== `tab-${S.tab}`));
  const fn = { objectives: renderObjectives, questions: renderQuestions, grading: renderGrading, analysis: renderAnalysis, practice: renderPractice, records: renderRecords }[S.tab];
  try { await fn(); } catch (e) { toast(e.message, true); }
}

// ---------- ① materials & objectives ----------

async function renderObjectives() {
  const [mats, objs] = await Promise.all([
    api('GET', `/api/projects/${S.pid}/materials`),
    api('GET', `/api/projects/${S.pid}/artifacts?type=objective`),
  ]);
  $('#materials').innerHTML = mats.length
    ? mats.map((m) => `<div class="item"><div class="item-head"><b>${esc(m.filename)}</b><span class="pill">${m.chars} 字 · 材料家族 ${esc(m.family_id.slice(-6))}</span></div><div class="kv">${esc(m.text)}${m.chars > 400 ? '…' : ''}</div></div>`).join('')
    : '<div class="empty">还没有材料。添加教材、讲义或教案后，可以按材料起草教学目标。</div>';
  $('#objectives').innerHTML = objs.length ? objs.map(objectiveItem).join('') : '<div class="empty">还没有教学目标。</div>';
  if (!mats.length) $('#tab-objectives details').open = true;  // a new project starts by adding material
}

function objectiveItem(a) {
  const c = a.content;
  const needsKind = a.status === 'draft' && a.confirmed_version;
  return `<div class="item" data-id="${a.id}">
    <div class="item-head"><div><b>${esc(c.text)}</b> ${c.inferred ? '<span class="pill">由导入试卷反推</span>' : ''}</div><div>${statusPill(a)}</div></div>
    <div class="kv">达成标准：${esc(c.success_criterion || '（未填写）')}</div>
    <div class="item-actions">
      <button class="small secondary" data-act="edit-objective">编辑</button>
      ${a.status !== 'confirmed' ? `${changeKindSelect(needsKind)}<button class="small" data-act="confirm">确认</button>` : ''}
    </div>
    <div class="edit-area"></div>
  </div>`;
}

function objectiveForm(c = {}) {
  return `<div class="edit-grid"><label>目标<input class="f-text" value="${esc(c.text || '')}"></label>
    <label>达成标准<input class="f-crit" value="${esc(c.success_criterion || '')}"></label>
    <div class="item-actions"><button class="small" data-act="save-objective">保存为新版本</button><button class="small secondary" data-act="cancel-edit">取消</button></div></div>`;
}

// ---------- ② questions ----------

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
  const qs = await api('GET', `/api/projects/${S.pid}/questions?round=${round}`);
  const candidates = qs.filter((q) => q.status === 'candidate' && q.candidate && S.overview.pending_ab.includes(q.candidate.candidate_group));
  const groups = {};
  candidates.forEach((q) => { (groups[q.candidate.candidate_group] ||= { A: [], B: [] })[q.candidate.position].push(q); });
  $('#ab-panel').innerHTML = Object.entries(groups).map(([g, sides]) => `<div class="card" data-group="${g}">
    <h3>A/B 两版候选：请选择更合适的一版</h3>
    <p class="muted">两版只在出题提示上有一处不同；左右位置是随机的。选择后被采用的一版进入草稿，未采用的保留为研究记录。</p>
    <div class="ab">${['A', 'B'].map((pos) => `<div><h4>候选 ${pos}</h4>${sides[pos].map((q) => questionBody(q)).join('') || '<div class="empty">（无）</div>'}</div>`).join('')}</div>
    <div class="row"><input class="ab-reason" placeholder="理由（选填）">
      <button data-verdict="A">采用 A</button><button data-verdict="B">采用 B</button>
      <button class="secondary" data-verdict="same">差不多</button><button class="secondary" data-verdict="neither">都不合适</button></div>
  </div>`).join('');
  const shown = qs.filter((q) => q.status !== 'candidate');
  $('#questions').innerHTML = (shown.length ? shown.map(questionItem).join('') : '<div class="empty">这一轮还没有题目。</div>')
    + `<div class="row"><button class="secondary" data-act="new-question">手工添加题目</button></div><div class="new-question-area"></div>`;
}

function questionBody(q) {
  const c = q.content;
  const r = q.rubric ? q.rubric.content : { points: [], total: 0 };
  const checks = c.checks || {};
  const warn = [];
  if (checks.answer_suspect) warn.push('<span class="pill bad">答案存疑</span>');
  if (checks.solver === 'agree') warn.push('<span class="pill ok">独立解题自检通过</span>');
  const notes = (checks.notes || []).map((n) => `<div class="kv">⚠ ${esc(n)}</div>`).join('');
  const options = (c.options || []).map((o, i) => `<div>${'ABCDEFGH'[i]}. ${esc(o)}</div>`).join('');
  return `<div class="item-head"><div><b>第 ${c.no} 题</b> · ${esc(S.meta.question_types[c.type] || c.type)} · 满分 ${r.total}
      ${(c.target_patterns || []).length ? '<span class="pill info">巩固练习</span>' : ''}</div><div>${warn.join(' ')}</div></div>
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

// ---------- ③ grading ----------

async function renderGrading() {
  fillRoundSelect('#g-round');
  const round = Number($('#g-round').value || 1);
  const [learners, queue, gradings] = await Promise.all([
    api('GET', `/api/projects/${S.pid}/learners`),
    api('GET', `/api/projects/${S.pid}/review-queue?round=${round}`),
    api('GET', `/api/projects/${S.pid}/gradings?round=${round}`),
  ]);
  $('#roster-list').textContent = learners.length ? `名单（${learners.length} 人）：${learners.map((l) => l.alias).join('、')}` : '名单为空';
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

function queueItem(g) {
  const c = g.content;
  const tags = S.subject.tags;
  return `<div class="item" data-id="${g.id}">
    <div class="item-head"><div><b>${esc(c.learner_alias)}</b> · 第 ${c.question_no} 题 · 当前 ${c.total}/${c.max_total}</div>
      <div>${c.risks.map((r) => `<span class="pill warn">${esc(RISK_LABEL[r] || r)}</span>`).join(' ')}</div></div>
    ${g.question ? `<details><summary>题目</summary><div class="stem">${esc(g.question.stem)}</div><div class="kv">参考答案：${esc(g.question.reference)}</div></details>` : ''}
    <div class="answer">${esc(g.answer)}</div>
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

// ---------- ④ analysis ----------

async function renderAnalysis() {
  fillRoundSelect('#a-round');
  const round = Number($('#a-round').value || 1);
  const [analyses, patterns] = await Promise.all([api('GET', `/api/projects/${S.pid}/analyses`), api('GET', `/api/projects/${S.pid}/patterns`)]);
  const mine = analyses.filter((a) => (a.content.round || 1) === round);
  const latest = mine[mine.length - 1];
  if (!latest) { $('#analysis').innerHTML = '<div class="empty">这一轮还没有学情分析。</div>'; }
  else {
    const detail = await api('GET', `/api/artifacts/${latest.id}`);
    $('#analysis').innerHTML = analysisView(latest, detail.context_note);
  }
  $('#patterns').innerHTML = patterns.length ? patterns.map(patternItem).join('') : '<div class="empty">还没有问题模式。</div>';
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
  return `${head}<p>${esc(c.summary)}</p>
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

// ---------- ⑤ practice & comparison ----------

async function renderPractice() {
  const [patterns, comps] = await Promise.all([api('GET', `/api/projects/${S.pid}/patterns`), api('GET', `/api/projects/${S.pid}/comparisons`)]);
  const confirmed = patterns.filter((p) => p.confirmed_version);
  $('#practice-patterns').innerHTML = confirmed.length
    ? confirmed.map((p) => `<label><input type="checkbox" value="${p.lineage_id}" checked> ${esc(p.content.name)}（${p.content.affected_learners.length} 人）</label>`).join('')
    : '<div class="empty">还没有已确认的问题模式（在“④ 学情分析”中确认）。</div>';
  const latest = comps[comps.length - 1];
  $('#comparison').innerHTML = latest ? comparisonView(latest) : '<div class="empty">还没有对比结果。</div>';
}

function comparisonView(a) {
  const c = a.content;
  const list = (xs) => xs.length ? esc(xs.join('、')) : '—';
  return `<p class="muted">第 ${c.round_a} 轮 → 第 ${c.round_b} 轮 ${a.stale ? '<span class="pill bad">依据已变化</span>' : ''}</p>
    <table><tr><th>问题模式</th><th>前一轮涉及</th><th>复测</th><th>已改善</th><th>仍存在</th><th>新出现</th><th>未复测</th></tr>
    ${c.patterns.map((r) => `<tr><td>${esc(r.name)}${r.enough_evidence ? '' : '<div class="kv">没有针对它的复测题，证据不足</div>'}</td><td>${list(r.before)}</td><td>${list(r.retested)}</td>
      <td>${list(r.improved)}</td><td>${list(r.persisting)}</td><td>${list(r.new)}</td><td>${list(r.not_retested)}</td></tr>`).join('')}</table>`;
}

// ---------- ⑥ stale & export ----------

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
}

// ---------- events ----------

function bindEvents() {
  $('#project-select').addEventListener('change', (e) => selectProject(e.target.value));
  $('#new-project-btn').addEventListener('click', () => showWelcome(true));
  $('#cancel-project').addEventListener('click', () => showWelcome(false));
  $('#project-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    const p = await busy(e.submitter, '创建中…', () => api('POST', '/api/projects', Object.fromEntries(f.entries())));
    if (p) { e.target.reset(); await loadProjects(p.id); toast('项目已创建，先添加课程材料'); }
  });
  $('#tabs').addEventListener('click', (e) => { if (e.target.dataset.tab) { S.tab = e.target.dataset.tab; showNote(''); renderTab(); } });
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
  $('#draft-objectives').addEventListener('click', (e) => busy(e.target, '起草中…', async () => {
    const r = await api('POST', `/api/projects/${S.pid}/objectives/draft`);
    showNote(r.context_note); toast(`起草了 ${r.objectives.length} 条目标，请编辑后确认`); await refresh();
  }));
  $('#add-objective').addEventListener('click', () => {
    const box = $('#objectives');
    if ($('.new-objective', box)) return;
    box.insertAdjacentHTML('afterbegin', `<div class="item new-objective">${objectiveForm()}</div>`);
  });
  $('#generate-questions').addEventListener('click', (e) => busy(e.target, '出题与自检中…（约 1 分钟）', async () => {
    const types = $$('#type-boxes input:checked').map((i) => i.value);
    const r = await api('POST', `/api/projects/${S.pid}/questions/generate`, {
      types, count: Number($('#q-count').value), difficulty: $('#q-difficulty').value, notes: $('#q-notes').value, ab: $('#q-ab').checked ? true : false,
    });
    showNote(r.context_note);
    const dropped = r.candidates.flatMap((c) => c.dropped || []);
    toast((r.candidate_group ? '生成了 A/B 两版，请先选择' : `生成了 ${r.candidates[0].questions.length} 道题，请核对后确认`) + (dropped.length ? `；丢弃 ${dropped.length} 道不完整的题：${dropped.join('；')}` : ''), dropped.length > 0);
    S.showRound = 1; await refresh();
  }));
  $('#import-paper').addEventListener('click', (e) => busy(e.target, '整理中…', async () => {
    const r = await api('POST', `/api/projects/${S.pid}/import-paper`, { text: $('#paper-text').value, filename: '导入试卷' });
    $('#paper-text').value = ''; showNote(r.context_note);
    toast(`导入了 ${r.questions.length} 道题、反推 ${r.objectives.length} 条目标，请核对确认` + ((r.dropped || []).length ? `；${r.dropped.length} 道无法整理：${r.dropped.join('；')}` : ''), (r.dropped || []).length > 0); await refresh();
  }));
  $('#add-roster').addEventListener('click', (e) => busy(e.target, '加入中…', async () => {
    const aliases = $('#roster').value.split(/[\n,，]/).map((s) => s.trim()).filter(Boolean);
    const r = await api('POST', `/api/projects/${S.pid}/learners`, { aliases });
    $('#roster').value = ''; toast(`加入 ${r.added.length} 人`); await refresh();
  }));
  $('#import-subs').addEventListener('click', (e) => busy(e.target, '导入中…', async () => {
    const r = await api('POST', `/api/projects/${S.pid}/submissions`, { csv: $('#sub-csv').value, round: Number($('#g-round').value || 1), add_missing: $('#sub-add-missing').checked });
    const msgs = [`导入 ${r.created} 份作答`];
    if (r.unmatched_learners.length) msgs.push(`名单中没有：${r.unmatched_learners.join('、')}（未导入）`);
    if (r.problems.length) msgs.push(r.problems.join('；'));
    $('#import-result').textContent = msgs.join('。');
    if (r.created) $('#sub-csv').value = '';
    await refresh();
  }));
  $('#grade-round').addEventListener('click', (e) => busy(e.target, '批改中…', async () => {
    const r = await api('POST', `/api/projects/${S.pid}/grade`, { round: Number($('#g-round').value || 1) });
    const msgs = [`批改 ${r.graded} 份`, `进入复核队列 ${r.review_queue} 份`];
    if (r.skipped.length) msgs.push(`跳过 ${r.skipped.length} 份（题目或评分标准未确认）`);
    if (r.failed.length) msgs.push(`失败 ${r.failed.length} 份：${r.failed[0].error}`);
    toast(msgs.join('，'), r.failed.length > 0); await refresh();
  }));
  $('#confirm-auto').addEventListener('click', (e) => busy(e.target, '确认中…', async () => {
    const r = await api('POST', `/api/projects/${S.pid}/gradings/confirm`, { round: Number($('#g-round').value || 1) });
    toast(`确认了 ${r.confirmed} 份批改`); await refresh();
  }));
  $('#analyze').addEventListener('click', (e) => busy(e.target, '分析中…', async () => {
    const r = await api('POST', `/api/projects/${S.pid}/analyze`, { round: Number($('#a-round').value || 1) });
    showNote(r.context_note); toast(r.patterns.length ? `归纳出 ${r.patterns.length} 个问题模式，请核对确认` : '分析完成'); await refresh();
  }));
  $('#practice').addEventListener('click', (e) => busy(e.target, '出题与自检中…', async () => {
    const ids = $$('#practice-patterns input:checked').map((i) => i.value);
    const r = await api('POST', `/api/projects/${S.pid}/practice`, { pattern_ids: ids, count: Number($('#p-count').value) });
    showNote(r.context_note);
    toast(`第 ${r.round} 轮巩固练习：${r.questions.length} 道题，请到“② 出题改题”确认` + ((r.dropped || []).length ? `；丢弃 ${r.dropped.length} 道：${r.dropped.join('；')}` : ''), (r.dropped || []).length > 0);
    S.showRound = r.round; S.tab = 'questions'; await refresh();
  }));
  $('#compare').addEventListener('click', (e) => busy(e.target, '对比中…', async () => {
    await api('POST', `/api/projects/${S.pid}/compare`, { round_a: Number($('#cmp-a').value), round_b: Number($('#cmp-b').value) });
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
      await api('POST', `/api/projects/${S.pid}/choices`, { group: card.dataset.group, verdict: btn.dataset.verdict, reason: $('.ab-reason', card).value });
      toast('已记录选择'); await refresh();
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
    case 'edit-objective': {
      const a = await api('GET', `/api/artifacts/${item.dataset.id}`);
      $('.edit-area', item).innerHTML = objectiveForm(a.content);
      break;
    }
    case 'save-objective': {
      const box = btn.closest('.item');
      const content = { text: $('.f-text', box).value, success_criterion: $('.f-crit', box).value };
      await busy(btn, '保存中…', async () => {
        if (box.classList.contains('new-objective')) await api('POST', `/api/projects/${S.pid}/objectives`, { content });
        else await api('PUT', `/api/artifacts/${box.dataset.id}`, { content });
        toast('已保存为草稿，确认后生效'); await refresh();
      });
      break;
    }
    case 'cancel-edit': {
      const box = btn.closest('.new-objective');
      if (box) box.remove(); else { const area = btn.closest('.edit-area') || btn.closest('.new-question-area'); if (area) area.innerHTML = ''; }
      break;
    }
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
      const list = $('.f-points', btn.closest('.edit-grid'));
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
    case 'regrade-stale':
      await busy(btn, '重新批改中…', async () => {
        let n = 0;
        for (const r of rounds()) n += (await api('POST', `/api/projects/${S.pid}/grade`, { round: r })).graded;
        toast(`重新批改了 ${n} 份`); await refresh();
      });
      break;
    default:
  }
}

function pick(obj, keys) { const o = {}; keys.forEach((k) => { o[k] = obj[k] ?? (Array.isArray(obj[k]) ? [] : obj[k]); }); return o; }

init().catch((e) => toast(e.message, true));
