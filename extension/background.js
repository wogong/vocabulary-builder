/**
 * Service worker: the only place that talks to the server.
 *
 * Requests go out from the extension origin, so page CORS policy is irrelevant
 * and the token never enters a web page's context.
 *
 * P0 sends immediately and reports failure to the user. The retry queue of
 * §6.5 arrives in P1.
 */

async function postItem(payload) {
  const { serverUrl, token } = await chrome.storage.local.get({ serverUrl: '', token: '' });
  if (!serverUrl || !token) {
    return { ok: false, error: '尚未配置服务器地址或 Token' };
  }

  let endpoint;
  try {
    endpoint = new URL('/v1/items', serverUrl).toString();
  } catch {
    return { ok: false, error: `服务器地址无效：${serverUrl}` };
  }

  let res;
  try {
    res = await fetch(endpoint, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
      body: JSON.stringify(payload),
    });
  } catch (err) {
    return { ok: false, error: `网络错误：${err.message}` };
  }

  if (!res.ok) {
    let detail = '';
    try {
      const body = await res.json();
      detail = body.detail ? `：${JSON.stringify(body.detail)}` : '';
    } catch {
      /* non-JSON error body */
    }
    return { ok: false, error: `HTTP ${res.status}${detail}` };
  }

  return { ok: true, data: await res.json() };
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message?.type !== 'vocab:add') return undefined;
  postItem(message.payload).then(sendResponse);
  return true; // keep the channel open for the async reply
});
