(() => {
  const $ = (s) => document.querySelector(s);
  const $$ = (s) => [...document.querySelectorAll(s)];

  let lang = localStorage.getItem('dailyDuckLang') || 'ja';
  const data = window.DAILY_DUCK_DATA || {};
  const archiveAll = Array.isArray(data.archive) ? data.archive : [];
  const publishedArchive = archiveAll.filter(item => item.published !== false);
  const byDate = new Map(archiveAll.map(item => [item.date, item]));

  const params = new URLSearchParams(location.search);
  const requestedDate = params.get('date');

  let currentData =
    (requestedDate && byDate.get(requestedDate)?.published !== false)
      ? byDate.get(requestedDate)
      : data.today || publishedArchive[0] || null;

  const translatable = $$('[data-ja][data-en]');

  function applyStaticLanguage() {
    document.documentElement.lang = lang;
    translatable.forEach((el) => {
      el.textContent = el.dataset[lang];
    });

    const toggle = $('#langToggle');
    if (toggle) {
      toggle.textContent = lang === 'ja' ? 'EN' : 'JP';
      toggle.setAttribute(
        'aria-label',
        lang === 'ja' ? 'Switch to English' : '日本語に切り替える'
      );
    }

    localStorage.setItem('dailyDuckLang', lang);
  }

  function renderCurrent() {
    if (!currentData) return;

    const image = $('#todayImage');
    image.src = currentData.image;
    image.alt = lang === 'ja' ? currentData.imageAltJa : currentData.imageAltEn;

    const date = $('#todayDate');
    date.textContent = currentData.displayDate;
    date.dateTime = currentData.date;

    $('#todayTitle').textContent = currentData.title;
    $('#todayStory').textContent =
      lang === 'ja' ? currentData.storyJa : currentData.storyEn;
    $('#todayDuck').textContent =
      lang === 'ja' ? currentData.duckJa : currentData.duckEn;

    const source = $('#sourceLink');
    source.href = currentData.sourceUrl;
    $('#sourceText').textContent =
      (lang === 'ja' ? '出典：' : 'Source: ') + currentData.sourceLabel;

    const todayLabel = document.querySelector('.eyebrow');
    if (todayLabel) {
      const isToday = currentData.date === data.today?.date;
      todayLabel.textContent = isToday
        ? (lang === 'ja' ? '🐤 今日のダック' : "🐤 TODAY'S DUCK")
        : (lang === 'ja' ? '🐤 アーカイブのダック' : '🐤 ARCHIVE DUCK');
    }

    document.title = `The Daily Duck — ${currentData.title}`;
  }

  function archiveCard(item) {
    const summary = lang === 'ja' ? item.archiveSummaryJa : item.archiveSummaryEn;
    const imageAlt = lang === 'ja' ? item.imageAltJa : item.imageAltEn;
    const href = `/ducks/${encodeURIComponent(item.date)}/`;

    return `
      <article class="duck-card">
        <a class="duck-card-link" href="${href}" aria-label="${item.title} ${item.date}">
          <div class="duck-card-image">
            <img src="${item.image}" alt="${imageAlt}" loading="lazy">
          </div>
          <div>
            <time datetime="${item.date}">${item.date.replaceAll('-', '.')}</time>
            <h3>${item.title}</h3>
            <p>${summary}</p>
            <span class="view-story">${lang === 'ja' ? 'この日のページを見る →' : 'View this day →'}</span>
          </div>
        </a>
      </article>
    `;
  }

  function renderArchive() {
    const grid = $('#archiveGrid');
    if (!grid) return;
    grid.innerHTML = publishedArchive.map(archiveCard).join('');
  }


  function updateSocialMeta() {
    if (!currentData) return;
    const pageUrl = currentData.date === data.today?.date
      ? 'https://www.thedailyduck.ai/'
      : `https://www.thedailyduck.ai/?date=${currentData.date}`;
    const imageUrl = new URL(currentData.image, 'https://www.thedailyduck.ai/').href;
    const desc = lang === 'ja' ? currentData.archiveSummaryJa : currentData.archiveSummaryEn;
    const setMeta = (selector, value) => {
      const el = document.querySelector(selector);
      if (el) el.setAttribute('content', value);
    };
    setMeta('meta[property="og:title"]', `The Daily Duck — ${currentData.title}`);
    setMeta('meta[property="og:description"]', desc);
    setMeta('meta[property="og:url"]', pageUrl);
    setMeta('meta[property="og:image"]', imageUrl);
    setMeta('meta[property="og:image:alt"]', lang === 'ja' ? currentData.imageAltJa : currentData.imageAltEn);
    setMeta('meta[name="twitter:title"]', `The Daily Duck — ${currentData.title}`);
    setMeta('meta[name="twitter:description"]', desc);
    setMeta('meta[name="twitter:image"]', imageUrl);
  }

  function updateCanonical() {
    const canonical = document.querySelector('link[rel="canonical"]');
    if (!canonical || !currentData) return;
    canonical.href = currentData.date === data.today?.date
      ? 'https://www.thedailyduck.ai/'
      : `https://www.thedailyduck.ai/?date=${currentData.date}`;
  }

  const langToggle = $('#langToggle');
  if (langToggle) {
    langToggle.addEventListener('click', () => {
      lang = lang === 'ja' ? 'en' : 'ja';
      applyStaticLanguage();
      renderCurrent();
      renderArchive();
      updateSocialMeta();
    });
  }

  const shareBtn = $('#shareBtn');
  if (shareBtn) {
    shareBtn.addEventListener('click', async () => {
      if (!currentData) return;
      const url = currentData.date === data.today?.date
        ? 'https://www.thedailyduck.ai/'
        : `https://www.thedailyduck.ai/?date=${currentData.date}`;

      const shareData = {
        title: `The Daily Duck — ${currentData.title}`,
        text:
          lang === 'ja'
            ? `${currentData.displayDate} のダックは ${currentData.title} 🐤`
            : `${currentData.displayDate}'s duck is ${currentData.title} 🐤`,
        url
      };

      try {
        if (navigator.share) {
          await navigator.share(shareData);
        } else if (navigator.clipboard) {
          await navigator.clipboard.writeText(url);
          $('#shareStatus').textContent =
            lang === 'ja' ? 'URLをコピーしました' : 'URL copied';
        }
      } catch (error) {
        if (error?.name !== 'AbortError' && $('#shareStatus')) {
          $('#shareStatus').textContent =
            lang === 'ja' ? 'シェアできませんでした' : 'Could not share';
        }
      }
    });
  }

  applyStaticLanguage();
  renderCurrent();
  renderArchive();
  updateCanonical();
  updateSocialMeta();
})();
