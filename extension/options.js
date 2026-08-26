const DEFAULTS = {
  serverUrl: '',
  token: '',
  clientId: 'chrome',
  lang: 'en',
  enabled: true,
  blacklist: [],
};

const $ = (id) => document.getElementById(id);
const status = $('status');

function say(text, tone) {
  status.textContent = text;
  status.className = tone || '';
}

async function load() {
  const s = await chrome.storage.local.get(DEFAULTS);
  $('serverUrl').value = s.serverUrl;
  $('token').value = s.token;
  $('clientId').value = s.clientId;
  $('lang').value = s.lang;
  $('enabled').checked = s.enabled;
  $('blacklist').value = (s.blacklist || []).join('\n');
}

async function save() {
  await chrome.storage.local.set({
    serverUrl: $('serverUrl').value.trim(),
    token: $('token').value.trim(),
    clientId: $('clientId').value.trim() || 'chrome',
    lang: $('lang').value,
    enabled: $('enabled').checked,
    blacklist: $('blacklist').value.split('\n').map((s) => s.trim()).filter(Boolean),
  });
  say('已保存', 'ok');
}

/** Hits an authenticated endpoint so it checks the URL and the token together. */
async function test() {
  const url = $('serverUrl').value.trim();
  const token = $('token').value.trim();
  if (!url || !token) return say('请先填写服务器地址和 Token', 'error');
  say('测试中…');
  try {
    const res = await fetch(new URL('/v1/stats', url), {
      headers: { Authorization: `Bearer ${token}` },
    });
    if (res.status === 401) return say('Token 无效', 'error');
    if (!res.ok) return say(`服务器返回 HTTP ${res.status}`, 'error');
    const body = await res.json();
    say(`连接正常 · 已收 ${body.inbox.distinct_words} 个词`, 'ok');
  } catch (err) {
    say(`连接失败：${err.message}`, 'error');
  }
}

$('save').addEventListener('click', save);
$('test').addEventListener('click', test);
load();
