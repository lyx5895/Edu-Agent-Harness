// Student workspace: pick a task, talk it through, then prove it on a new question.
import {
  $, $$, ACTION, ApiError, STATUS, api, brand, busy, esc, extractMaterial, html, icon, identify, openDialog, pill,
  pillFor, raw, readImage, requestKey, signOut, store, toast, uniqueKey, when,
} from '/static/common.js';

const root = $('#app');
const HINT_LABELS = ['提示 1 · 理解概念', '提示 2 · 找到下一步', '提示 3 · 看一个例子'];
const INBOX_STATUS = { queued: ['待开始', 'blue'], active: ['进行中', 'accent'], completed: ['已完成', 'green'], needs_recheck: ['需重做', 'red'], cancelled: ['已撤销', ''] };
const KIND = { review: '复习', remediation: '补救', diagnostic: '诊断' };

const state = {
  me: null,
  learner: '',
  classId: '',
  inbox: [],
  session: null,
  waiting: null, // {text} while an operation is in flight
};

// ---------- derived state ----------

/** What the learner can do right now; mirrors the server's rules so buttons match what will succeed. */
export function controls(s) {
  if (!s || s.engine !== 'open-task-v1') return { active: false };
  const task = s.task || {};
  const history = s.history || [];
  const last = [...history].reverse().find((e) => e.feedback) || {};
  const check = task.phase === 'check';
  const blocked = Boolean(s.needs_review || s.pending_image || last.action === 'teacher_review');
  const ready = Boolean(s.task) && !blocked && !s.completed;
  const hints = history.filter((e) => String(e.action || '').startsWith('scaffold')).length;
  return {
    active: true,
    ready,
    check,
    blocked,
    last,
    pending: ready && !check && Boolean(s.pending_question),
    canHelp: ready && !check,
    canCheck: Boolean(s.completed) && !check && !blocked,
    canBack: check && !s.needs_review,
    hintLabel: hints < 3 ? HINT_LABELS[hints] : '再看一个例子',
    attempts: history.filter((e) => e.text).length,
    hints,
  };
}

function stepIndex(s, c) {
  if (!s) return 0;
  if (c.check || s.completed) return 3;
  if (s.pending_question) return 2;
  return c.attempts ? 2 : 1;
}

// ---------- boot ----------

async function boot() {
  try {
    state.me = await identify(root, 'student');
  } catch (error) {
    root.innerHTML = `<div class="empty" style="padding-top:20vh"><strong>无法连接服务器</strong>${esc(error.message)}</div>`;
    return;
  }
  if (state.me.mode === 'classroom') {
    state.learner = state.me.learner || '';
    state.classId = state.me.class_id;
  } else {
    state.learner = store.get('learner', '');
    state.classId = store.get('class', '');
  }
  renderShell();
  if (!state.learner) {
    openProfile();
    return;
  }
  await Promise.all([loadInbox(), restoreLast()]);
}

function renderShell() {
  const classroom = state.me.mode === 'classroom';
  root.innerHTML = html`
    <header class="topbar">
      ${raw(brand('学习'))}
      <span class="spacer"></span>
      <div class="who">
        <span class="avatar" id="who-avatar"></span>
        <span id="who-name"></span>
        ${classroom
          ? raw(`<button class="btn ghost small" id="sign-out" title="退出">${icon('logout')}</button>`)
          : raw('<button class="btn ghost small" id="edit-profile">切换</button>')}
      </div>
    </header>
    <div class="student-layout">
      <aside class="side tasks-col">
        <div class="row between" style="margin-bottom:8px">
          <span class="section-label" style="margin:0">老师布置的任务</span>
          <button class="btn ghost small" id="refresh-inbox" title="刷新">${raw(icon('refresh'))}</button>
        </div>
        <div class="task-list" id="inbox"></div>
        <button class="btn block" id="new-question" style="margin-top:12px">${raw(icon('plus'))}自己提一个问题</button>
        <div class="row wrap small" style="margin-top:14px;gap:4px">
          <button class="btn ghost small" id="open-path">学习路径</button>
          <button class="btn ghost small" id="open-resume">恢复会话</button>
        </div>
      </aside>
      <main class="lesson" id="lesson" aria-live="polite"></main>
      <aside class="side progress-col" id="progress"></aside>
    </div>
    ${raw(dialogs())}`;
  renderIdentity();
  $('#refresh-inbox').onclick = (e) => busy(e.currentTarget, loadInbox);
  $('#new-question').onclick = () => openDialog($('#dlg-question'));
  $('#open-path').onclick = openPath;
  $('#open-resume').onclick = () => openDialog($('#dlg-resume'));
  if (classroom) $('#sign-out').onclick = signOut;
  else $('#edit-profile').onclick = openProfile;
  bindDialogs();
  renderLesson();
}

function renderIdentity() {
  $('#who-avatar').textContent = (state.learner || '?').slice(0, 1);
  $('#who-name').textContent = [state.learner || '未填写名字', state.classId].filter(Boolean).join(' · ');
}

// ---------- data ----------

async function loadInbox() {
  const box = $('#inbox');
  if (!state.classId || !state.learner) {
    box.innerHTML = '<p class="muted small">填写班级后，这里会列出老师布置给你的任务。</p>';
    return;
  }
  try {
    state.inbox = await api(`/api/learning/inbox?${new URLSearchParams({ class_id: state.classId, learner: state.learner })}`);
  } catch (error) {
    box.innerHTML = `<p class="muted small">${esc(error.message)}</p>`;
    return;
  }
  renderInbox();
}

function renderInbox() {
  const box = $('#inbox');
  const visible = state.inbox.filter((x) => x.status !== 'cancelled');
  if (!visible.length) {
    box.innerHTML = '<p class="muted small">暂时没有老师布置的任务。</p>';
    return;
  }
  const order = { active: 0, queued: 1, needs_recheck: 2, completed: 3 };
  visible.sort((a, b) => (order[a.status] ?? 9) - (order[b.status] ?? 9) || (a.due_at || 0) - (b.due_at || 0));
  box.innerHTML = visible
    .map((item) => {
      const active = state.session && state.session.followup_id === item.followup_id;
      const [label, color] = INBOX_STATUS[item.status] || [item.status, ''];
      return html`<button class="list-item ${active ? 'active' : ''}" data-followup="${item.followup_id}" ${item.status === 'needs_recheck' ? 'disabled' : ''}>
        <span class="title">${item.objective}</span>
        <span class="meta">${raw(pill(label, color))}<span>${item.course_id}</span>${item.due_at ? raw(`<span>· ${esc(when(item.due_at))}</span>`) : ''}</span>
      </button>`;
    })
    .join('');
  $$('[data-followup]', box).forEach((button) => {
    button.onclick = () => busy(button, () => startSession({ followup_id: button.dataset.followup, request_id: 'followup:' + button.dataset.followup }));
  });
}

async function startSession(body) {
  const session = await api('/api/sessions', { method: 'POST', body: { learner: state.learner, ...body } });
  setSession(session);
  if (body.followup_id) loadInbox(); // the task is now "in progress"
  return session;
}

async function restoreLast() {
  const sid = store.get('session:' + state.learner);
  if (!sid) return;
  try {
    setSession(await api('/api/sessions/' + encodeURIComponent(sid)));
  } catch {
    store.set('session:' + state.learner, null);
  }
}

function setSession(session) {
  state.session = session;
  if (session) store.set('session:' + state.learner, session.session_id);
  renderLesson();
  if (state.inbox.length) renderInbox();
}

async function operate(action, { text = '', image_url = '' } = {}) {
  const s = state.session;
  const body = { version: s.version, text, image_url };
  body.request_id = requestKey(s.session_id, s.version, action, text, image_url.length);
  state.waiting = { text: ['attempt', 'reply'].includes(action) ? text : '', action };
  renderLesson();
  try {
    const result = await api(`/api/sessions/${s.session_id}/${action}`, { method: 'POST', body });
    state.waiting = null;
    setSession(result.session);
    if (result.session.completed || action === 'check') loadInbox();
    return true;
  } catch (error) {
    state.waiting = null;
    if (error instanceof ApiError && error.status === 409) {
      try {
        setSession(await api('/api/sessions/' + s.session_id));
      } catch {
        /* keep the current view */
      }
    } else renderLesson();
    toast(error.message, 'error');
    return false;
  }
}

// ---------- lesson ----------

function renderLesson() {
  const s = state.session;
  const lesson = $('#lesson');
  renderProgress();
  if (!s) {
    lesson.innerHTML = welcome();
    $('#welcome-ask')?.addEventListener('click', () => openDialog($('#dlg-question')));
    return;
  }
  if (s.engine !== 'open-task-v1') {
    lesson.innerHTML = '<div class="card empty"><strong>这是旧版示例课程的会话</strong>示例课程请通过接口使用。请选择一个任务或自己提问。</div>';
    return;
  }
  const c = controls(s);
  lesson.innerHTML = [taskCard(s), statusBanner(s, c), thread(s), pendingImageCard(s), composer(s, c)].join('');
  bindLesson(s, c);
  const end = $('#thread-end');
  if (end) end.scrollIntoView({ block: 'nearest' });
}

function welcome() {
  const greeting = state.learner ? `${state.learner}，今天想学点什么？` : '今天想学点什么？';
  return html`<section class="card welcome">
    <h2>${greeting}</h2>
    <p class="lead">从左边选一个老师布置的任务，或者带着自己的问题来。任何学科都可以。</p>
    <button class="btn primary" id="welcome-ask">${raw(icon('plus'))}自己提一个问题</button>
    <div class="welcome-steps">
      <div><b>先自己试</b>写下你的解答和理由，不确定的地方也可以写。</div>
      <div><b>卡住就要提示</b>提示一级一级给，不会直接告诉你答案。</div>
      <div><b>再做一道新题</b>练完后用没见过的题检查自己是不是真的会了。</div>
    </div>
  </section>`;
}

function taskCard(s) {
  const t = s.task;
  if (!t) {
    return '<section class="card task-card"><div class="tags"><span class="pill blue">题目照片</span></div><div class="question">先确认照片里的题目文字</div><p class="objective">系统识别了你上传的题目，核对无误后就可以开始。</p></section>';
  }
  const pages = (t.source_pages || []).length ? ` · 材料第 ${t.source_pages.join('、')} 页` : '';
  return html`<section class="card task-card">
    <div class="tags">
      ${raw(t.phase === 'check' ? pill('独立检查', 'violet') : pill('练习', 'accent'))}
      ${t.domain ? raw(pill(t.domain)) : ''}
      ${s.scope && s.scope.course_id ? raw(pill(s.scope.course_id)) : ''}
    </div>
    <div class="question">${t.question}</div>
    ${t.objective ? raw(`<p class="objective">学习目标：${esc(t.objective)}${esc(pages)}</p>`) : ''}
  </section>`;
}

function statusBanner(s, c) {
  const t = s.task || {};
  if (s.needs_review) {
    return banner('warn', '老师修订了这次判定', '相关学习记录需要重新检查。请从左边重新开始这个任务。');
  }
  if (c.last.action === 'teacher_review') {
    return banner('warn', '这道题已经转给老师', '系统暂时没法给出合适的帮助，老师会来看你的作答和问题。可以先做别的任务。');
  }
  if (s.completed && c.check) {
    return banner('ok', '独立检查通过', '你在没有提示的情况下做对了一道新题。可以选下一个任务继续。');
  }
  if (s.completed) {
    const assisted = c.last.evidence === 'assisted_complete';
    return banner(
      'ok',
      assisted ? '借助帮助完成了练习' : '练习完成',
      assisted ? '接下来试一道新题，看看不借助提示能不能独立做出来。' : '试一道新题，确认自己能独立运用。',
      c.canCheck ? `<button class="btn primary" data-act="check">开始独立检查 ${icon('arrow')}</button>` : '',
    );
  }
  if (t.phase === 'check') {
    return banner('check', '独立检查中', '这一题不提供提示和讲解，请独立完成。', c.canBack ? '<button class="btn" data-act="resume_practice">回到原任务</button>' : '');
  }
  return '';
}

function banner(kind, title, text, actions = '') {
  return `<section class="card banner ${kind}"><div class="text"><strong>${esc(title)}</strong><span class="small">${esc(text)}</span></div>${actions}</section>`;
}

function splitQuestion(text) {
  const match = /([^。！？!?\n]*[?？])\s*$/.exec(text);
  if (!match || match.index === 0) return [text, ''];
  return [text.slice(0, match.index).trim(), match[1].trim()];
}

function thread(s) {
  const items = [];
  const history = s.history || [];
  if (!history.length && s.task) {
    items.push(tutorBubble(s.task.phase === 'check' ? '这是一次独立检查，请不借助提示完成。' : '先试着写出你的解答和理由。卡住了可以随时要提示。'));
  }
  for (const e of history) {
    if (e.type === 'correction') {
      items.push('<div class="msg system"><div class="bubble">老师修订了这次判定</div></div>');
      continue;
    }
    if (e.text) items.push(html`<div class="msg me"><div class="bubble">${e.text}</div></div>`);
    if (e.feedback) items.push(feedbackBubble(e));
  }
  if (state.waiting) {
    if (state.waiting.text) items.push(html`<div class="msg me"><div class="bubble">${state.waiting.text}</div></div>`);
    items.push(`<div class="msg tutor"><span class="tutor-avatar">${icon('book')}</span><div class="bubble muted"><span class="spinner"></span> 正在看你的作答…</div></div>`);
  }
  return `<section class="thread" aria-label="对话">${items.join('')}<span id="thread-end"></span></section>`;
}

function tutorBubble(body, tags = '') {
  return `<div class="msg tutor"><span class="tutor-avatar">${icon('book')}</span><div class="bubble">${tags ? `<div class="who-tag">${tags}</div>` : ''}${body}</div></div>`;
}

function feedbackBubble(e) {
  // Label what the learner actually received: a rejected draft is replaced by a generic or template reply.
  const tags = [pillFor(ACTION, e.delivered_action || e.action), e.status ? pillFor(STATUS, e.status) : ''].join('');
  const [support, question] = splitQuestion(e.feedback);
  let body = esc(support);
  if (question) body += `<div class="ask">${esc(question)}</div>`;
  if (e.criteria_results && e.criteria_results.length) {
    body += `<div class="criteria">${e.criteria_results.map((x) => pill(`${x.description}：${(STATUS[x.status] || [x.status])[0]}`, (STATUS[x.status] || [])[1])).join('')}</div>`;
  }
  return tutorBubble(body, tags);
}

function pendingImageCard(s) {
  const pending = s.pending_image;
  if (!pending) return '';
  const forTask = pending.purpose === 'task';
  return html`<section class="card card-pad">
    <div class="section-label">${forTask ? '照片里的题目' : '照片里的作答'}</div>
    <p class="small muted" style="margin-bottom:10px">这是系统识别出的文字。有识别错的地方请直接改，确认后才会${forTask ? '开始辅导' : '判定'}。</p>
    <textarea class="field" id="transcript" rows="5">${pending.text || ''}</textarea>
    <div class="row" style="margin-top:10px;justify-content:flex-end">
      <button class="btn primary" id="confirm-image">${forTask ? '确认题目' : '确认并提交'}</button>
    </div>
  </section>`;
}

function composer(s, c) {
  if (!c.ready) return '';
  const placeholder = c.pending ? '回答上面的问题。写好了整道题的解答，可以勾选“完整解答”再发送。' : c.check ? '独立写出你的完整解答…' : '写下你的解答、步骤，或者不确定的地方…';
  const disabled = state.waiting ? 'disabled' : '';
  return html`<section class="card composer">
    <textarea class="field" id="work" aria-label="你的作答" placeholder="${placeholder}" ${raw(disabled)}>${store.get('draft:' + s.session_id, '')}</textarea>
    <div class="composer-bar">
      ${c.canHelp ? raw(`<button class="btn ghost small" data-act="hint" ${disabled}>${icon('bulb')}${esc(c.hintLabel)}</button>`) : ''}
      ${c.canHelp ? raw(`<button class="btn ghost small" data-act="explain" ${disabled}>${icon('explain')}请完整讲解</button>`) : ''}
      <label class="btn ghost small" title="拍照上传手写过程">${raw(icon('camera'))}拍照作答<input type="file" id="photo" accept="image/png,image/jpeg,image/webp" hidden ${raw(disabled)}></label>
      <span class="spacer"></span>
      ${c.pending ? raw('<label class="check complete-toggle"><input type="checkbox" id="complete"> 完整解答</label>') : ''}
      <button class="btn primary" id="send" ${raw(disabled)}>发送 ${raw(icon('send'))}</button>
    </div>
  </section>`;
}

function bindLesson(s, c) {
  $$('[data-act]', $('#lesson')).forEach((button) => {
    button.onclick = () => {
      const action = button.dataset.act;
      if (action === 'explain' && !confirm('看过完整讲解后，这道题之后做对会记为“借助帮助完成”。确定要看讲解吗？')) return;
      operate(action);
    };
  });
  const confirmImage = $('#confirm-image');
  if (confirmImage) {
    confirmImage.onclick = () => {
      const text = $('#transcript').value.trim();
      if (!text) return toast('请先核对识别出的文字', 'error');
      operate(s.pending_image.purpose === 'task' ? 'confirm_task' : 'confirm', { text });
    };
  }
  const work = $('#work');
  if (!work) return;
  const send = () => {
    const text = work.value.trim();
    if (!text) {
      work.focus();
      return toast('先写下你的想法，不确定也没关系');
    }
    const complete = $('#complete') && $('#complete').checked;
    store.set('draft:' + s.session_id, null);
    operate(c.pending && !complete ? 'reply' : 'attempt', { text });
  };
  $('#send').onclick = send;
  work.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) send();
  });
  work.addEventListener('input', () => store.set('draft:' + s.session_id, work.value));
  $('#photo').onchange = async (event) => {
    const file = event.target.files[0];
    if (!file) return;
    try {
      const image_url = await readImage(file);
      await operate('image', { image_url });
    } catch (error) {
      toast(error.message, 'error');
    }
  };
  if (!state.waiting) work.focus({ preventScroll: true });
}

function renderProgress() {
  const box = $('#progress');
  const s = state.session;
  const c = controls(s);
  const current = stepIndex(s, c);
  const labels = ['选择任务', '尝试解答', '修正思路', '独立检查'];
  const steps = labels
    .map((label, i) => {
      const done = i < current || (i === 3 && s && s.completed && c.check);
      return `<li class="${done ? 'done' : i === current ? 'current' : ''}"${i === current ? ' aria-current="step"' : ''}><span class="dot">${done ? '✓' : i + 1}</span>${label}</li>`;
    })
    .join('');
  const stats = s && c.active
    ? html`<dl class="stat-list">
        <dt>作答</dt><dd>${c.attempts} 次</dd>
        <dt>用过提示</dt><dd>${c.hints} 次</dd>
        ${s.scope && s.scope.class_id ? raw(`<dt>班级</dt><dd>${esc(s.scope.class_id)}</dd>`) : ''}
      </dl>
      <details class="more" style="margin-top:14px"><summary>会话编号</summary><code style="word-break:break-all">${s.session_id}</code></details>`
    : '<p class="small muted" style="margin-top:12px">开始一个任务后，这里会显示你的进度。</p>';
  box.innerHTML = `<section class="card card-pad"><div class="section-label">这次的进度</div><ol class="steps">${steps}</ol>${stats}</section>`;
}

// ---------- dialogs ----------

function dialogs() {
  return html`
  <dialog class="modal" id="dlg-profile">
    <form method="dialog" id="profile-form">
      <div class="modal-head"><h3>你是谁？</h3></div>
      <div class="modal-body">
        <p class="small muted" style="margin-bottom:14px">名字和班级用来找到老师布置给你的任务，并保存你的学习记录。</p>
        <div class="form-row"><label class="label" for="p-name">名字</label><input class="field" id="p-name" required maxlength="80" autocomplete="name"></div>
        <div class="form-row"><label class="label" for="p-class">班级 <span class="opt">（可以先不填）</span></label><input class="field" id="p-class" maxlength="80" placeholder="例如：七年一班"></div>
      </div>
      <div class="modal-foot"><button class="btn primary" value="ok">保存</button></div>
    </form>
  </dialog>

  <dialog class="modal" id="dlg-question">
    <form method="dialog" id="question-form">
      <div class="modal-head"><h3>自己提一个问题</h3><button class="btn ghost small" value="cancel" formnovalidate aria-label="关闭">${raw(icon('close'))}</button></div>
      <div class="modal-body">
        <div class="form-row">
          <div class="segmented" role="radiogroup" aria-label="方式">
            <label><input type="radio" name="mode" value="problem" checked><span>辅导一道题</span></label>
            <label><input type="radio" name="mode" value="learn"><span>学习一个目标或材料</span></label>
          </div>
        </div>
        <div class="form-row">
          <label class="label" for="q-text">题目或学习目标</label>
          <textarea class="field" id="q-text" rows="4" maxlength="1600" placeholder="粘贴任何学科的题目，或者说说你想弄懂什么"></textarea>
        </div>
        <details class="more">
          <summary>附上题目照片或学习材料</summary>
          <div class="form-row">
            <label class="label" for="q-image">题目照片 <span class="opt">（识别后需要你确认）</span></label>
            <input class="field" id="q-image" type="file" accept="image/png,image/jpeg,image/webp">
          </div>
          <div class="form-row">
            <label class="label" for="q-file">材料文件 <span class="opt">（.txt / .md / 文字版 PDF）</span></label>
            <input class="field" id="q-file" type="file" accept=".txt,.md,.pdf">
          </div>
          <div class="form-row">
            <label class="label" for="q-material">或者直接粘贴材料</label>
            <textarea class="field" id="q-material" rows="4" maxlength="100000"></textarea>
          </div>
        </details>
      </div>
      <div class="modal-foot"><button class="btn ghost" value="cancel" formnovalidate>取消</button><button class="btn primary" id="q-start" value="start">开始</button></div>
    </form>
  </dialog>

  <dialog class="modal" id="dlg-resume">
    <form method="dialog" id="resume-form">
      <div class="modal-head"><h3>恢复会话</h3></div>
      <div class="modal-body">
        <label class="label" for="r-sid">会话编号</label>
        <input class="field mono" id="r-sid" placeholder="open_…" required>
        <p class="hint">会话编号在右侧“这次的进度”里，可以换一台设备继续。</p>
      </div>
      <div class="modal-foot"><button class="btn ghost" value="cancel" formnovalidate>取消</button><button class="btn primary" value="ok">恢复</button></div>
    </form>
  </dialog>

  <dialog class="modal" id="dlg-path">
    <div class="modal-head"><h3>复习与学习路径</h3><button class="btn ghost small" id="path-close" aria-label="关闭">${raw(icon('close'))}</button></div>
    <div class="modal-body" id="path-body"></div>
  </dialog>`;
}

function bindDialogs() {
  $('#profile-form').addEventListener('submit', (event) => {
    const name = $('#p-name').value.trim();
    if (!name) {
      event.preventDefault();
      return;
    }
    if (name !== state.learner) state.session = null;
    state.learner = name;
    state.classId = $('#p-class').value.trim();
    store.set('learner', state.learner);
    store.set('class', state.classId);
    renderIdentity();
    renderLesson();
    loadInbox();
    restoreLast();
  });
  $('#dlg-profile').addEventListener('cancel', (event) => {
    if (!state.learner) event.preventDefault();
  });

  $('#question-form').addEventListener('submit', async (event) => {
    if (event.submitter && event.submitter.value !== 'start') return;
    event.preventDefault();
    const button = $('#q-start');
    await busy(button, async () => {
      const task = $('#q-text').value.trim();
      let material = $('#q-material').value.trim();
      const file = $('#q-file').files[0];
      if (file) material = [material, await extractMaterial(file)].filter(Boolean).join('\n');
      const imageFile = $('#q-image').files[0];
      const image_url = imageFile ? await readImage(imageFile) : '';
      if (!task && !material && !image_url) throw new Error('请写下题目或学习目标，或附上照片、材料');
      const mode = $('#question-form').elements.mode.value;
      await startSession({ task, material, image_url, mode, request_id: uniqueKey() });
      $('#dlg-question').close();
      $('#question-form').reset();
    });
  });

  $('#resume-form').addEventListener('submit', async (event) => {
    if (event.submitter && event.submitter.value !== 'ok') return;
    event.preventDefault();
    const sid = $('#r-sid').value.trim();
    try {
      setSession(await api('/api/sessions/' + encodeURIComponent(sid)));
      $('#dlg-resume').close();
    } catch (error) {
      toast(error.message, 'error');
    }
  });
  $('#path-close').onclick = () => $('#dlg-path').close();
}

function openProfile() {
  $('#p-name').value = state.learner;
  $('#p-class').value = state.classId;
  openDialog($('#dlg-profile'));
}

async function openPath() {
  const body = $('#path-body');
  body.innerHTML = html`
    <div class="section-label">复习与补救</div>
    <div id="followups" class="small muted">${state.classId ? '加载中…' : '填写班级后可以查看。'}</div>
    <div class="section-label" style="margin-top:22px">按学习路径看下一步</div>
    <div class="row"><input class="field mono" id="path-id" placeholder="老师给的学习路径编号" value="${store.get('path', '')}"><button class="btn" id="path-go">查看</button></div>
    <div id="path-result" style="margin-top:12px"></div>`;
  openDialog($('#dlg-path'));
  $('#path-go').onclick = (e) => busy(e.currentTarget, showNextStep);
  if (!state.classId) return;
  try {
    const rows = await api(`/api/learning/followups?${new URLSearchParams({ class_id: state.classId, learner: state.learner })}`);
    $('#followups').innerHTML = rows.length
      ? `<table class="data"><tbody>${rows
          .map((r) => html`<tr><td>${KIND[r.body.kind] || r.body.kind}</td><td>${r.body.objective || ''}</td><td>${raw(pill(...(INBOX_STATUS[r.status] || [r.status, ''])))}</td><td class="muted">${when(r.due_at)}</td></tr>`)
          .join('')}</tbody></table>`
      : '暂时没有复习或补救任务。';
  } catch (error) {
    $('#followups').textContent = error.message;
  }
}

const NEXT = {
  resume: '继续没做完的作答',
  followup: '先完成老师布置的任务',
  request_review: '请老师安排复习',
  check: '做一次独立检查',
  remediate: '补一补这个知识点',
  diagnose: '先做一道诊断题',
  complete: '这条路径都完成了',
};

async function showNextStep() {
  const pathId = $('#path-id').value.trim();
  if (!pathId) throw new Error('请输入学习路径编号');
  store.set('path', pathId);
  const r = await api(`/api/learning/next?${new URLSearchParams({ path_id: pathId, learner: state.learner })}`);
  const action =
    r.followup_id ? `<button class="btn primary small" id="next-start">开始</button>` : r.session_id ? `<button class="btn primary small" id="next-resume">继续</button>` : '';
  $('#path-result').innerHTML = html`<div class="notice info"><strong>${NEXT[r.action] || r.action}</strong><br>${r.reason}</div><div style="margin-top:10px">${raw(action)}</div>`;
  $('#next-start')?.addEventListener('click', async () => {
    await startSession({ followup_id: r.followup_id, request_id: 'followup:' + r.followup_id });
    $('#dlg-path').close();
  });
  $('#next-resume')?.addEventListener('click', async () => {
    setSession(await api('/api/sessions/' + r.session_id));
    $('#dlg-path').close();
  });
}

boot();
