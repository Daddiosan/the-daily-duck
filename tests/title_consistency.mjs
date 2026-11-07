import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source = fs.readFileSync(new URL('../script.js', import.meta.url), 'utf8');
const today = {
  date: '2026-11-06', displayDate: 'November 6, 2026', published: true,
  title: 'DUCK NAME', duckName: 'Duck Name', storyTitleEn: 'Chill, Duck! & "Fly"',
  storyTitleJa: '日本語タイトル', storyJa: '日本語本文', storyEn: 'English story',
  duckJa: '日本語コメント', duckEn: 'English comment',
  image: 'image.png', imageAltJa: '画像', imageAltEn: 'Image',
  archiveSummaryJa: '日本語要約', archiveSummaryEn: 'English summary',
  sourceUrl: 'https://example.com', sourceLabel: 'Example',
};
const old = {...today, date: '2026-11-05', title: 'OTHER DUCK', storyTitleEn: 'Yesterday Has Its Own Title'};
const {storyTitleEn: omittedTitle, ...legacyFields} = today;
const legacy = {...legacyFields, date: '2026-11-04', title: 'Legacy Duck'};
assert.equal(Object.hasOwn(legacy, 'storyTitleEn'), false);
const archive = [today, old, legacy];
const cases = [
  {home: today, requested: '', selected: today, title: today.storyTitleEn},
  {home: today, requested: '?date=2026-11-05', selected: old, title: old.storyTitleEn},
  {home: today, requested: '?date=2026-11-04', selected: legacy, title: legacy.title},
  {home: legacy, requested: '', selected: legacy, title: legacy.title},
];
for (const lang of ['en', 'ja']) {
  for (const {home, requested, selected, title} of cases) {
    const data = structuredClone({today: home, archive});
    const before = JSON.stringify(data);
    const elements = new Map();
    const element = selector => {
      if (!elements.has(selector)) elements.set(selector, {
        textContent: '', innerHTML: '', attributes: {}, dataset: {}, listeners: {},
        setAttribute(k, v) { this.attributes[k] = v; },
        addEventListener(k, fn) { this.listeners[k] = fn; },
      });
      return elements.get(selector);
    };
    const shares = [];
    const document = {title: '', documentElement: {}, querySelector: element, querySelectorAll: () => []};
    vm.runInNewContext(source, {window: {DAILY_DUCK_DATA: data}, document, URL, URLSearchParams,
      location: {search: requested}, localStorage: {getItem: () => lang, setItem() {}},
      navigator: {share: async payload => { shares.push(payload); }}});
    for (const activeLang of [lang, lang === 'en' ? 'ja' : 'en']) {
      assert.equal(element('#todayTitle').textContent, title);
      assert.equal(element('#todayStory').textContent, activeLang === 'ja' ? selected.storyJa : selected.storyEn);
      assert.equal(element('#todayDuck').textContent, activeLang === 'ja' ? selected.duckJa : selected.duckEn);
      assert.equal(document.title, `The Daily Duck — ${title}`);
      for (const selector of ['meta[property="og:title"]', 'meta[name="twitter:title"]']) {
        assert.equal(element(selector).attributes.content, `The Daily Duck — ${title}`);
      }
      const archiveHtml = element('#archiveGrid').innerHTML;
      assert.ok(archiveHtml.includes('<h3>Chill, Duck! &amp; &quot;Fly&quot;</h3>'));
      assert.ok(archiveHtml.includes('aria-label="Chill, Duck! &amp; &quot;Fly&quot; 2026-11-06"'));
      assert.ok(archiveHtml.includes(`<h3>${old.storyTitleEn}</h3>`));
      assert.ok(archiveHtml.includes(`<h3>${legacy.title}</h3>`));
      assert.ok(archiveHtml.includes(`aria-label="${legacy.title} ${legacy.date}"`));
      const shareCount = shares.length;
      await element('#shareBtn').listeners.click();
      assert.equal(shares.length, shareCount + 1, 'share click must call navigator.share');
      const share = shares.at(-1);
      assert.equal(share.title, `The Daily Duck — ${title}`);
      assert.equal(share.text, activeLang === 'ja'
        ? `${selected.displayDate} のダックは ${selected.title} 🐤`
        : `${selected.displayDate}'s duck is ${selected.title} 🐤`);
      assert.equal(share.url, selected.date === home.date
        ? 'https://www.thedailyduck.ai/'
        : `https://www.thedailyduck.ai/?date=${selected.date}`);
      assert.equal(JSON.stringify(data), before, 'published data and Duck names remain unchanged');
      if (activeLang === lang) element('#langToggle').listeners.click();
    }
  }
}
console.log('title consistency: 8 language/date cases PASS');
