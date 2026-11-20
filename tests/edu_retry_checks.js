// Run checkRetryClient(appJsSource) in a JavaScript host; no DOM or network needed.
async function checkRetryClient(source) {
  let checks = 0;
  const check = (ok, message) => { if (!ok) throw new Error(message); checks++; };
  const storage = new Map();
  const calls = [];
  let next = 0;
  let response = () => { throw new Error('lost response'); };
  const deps = {
    crypto: { randomUUID: () => `key-${++next}`, subtle: { digest: async (_algorithm, bytes) => bytes } },
    TextEncoder: class { encode(s) { return Uint8Array.from(s, (c) => c.charCodeAt(0)); } },
    FormData: class {}, Blob: class {},
    sessionStorage: { getItem: (k) => storage.get(k), setItem: (k, v) => storage.set(k, v), removeItem: (k) => storage.delete(k) },
    fetch: async (_path, opts) => { calls.push(opts.headers['Idempotency-Key']); return response(); },
  };
  const client = () => new Function('deps',
    'const {crypto,TextEncoder,FormData,Blob,sessionStorage,fetch}=deps;\n' + source.split('\nlet toastTimer')[0] +
    '\nreturn {api, user: id => {S.userId=id;}};')(deps);
  const ok = () => ({ ok: true, status: 200, text: async () => '{"id":"saved"}' });
  const failure = (status, detail) => () => ({ ok: false, status, text: async () => JSON.stringify({detail}) });
  const post = (c, body = {instruction: 'shorten'}) => c.api('POST', '/generate', body);
  const fails = async (fn) => { let failed = false; try { await fn(); } catch { failed = true; } check(failed, 'expected error'); };
  let c = client();
  await fails(() => post(c));
  const original = calls.at(-1);
  c = client(); // Simulate a reload with the same tab's sessionStorage.
  response = ok;
  await post(c);
  check(calls.at(-1) === original, 'reload retry must reuse key');
  await post(c);
  check(calls.at(-1) !== original, 'successful operation followed by generate must use new key');
  for (const makeResponse of [
    () => ({ok: true, status: 200, text: async () => '{broken'}),
    failure(500, 'server error'),
    failure(409, '同一操作正在进行，请稍后刷新查看结果'),
  ]) {
    response = makeResponse;
    await fails(() => post(c));
    const key = calls.at(-1);
    response = ok;
    await post(c);
    check(calls.at(-1) === key, 'uncertain operation must keep key');
  }
  response = failure(409, '内容已变化');
  await fails(() => post(c));
  const rejected = calls.at(-1);
  response = ok;
  await post(c);
  check(calls.at(-1) !== rejected, 'definite rejection must release key');
  response = failure(500, 'server error');
  await fails(() => post(c));
  const pending = calls.at(-1);
  await fails(() => post(c, {instruction: 'different'}));
  check(calls.at(-1) !== pending, 'changed body is a distinct operation');
  c.user('another-user');
  await fails(() => post(c));
  check(calls.at(-1) !== pending, 'different users cannot share a pending key');
  response = ok;
  await c.api('GET', '/status');
  check(calls.at(-1) === undefined, 'GET must not send idempotency key');
  return checks;
}

if (typeof require === 'function' && require.main === module) {
  const fs = require('node:fs');
  const path = require('node:path');
  checkRetryClient(fs.readFileSync(path.join(__dirname, '../edu_app/static/app.js'), 'utf8'))
    .then((count) => console.log(`${count} retry assertions passed`))
    .catch((error) => { console.error(error); process.exitCode = 1; });
}
