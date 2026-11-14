// Teacher workspace: class overview, authoring, planning and review. Hash routes: #dashboard, #publish, ...
import {
  $, $$, ACTION, EVIDENCE, STATUS, api, brand, busy, esc, extractMaterial, html, icon, identify, lines, openDialog,
  pill, pillFor, raw, signOut, store, toast, uniqueKey, when,
} from '/static/common.js';

const root = $('#app');
const PAGES = {
  dashboard: { title: '班级学情', icon: 'chart', render: dashboardPage, sub: '按学习目标汇总的证据、需要帮助的学生和待复核的作答。' },
  publish: { title: '创建练习', icon: 'pencil', render: publishPage, sub: '学生只看到题目和学习要求；参考答案和评分细则只用于判定。' },
  packs: { title: '批量出题', icon: 'layers', render: packsPage, sub: '列出学习目标，模型起草题目，你逐题审核后发布。' },
  planning: { title: '学习安排', icon: 'route', render: planningPage, sub: '组织学习顺序，给学生布置复习、补救或诊断。' },
  review: { title: '作答复核', icon: 'review', render: reviewPage, sub: '核对系统判定的依据，确认或改判。改判会让依赖它的学习记录重新检查。' },
};
const PRIORITY = { urgent: ['紧急', 'red'], risk: ['风险', 'amber'], sample: ['抽检', ''] };
const RISKS = {
  negative_judgment: '负面判定',
  low_confidence: '低置信',
  independent_by_model_only: '仅模型判定',
  model_reference: '模型参考答案',
  criterion_undetermined: '分项未定',
};
const FOLLOWUP_STATUS = { queued: ['待开始', 'blue'], active: ['进行中', 'accent'], completed: ['独立检查通过', 'green'], cancelled: ['已撤销', ''], needs_recheck: ['证据失效', 'red'] };
const KIND = { review: '复习', remediation: '补救', diagnostic: '诊断' };

const state = { me: null, meta: null, classId: '', classes: [], assignments: null, reviewCount: 0 };

// ---------- shell ----------

async function boot() {
  try {
    state.me = await identify(root, 'teacher');
    state.meta = await api('/api/meta');
  } catch (error) {
    root.innerHTML = `<div class="empty" style="padding-top:20vh"><strong>无法连接服务器</strong>${esc(error.message)}</div>`;
    return;
  }
  state.classes = await api('/api/teacher/classes').catch(() => []);
  state.classId = state.me.mode === 'classroom' ? state.me.class_id : store.get('teacher.class', '');
  // With exactly one class in use there is nothing to choose.
  if (!state.classId && state.classes.length === 1) state.classId = state.classes[0];
  renderShell();
  window.addEventListener('hashchange', route);
  route();
}

function renderShell() {
  const classroom = state.me.mode === 'classroom';
  const nav = Object.entries(PAGES)
    .map(([key, page]) => `<a class="nav-item" href="#${key}" data-nav="${key}">${icon(page.icon)}${esc(page.title)}${key === 'review' ? '<span class="count" id="review-count" hidden></span>' : ''}</a>`)
    .join('');
  root.innerHTML = html`
    <div class="teacher-layout">
      <nav class="sidebar" id="sidebar" aria-label="教师工作台">
        ${raw(brand('教师'))}
        ${raw(nav)}
        <div class="sidebar-foot">
          ${classroom
            ? raw(`<div class="row between"><span>班级模式 · ${esc(state.me.class_id)}</span><button class="btn ghost small" id="sign-out" title="退出">${icon('logout')}</button></div>`)
            : raw(`<div>本地演示</div><div>${esc(state.meta.model)} · ${esc(state.meta.policy)}</div>`)}
        </div>
      </nav>
      <div class="main">
        <div class="mobile-bar"><button class="btn ghost small menu-toggle" id="menu" aria-label="菜单">${raw(icon('menu'))}</button>${raw(brand('教师'))}</div>
        <header class="page-head">
          <div><h1 id="page-title"></h1><p id="page-sub"></p></div>
          <div class="class-picker">
            <label class="label" for="class-id" style="margin:0;white-space:nowrap">班级</label>
            <input class="field" id="class-id" list="class-list" placeholder="输入或选择班级" value="${state.classId}" ${raw(classroom ? 'readonly' : '')}>
            <datalist id="class-list">${raw(state.classes.map((c) => `<option value="${esc(c)}">`).join(''))}</datalist>
          </div>
        </header>
        <div id="page"></div>
      </div>
    </div>`;
  if (classroom) $('#sign-out').onclick = signOut;
  $('#menu').onclick = () => $('#sidebar').classList.toggle('open');
  $('#class-id').addEventListener('change', (event) => setClass(event.target.value.trim()));
}

function setClass(classId) {
  state.classId = classId;
  state.assignments = null;
  store.set('teacher.class', classId);
  if (classId && !state.classes.includes(classId)) {
    state.classes = [...state.classes, classId].sort();
    $('#class-list').innerHTML = state.classes.map((c) => `<option value="${esc(c)}">`).join('');
  }
  route();
}

function route() {
  const [key, ...args] = (location.hash.slice(1) || 'dashboard').split('/');
  const page = PAGES[key] || PAGES.dashboard;
  $$('[data-nav]').forEach((a) => a.classList.toggle('active', a.dataset.nav === (PAGES[key] ? key : 'dashboard')));
  $('#sidebar').classList.remove('open');
  $('#page-title').textContent = page.title;
  $('#page-sub').textContent = page.sub;
  const container = $('#page');
  container.innerHTML = '<div class="empty"><span class="spinner"></span></div>';
  Promise.resolve(page.render(container, args.map(decodeURIComponent))).catch((error) => {
    container.innerHTML = `<div class="card empty"><strong>页面加载失败</strong>${esc(error.message)}</div>`;
  });
}

function needClass(container, what) {
  if (state.classId) return false;
  const known = state.classes.length
    ? `<div class="row wrap" style="justify-content:center;margin-top:14px">${state.classes.map((c) => `<button class="btn small" data-class="${esc(c)}">${esc(c)}</button>`).join('')}</div>`
    : '';
  container.innerHTML = html`<div class="card empty">
    <strong>${what}前先选择班级</strong>
    在上方“班级”里填写班级名称后按回车。班级不需要事先创建：填一个新名字就是一个新班级，学生在学生端填同样的名字就能收到任务。
    ${raw(known ? '<div style="margin-top:14px">已有的班级：</div>' + known : '')}
  </div>`;
  $$('[data-class]', container).forEach((b) => (b.onclick = () => selectClass(b.dataset.class)));
  $('#class-id').focus();
  return true;
}

function selectClass(classId) {
  $('#class-id').value = classId;
  setClass(classId);
}

async function dashboard() {
  const query = state.classId ? '?' + new URLSearchParams({ class_id: state.classId }) : '';
  const report = await api('/api/teacher/dashboard' + query);
  setReviewCount(report.review_queue.length);
  return report;
}

function setReviewCount(n) {
  state.reviewCount = n;
  const badge = $('#review-count');
  if (!badge) return;
  badge.hidden = !n;
  badge.textContent = n > 99 ? '99+' : String(n);
}

async function assignments(force = false) {
  if (!state.classId) return [];
  if (!state.assignments || force) {
    state.assignments = await api('/api/teacher/assignments?' + new URLSearchParams({ class_id: state.classId }));
  }
  return state.assignments;
}

function assignmentOptions(list, selected = '') {
  return list
    .map((a) => `<option value="${esc(a.assignment_id)}" ${a.assignment_id === selected ? 'selected' : ''}>${esc(a.course_id)} · ${esc(a.objective)}</option>`)
    .join('');
}

// ---------- dashboard ----------

async function dashboardPage(container) {
  const report = await dashboard();
  const learning = report.learning;
  const learners = new Set(learning.map((r) => r.learner));
  const support = learning.filter((r) => ['needs_support', 'needs_recheck'].includes(r.state));
  const independent = learning.filter((r) => r.state === 'independent_observed').length;
  const queue = report.review_queue;
  const urgent = queue.filter((r) => r.priority === 'urgent').length;

  const order = { needs_recheck: 0, needs_support: 1, insufficient_evidence: 2, assisted_complete: 3, practice_complete: 4, independent_observed: 5 };
  const rows = [...learning].sort((a, b) => (order[a.state] ?? 9) - (order[b.state] ?? 9) || a.learner.localeCompare(b.learner, 'zh'));

  container.innerHTML = html`
    <div class="kpis">
      ${raw(kpi('学生', learners.size, '人', `涉及 ${learning.length} 个学习目标`))}
      ${raw(kpi('需要帮助', support.length, '项', '需要帮助或重新检查', support.length > 0))}
      ${raw(kpi('独立完成', independent, '项', '通过了无提示的新题检查'))}
      ${raw(kpi('待复核', queue.length, '条', urgent ? `其中 ${urgent} 条紧急` : '没有紧急项', urgent > 0))}
    </div>
    <div class="grid-2">
      <section class="card">
        <div class="card-head"><div><h3>学生学习情况</h3><div class="sub">需要帮助的排在前面</div></div></div>
        ${raw(
          rows.length
            ? `<div class="table-wrap"><table class="data responsive"><thead><tr><th>学生</th><th>学习目标</th><th>状态</th><th class="num">独立检查</th><th>建议下一步</th></tr></thead><tbody>${rows
                .map((r) => html`<tr><td><strong>${r.learner}</strong></td><td><div class="clip">${r.objective}</div></td><td>${raw(pillFor(EVIDENCE, r.state))}</td><td class="num" data-label="独立检查">${r.independent_count}</td><td class="small" data-label="建议">${r.next_step}</td></tr>`)
                .join('')}</tbody></table></div>`
            : `<div class="empty"><strong>还没有学习记录</strong>${state.classId ? '学生开始做这个班级的任务后，这里会出现他们的进展。' : '发布练习并让学生开始后，这里会出现他们的进展。'}</div>`,
        )}
      </section>
      <section class="card">
        <div class="card-head"><div><h3>建议分组补救</h3><div class="sub">同一目标、同一处困难的学生</div></div></div>
        ${raw(
          report.support_groups.length
            ? report.support_groups
                .map((g) => html`<div class="group"><div class="g-title">${g.objective}</div><div class="g-meta">${g.criterion}</div><div class="row wrap" style="margin-top:6px;gap:4px">${raw(g.learners.map((l) => pill(l)).join(''))}</div></div>`)
                .join('')
            : '<div class="empty">出现多个学生卡在同一处时，这里会建议分组。</div>',
        )}
      </section>
    </div>
    <section class="card mt">
      <div class="card-head"><div><h3>待复核作答</h3><div class="sub">按风险排序，点击一行去复核</div></div>${queue.length ? raw('<a class="btn small" href="#review">全部复核</a>') : ''}</div>
      ${raw(queue.length ? queueTable(queue.slice(0, 8)) : '<div class="empty">没有需要复核的作答。</div>')}
    </section>`;
  $$('tr[data-review]', container).forEach((tr) => {
    tr.onclick = () => (location.hash = 'review/' + tr.dataset.review);
  });
}

function kpi(label, value, unit, note, attention = false) {
  return `<div class="card kpi ${attention ? 'attention' : ''}"><div class="k-label">${esc(label)}</div><div class="k-value">${value}<small>${unit}</small></div><div class="k-note">${esc(note)}</div></div>`;
}

function queueTable(queue) {
  return `<div class="table-wrap"><table class="data responsive"><thead><tr><th>优先级</th><th>学生</th><th>题目与作答</th><th>原判定</th><th>原因</th></tr></thead><tbody>${queue
    .map(
      (r) => html`<tr class="clickable" tabindex="0" data-review="${r.session_id}/${r.event_id}">
        <td>${raw(pillFor(PRIORITY, r.priority))}</td>
        <td><strong>${r.learner}</strong></td>
        <td><div class="clip">${r.text || '（没有文字作答）'}</div><div class="small muted clip">${r.question}</div></td>
        <td>${raw(r.status ? pillFor(STATUS, r.status) : pill('反馈待审', 'blue'))}</td>
        <td class="small" data-label="原因">${(r.risks || []).map((x) => RISKS[x] || x).join('、') || (r.priority === 'urgent' ? '判定不确定或已转老师' : '随机抽检')}</td>
      </tr>`,
    )
    .join('')}</tbody></table></div>`;
}

// ---------- publish ----------

const ASSIGNMENT_FIELDS = ['course_id', 'domain', 'task_type', 'concept_id', 'prerequisite', 'objective', 'question', 'rubric', 'reference', 'material', 'teaching_note', 'check_requirements'];

async function publishPage(container) {
  if (needClass(container, '发布练习')) return;
  const bank = await assignments();
  const draft = store.get('draft.publish') || {}; // cleared drafts are stored as null
  const types = Object.entries(state.meta.task_types).map(([k, v]) => `<option value="${esc(k)}">${esc(v)}</option>`).join('');
  container.innerHTML = html`
    <form id="publish-form" class="stack" novalidate>
      <div class="grid-even">
        <section class="card card-pad">
          <div class="section-label">任务</div>
          <div class="form-grid">
            <div class="form-row"><label class="label" for="f-course_id">课程</label><input class="field" id="f-course_id" required maxlength="80" placeholder="例如：七年级数学"></div>
            <div class="form-row"><label class="label" for="f-domain">学科</label><input class="field" id="f-domain" required maxlength="80" placeholder="例如：数学"></div>
            <div class="form-row"><label class="label" for="f-task_type">教学类型</label><select class="field" id="f-task_type">${raw(types)}</select></div>
          </div>
          <div class="form-row"><label class="label" for="f-objective">学习目标 <span class="opt">（写成可以观察到的行为）</span></label><input class="field" id="f-objective" required maxlength="240" placeholder="例如：能根据路程和时间求平均速度，并写出单位"></div>
          <div class="form-row"><label class="label" for="f-question">题目</label><textarea class="field" id="f-question" rows="5" required maxlength="900"></textarea></div>
          <div class="form-grid">
            <div class="form-row"><label class="label" for="f-concept_id">知识点</label><input class="field" id="f-concept_id" required maxlength="80" placeholder="名称或知识点库编号"></div>
            <div class="form-row"><label class="label" for="f-prerequisite">先修知识 <span class="opt">（可选）</span></label><input class="field" id="f-prerequisite" maxlength="240"></div>
          </div>
          <div class="row wrap">
            <select class="field" id="kp-stage" style="width:auto"><option value="">学段不限</option><option>小学</option><option>初中</option><option>高中</option></select>
            <button type="button" class="btn small" id="kp-suggest" ${raw(state.meta.knowledge_available ? '' : 'disabled title="知识点库未导入"')}>${raw(icon('spark'))}从知识点库推荐</button>
          </div>
          <div id="kp-result" style="margin-top:10px"></div>
        </section>
        <section class="card card-pad">
          <div class="section-label">评分（学生看不到）</div>
          <div class="form-row"><label class="label" for="f-rubric">评分要求</label><textarea class="field" id="f-rubric" rows="3" required maxlength="600" placeholder="写清楚需要的解释、单位、形式等"></textarea></div>
          <div class="form-row"><label class="label" for="f-reference">参考答案</label><textarea class="field" id="f-reference" rows="3" required maxlength="700"></textarea></div>
          <div class="form-row"><label class="label" for="f-check_requirements">独立检查要求 <span class="opt">（每行一项，写可以迁移到新题的要求）</span></label><textarea class="field" id="f-check_requirements" rows="2"></textarea></div>
          <div class="form-row"><label class="label" for="f-teaching_note">给系统的教学提示 <span class="opt">（可选，不要写检查题答案）</span></label><input class="field" id="f-teaching_note" maxlength="500"></div>
          <label class="check"><input type="checkbox" id="f-allow_explanation" checked> 允许学生请求完整讲解</label>
        </section>
      </div>
      <section class="card card-pad">
        <details class="more" id="advanced">
          <summary>材料、分项评分、代数验证和检查题库</summary>
          <div class="form-row">
            <label class="label" for="f-material">教学材料 <span class="opt">（可选；判定和提示会引用它）</span></label>
            <textarea class="field" id="f-material" rows="4" maxlength="100000"></textarea>
            <div class="row" style="margin-top:6px"><label class="btn small">${raw(icon('file'))}从文件导入<input type="file" id="material-file" accept=".txt,.md,.pdf" hidden></label><span class="hint" style="margin:0">.txt / .md / 文字版 PDF</span></div>
          </div>
          <div class="form-row">
            <div class="row between"><span class="label">分项评分 <span class="opt">（最多 4 项）</span></span><button type="button" class="btn small" id="add-criterion">${raw(icon('plus'))}添加一项</button></div>
            <div class="editor-head criteria-row" id="criteria-head" hidden><span>编号</span><span>类型</span><span>要求</span><span>预期值</span><span></span></div>
            <div class="editor-rows" id="criteria"></div>
            <p class="hint">数值、单位、因式分解、引文和字数需要填写预期值；字数写成“最小:最大”。</p>
          </div>
          <div class="form-row">
            <span class="label">代数验证 <span class="opt">（可留空：题目里只有一个一元方程时会自动识别）</span></span>
            <div class="row wrap">
              <input class="field" id="f-algebra_initial" placeholder="原式，如 2x+3=7" style="flex:1;min-width:180px">
              <select class="field" id="f-algebra_kind" style="width:auto"><option value="none">自动</option><option value="equation">方程</option><option value="expression">表达式</option></select>
              <button type="button" class="btn small" id="algebra-preview">核对</button>
            </div>
            <div id="algebra-result" class="small" style="margin-top:6px"></div>
          </div>
          <div class="form-row">
            <span class="label">独立检查题库 <span class="opt">（勾选已发布的同知识点题目；不选则由系统生成并审核新题）</span></span>
            <div id="check-bank" class="stack small">${raw(
              bank.length
                ? bank.map((a) => html`<label class="check"><input type="checkbox" value="${a.assignment_id}"> ${a.course_id} · ${a.objective}</label>`).join('')
                : '<span class="muted">这个班级还没有已发布的练习。</span>',
            )}</div>
          </div>
        </details>
      </section>
      <div class="row" style="justify-content:flex-end;gap:8px">
        <button type="button" class="btn ghost" id="clear-form">清空</button>
        <button type="submit" class="btn primary" id="publish-btn">发布练习</button>
      </div>
      <div id="publish-result"></div>
    </form>`;

  const form = $('#publish-form');
  for (const key of ASSIGNMENT_FIELDS) if (draft[key] !== undefined) $('#f-' + key).value = draft[key];
  if (draft.allow_explanation === false) $('#f-allow_explanation').checked = false;
  (draft.criteria || []).forEach((c) => addCriterion(c));
  form.addEventListener('input', () => store.set('draft.publish', collectAssignment(false)));
  $('#add-criterion').onclick = () => addCriterion();
  $('#material-file').onchange = (e) => importFile(e.target, $('#f-material'));
  $('#kp-suggest').onclick = (e) => busy(e.currentTarget, suggestKnowledge);
  $('#algebra-preview').onclick = (e) => busy(e.currentTarget, previewAlgebra);
  $('#clear-form').onclick = () => {
    store.set('draft.publish', null);
    route();
  };
  form.addEventListener('submit', (event) => {
    event.preventDefault();
    busy($('#publish-btn'), publishAssignment);
  });
}

function addCriterion(value = {}) {
  const box = $('#criteria');
  if (box.children.length >= 4) return toast('每道题最多 4 项分项评分');
  const kinds = Object.entries(state.meta.criterion_kinds).map(([k, v]) => `<option value="${esc(k)}" ${k === (value.kind || 'model') ? 'selected' : ''}>${esc(v)}</option>`).join('');
  const row = document.createElement('div');
  row.className = 'editor-row criteria-row';
  row.innerHTML = html`
    <input class="field" data-k="id" placeholder="编号，如 unit" value="${value.id || ''}" aria-label="编号">
    <select class="field" data-k="kind" aria-label="类型">${raw(kinds)}</select>
    <input class="field" data-k="description" placeholder="评分要求" value="${value.description || ''}" aria-label="要求">
    <input class="field" data-k="expected" placeholder="预期值" value="${value.expected || ''}" aria-label="预期值">
    <button type="button" class="btn ghost small remove" aria-label="删除">${raw(icon('close'))}</button>`;
  row.querySelector('.remove').onclick = () => {
    row.remove();
    $('#criteria-head').hidden = !box.children.length;
    store.set('draft.publish', collectAssignment(false));
  };
  box.append(row);
  $('#criteria-head').hidden = false;
}

function collectAssignment(strict = true) {
  const data = { class_id: state.classId };
  for (const key of ASSIGNMENT_FIELDS) data[key] = $('#f-' + key).value.trim();
  data.check_requirements = strict ? lines(data.check_requirements) : data.check_requirements;
  data.allow_explanation = $('#f-allow_explanation').checked;
  data.algebra_initial = $('#f-algebra_initial').value.trim();
  data.algebra_kind = $('#f-algebra_kind').value;
  data.check_assignment_ids = $$('#check-bank input:checked').map((x) => x.value);
  data.criteria = $$('#criteria .editor-row')
    .map((row) => Object.fromEntries($$('[data-k]', row).map((x) => [x.dataset.k, x.value.trim()])))
    .filter((c) => c.id || c.description);
  return data;
}

async function publishAssignment() {
  const form = $('#publish-form');
  const missing = $$('[required]', form).find((x) => !x.value.trim());
  if (missing) {
    missing.focus();
    throw new Error('请填写：' + ($(`label[for="${missing.id}"]`)?.childNodes[0].textContent.trim() || '必填项'));
  }
  const result = await api('/api/teacher/assignments', { method: 'POST', body: collectAssignment() });
  store.set('draft.publish', null);
  state.assignments = null;
  $('#publish-result').innerHTML = html`<div class="card banner ok">
    <div class="text"><strong>已发布</strong><span class="small">布置给学生后，它才会出现在学生的任务列表里。任务编号 <code>${result.assignment_id}</code></span></div>
    <a class="btn primary" href="#planning/${result.assignment_id}">布置给学生</a>
    <button type="button" class="btn" id="publish-another">再发布一个</button>
  </div>`;
  $('#publish-another').onclick = () => route();
  $('#publish-result').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

async function suggestKnowledge() {
  const question = $('#f-question').value.trim();
  if (!question) throw new Error('请先填写题目');
  const params = new URLSearchParams({ question, subject: $('#f-domain').value.trim(), stage: $('#kp-stage').value });
  const result = await api('/api/teacher/knowledge/suggest?' + params);
  const box = $('#kp-result');
  if (!result.items.length) {
    box.innerHTML = '<p class="small muted">知识点库里没有找到匹配的知识点。</p>';
    return;
  }
  box.innerHTML = html`<div class="stack small">${raw(
    result.items
      .map((item, i) => html`<div class="row between" style="gap:12px"><span><code>${item.id}</code> ${item.label}${item.prerequisites.length ? raw(`<span class="muted">；先修：${esc(item.prerequisites.map((p) => p.name).join('、'))}</span>`) : ''}</span><button type="button" class="btn small" data-use="${i}">使用</button></div>`)
      .join(''),
  )}<p class="hint">来源：${result.license}</p></div>`;
  $$('[data-use]', box).forEach((button) => {
    button.onclick = () => {
      const item = result.items[Number(button.dataset.use)];
      $('#f-concept_id').value = item.id;
      if (item.prerequisites.length) $('#f-prerequisite').value = item.prerequisites.map((p) => p.name).join('、');
      store.set('draft.publish', collectAssignment(false));
      toast('已填入知识点，请核对');
    };
  });
}

async function previewAlgebra() {
  const question = $('#f-question').value.trim();
  if (!question) throw new Error('请先填写题目');
  const r = await api('/api/teacher/algebra/preview', { method: 'POST', body: { question, initial: $('#f-algebra_initial').value.trim(), kind: $('#f-algebra_kind').value } });
  $('#algebra-result').innerHTML = r.algebra_kind === 'none'
    ? '<span class="muted">没有绑定代数验证，判定将只依据评分要求。</span>'
    : html`<span class="pill green">已绑定</span> ${r.algebra_kind === 'equation' ? '方程' : '表达式'}：<code>${r.algebra_initial}</code>`;
}

async function importFile(input, target) {
  const file = input.files[0];
  if (!file) return;
  try {
    const text = await extractMaterial(file);
    target.value = [target.value.trim(), text].filter(Boolean).join('\n');
    target.dispatchEvent(new Event('input', { bubbles: true }));
    toast(`已导入 ${file.name}`);
  } catch (error) {
    toast(error.message, 'error');
  }
  input.value = '';
}

// ---------- packs ----------

async function packsPage(container, [packId]) {
  if (needClass(container, '批量出题')) return;
  const packs = await api('/api/teacher/packs?' + new URLSearchParams({ class_id: state.classId }));
  const current = packId ? packs.find((p) => p.pack_id === packId) || (await api('/api/teacher/packs/' + encodeURIComponent(packId))) : null;
  container.innerHTML = html`<div class="split">
    <aside class="card">
      <div class="card-head"><h3>练习包</h3><a class="btn small" href="#packs">${raw(icon('plus'))}新建</a></div>
      <div class="queue">${raw(
        packs.length
          ? packs
              .map((p) => html`<a class="list-item ${current && current.pack_id === p.pack_id ? 'active' : ''}" href="#packs/${p.pack_id}" style="text-decoration:none">
                <span class="title">${p.body.title}</span>
                <span class="meta">${raw(p.status === 'published' ? pill('已发布', 'green') : pill('草稿', 'amber'))}<span>${p.body.items.length} 题 · 版本 ${p.version}</span></span></a>`)
              .join('')
          : '<div class="empty">还没有练习包。</div>',
      )}</div>
    </aside>
    <div id="pack-main"></div>
  </div>`;
  if (current) renderPack($('#pack-main'), current);
  else renderGenerate($('#pack-main'));
}

function renderGenerate(box) {
  const types = Object.entries(state.meta.task_types).map(([k, v]) => `<option value="${esc(k)}">${esc(v)}</option>`).join('');
  box.innerHTML = html`<form class="card card-pad" id="generate-form" novalidate>
    <div class="section-label">第一步 · 列出学习目标</div>
    <div class="form-row"><label class="label" for="g-title">练习包名称</label><input class="field" id="g-title" maxlength="160" placeholder="例如：第三章 速度与时间"></div>
    <div class="form-row">
      <div class="row between"><span class="label">学习目标 <span class="opt">（最多 6 个，可以跨课程和学科）</span></span><button type="button" class="btn small" id="add-goal">${raw(icon('plus'))}添加目标</button></div>
      <div class="editor-head goal-row"><span>课程</span><span>知识点</span><span>学科</span><span>教学类型</span><span>学习目标</span><span></span></div>
      <div class="editor-rows" id="goals"></div>
    </div>
    <div class="form-row">
      <label class="label" for="g-material">参考材料 <span class="opt">（可选）</span></label>
      <textarea class="field" id="g-material" rows="4" maxlength="100000"></textarea>
      <div class="row" style="margin-top:6px"><label class="btn small">${raw(icon('file'))}从文件导入<input type="file" id="g-file" accept=".txt,.md,.pdf" hidden></label></div>
    </div>
    <div class="row" style="justify-content:flex-end"><button class="btn primary" id="generate-btn">生成待审核草稿</button></div>
    <p class="hint" style="text-align:right">模型逐题起草，通常每题需要十几秒。草稿发布前不会出现在学生端。</p>
  </form>`;
  const addGoal = () => {
    const goals = $('#goals');
    if (goals.children.length >= 6) return toast('一次最多 6 个目标');
    const row = document.createElement('div');
    row.className = 'editor-row goal-row';
    row.innerHTML = html`<input class="field" data-k="course_id" placeholder="课程" aria-label="课程"><input class="field" data-k="concept_id" placeholder="知识点" aria-label="知识点"><input class="field" data-k="domain" placeholder="学科" aria-label="学科"><select class="field" data-k="task_type" aria-label="教学类型">${raw(types)}</select><input class="field" data-k="objective" placeholder="可以观察到的学习目标" aria-label="学习目标"><button type="button" class="btn ghost small remove" aria-label="删除">${raw(icon('close'))}</button>`;
    row.querySelector('.remove').onclick = () => row.remove();
    goals.append(row);
  };
  addGoal();
  $('#add-goal').onclick = addGoal;
  $('#g-file').onchange = (e) => importFile(e.target, $('#g-material'));
  $('#generate-form').addEventListener('submit', (event) => {
    event.preventDefault();
    busy($('#generate-btn'), async () => {
      const goals = $$('#goals .editor-row')
        .map((row) => Object.fromEntries($$('[data-k]', row).map((x) => [x.dataset.k, x.value.trim()])))
        .filter((g) => g.objective || g.course_id);
      if (!goals.length) throw new Error('请至少填写一个学习目标');
      const title = $('#g-title').value.trim();
      if (!title) throw new Error('请填写练习包名称');
      const pack = await api('/api/teacher/packs/generate', {
        method: 'POST',
        body: { class_id: state.classId, title, material: $('#g-material').value, goals, request_id: uniqueKey() },
      });
      location.hash = 'packs/' + pack.pack_id;
    });
  });
}

const PACK_FIELDS = [
  ['objective', '学习目标', 'input'],
  ['question', '题目', 'textarea'],
  ['rubric', '评分要求', 'textarea'],
  ['reference', '参考答案', 'textarea'],
  ['check_requirements', '独立检查要求（每行一项）', 'textarea'],
];

function renderPack(box, pack) {
  const published = pack.status === 'published';
  const items = pack.body.items;
  box.innerHTML = html`
    <section class="card card-pad" style="margin-bottom:14px">
      <div class="row between wrap">
        <div><h3 style="font-size:17px">${pack.body.title}</h3><p class="small muted">${items.length} 题 · 版本 ${pack.version} · ${published ? '已发布' : '草稿，发布前学生看不到'}</p></div>
        ${raw(published ? pill('已发布', 'green') : pill('待审核', 'amber'))}
      </div>
    </section>
    <div id="pack-items">${raw(items.map((item, i) => packItem(item, i, published)).join(''))}</div>
    ${raw(
      published
        ? `<section class="card card-pad mt"><div class="notice ok">已发布并分发。${pack.result && pack.result.followups ? `共布置给 ${pack.result.followups.length} 名学生。` : ''}</div></section>`
        : html`<section class="card card-pad mt">
            <div class="section-label">第二步 · 确认发布</div>
            <div class="grid-even">
              <div class="form-row"><label class="label" for="pk-learners">分发给学生 <span class="opt">（每行一个名字；留空则只进入题库）</span></label><textarea class="field" id="pk-learners" rows="4"></textarea></div>
              <div>
                <div class="form-row"><label class="label" for="pk-note">审核说明</label><input class="field" id="pk-note" maxlength="400" placeholder="例如：已逐题核对参考答案"></div>
                <label class="check"><input type="checkbox" id="pk-reviewed"> 我已逐题核对题目、参考答案、评分要求和独立检查要求</label>
              </div>
            </div>
            <div class="row" style="justify-content:flex-end;gap:8px;margin-top:8px">
              <button class="btn" id="pk-save">保存修改</button>
              <button class="btn primary" id="pk-publish">确认发布</button>
            </div>
            <p class="hint" style="text-align:right">发布在一个事务里完成：任何一题不通过校验，都不会发布任何一题。</p>
          </section>`,
    )}`;
  if (published) return;
  let dirty = false;
  $('#pack-items').addEventListener('input', () => {
    dirty = true;
    $('#pk-reviewed').checked = false; // edits invalidate the review confirmation
  });
  const save = async () => {
    const body = { ...pack.body, items: pack.body.items.map((item, i) => readPackItem(item, i)) };
    const saved = await api('/api/teacher/packs/' + pack.pack_id, { method: 'PUT', body: { ...body, request_id: uniqueKey(), version: pack.version } });
    pack = saved;
    dirty = false;
    return saved;
  };
  $('#pk-save').onclick = (e) =>
    busy(e.currentTarget, async () => {
      await save();
      toast('已保存为版本 ' + pack.version);
      location.hash = 'packs/' + pack.pack_id;
      route();
    });
  $('#pk-publish').onclick = (e) =>
    busy(e.currentTarget, async () => {
      if (!$('#pk-reviewed').checked) throw new Error('请先逐题核对，并勾选确认');
      const note = $('#pk-note').value.trim();
      if (!note) throw new Error('请填写审核说明');
      if (dirty) await save();
      await api(`/api/teacher/packs/${pack.pack_id}/publish`, {
        method: 'POST',
        body: { learners: lines($('#pk-learners').value), reviewed: true, review_note: note, due_at: 0, version: pack.version, request_id: uniqueKey() },
      });
      state.assignments = null;
      toast('已发布');
      route();
    });
}

function packItem(item, i, readonly) {
  const fields = PACK_FIELDS.map(([key, label, kind]) => {
    const value = key === 'check_requirements' ? (item[key] || []).join('\n') : item[key] || '';
    const control = kind === 'input'
      ? html`<input class="field" data-k="${key}" value="${value}" ${raw(readonly ? 'readonly' : '')}>`
      : html`<textarea class="field" data-k="${key}" rows="${key === 'question' ? 4 : 2}" ${raw(readonly ? 'readonly' : '')}>${value}</textarea>`;
    return html`<div class="form-row"><label class="label">${label}</label>${raw(control)}</div>`;
  }).join('');
  const criteria = (item.criteria || []).length
    ? `<div class="small muted">分项评分：${(item.criteria || []).map((c) => esc(`${c.description}（${state.meta.criterion_kinds[c.kind] || c.kind}${c.expected ? '：' + c.expected : ''}）`)).join('；')}</div>`
    : '';
  return html`<article class="pack-item" data-item="${i}">
    <div class="pack-item-head"><span class="index">${i + 1}</span><strong>${item.course_id}</strong><span class="muted small">${item.domain} · ${item.concept_id}</span>
    ${raw(item.reference_origin === 'model_generated_unverified' ? pill('模型起草', 'amber') : pill('教师审核', 'green'))}</div>
    ${raw(fields)}${raw(criteria)}
  </article>`;
}

function readPackItem(item, i) {
  const el = $(`[data-item="${i}"]`);
  const next = { ...item };
  for (const [key] of PACK_FIELDS) {
    const value = $(`[data-k="${key}"]`, el).value;
    next[key] = key === 'check_requirements' ? lines(value) : value.trim();
  }
  return next;
}

// ---------- planning ----------

async function planningPage(container, [preselect]) {
  if (needClass(container, '学习安排')) return;
  const [bank, report] = await Promise.all([assignments(true), dashboard()]);
  const options = assignmentOptions(bank, preselect);
  container.innerHTML = html`<div class="grid-even">
    <section class="card card-pad">
      <div class="section-label">布置复习、补救或诊断</div>
      ${raw(bank.length ? '' : '<div class="notice warn" style="margin-bottom:12px">这个班级还没有已发布的练习，请先到“创建练习”发布。</div>')}
      <div class="form-row"><label class="label" for="fu-assignment">练习</label><select class="field" id="fu-assignment">${raw(options)}</select></div>
      <div class="form-row"><label class="label" for="fu-learners">学生 <span class="opt">（每行一个名字）</span></label><textarea class="field" id="fu-learners" rows="4"></textarea></div>
      <div class="form-row">
        <span class="label">类型</span>
        <div class="segmented" role="radiogroup" aria-label="类型">
          <label><input type="radio" name="fu-kind" value="remediation" checked><span>补救</span></label>
          <label><input type="radio" name="fu-kind" value="review"><span>复习</span></label>
          <label><input type="radio" name="fu-kind" value="diagnostic"><span>诊断</span></label>
        </div>
        <p class="hint">复习和诊断要求学生从没见过这道题；补救可以用提示，但之后仍需通过独立检查。</p>
      </div>
      <div class="form-grid">
        <div class="form-row"><label class="label" for="fu-hours">多少小时后到期</label><input class="field" id="fu-hours" type="number" min="0" max="8760" value="24"></div>
        <div class="form-row"><label class="label" for="fu-reason">安排依据</label><input class="field" id="fu-reason" maxlength="400" placeholder="例如：单位换算反复出错"></div>
      </div>
      <div class="row" style="justify-content:flex-end"><button class="btn primary" id="fu-assign" ${raw(bank.length ? '' : 'disabled')}>布置</button></div>
    </section>
    <section class="card card-pad">
      <div class="section-label">学习顺序</div>
      <p class="small muted" style="margin-bottom:12px">用知识点和先修关系排好顺序，学生输入路径编号后，系统按他们的证据推荐下一步。</p>
      <div class="form-grid">
        <div class="form-row"><label class="label" for="p-title">名称</label><input class="field" id="p-title" maxlength="160"></div>
        <div class="form-row"><label class="label" for="p-course">课程</label><input class="field" id="p-course" maxlength="80"></div>
      </div>
      <div class="editor-head node-row"><span>知识点编号</span><span>名称</span><span>练习</span><span>先修（逗号分隔）</span><span></span></div>
      <div class="editor-rows" id="nodes"></div>
      <div class="row wrap" style="margin-top:10px">
        <button class="btn small" id="add-node">${raw(icon('plus'))}添加知识点</button>
        <button class="btn small" id="fill-nodes" ${raw(state.meta.knowledge_available ? '' : 'disabled title="知识点库未导入"')}>按知识点库补全</button>
        <span class="spacer"></span>
        <button class="btn primary" id="p-publish" ${raw(bank.length ? '' : 'disabled')}>发布学习顺序</button>
      </div>
      <div id="path-note" style="margin-top:12px"></div>
    </section>
  </div>
  <section class="card mt">
    <div class="card-head"><div><h3>已布置的任务</h3><div class="sub">${state.classId}</div></div></div>
    <div id="followup-table"></div>
  </section>
  ${raw(reasonDialog())}`;

  const addNode = (v = {}) => {
    const row = document.createElement('div');
    row.className = 'editor-row node-row';
    row.innerHTML = html`<input class="field" data-k="concept_id" placeholder="编号" value="${v.concept_id || ''}" aria-label="知识点编号"><input class="field" data-k="title" placeholder="名称" value="${v.title || ''}" aria-label="名称"><select class="field" data-k="assignment_id" aria-label="练习">${raw(assignmentOptions(bank, v.assignment_id))}</select><input class="field" data-k="prerequisite_ids" placeholder="先修编号" value="${v.prerequisite_ids || ''}" aria-label="先修"><button type="button" class="btn ghost small remove" aria-label="删除">${raw(icon('close'))}</button>`;
    row.querySelector('.remove').onclick = () => row.remove();
    $('#nodes').append(row);
  };
  addNode();
  addNode();
  $('#add-node').onclick = () => addNode();
  $('#fill-nodes').onclick = (e) => busy(e.currentTarget, fillNodes);
  $('#p-publish').onclick = (e) => busy(e.currentTarget, publishPath);
  $('#fu-assign').onclick = (e) => busy(e.currentTarget, assignFollowups);
  renderFollowups(report.followups);
}

function readNodes() {
  return $$('#nodes .editor-row')
    .map((row) => Object.fromEntries($$('[data-k]', row).map((x) => [x.dataset.k, x.value.trim()])))
    .filter((n) => n.concept_id);
}

async function fillNodes() {
  const nodes = readNodes();
  const ids = new Set(nodes.map((n) => n.concept_id));
  const outside = [];
  for (const row of $$('#nodes .editor-row')) {
    const id = $('[data-k="concept_id"]', row).value.trim();
    if (!id) continue;
    let node;
    try {
      node = await api('/api/teacher/knowledge/nodes/' + encodeURIComponent(id));
    } catch {
      continue;
    }
    const title = $('[data-k="title"]', row);
    if (!title.value) title.value = node.name;
    const prereq = $('[data-k="prerequisite_ids"]', row);
    const listed = prereq.value.split(/[,，]/).map((x) => x.trim()).filter(Boolean);
    prereq.value = [...new Set([...listed, ...node.prerequisites.filter((p) => ids.has(p.id)).map((p) => p.id)])].join(',');
    outside.push(...node.prerequisites.filter((p) => !ids.has(p.id)).map((p) => `${node.name} ← ${p.name}（${p.id}）`));
  }
  $('#path-note').innerHTML = outside.length
    ? html`<div class="notice info">知识点库里还有这些先修不在路径中，可以考虑补充：<br>${raw(outside.map(esc).join('<br>'))}</div>`
    : '<div class="notice ok">已按知识点库补全。</div>';
}

async function publishPath() {
  const nodes = readNodes().map((n) => ({ ...n, title: n.title || n.concept_id, prerequisite_ids: n.prerequisite_ids.split(/[,，]/).map((x) => x.trim()).filter(Boolean) }));
  if (!nodes.length) throw new Error('请至少填写一个知识点');
  const result = await api('/api/teacher/paths', {
    method: 'POST',
    body: { class_id: state.classId, course_id: $('#p-course').value.trim(), title: $('#p-title').value.trim(), nodes },
  });
  $('#path-note').innerHTML = html`<div class="notice ok">已发布。把路径编号发给学生：<code>${result.path_id}</code></div>`;
}

async function assignFollowups() {
  const learners = lines($('#fu-learners').value);
  if (!learners.length) throw new Error('请填写学生名字');
  const reason = $('#fu-reason').value.trim();
  if (!reason) throw new Error('请填写安排依据');
  const hours = Number($('#fu-hours').value || 0);
  const created = await api('/api/teacher/followups', {
    method: 'POST',
    body: {
      class_id: state.classId,
      assignment_id: $('#fu-assignment').value,
      learners,
      kind: $('input[name="fu-kind"]:checked').value,
      due_at: Date.now() / 1000 + hours * 3600,
      reason,
      request_id: uniqueKey(),
    },
  });
  toast(`已布置给 ${created.length} 名学生`);
  $('#fu-learners').value = '';
  renderFollowups((await dashboard()).followups);
}

function renderFollowups(rows) {
  const box = $('#followup-table');
  if (!rows.length) {
    box.innerHTML = '<div class="empty">还没有布置任何任务。</div>';
    return;
  }
  box.innerHTML = `<div class="table-wrap"><table class="data responsive"><thead><tr><th>学生</th><th>类型</th><th>学习目标</th><th>状态</th><th>到期</th><th></th></tr></thead><tbody>${rows
    .map((r) => {
      const open = ['queued', 'active'].includes(r.status);
      return html`<tr>
        <td><strong>${r.learner}</strong></td><td>${KIND[r.body.kind] || r.body.kind}</td>
        <td><div class="clip">${r.body.objective || ''}</div><div class="small muted">${r.body.reason || ''}</div></td>
        <td>${raw(pillFor(FOLLOWUP_STATUS, r.status))}${r.due && open ? raw(' ' + pill('已到期', 'amber')) : ''}</td>
        <td class="small" data-label="到期">${when(r.due_at)}</td>
        <td style="white-space:nowrap">${raw(open ? `<button class="btn ghost small" data-postpone="${esc(r.id)}">延期</button><button class="btn ghost small danger" data-cancel="${esc(r.id)}">撤销</button>` : '')}</td>
      </tr>`;
    })
    .join('')}</tbody></table></div>`;
  $$('[data-postpone]', box).forEach((b) => (b.onclick = () => reviseFollowup(b.dataset.postpone, 'postpone')));
  $$('[data-cancel]', box).forEach((b) => (b.onclick = () => reviseFollowup(b.dataset.cancel, 'cancel')));
}

function reasonDialog() {
  return `<dialog class="modal" id="dlg-reason"><form method="dialog" id="reason-form">
    <div class="modal-head"><h3 id="reason-title"></h3></div>
    <div class="modal-body">
      <div class="form-row" id="reason-hours-row"><label class="label" for="reason-hours">延期到多少小时后</label><input class="field" id="reason-hours" type="number" min="0" max="8760" value="24"></div>
      <div class="form-row"><label class="label" for="reason-text">原因</label><input class="field" id="reason-text" maxlength="400" required></div>
    </div>
    <div class="modal-foot"><button class="btn ghost" value="cancel" formnovalidate>取消</button><button class="btn primary" value="ok">确定</button></div>
  </form></dialog>`;
}

function reviseFollowup(key, action) {
  const dialog = $('#dlg-reason');
  $('#reason-title').textContent = action === 'cancel' ? '撤销这项任务' : '延期这项任务';
  $('#reason-hours-row').hidden = action === 'cancel';
  $('#reason-text').value = '';
  openDialog(dialog);
  $('#reason-form').onsubmit = async (event) => {
    if (event.submitter && event.submitter.value !== 'ok') return;
    event.preventDefault();
    const reason = $('#reason-text').value.trim();
    if (!reason) return $('#reason-text').focus();
    try {
      const hours = Number($('#reason-hours').value || 0);
      await api('/api/teacher/followups/' + encodeURIComponent(key), {
        method: 'PATCH',
        body: { action, reason, due_at: action === 'postpone' ? Date.now() / 1000 + hours * 3600 : null, request_id: uniqueKey() },
      });
      dialog.close();
      toast(action === 'cancel' ? '已撤销' : '已延期');
      renderFollowups((await dashboard()).followups);
    } catch (error) {
      toast(error.message, 'error');
    }
  };
}

// ---------- review ----------

async function reviewPage(container, [sid, eventId]) {
  const report = await dashboard();
  const queue = report.review_queue;
  container.innerHTML = html`<div class="split">
    <aside class="card">
      <div class="card-head"><div><h3>待复核</h3><div class="sub">${queue.length} 条 · 按风险排序</div></div><button class="btn ghost small" id="refresh-queue" title="刷新">${raw(icon('refresh'))}</button></div>
      <div class="queue" id="queue">${raw(
        queue.length
          ? queue
              .map((r) => html`<a class="list-item ${r.session_id === sid && String(r.event_id) === eventId ? 'active' : ''}" href="#review/${r.session_id}/${r.event_id}" style="text-decoration:none">
                <span class="row between"><strong>${r.learner}</strong>${raw(pillFor(PRIORITY, r.priority))}</span>
                <span class="title small" style="font-weight:400;margin-top:4px">${r.text || r.question || '教学反馈记录'}</span>
                <span class="meta">${raw(r.status ? pillFor(STATUS, r.status) : pill('反馈待审', 'blue'))}<span>${(r.risks || []).map((x) => RISKS[x] || x).join('、')}</span></span></a>`)
              .join('')
          : '<div class="empty"><strong>都处理完了</strong>目前没有需要复核的作答。</div>',
      )}</div>
    </aside>
    <section class="card" id="review-detail">${raw(
      sid ? '<div class="empty"><span class="spinner"></span></div>' : `<div class="empty" style="padding:80px 20px">${icon('review')}<strong>选择一份作答</strong>从左边选择，核对题目、学生作答和原判定的依据。</div>`,
    )}</section>
  </div>`;
  $('#refresh-queue').onclick = () => route();
  if (sid) await renderReview($('#review-detail'), sid, Number(eventId));
}

async function renderReview(box, sid, eventId) {
  const base = `/api/teacher/tasks/${encodeURIComponent(sid)}/attempts/${eventId}`;
  const d = await api(base);
  const task = d.task || {};
  const scope = [d.scope.class_id, d.scope.course_id].filter(Boolean).join(' · ');
  const decision = d.decisions.length ? d.decisions[d.decisions.length - 1] : null;
  const block = (label, body, extra = '') => html`<div class="detail-block ${extra}"><div class="section-label">${label}</div><div class="body">${body || '（无）'}</div></div>`;
  box.innerHTML = html`
    <div class="card-head"><div><h3>${d.learner}</h3><div class="sub">${scope}</div></div>${raw(d.status ? `<span class="row small muted">原判定 ${pillFor(STATUS, d.status)}</span>` : pill('教学反馈', 'blue'))}</div>
    ${raw(block('题目', task.question))}
    <div class="detail-block"><div class="section-label">学生作答</div><div class="answer">${d.text || '这条记录没有文字作答，请结合原始照片核对。'}</div><div id="original-photo"></div></div>
    ${raw(block('判定依据', d.observation || '这条记录没有模型判定依据，请对照评分要求核对。'))}
    ${raw(
      d.criteria_results.length
        ? `<div class="detail-block"><div class="section-label">分项检查</div><div class="criteria" style="margin:0">${d.criteria_results.map((c) => pill(`${c.description}：${(STATUS[c.status] || [c.status])[0]}`, (STATUS[c.status] || [])[1])).join('')}</div></div>`
        : '',
    )}
    <div class="detail-block"><details class="more"><summary>评分要求和参考答案</summary>
      <div class="section-label">评分要求</div><div class="body" style="margin-bottom:10px">${task.rubric || '（无）'}</div>
      <div class="section-label">参考答案</div><div class="body">${task.reference || '（无）'}</div>
    </details></div>
    ${raw(d.feedback ? block('当时给学生的反馈', d.feedback) : '')}
    <div class="detail-block"><details class="more" id="trace-details"><summary>教学过程与策略回放</summary><div id="trace-body"><span class="spinner"></span></div></details></div>
    <div class="decision" id="decision"></div>`;

  if (d.image_event_id) {
    api(`/api/teacher/tasks/${encodeURIComponent(sid)}/images/${d.image_event_id}`)
      .then((img) => {
        $('#original-photo').innerHTML = `<div class="section-label" style="margin-top:12px">原始照片</div><img class="photo-preview" alt="学生上传的作答照片" src="${esc(img.image_url)}">`;
      })
      .catch(() => {
        $('#original-photo').innerHTML = '<p class="small muted" style="margin-top:8px">原始照片暂时无法读取。</p>';
      });
  }
  $('#trace-details').addEventListener('toggle', function onToggle() {
    this.removeEventListener('toggle', onToggle);
    renderTrace($('#trace-body'), sid, eventId);
  });

  const panel = $('#decision');
  if (decision) {
    const text = decision.kind === 'review_confirmed' ? '已确认原判定' : `已改判为“${(STATUS[decision.status] || [decision.status])[0]}”`;
    panel.innerHTML = html`<div class="notice ok"><strong>${text}</strong><br>${decision.reason || ''}</div>`;
    return;
  }
  if (!d.can_correct && !d.can_confirm) {
    panel.innerHTML = `<p class="small muted">${d.needs_review ? '相关学习证据已失效，学生需要重新检查。' : '这条记录没有可改判的作答结果，可以在上面查看教学过程。'}</p>`;
    return;
  }
  panel.innerHTML = html`
    <label class="label" for="review-reason">复核说明</label>
    <textarea class="field" id="review-reason" rows="2" maxlength="400" placeholder="写下核对依据，例如：已对照题目检查公式、数值和单位"></textarea>
    <div class="row wrap" style="margin-top:10px">
      <button class="btn primary" id="confirm-review" ${raw(d.can_confirm ? '' : 'disabled title="不确定或已失效的判定需要改判"')}>${raw(icon('check'))}确认原判定</button>
      <span class="spacer"></span>
      ${raw(
        d.can_correct
          ? `<select class="field" id="new-status" style="width:auto" aria-label="改判为"><option value="">改判为…</option>${Object.entries(STATUS)
              .filter(([k]) => k !== d.status)
              .map(([k, [label]]) => `<option value="${k}">${label}</option>`)
              .join('')}</select><button class="btn" id="save-correction" disabled>保存改判</button>`
          : '',
      )}
    </div>
    <p class="hint">改判会保留原始作答，并把依赖这次判定的学习记录标为需要重新检查。</p>`;
  const reason = () => {
    const value = $('#review-reason').value.trim();
    if (!value) {
      $('#review-reason').focus();
      throw new Error('请填写复核说明');
    }
    return value;
  };
  $('#confirm-review').onclick = (e) =>
    busy(e.currentTarget, async () => {
      await api(base + '/reviews', { method: 'POST', body: { reason: reason(), request_id: uniqueKey() } });
      toast('已确认原判定');
      route();
    });
  const select = $('#new-status');
  if (select) {
    select.onchange = () => ($('#save-correction').disabled = !select.value);
    $('#save-correction').onclick = (e) =>
      busy(e.currentTarget, async () => {
        const result = await api(base + '/corrections', { method: 'POST', body: { status: select.value, reason: reason(), request_id: uniqueKey() } });
        toast(`已改判，${result.invalidated_sessions.length} 个会话需要重新检查`);
        route();
      });
  }
}

async function renderTrace(box, sid, eventId) {
  try {
    const [traces, replay] = await Promise.all([
      api(`/api/teacher/sessions/${encodeURIComponent(sid)}/trace`),
      api(`/api/teacher/sessions/${encodeURIComponent(sid)}/replay`),
    ]);
    const matches = Object.fromEntries(replay.turns.map((t) => [t.event_id, t.matches]));
    const OPS = { attempt: '提交作答', reply: '回答追问', hint: '请求提示', explain: '请求讲解', check: '开始独立检查', confirm: '确认照片作答', image: '上传照片', confirm_task: '确认题目', resume_practice: '回到原任务' };
    box.innerHTML = html`
      <div class="notice ${replay.passed ? 'ok' : 'error'}" style="margin-bottom:12px">${replay.passed ? '策略回放一致：每一步的教学决定都能由记录的输入重新算出（不调用模型）。' : '策略回放不一致：有决定无法由记录的输入重新算出。'}</div>
      <ol class="timeline">${raw(
        traces
          .map((t) => {
            const decision = t.decision || {};
            const mismatch = matches[t.event_id] === false;
            const focus = t.evidence_event_id === eventId;
            return html`<li class="${mismatch ? 'mismatch' : ''}" ${raw(focus ? 'style="background:var(--accent-soft);border-radius:0 8px 8px 0"' : '')}>
              <div class="row wrap"><strong>${OPS[t.operation] || t.operation}</strong>${decision.action ? raw(pillFor(ACTION, decision.action)) : ''}${focus ? raw(pill('当前作答', 'accent')) : ''}${mismatch ? raw(pill('回放不一致', 'red')) : ''}<span class="muted small">${t.seconds >= 0.1 ? t.seconds.toFixed(1) + ' 秒' : ''}</span></div>
              ${decision.reason ? raw(`<div class="small muted">${esc(decision.reason)}</div>`) : ''}
              ${t.output_validation && t.output_validation.fallback ? raw('<div class="small" style="color:var(--amber)">候选回复未通过审核，使用了兜底回复</div>') : ''}
            </li>`;
          })
          .join(''),
      )}</ol>
      <details class="more" style="margin-top:12px"><summary>原始记录（JSON）</summary><pre class="json">${JSON.stringify(traces, null, 2)}</pre></details>`;
  } catch (error) {
    box.innerHTML = `<p class="small muted">${esc(error.message)}</p>`;
  }
}

boot();
