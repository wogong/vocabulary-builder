/**
 * Service worker: the only place that talks to the server.
 *
 * Requests go out from the extension origin, so page CORS policy is irrelevant
 * and the token never enters a web page's context.
 *
 * Captures that fail to send are queued in storage and retried with growing
 * delays (§6.5). Every item carries an idem_key, so a retry that actually did
 * land the first time is rejected as a duplicate rather than double-counted.
 */

const QUEUE_KEY = 'queue';
const MAX_QUEUE = 500;
// 1s, 5s, 30s, 5min, 30min (§6.5). Past the end we keep retrying at 30min.
const BACKOFF_MS = [1000, 5000, 30000, 300000, 1800000];
const FLUSH_ALARM = 'vocab:flush';

// -- settings -------------------------------------------------------

async function settings() {
  return chrome.storage.local.get({ serverUrl: '', token: '' });
}

// -- transport ------------------------------------------------------

/**
 * One HTTP attempt. Distinguishes "try again later" from "this will never work",
 * because retrying a 401 forever just hides a bad token.
 */
async function request(path, { method = 'GET', body = null, query = null } = {}) {
  const { serverUrl, token } = await settings();
  if (!serverUrl || !token) {
    return { ok: false, retryable: false, error: '尚未配置服务器地址或 Token' };
  }

  let url;
  try {
    url = new URL(path, serverUrl);
  } catch {
    return { ok: false, retryable: false, error: `服务器地址无效：${serverUrl}` };
  }
  if (query) for (const [k, v] of Object.entries(query)) url.searchParams.set(k, v);

  let res;
  try {
    res = await fetch(url, {
      method,
      headers: {
        Authorization: `Bearer ${token}`,
        ...(body ? { 'Content-Type': 'application/json' } : {}),
      },
      body: body ? JSON.stringify(body) : undefined,
    });
  } catch (err) {
    return { ok: false, retryable: true, error: `网络错误：${err.message}` };
  }

  if (res.ok) return { ok: true, data: await res.json() };

  let detail = '';
  try {
    const parsed = await res.json();
    detail = parsed.detail ? `：${JSON.stringify(parsed.detail)}` : '';
  } catch {
    /* not JSON */
  }
  // 5xx and 429 are the server's problem and may pass; 4xx is ours and will not.
  const retryable = res.status >= 500 || res.status === 429;
  return { ok: false, retryable, status: res.status, error: `HTTP ${res.status}${detail}` };
}

// -- offline queue --------------------------------------------------

async function readQueue() {
  return (await chrome.storage.local.get({ [QUEUE_KEY]: [] }))[QUEUE_KEY];
}

async function writeQueue(queue) {
  await chrome.storage.local.set({ [QUEUE_KEY]: queue });
  const count = queue.length;
  await chrome.action.setBadgeText({ text: count ? String(count) : '' });
  await chrome.action.setBadgeBackgroundColor({ color: '#e65100' });
  return count;
}

function delayFor(attempts) {
  return BACKOFF_MS[Math.min(attempts, BACKOFF_MS.length - 1)];
}

async function enqueue(payload, attempts = 0) {
  const queue = await readQueue();
  queue.push({ payload, attempts, nextAttempt: Date.now() + delayFor(attempts) });
  let dropped = 0;
  while (queue.length > MAX_QUEUE) {
    queue.shift();
    dropped++;
  }
  await writeQueue(queue);
  if (dropped) console.warn(`vocab: queue full, dropped ${dropped} oldest capture(s)`);
  scheduleFlush(delayFor(attempts));
  return queue.length;
}

/** Alarms cannot fire sub-minute reliably; setTimeout covers the short waits. */
function scheduleFlush(delayMs) {
  if (delayMs < 60000) {
    setTimeout(() => { flush(); }, delayMs);
  }
  chrome.alarms.create(FLUSH_ALARM, { delayInMinutes: Math.max(1, delayMs / 60000) });
}

let flushing = false;

async function flush() {
  if (flushing) return;
  flushing = true;
  try {
    const queue = await readQueue();
    if (!queue.length) {
      await writeQueue(queue);
      return;
    }
    const now = Date.now();
    const remaining = [];
    let soonest = Infinity;

    for (const entry of queue) {
      if (entry.nextAttempt > now) {
        remaining.push(entry);
        soonest = Math.min(soonest, entry.nextAttempt - now);
        continue;
      }
      const result = await request('/v1/items', { method: 'POST', body: entry.payload });
      if (result.ok) continue;
      // The server already has it; a retry landing as 409 is success, not failure.
      if (result.status === 409) continue;
      if (!result.retryable) {
        console.warn('vocab: dropping unsendable capture', result.error);
        continue;
      }
      const attempts = entry.attempts + 1;
      const wait = delayFor(attempts);
      remaining.push({ ...entry, attempts, nextAttempt: now + wait });
      soonest = Math.min(soonest, wait);
    }

    await writeQueue(remaining);
    if (remaining.length) scheduleFlush(soonest === Infinity ? 60000 : soonest);
  } finally {
    flushing = false;
  }
}

// -- actions --------------------------------------------------------

async function addItem(payload) {
  const result = await request('/v1/items', { method: 'POST', body: payload });
  if (result.ok) return result;
  if (!result.retryable) return result;
  const depth = await enqueue(payload);
  return { ok: false, queued: true, depth, error: `${result.error}（已排队，稍后重试）` };
}

const HANDLERS = {
  'vocab:lookup': (msg) => request('/v1/lookup', { query: { word: msg.word, lang: msg.lang } }),
  'vocab:add': (msg) => addItem(msg.payload),
  'vocab:known': (msg) =>
    request('/v1/known', { method: 'POST', body: { word: msg.word, lang: msg.lang } }),
  'vocab:queue': async () => ({ ok: true, data: { depth: (await readQueue()).length } }),
  'vocab:flush': async () => {
    await flush();
    return { ok: true, data: { depth: (await readQueue()).length } };
  },
};

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  const handler = HANDLERS[message?.type];
  if (!handler) return undefined;
  handler(message).then(sendResponse);
  return true; // keep the channel open for the async reply
});

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === FLUSH_ALARM) flush();
});

chrome.runtime.onStartup.addListener(() => {
  chrome.alarms.create(FLUSH_ALARM, { periodInMinutes: 5 });
  flush();
});

chrome.runtime.onInstalled.addListener(() => {
  chrome.alarms.create(FLUSH_ALARM, { periodInMinutes: 5 });
  flush();
});

// The toolbar button had no action. Opening the review page is the one thing
// worth a single click: it is where a mis-captured word gets deleted.
chrome.action.onClicked.addListener(() => {
  chrome.tabs.create({ url: chrome.runtime.getURL('review.html') });
});
