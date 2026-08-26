/**
 * Content script: double-click a word, capture its sentence, send it off.
 *
 * P0 submits on double-click directly. The lookup card of §6.4 (definition
 * preview, editable sentence, "already known") is P1; until then the toast is
 * the only feedback, and it shows what was captured so a bad grab is obvious.
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

  function isWord(text) {
    return text.length > 0 && text.length <= 50 && WORD.test(text);
  }

  function isEditable(node) {
    const el = node?.nodeType === 3 ? node.parentElement : node;
    return !!el?.closest?.('input, textarea, select, [contenteditable=""], [contenteditable="true"]');
  }

  function isBlacklisted() {
    return (settings.blacklist || []).some(
      (entry) => entry.trim() && location.hostname.includes(entry.trim())
    );
  }

  // -- toast ----------------------------------------------------------

  let toastHost = null;

  function toast(text, tone, anchorRect) {
    toastHost?.remove();
    toastHost = document.createElement('div');
    toastHost.style.cssText = 'all:initial;position:absolute;z-index:2147483647;';
    const top = (anchorRect ? anchorRect.bottom + window.scrollY + 8 : window.scrollY + 16);
    const left = Math.min(
      anchorRect ? anchorRect.left + window.scrollX : 16,
      window.scrollX + document.documentElement.clientWidth - 320
    );
    toastHost.style.top = `${top}px`;
    toastHost.style.left = `${Math.max(8, left)}px`;

    // Closed shadow root so page CSS cannot reach in and page JS cannot read it.
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

    const host = toastHost;
    setTimeout(() => host.remove(), tone === 'error' ? 5000 : 2500);
  }

  // -- capture --------------------------------------------------------

  async function onDoubleClick(event) {
    if (!settings.enabled || isBlacklisted()) return;
    if (isEditable(event.target)) return;

    const selection = window.getSelection();
    const word = (selection ? selection.toString() : '').trim();
    if (!isWord(word)) return;

    const context = VocabSentence.extract(selection, word, settings.lang);
    if (!context || !context.sentence) return;

    const rect = selection.rangeCount ? selection.getRangeAt(0).getBoundingClientRect() : null;

    const response = await chrome.runtime.sendMessage({
      type: 'vocab:add',
      payload: {
        idem_key: crypto.randomUUID(),
        source: 'chrome',
        client_id: settings.clientId,
        word,
        lang: settings.lang,
        sentence: context.sentence,
        word_offset: context.wordOffset,
        context_before: context.contextBefore || null,
        context_after: context.contextAfter || null,
        url: location.href,
        page_title: document.title,
        client_ts: new Date().toISOString(),
      },
    });

    if (!response?.ok) {
      toast(`✗ ${word}\n${response?.error || '提交失败'}`, 'error', rect);
      return;
    }

    const { dedup, existing_contexts: existing } = response.data;
    if (dedup === 'duplicate') {
      toast(`• ${word}\n这条语境已经收过了`, 'warn', rect);
    } else if (dedup === 'merged') {
      toast(`✓ ${word}\n已追加语境（原有 ${existing} 条）`, 'ok', rect);
    } else if (context.lowConfidence) {
      toast(`✓ ${word}\n句子可能不完整，建议之后在 Anki 里修一下：\n${context.sentence}`, 'warn', rect);
    } else {
      toast(`✓ ${word}\n${context.sentence}`, 'ok', rect);
    }
  }

  document.addEventListener('dblclick', onDoubleClick, true);
})();
