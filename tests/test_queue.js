/**
 * Offline queue in the service worker (§6.5).
 *
 * Loads the real background.js against a stubbed chrome API and fake fetch.
 *
 *   node tests/test_queue.js
 */
const assert = require('assert');

let failures = 0;
/** Runs immediately: these assertions read mutable state that keeps moving. */
function check(label, fn) {
  try {
    fn();
    console.log(`  [\x1b[32mPASS\x1b[0m] ${label}`);
  } catch (err) {
    failures++;
    console.log(`  [\x1b[31mFAIL\x1b[0m] ${label}\n        ${err.message.split('\n')[0]}`);
  }
}

// -- chrome + fetch stubs -------------------------------------------

const store = {};
let badge = '';
const alarms = [];
let onMessage = null;

global.chrome = {
  storage: {
    local: {
      async get(defaults) {
        const out = {};
        for (const [key, fallback] of Object.entries(defaults)) {
          out[key] = key in store ? store[key] : fallback;
        }
        return out;
      },
      async set(values) {
        Object.assign(store, values);
      },
    },
    onChanged: { addListener() {} },
  },
  action: {
    async setBadgeText({ text }) { badge = text; },
    async setBadgeBackgroundColor() {},
    onClicked: { addListener(fn) { onActionClick = fn; } },
  },
  tabs: {
    create({ url }) { opened.push(url); },
  },
  alarms: {
    create(name, opts) { alarms.push({ name, opts }); },
    onAlarm: { addListener() {} },
  },
  runtime: {
    onMessage: { addListener(fn) { onMessage = fn; } },
    onStartup: { addListener() {} },
    onInstalled: { addListener() {} },
    getURL(path) { return `chrome-extension://test/${path}`; },
  },
};

let onActionClick = null;
const opened = [];

let responder = null;
global.fetch = async (url, init) => responder(String(url), init);

function reply(status, body = {}) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  };
}

require('../extension/background.js');

/** Drive the worker the way a content script would. */
function sendMessage(message) {
  return new Promise((resolve) => {
    onMessage(message, {}, resolve);
  });
}

const queue = () => store.queue || [];
const payload = (word) => ({ idem_key: `k-${word}`, word, sentence: `a ${word} here.` });

// -- tests -----------------------------------------------------------

(async () => {
  console.log('configuration');
  responder = () => reply(200, {});
  let res = await sendMessage({ type: 'vocab:add', payload: payload('x') });
  check('unconfigured server is a permanent failure', () => {
    assert.strictEqual(res.ok, false);
    assert.match(res.error, /尚未配置/);
    assert.strictEqual(queue().length, 0, 'must not queue what can never send');
  });

  store.serverUrl = 'https://api.example.com';
  store.token = 'tok';

  console.log('happy path');
  let seen = null;
  responder = (url, init) => {
    seen = { url, body: JSON.parse(init.body) };
    return reply(202, { id: 1, dedup: 'new', existing_contexts: 0 });
  };
  res = await sendMessage({ type: 'vocab:add', payload: payload('alpha') });
  check('sends and reports success', () => {
    assert.strictEqual(res.ok, true);
    assert.strictEqual(res.data.dedup, 'new');
    assert.strictEqual(seen.url, 'https://api.example.com/v1/items');
    assert.strictEqual(seen.body.word, 'alpha');
  });
  check('nothing queued on success', () => assert.strictEqual(queue().length, 0));

  console.log('network failure');
  responder = () => { throw new Error('offline'); };
  res = await sendMessage({ type: 'vocab:add', payload: payload('beta') });
  check('reports it was queued', () => {
    assert.strictEqual(res.ok, false);
    assert.strictEqual(res.queued, true);
  });
  check('capture is retained', () => {
    assert.strictEqual(queue().length, 1);
    assert.strictEqual(queue()[0].payload.word, 'beta');
  });
  check('badge shows the backlog', () => assert.strictEqual(badge, '1'));

  console.log('retry');
  store.queue = [{ payload: payload('beta'), attempts: 0, nextAttempt: Date.now() - 1 }];
  responder = () => reply(503, {});
  await sendMessage({ type: 'vocab:flush' });
  check('a 5xx keeps the item and grows the delay', () => {
    assert.strictEqual(queue().length, 1);
    assert.strictEqual(queue()[0].attempts, 1);
    assert.ok(queue()[0].nextAttempt > Date.now() + 1000, 'delay should grow past 1s');
  });

  store.queue[0].nextAttempt = Date.now() - 1;
  responder = () => reply(202, { id: 2, dedup: 'new' });
  await sendMessage({ type: 'vocab:flush' });
  check('drains once the server recovers', () => assert.strictEqual(queue().length, 0));
  check('badge clears', () => assert.strictEqual(badge, ''));

  console.log('not-retryable outcomes');
  store.queue = [{ payload: payload('gamma'), attempts: 0, nextAttempt: Date.now() - 1 }];
  responder = () => reply(409, { detail: 'idem_key already accepted' });
  await sendMessage({ type: 'vocab:flush' });
  check('409 counts as delivered, not as a loss', () =>
    assert.strictEqual(queue().length, 0));

  store.queue = [{ payload: payload('delta'), attempts: 0, nextAttempt: Date.now() - 1 }];
  responder = () => reply(401, { detail: 'bad token' });
  await sendMessage({ type: 'vocab:flush' });
  check('401 is dropped rather than retried forever', () =>
    assert.strictEqual(queue().length, 0));

  console.log('respecting the schedule');
  store.queue = [{ payload: payload('later'), attempts: 2, nextAttempt: Date.now() + 60000 }];
  let called = 0;
  responder = () => { called++; return reply(202, {}); };
  await sendMessage({ type: 'vocab:flush' });
  check('an item not yet due is left alone', () => {
    assert.strictEqual(called, 0);
    assert.strictEqual(queue().length, 1);
  });

  console.log('bounded growth');
  store.queue = Array.from({ length: 500 }, (_, i) => ({
    payload: payload(`w${i}`), attempts: 0, nextAttempt: Date.now() + 600000,
  }));
  responder = () => { throw new Error('offline'); };
  await sendMessage({ type: 'vocab:add', payload: payload('newest') });
  check('queue is capped at 500', () => assert.strictEqual(queue().length, 500));
  check('the oldest is dropped, not the newest', () => {
    assert.strictEqual(queue()[0].payload.word, 'w1');
    assert.strictEqual(queue()[499].payload.word, 'newest');
  });

  console.log('other endpoints');
  responder = (url) => reply(200, { url });
  const lookup = await sendMessage({ type: 'vocab:lookup', word: 'alpha', lang: 'en' });
  check('lookup hits /v1/lookup with the word', () => {
    assert.ok(lookup.data.url.includes('/v1/lookup'));
    assert.ok(lookup.data.url.includes('word=alpha'));
  });
  const known = await sendMessage({ type: 'vocab:known', word: 'alpha', lang: 'en' });
  check('known hits /v1/known', () => assert.ok(known.data.url.includes('/v1/known')));

  onActionClick();
  check('the toolbar button opens the review page', () =>
    assert.deepStrictEqual(opened, ['chrome-extension://test/review.html']));

  console.log();
  if (failures) {
    console.log(`${failures} check(s) failed`);
    process.exit(1);
  }
  console.log('offline queue: retries, backs off, and never loses or duplicates a capture');
  process.exit(0);
})();
