/**
 * Unit tests for the text half of the extension's sentence extraction (§6.3).
 *
 *   node tests/test_sentence.js
 */
const assert = require('assert');
const S = require('../extension/sentence.js');

let failures = 0;
function check(label, fn) {
  try {
    fn();
    console.log(`  [\x1b[32mPASS\x1b[0m] ${label}`);
  } catch (err) {
    failures++;
    console.log(`  [\x1b[31mFAIL\x1b[0m] ${label}\n        ${err.message.split('\n')[0]}`);
  }
}

/** Helper: build flat text with the target marked by | and return the extraction. */
function extract(marked, word, lang) {
  const offset = marked.indexOf('|');
  return S.extractFromFlat(marked.replace('|', ''), offset, word, lang);
}

console.log('sentence boundaries');

check('picks the sentence containing the word', () => {
  const r = extract('First one here. Smartphones are now |ubiquitous today. And a third.', 'ubiquitous');
  assert.strictEqual(r.sentence, 'Smartphones are now ubiquitous today.');
  assert.strictEqual(r.sentence.slice(r.wordOffset, r.wordOffset + 10), 'ubiquitous');
});

check('does not split on Mr. / U.S. / e.g.', () => {
  const r = extract('Mr. Smith of the U.S. said it was |ubiquitous, e.g. everywhere.', 'ubiquitous');
  assert.ok(r.sentence.startsWith('Mr. Smith'), r.sentence);
  assert.ok(r.sentence.includes('e.g. everywhere'), r.sentence);
});

check('captures the neighbouring sentences', () => {
  const r = extract('Tech changed fast. It is |ubiquitous now. New questions arise.', 'ubiquitous');
  assert.strictEqual(r.contextBefore, 'Tech changed fast.');
  assert.strictEqual(r.contextAfter, 'New questions arise.');
});

console.log('offset tracking');

check('resolves the second occurrence, not the first', () => {
  const r = extract('A ubiquitous problem: |ubiquitous is hard to spell.', 'ubiquitous');
  assert.strictEqual(r.sentence, 'A ubiquitous problem: ubiquitous is hard to spell.');
  assert.strictEqual(r.wordOffset, 22, `got ${r.wordOffset}`);
  assert.strictEqual(r.sentence.slice(r.wordOffset, r.wordOffset + 10), 'ubiquitous');
});

check('survives collapsed whitespace and newlines', () => {
  const r = extract('The   device\n\n  became  |ubiquitous   within\ta decade.', 'ubiquitous');
  assert.strictEqual(r.sentence, 'The device became ubiquitous within a decade.');
  assert.strictEqual(r.sentence.slice(r.wordOffset, r.wordOffset + 10), 'ubiquitous');
});

check('strips zero-width characters', () => {
  const r = extract('It is ​now |ubiquitous​ here.', 'ubiquitous');
  assert.ok(!/[​]/.test(r.sentence), JSON.stringify(r.sentence));
  assert.strictEqual(r.sentence.slice(r.wordOffset, r.wordOffset + 10), 'ubiquitous');
});

check('strips footnote markers and keeps the offset', () => {
  const r = extract('Studies[1] show it is |ubiquitous[23] today.', 'ubiquitous');
  assert.strictEqual(r.sentence, 'Studies show it is ubiquitous today.');
  assert.strictEqual(r.sentence.slice(r.wordOffset, r.wordOffset + 10), 'ubiquitous');
});

check('handles a word at the very start of the block', () => {
  const r = extract('|Ubiquitous devices are everywhere.', 'Ubiquitous');
  assert.strictEqual(r.wordOffset, 0);
  assert.strictEqual(r.sentence, 'Ubiquitous devices are everywhere.');
});

check('handles leading whitespace in the block', () => {
  const r = extract('\n\n   It is |ubiquitous here.', 'ubiquitous');
  assert.strictEqual(r.sentence, 'It is ubiquitous here.');
  assert.strictEqual(r.sentence.slice(r.wordOffset, r.wordOffset + 10), 'ubiquitous');
});

console.log('clipping and confidence');

check('clips an over-long sentence around the word', () => {
  const filler = 'padding words here '.repeat(40); // ~760 chars
  const r = extract(`${filler}and then |ubiquitous ${filler}`, 'ubiquitous');
  assert.ok(r.sentence.length <= 420, `length ${r.sentence.length}`);
  assert.ok(r.sentence.startsWith('…') && r.sentence.endsWith('…'), r.sentence);
  assert.strictEqual(r.sentence.slice(r.wordOffset, r.wordOffset + 10), 'ubiquitous');
});

check('flags a too-short fragment as low confidence', () => {
  const r = extract('|Home', 'Home');
  assert.strictEqual(r.lowConfidence, true);
});

check('normal sentence is not flagged', () => {
  const r = extract('It is |ubiquitous in daily life.', 'ubiquitous');
  assert.strictEqual(r.lowConfidence, false);
});

console.log('chinese');

check('segments chinese by 。', () => {
  const r = extract('这是第一句。智能手机现在|无处不在。还有第三句。', '无处不在', 'zh');
  assert.strictEqual(r.sentence, '智能手机现在无处不在。');
  assert.strictEqual(r.sentence.slice(r.wordOffset, r.wordOffset + 4), '无处不在');
});

console.log('regex fallback (no Intl.Segmenter)');

check('falls back when Intl.Segmenter is unavailable', () => {
  const realIntl = global.Intl;
  global.Intl = { ...realIntl, Segmenter: undefined };
  delete global.Intl.Segmenter;
  try {
    const r = extract('First one here. Smartphones are now |ubiquitous today. Third.', 'ubiquitous');
    assert.strictEqual(r.sentence, 'Smartphones are now ubiquitous today.');
    assert.strictEqual(r.sentence.slice(r.wordOffset, r.wordOffset + 10), 'ubiquitous');
  } finally {
    global.Intl = realIntl;
  }
});

console.log();
if (failures) {
  console.log(`${failures} check(s) failed`);
  process.exit(1);
}
console.log('sentence extraction: all checks passed');
