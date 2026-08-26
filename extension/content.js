/**
 * Content script: double-click a word, see what it means, decide whether to keep it.
 *
 * P1 shows a lookup card instead of submitting straight away (§6.4, D5):
 * looking a word up is not the same as wanting to memorise it. Adding is still
 * one click, so collecting stays cheap.
 */
(function () {
  'use strict';

  const DEFAULTS = {
    serverUrl: '',
    token: '',
    clientId: 'chrome',
    lang: 'en',
    enabled: true,
    blacklist: [],
  };

  let settings = { ...DEFAULTS };
  chrome.storage.local.get(DEFAULTS).then((loaded) => {
    settings = loaded;
  });
  chrome.storage.onChanged.addListener((changes, area) => {
    if (area !== 'local') return;
    for (const [key, { newValue }] of Object.entries(changes)) settings[key] = newValue;
  });

  const WORD = /^[\p{L}\p{M}][\p{L}\p{M}'’·-]*$/u;

  const isWord = (text) => text.length > 0 && text.length <= 50 && WORD.test(text);

  const isEditable = (node) => {
    const el = node?.nodeType === 3 ? node.parentElement : node;
    return !!el?.closest?.('input, textarea, select, [contenteditable=""], [contenteditable="true"]');
  };

  const isBlacklisted = () =>
    (settings.blacklist || []).some(
      (entry) => entry.trim() && location.hostname.includes(entry.trim())
    );

  const send = (message) => chrome.runtime.sendMessage(message);

  // -- card -----------------------------------------------------------

  const CSS = `
    :host { all: initial; }
    .card {
      font: 14px/1.55 -apple-system, "Segoe UI", "Noto Sans CJK SC", sans-serif;
      color: #1a1a1a; background: #fff; border: 1px solid #d8d8d8;
      border-radius: 8px; box-shadow: 0 4px 20px rgba(0,0,0,.18);
      width: 340px; box-sizing: border-box; padding: 12px 14px;
    }
    .head { display: flex; align-items: baseline; gap: 8px; }
    .word { font-size: 17px; font-weight: 700; }
    .phon { color: #7f8c8d; font-size: 13px; }
    .close { margin-left: auto; cursor: pointer; color: #aaa; font-size: 16px;
             line-height: 1; padding: 0 2px; }
    .close:hover { color: #555; }
    .defs { margin: 7px 0 0; max-height: 132px; overflow-y: auto; }
    .defs div { margin: 1px 0; }
    .muted { color: #888; font-size: 12.5px; margin-top: 6px; }
    .tag { display: inline-block; background: #fff3e0; color: #e65100;
           border-radius: 3px; padding: 0 5px; font-size: 12px; margin-left: 6px; }
    .tag.known { background: #e8f5e9; color: #1b5e20; }
    .tag.deck { background: #e3f2fd; color: #0d47a1; }
    textarea {
      width: 100%; box-sizing: border-box; margin-top: 9px; padding: 6px 8px;
      border: 1px solid #ddd; border-radius: 5px; font: inherit; font-size: 13px;
      resize: vertical; min-height: 54px; color: #333; background: #fafafa;
    }
    textarea:focus { outline: none; border-color: #90caf9; background: #fff; }
    .row { display: flex; gap: 8px; margin-top: 9px; }
    button {
      font: inherit; font-size: 13px; padding: 6px 12px; border-radius: 5px;
      border: 1px solid #ccc; background: #f7f7f7; cursor: pointer; color: #333;
    }
    button:hover { background: #efefef; }
    button.primary { background: #1a73e8; border-color: #1a73e8; color: #fff; flex: 1; }
    button.primary:hover { background: #1765cc; }
    button:disabled { opacity: .55; cursor: default; }
    .status { font-size: 12.5px; margin-top: 8px; }
    .status.err { color: #b71c1c; }
    .status.ok { color: #1b5e20; }
  `;

  let host = null;

  function close() {
    host?.remove();
    host = null;
    document.removeEventListener('keydown', onKeydown, true);
    document.removeEventListener('mousedown', onOutside, true);
  }

  function onKeydown(event) {
    if (event.key === 'Escape') close();
  }

  function onOutside(event) {
    // Events from inside a closed shadow root retarget to the host element.
    if (host && !host.contains(event.target)) close();
  }

  function place(rect) {
    const width = 340;
    const margin = 8;
    const left = Math.max(
      margin,
      Math.min(
        (rect ? rect.left : margin) + window.scrollX,
        window.scrollX + document.documentElement.clientWidth - width - margin
      )
    );
    const below = (rect ? rect.bottom : 0) + window.scrollY + 8;
    const above = (rect ? rect.top : 0) + window.scrollY - margin;
    const room = window.innerHeight - (rect ? rect.bottom : 0);
    return { left, top: room < 260 && above > 260 ? above - 260 : below };
  }

  function openCard(word, context, rect) {
    close();
    host = document.createElement('div');
    host.style.cssText = 'all:initial;position:absolute;z-index:2147483647;';
    const pos = place(rect);
    host.style.left = `${pos.left}px`;
    host.style.top = `${pos.top}px`;

    const shadow = host.attachShadow({ mode: 'closed' });
    const style = document.createElement('style');
    style.textContent = CSS;
    const card = document.createElement('div');
    card.className = 'card';
    card.innerHTML = `
      <div class="head">
        <span class="word"></span>
        <span class="phon"></span>
        <span class="close" title="关闭 (Esc)">×</span>
      </div>
      <div class="defs"><div class="muted">查询中…</div></div>
      <div class="meta muted"></div>
      <textarea spellcheck="false"></textarea>
      <div class="row">
        <button class="primary add" disabled>+ 加入</button>
        <button class="known">✓ 已认识</button>
      </div>
      <div class="status"></div>`;
    shadow.append(style, card);
    document.body.appendChild(host);

    const $ = (sel) => card.querySelector(sel);
    $('.word').textContent = word;
    $('textarea').value = context.sentence;
    if (context.lowConfidence) {
      $('.status').className = 'status err';
      $('.status').textContent = '句子可能不完整，加入前请先改一下';
    }
    $('.close').addEventListener('click', close);

    document.addEventListener('keydown', onKeydown, true);
    document.addEventListener('mousedown', onOutside, true);

    return { card, $ };
  }

  // -- flow -----------------------------------------------------------

  function renderLookup(ui, word, info) {
    const { $ } = ui;
    if (info.phonetic) $('.phon').textContent = info.phonetic;

    const defs = $('.defs');
    defs.textContent = '';
    if (info.definitions?.length) {
      for (const line of info.definitions.slice(0, 8)) {
        const row = document.createElement('div');
        row.textContent = line;
        defs.appendChild(row);
      }
    } else {
      const row = document.createElement('div');
      row.className = 'muted';
      row.textContent = info.dictionary === null ? '未配置词典' : '词典里没有这个词';
      defs.appendChild(row);
    }

    const meta = $('.meta');
    meta.textContent = '';
    if (info.lemma && info.lemma !== word.toLowerCase()) {
      meta.append(`原型 ${info.lemma}`);
    }
    if (info.freq_rank) {
      meta.append(`${meta.textContent ? ' · ' : ''}词频 ${info.freq_rank}`);
    }
    if (info.common) {
      const tag = document.createElement('span');
      tag.className = 'tag';
      tag.textContent = '常见词';
      meta.appendChild(tag);
    }
    if (info.already_known) {
      const tag = document.createElement('span');
      tag.className = 'tag known';
      tag.textContent = '已标为认识';
      meta.appendChild(tag);
    }
    if (info.in_deck) {
      const tag = document.createElement('span');
      tag.className = 'tag deck';
      tag.textContent = `牌组中 · ${info.existing_contexts} 条语境`;
      meta.appendChild(tag);
    }

    const add = $('.add');
    add.disabled = false;
    add.textContent = info.in_deck ? `+ 追加语境（已有 ${info.existing_contexts}）` : '+ 加入';
  }

  async function onDoubleClick(event) {
    if (!settings.enabled || isBlacklisted()) return;
    if (isEditable(event.target)) return;
    if (host && host.contains(event.target)) return;

    const selection = window.getSelection();
    const word = (selection ? selection.toString() : '').trim();
    if (!isWord(word)) return;

    const context = VocabSentence.extract(selection, word, settings.lang);
    if (!context || !context.sentence) return;
    const rect = selection.rangeCount
      ? selection.getRangeAt(0).getBoundingClientRect()
      : null;

    const ui = openCard(word, context, rect);
    const { $ } = ui;
    const mine = host;

    const lookup = await send({ type: 'vocab:lookup', word, lang: settings.lang });
    if (host !== mine) return; // card was closed or replaced while we waited
    if (lookup?.ok) {
      renderLookup(ui, word, lookup.data);
    } else {
      $('.defs').textContent = '';
      $('.status').className = 'status err';
      $('.status').textContent = lookup?.error || '查询失败';
      $('.add').disabled = false; // capturing without a definition is still fine
    }

    $('.add').addEventListener('click', async () => {
      const sentence = $('textarea').value.trim();
      if (!sentence) {
        $('.status').className = 'status err';
        $('.status').textContent = '句子不能为空';
        return;
      }
      $('.add').disabled = true;
      $('.status').className = 'status';
      $('.status').textContent = '提交中…';

      // The sentence may have been edited, so locate the word again.
      const at = sentence.toLowerCase().indexOf(word.toLowerCase());
      const result = await send({
        type: 'vocab:add',
        payload: {
          idem_key: crypto.randomUUID(),
          source: 'chrome',
          client_id: settings.clientId,
          word,
          lang: settings.lang,
          sentence,
          word_offset: at >= 0 ? at : null,
          context_before: context.contextBefore || null,
          context_after: context.contextAfter || null,
          url: location.href,
          page_title: document.title,
          client_ts: new Date().toISOString(),
        },
      });
      if (host !== mine) return;

      if (result?.ok) {
        const { dedup, existing_contexts: existing } = result.data;
        toast(
          dedup === 'duplicate'
            ? `• ${word}\n这条语境已经收过了`
            : dedup === 'merged'
              ? `✓ ${word}\n已追加语境（原有 ${existing} 条）`
              : `✓ ${word}\n已加入`,
          dedup === 'duplicate' ? 'warn' : 'ok',
          rect
        );
        close();
      } else if (result?.queued) {
        toast(`⏳ ${word}\n${result.error}`, 'warn', rect);
        close();
      } else {
        $('.status').className = 'status err';
        $('.status').textContent = result?.error || '提交失败';
        $('.add').disabled = false;
      }
    });

    $('.known').addEventListener('click', async () => {
      $('.known').disabled = true;
      const result = await send({ type: 'vocab:known', word, lang: settings.lang });
      if (host !== mine) return;
      if (result?.ok) {
        toast(`✓ ${word}\n已标为认识，以后不再提示`, 'ok', rect);
        close();
      } else {
        $('.status').className = 'status err';
        $('.status').textContent = result?.error || '标记失败';
        $('.known').disabled = false;
      }
    });
  }

  // -- toast ----------------------------------------------------------

  let toastHost = null;

  function toast(text, tone, anchorRect) {
    toastHost?.remove();
    toastHost = document.createElement('div');
    toastHost.style.cssText = 'all:initial;position:absolute;z-index:2147483647;';
    toastHost.style.top = `${(anchorRect ? anchorRect.bottom : 0) + window.scrollY + 8}px`;
    toastHost.style.left = `${Math.max(
      8,
      Math.min(
        (anchorRect ? anchorRect.left : 8) + window.scrollX,
        window.scrollX + document.documentElement.clientWidth - 320
      )
    )}px`;

    const shadow = toastHost.attachShadow({ mode: 'closed' });
    const colors = {
      ok: ['#1b5e20', '#e8f5e9', '#a5d6a7'],
      warn: ['#e65100', '#fff3e0', '#ffcc80'],
      error: ['#b71c1c', '#ffebee', '#ef9a9a'],
    }[tone] || ['#263238', '#eceff1', '#b0bec5'];
    const box = document.createElement('div');
    box.textContent = text;
    box.style.cssText = `
      font: 13px/1.5 -apple-system, "Segoe UI", "Noto Sans CJK SC", sans-serif;
      color: ${colors[0]}; background: ${colors[1]};
      border: 1px solid ${colors[2]}; border-radius: 6px;
      padding: 8px 12px; max-width: 320px; box-sizing: border-box;
      box-shadow: 0 2px 12px rgba(0,0,0,.15); white-space: pre-wrap; word-break: break-word;`;
    shadow.appendChild(box);
    document.body.appendChild(toastHost);

    const current = toastHost;
    setTimeout(() => current.remove(), tone === 'error' ? 5000 : 2500);
  }

  document.addEventListener('dblclick', onDoubleClick, true);
})();
