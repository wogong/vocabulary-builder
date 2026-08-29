const $ = (id) => document.getElementById(id);
const status = $('status');

function say(text, tone) {
  status.textContent = text;
  status.className = tone || '';
}

async function settings() {
  const s = await chrome.storage.local.get({ serverUrl: '', token: '' });
  if (!s.serverUrl || !s.token) throw new Error('还没配置服务器地址和 Token，请先打开设置页');
  return s;
}

async function api(path, options = {}) {
  const { serverUrl, token } = await settings();
  const res = await fetch(new URL(path, serverUrl), {
    ...options,
    headers: { Authorization: `Bearer ${token}` },
  });
  if (res.status === 401) throw new Error('Token 无效');
  if (!res.ok) throw new Error(`服务器返回 HTTP ${res.status}`);
  return res.json();
}

/**
 * Bold the captured word inside its sentence, built as nodes rather than HTML:
 * this text comes from arbitrary web pages and must never be parsed as markup.
 * Mirrors app/render.locate — trust word_offset only if it lands on the word.
 */
function sentenceNode(item) {
  const el = document.createElement('div');
  el.className = 'sentence';
  const { sentence, raw_word: word } = item;
  let start = item.word_offset;
  if (
    !(Number.isInteger(start) &&
      sentence.slice(start, start + word.length).toLowerCase() === word.toLowerCase())
  ) {
    start = sentence.toLowerCase().indexOf(word.toLowerCase());
  }
  if (start < 0) {
    el.textContent = sentence;
    return el;
  }
  const hit = document.createElement('b');
  hit.textContent = sentence.slice(start, start + word.length);
  el.append(sentence.slice(0, start), hit, sentence.slice(start + word.length));
  return el;
}

function row(item, onDelete) {
  const li = document.createElement('li');
  const dead = item.status === 'skipped' || item.status === 'failed';
  if (dead) li.className = 'gone';

  const word = document.createElement('div');
  word.className = 'word';
  word.textContent = item.lemma || item.raw_word;
  const sub = document.createElement('small');
  sub.textContent = item.definition ? item.definition.slice(0, 40) : '';
  word.append(sub);

  const body = document.createElement('div');
  body.className = 'body';
  body.append(sentenceNode(item));

  const meta = document.createElement('div');
  meta.className = 'meta';
  const tag = document.createElement('span');
  tag.className = 'tag';
  tag.textContent = item.status;
  meta.append(tag, ` ${item.source} · ${item.created_at.replace('T', ' ').slice(0, 16)}`);
  if (item.url) {
    const a = document.createElement('a');
    a.href = item.url;
    a.target = '_blank';
    a.rel = 'noreferrer';
    a.textContent = item.page_title || item.url;
    meta.append(' · ', a);
  }
  body.append(meta);

  const button = document.createElement('button');
  button.className = 'danger';
  if (dead) {
    button.textContent = '已删除';
    button.disabled = true;
  } else {
    button.textContent = '删除';
    button.addEventListener('click', () => onDelete(item, button));
  }

  li.append(word, body, button);
  return li;
}

async function remove(item, button) {
  if (!confirm(`删除「${item.lemma || item.raw_word}」的这条语境？`)) return;
  button.disabled = true;
  button.textContent = '删除中…';
  try {
    const out = await api(`/v1/items/${item.id}`, { method: 'DELETE' });
    say(
      out.removed === 'note'
        ? `已删除 ${out.lemma}，整张卡已移除`
        : `已删除 ${out.lemma} 的这条语境`,
      'ok',
    );
    await load();
  } catch (err) {
    button.disabled = false;
    button.textContent = '删除';
    say(`删除失败：${err.message}`, 'error');
  }
}

async function load() {
  const list = $('list');
  const query = $('q').value.trim();
  try {
    const body = await api(`/v1/items?limit=200&q=${encodeURIComponent(query)}`);
    list.replaceChildren(...body.items.map((item) => row(item, remove)));
    if (!body.items.length) say(query ? '没有匹配的词' : '还没有收集任何词');
    else if (status.className !== 'ok') say(`${body.items.length} 条`);
  } catch (err) {
    list.replaceChildren();
    say(err.message, 'error');
  }
}

$('refresh').addEventListener('click', () => { say(''); load(); });
$('q').addEventListener('change', () => { say(''); load(); });
load();
