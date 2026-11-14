// Shared helpers for the student and teacher workspaces. No framework, no build step.
// All model, student and teacher text is inserted as text or through esc(); never as raw HTML.

const TOKEN_KEY = 'omniedu.token';

export const store = {
  get(key, fallback = null) {
    try {
      const raw = localStorage.getItem('omniedu.' + key);
      return raw === null ? fallback : JSON.parse(raw);
    } catch {
      return fallback;
    }
  },
  set(key, value) {
    try {
      localStorage.setItem('omniedu.' + key, JSON.stringify(value));
    } catch {
      /* private mode: preferences are a convenience only */
    }
  },
};

function token() {
  try {
    return sessionStorage.getItem(TOKEN_KEY) || localStorage.getItem(TOKEN_KEY) || '';
  } catch {
    return '';
  }
}

export class ApiError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

/** JSON API call. Throws ApiError with the server's Chinese message. */
export async function api(path, { method = 'GET', body, form } = {}) {
  const headers = {};
  const auth = token();
  if (auth) headers.Authorization = 'Bearer ' + auth;
  let payload;
  if (form) payload = form;
  else if (body !== undefined) {
    headers['Content-Type'] = 'application/json';
    payload = JSON.stringify(body);
  }
  let response;
  try {
    response = await fetch(path, { method, headers, body: payload });
  } catch {
    throw new ApiError(0, '无法连接服务器，请检查网络后重试');
  }
  const data = await response.json().catch(() => null);
  if (!response.ok) {
    throw new ApiError(response.status, errorMessage(response.status, data));
  }
  return data;
}

function errorMessage(status, data) {
  const detail = data && data.detail;
  if (typeof detail === 'string') return detail;
  if (Array.isArray(detail) && detail.length) {
    // Pydantic validation errors: name the first offending field.
    const first = detail[0];
    const field = (first.loc || []).slice(1).join('.');
    return `${field ? field + '：' : ''}${first.msg}`;
  }
  return { 401: '需要登录', 403: '没有权限', 404: '找不到这条记录', 409: '内容已更新，请刷新后重试', 503: '模型服务暂时不可用，请稍后重试' }[status] || `请求失败（${status}）`;
}

export function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
}

/** Tagged template that escapes every interpolation unless it is wrapped with raw(). */
export function html(strings, ...values) {
  return strings.reduce((out, s, i) => {
    if (i === 0) return s;
    const v = values[i - 1];
    const text = v && v.__raw !== undefined ? v.__raw : Array.isArray(v) ? v.map((x) => (x && x.__raw !== undefined ? x.__raw : esc(x))).join('') : esc(v);
    return out + text + s;
  }, '');
}

export const raw = (s) => ({ __raw: s });
export const $ = (selector, root = document) => root.querySelector(selector);
export const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

export function toast(message, kind = '') {
  let box = $('.toasts');
  if (!box) {
    box = document.createElement('div');
    box.className = 'toasts';
    box.setAttribute('role', 'status');
    document.body.append(box);
  }
  const item = document.createElement('div');
  item.className = 'toast ' + kind;
  item.textContent = message;
  box.append(item);
  setTimeout(() => item.remove(), kind === 'error' ? 6000 : 3200);
}

/** Run an async action while a button shows a spinner; errors become toasts. */
export async function busy(button, action) {
  const label = button ? button.innerHTML : '';
  if (button) {
    button.disabled = true;
    button.innerHTML = '<span class="spinner"></span>' + label;
  }
  try {
    return await action();
  } catch (error) {
    toast(error.message || String(error), 'error');
    return undefined;
  } finally {
    if (button) {
      button.disabled = false;
      button.innerHTML = label;
    }
  }
}

/** Deterministic request id: retrying the same submission is idempotent on the server. */
export function requestKey(...parts) {
  const text = JSON.stringify(parts);
  let h1 = 0x811c9dc5;
  let h2 = 0x01000193;
  for (let i = 0; i < text.length; i++) {
    const c = text.charCodeAt(i);
    h1 = Math.imul(h1 ^ c, 0x01000193) >>> 0;
    h2 = Math.imul(h2 ^ c, 0x5bd1e995) >>> 0;
  }
  return 'ui-' + h1.toString(36) + h2.toString(36) + '-' + text.length;
}

export function uniqueKey() {
  return 'ui-' + Date.now().toString(36) + Math.random().toString(36).slice(2, 10);
}

export function lines(text) {
  return String(text || '').split('\n').map((x) => x.trim()).filter(Boolean);
}

const TIME = new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' });
export function when(seconds) {
  if (!seconds) return '';
  return TIME.format(new Date(seconds * 1000));
}

/** Read an image file as a data URL, downscaled so uploads stay small. */
export function readImage(file, maxSide = 2048) {
  return new Promise((resolve, reject) => {
    if (!/^image\/(png|jpeg|webp)$/.test(file.type)) return reject(new Error('图片须为 PNG、JPEG 或 WebP'));
    if (file.size > 30 * 1024 * 1024) return reject(new Error('图片需小于 30MB'));
    const reader = new FileReader();
    reader.onerror = () => reject(new Error('图片无法读取'));
    reader.onload = () => {
      const img = new Image();
      img.onerror = () => reject(new Error('图片无法读取'));
      img.onload = () => {
        const scale = Math.min(1, maxSide / Math.max(img.width, img.height));
        if (scale === 1 && file.size < 6 * 1024 * 1024) return resolve(reader.result);
        const canvas = document.createElement('canvas');
        canvas.width = Math.round(img.width * scale);
        canvas.height = Math.round(img.height * scale);
        canvas.getContext('2d').drawImage(img, 0, 0, canvas.width, canvas.height);
        resolve(canvas.toDataURL('image/jpeg', 0.9));
      };
      img.src = reader.result;
    };
    reader.readAsDataURL(file);
  });
}

export async function extractMaterial(file) {
  const form = new FormData();
  form.append('file', file);
  return (await api('/api/materials/extract', { method: 'POST', form })).text;
}

// ---------- icons (inline SVG, stroke = currentColor) ----------
const PATHS = {
  book: '<path d="M12 6.5C10.5 5.3 8.4 4.5 5 4.5v13c3.4 0 5.5.8 7 2 1.5-1.2 3.6-2 7-2v-13c-3.4 0-5.5.8-7 2Z"/><path d="M12 6.5v13"/>',
  people: '<circle cx="9" cy="8" r="3.2"/><path d="M3.5 19c.6-3.2 2.8-5 5.5-5s4.9 1.8 5.5 5"/><path d="M15.5 5.2a3 3 0 0 1 0 5.6M17.5 14.3c1.6.6 2.7 2.2 3 4.7"/>',
  chart: '<path d="M4 19.5h16"/><path d="M7 16v-5M12 16V7M17 16v-8"/>',
  pencil: '<path d="M4 20h4L19 9l-4-4L4 16v4Z"/><path d="m13.5 6.5 4 4"/>',
  layers: '<path d="m12 4 8 4-8 4-8-4 8-4Z"/><path d="m4 12 8 4 8-4M4 16l8 4 8-4"/>',
  route: '<circle cx="6" cy="18" r="2"/><circle cx="18" cy="6" r="2"/><path d="M8 18h7a3 3 0 0 0 0-6H9a3 3 0 0 1 0-6h7"/>',
  check: '<path d="M5 12.5 10 17l9-10"/>',
  review: '<rect x="5" y="4" width="14" height="17" rx="2"/><path d="M9 4.5V3h6v1.5M9 11h6M9 15h4"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  send: '<path d="M5 12h13M13 6l6 6-6 6"/>',
  camera: '<path d="M4 8.5A1.5 1.5 0 0 1 5.5 7h2l1.5-2h6l1.5 2h2A1.5 1.5 0 0 1 20 8.5v9a1.5 1.5 0 0 1-1.5 1.5h-13A1.5 1.5 0 0 1 4 17.5v-9Z"/><circle cx="12" cy="13" r="3.5"/>',
  bulb: '<path d="M9 18h6M10 21h4"/><path d="M12 3a6 6 0 0 0-3.5 10.9c.6.5 1 1.2 1 2V16h5v-.1c0-.8.4-1.5 1-2A6 6 0 0 0 12 3Z"/>',
  explain: '<path d="M4 5h16v11H9l-5 4V5Z"/><path d="M8 9.5h8M8 12.5h5"/>',
  arrow: '<path d="M5 12h14M13 6l6 6-6 6"/>',
  back: '<path d="M19 12H5M11 6l-6 6 6 6"/>',
  menu: '<path d="M4 7h16M4 12h16M4 17h16"/>',
  close: '<path d="M6 6l12 12M18 6 6 18"/>',
  refresh: '<path d="M19 11a7 7 0 1 0-2 5"/><path d="M19 5v6h-6"/>',
  spark: '<path d="M12 3v4M12 17v4M3 12h4M17 12h4M6 6l2.5 2.5M15.5 15.5 18 18M18 6l-2.5 2.5M8.5 15.5 6 18"/>',
  file: '<path d="M7 3h7l5 5v13H7V3Z"/><path d="M14 3v5h5"/>',
  logout: '<path d="M10 5H5v14h5M15 8l4 4-4 4M19 12H9"/>',
};

export function icon(name, cls = 'icon') {
  return `<svg class="${cls}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${PATHS[name] || ''}</svg>`;
}

// ---------- vocabulary shared by both workspaces ----------
export const ACTION = {
  locate: ['定位问题', 'amber'],
  locate_template: ['定位问题', 'amber'],
  probe: ['追问依据', 'blue'],
  clarify: ['请补充', ''],
  self_correct: ['自行修正', 'blue'],
  change_explanation: ['换个讲法', 'violet'],
  prerequisite: ['先修知识', 'violet'],
  scaffold: ['提示', 'accent'],
  scaffold_concept: ['提示 · 概念', 'accent'],
  scaffold_step: ['提示 · 步骤', 'accent'],
  scaffold_example: ['提示 · 例子', 'accent'],
  // Server-written hints used when every model draft for that level was rejected.
  scaffold_concept_template: ['提示 · 概念', 'accent'],
  scaffold_step_template: ['提示 · 步骤', 'accent'],
  scaffold_example_template: ['提示 · 检查思路', 'accent'],
  explain: ['完整讲解', 'violet'],
  offer_check: ['完成', 'green'],
  create_check: ['独立检查', 'violet'],
  assess_only: ['独立检查结果', ''],
  teacher_review: ['已转给老师', 'red'],
  resume_practice: ['回到原任务', ''],
};

export const STATUS = {
  correct: ['正确', 'green'],
  incorrect: ['有错', 'amber'],
  incomplete: ['未完成', 'amber'],
  unknown: ['待核对', ''],
};

export const EVIDENCE = {
  independent_observed: ['独立完成', 'green'],
  assisted_complete: ['借助帮助完成', 'accent'],
  practice_complete: ['练习完成', 'accent'],
  needs_support: ['需要帮助', 'amber'],
  needs_recheck: ['需重新检查', 'red'],
  insufficient_evidence: ['证据不足', ''],
};

export function pill(label, color = '') {
  return `<span class="pill ${esc(color)}">${esc(label)}</span>`;
}

export function pillFor(table, key, fallback = '') {
  const [label, color] = table[key] || [fallback || key || '', ''];
  return label ? pill(label, color) : '';
}

// ---------- identity ----------

/**
 * Resolve who is using the page. Local demo: returns {mode:'local-demo'}.
 * Classroom: requires a token; shows a sign-in card until a valid one is entered.
 */
export async function identify(root, role) {
  try {
    const me = await api('/api/me');
    if (me.mode === 'classroom' && me.role !== role) {
      renderGate(root, role, `当前账号是${me.role === 'teacher' ? '教师' : '学生'}账号，请换用${role === 'teacher' ? '教师' : '学生'}账号。`);
      return new Promise(() => {});
    }
    return me;
  } catch (error) {
    if (error.status !== 401) throw error;
    renderGate(root, role);
    return new Promise(() => {}); // the gate reloads the page after sign-in
  }
}

export function signOut() {
  try {
    sessionStorage.removeItem(TOKEN_KEY);
    localStorage.removeItem(TOKEN_KEY);
  } catch {
    /* ignore */
  }
  location.reload();
}

function renderGate(root, role, message = '') {
  root.innerHTML = html`
    <main class="gate">
      <form class="card" id="gate-form">
        <a class="brand" href="/">${raw(`<span class="brand-mark">${icon('book')}</span>`)}OmniEdu</a>
        <h2>${role === 'teacher' ? '教师登录' : '学生登录'}</h2>
        <p class="muted small" style="margin-bottom:18px">本服务运行在班级模式。请输入老师或管理员发给你的访问码。</p>
        ${message ? raw(`<div class="notice warn" style="margin-bottom:14px">${esc(message)}</div>`) : ''}
        <label class="label" for="gate-token">访问码</label>
        <input class="field" id="gate-token" type="password" autocomplete="current-password" required>
        <label class="check small" style="margin:12px 0 18px"><input type="checkbox" id="gate-remember"> 在这台设备上记住我</label>
        <button class="btn primary block" type="submit">进入</button>
      </form>
    </main>`;
  $('#gate-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const value = $('#gate-token').value.trim();
    try {
      sessionStorage.setItem(TOKEN_KEY, value);
      if ($('#gate-remember').checked) localStorage.setItem(TOKEN_KEY, value);
    } catch {
      /* ignore */
    }
    try {
      await api('/api/me');
      location.reload();
    } catch (error) {
      try {
        sessionStorage.removeItem(TOKEN_KEY);
        localStorage.removeItem(TOKEN_KEY);
      } catch {
        /* ignore */
      }
      toast(error.status === 401 ? '访问码无效' : error.message, 'error');
    }
  });
}

export function brand(subtitle) {
  return `<a class="brand" href="/"><span class="brand-mark">${icon('book')}</span>OmniEdu<small>${esc(subtitle)}</small></a>`;
}

export function openDialog(dialog) {
  if (typeof dialog.showModal === 'function') dialog.showModal();
  else dialog.setAttribute('open', '');
}
