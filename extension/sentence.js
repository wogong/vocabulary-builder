/**
 * Sentence extraction (spec §6.3).
 *
 * The DOM half finds the block the selection sits in and flattens it to text
 * while tracking where the selected word landed. The text half cleans that
 * string and cuts the sentence out of it, keeping the word offset correct
 * throughout — without it we cannot mask the right occurrence when a word
 * appears twice in one sentence (§5.3).
 *
 * Loadable in node so the text half can be unit-tested without a browser.
 */
(function (root) {
  'use strict';

  const MAX_SENTENCE = 400;
  const KEEP_EACH_SIDE = 200;
  const MIN_WORDS = 3;

  const BLOCK_TAGS = new Set([
    'P', 'LI', 'TD', 'TH', 'BLOCKQUOTE', 'H1', 'H2', 'H3', 'H4', 'H5', 'H6',
    'DD', 'DT', 'FIGCAPTION', 'DIV', 'SECTION', 'ARTICLE', 'MAIN', 'ASIDE',
    'PRE', 'CAPTION',
  ]);
  const SKIP_TAGS = new Set(['SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE', 'SVG']);
  const ZERO_WIDTH = /[\u200B-\u200F\u2060\uFEFF\u00AD]/;
  const FOOTNOTE = /\[\s*\d{1,3}\s*\]/g;

  /** Collapse whitespace and drop zero-width chars, keeping `offset` on its word. */
  function collapse(text, offset) {
    let out = '';
    let newOffset = null;
    let pendingSpace = false;
    let started = false;
    for (let i = 0; i < text.length; i++) {
      const ch = text[i];
      if (ZERO_WIDTH.test(ch)) continue;
      if (/\s/.test(ch)) {
        if (started) pendingSpace = true;
        continue;
      }
      if (pendingSpace) {
        out += ' ';
        pendingSpace = false;
      }
      // The word starts on a non-space char, so the first one at or past the
      // original offset is where it now begins.
      if (newOffset === null && i >= offset) newOffset = out.length;
      out += ch;
      started = true;
    }
    return { text: out, offset: newOffset === null ? out.length : newOffset };
  }

  /** Delete `ranges` ([start,end)) from text, keeping `offset` on its character. */
  function applyRemovals(text, offset, ranges) {
    const sorted = ranges.slice().sort((a, b) => a[0] - b[0]);
    let out = '';
    let cursor = 0;
    let removedBefore = 0;
    let newOffset = null;
    for (const [start, end] of sorted) {
      if (start < cursor) continue; // overlapping; already removed
      out += text.slice(cursor, start);
      if (newOffset === null) {
        if (offset >= end) removedBefore += end - start;
        else if (offset > start) newOffset = start - removedBefore; // inside: clamp
      }
      cursor = end;
    }
    out += text.slice(cursor);
    return { text: out, offset: newOffset === null ? offset - removedBefore : newOffset };
  }

  // ICU ships no abbreviation suppressions, so "Mr. Smith" gets split in two.
  // Only titles that practically never end a sentence — "etc." and "Inc." do.
  const ABBREV = /(?:^|\s)(?:Mr|Mrs|Ms|Dr|Prof|Sr|Jr|St|Mt|Lt|Gen|Sen|Rep|Gov|Col|Capt|Rev|Hon|vs)\.$/i;

  /** Re-join segments the segmenter split on a title. Offsets are unaffected. */
  function mergeAbbreviations(segments) {
    const out = [];
    for (const seg of segments) {
      const prev = out[out.length - 1];
      if (prev && ABBREV.test(prev.text.trimEnd())) prev.text += seg.text;
      else out.push({ text: seg.text, index: seg.index });
    }
    return out;
  }

  /** Split into sentences, preferring Intl.Segmenter over a regex (§6.3). */
  function segment(text, lang) {
    if (typeof Intl !== 'undefined' && Intl.Segmenter) {
      const segmenter = new Intl.Segmenter(lang || 'en', { granularity: 'sentence' });
      return mergeAbbreviations(
        Array.from(segmenter.segment(text), (s) => ({ text: s.segment, index: s.index }))
      );
    }
    const parts = [];
    const re = /(?<=[.!?])\s+(?=[A-Z"'“])/g;
    let start = 0;
    let m;
    while ((m = re.exec(text)) !== null) {
      parts.push({ text: text.slice(start, m.index), index: start });
      start = m.index + m[0].length;
    }
    parts.push({ text: text.slice(start), index: start });
    return parts;
  }

  function trimTo(sentence, wordOffset) {
    const leading = sentence.length - sentence.trimStart().length;
    return { text: sentence.trim(), offset: wordOffset - leading };
  }

  /** Shrink an over-long sentence around the target word. */
  function clip(sentence, wordOffset, wordLength) {
    if (sentence.length <= MAX_SENTENCE) return { text: sentence, offset: wordOffset };
    const start = Math.max(0, wordOffset - KEEP_EACH_SIDE);
    const end = Math.min(sentence.length, wordOffset + wordLength + KEEP_EACH_SIDE);
    const prefix = start > 0 ? '…' : '';
    const suffix = end < sentence.length ? '…' : '';
    return {
      text: prefix + sentence.slice(start, end) + suffix,
      offset: wordOffset - start + prefix.length,
    };
  }

  /**
   * Cut the sentence around `offset` out of already-flattened text.
   * Returns {sentence, wordOffset, contextBefore, contextAfter, lowConfidence}.
   */
  function extractFromFlat(flatText, offset, word, lang) {
    const collapsed = collapse(flatText, offset);
    const segments = segment(collapsed.text, lang);
    let index = segments.findIndex(
      (s) => collapsed.offset >= s.index && collapsed.offset < s.index + s.text.length
    );
    if (index < 0) index = Math.max(0, segments.length - 1);

    let sentence = segments[index].text;
    let wordOffset = collapsed.offset - segments[index].index;

    ({ text: sentence, offset: wordOffset } = trimTo(sentence, wordOffset));
    ({ text: sentence, offset: wordOffset } = applyRemovals(
      sentence,
      wordOffset,
      Array.from(sentence.matchAll(FOOTNOTE), (m) => [m.index, m.index + m[0].length])
    ));
    ({ text: sentence, offset: wordOffset } = trimTo(sentence, wordOffset));
    ({ text: sentence, offset: wordOffset } = clip(sentence, wordOffset, word.length));

    // Last line of defence: if the offset drifted, re-find the word.
    if (sentence.slice(wordOffset, wordOffset + word.length).toLowerCase() !== word.toLowerCase()) {
      const found = sentence.toLowerCase().indexOf(word.toLowerCase());
      wordOffset = found >= 0 ? found : 0;
    }

    const neighbour = (i) => (segments[i] ? segments[i].text.trim() : '');
    return {
      sentence,
      wordOffset,
      contextBefore: neighbour(index - 1),
      contextAfter: neighbour(index + 1),
      lowConfidence:
        sentence.split(/\s+/).filter(Boolean).length < MIN_WORDS ||
        sentence.toLowerCase().indexOf(word.toLowerCase()) < 0,
    };
  }

  // -- DOM half -------------------------------------------------------

  function isBlockContainer(el) {
    if (!BLOCK_TAGS.has(el.tagName)) return false;
    // A wrapper that holds other blocks is too coarse; descend to the leaf block.
    return !Array.from(el.children).some((child) => BLOCK_TAGS.has(child.tagName));
  }

  function findBlockContainer(node) {
    let el = node.nodeType === 3 ? node.parentElement : node;
    while (el && el !== document.body) {
      if (isBlockContainer(el)) return el;
      el = el.parentElement;
    }
    return (node.nodeType === 3 ? node.parentElement : node) || document.body;
  }

  /** Concatenate the container's text nodes, noting where the anchor sits. */
  function flatten(container, anchorNode, anchorOffset) {
    const walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT, {
      acceptNode(node) {
        const parent = node.parentElement;
        if (!parent || SKIP_TAGS.has(parent.tagName)) return NodeFilter.FILTER_REJECT;
        return NodeFilter.FILTER_ACCEPT;
      },
    });
    let text = '';
    let offset = null;
    let node;
    while ((node = walker.nextNode()) !== null) {
      if (node === anchorNode) offset = text.length + anchorOffset;
      text += node.nodeValue;
    }
    return { text, offset: offset === null ? 0 : offset };
  }

  /** Extract context for the current selection. Returns null if unusable. */
  function extract(selection, word, lang) {
    if (!selection || selection.rangeCount === 0) return null;
    const range = selection.getRangeAt(0);
    const anchor = range.startContainer;
    const container = findBlockContainer(anchor);
    const flat = flatten(container, anchor, range.startOffset);
    if (!flat.text.trim()) return null;
    return extractFromFlat(flat.text, flat.offset, word, lang);
  }

  const api = { extract, extractFromFlat, collapse, applyRemovals, segment, clip, findBlockContainer };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.VocabSentence = api;
})(typeof globalThis !== 'undefined' ? globalThis : this);
