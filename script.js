(() => {
  const $ = (s) => document.querySelector(s);
  const $$ = (s) => [...document.querySelectorAll(s)];

  let lang = localStorage.getItem('dailyDuckLang') || 'ja';
  const data = window.DAILY_DUCK_DATA || {};
  const todayData = data.today || null;
  const archiveData = Array.isArray(data.archive) ? data.archive : [];

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

  function renderToday() {
    if (!todayData) {
      const story = $('#todayStory');
      if (story) {
        story.textContent =
          lang === 'ja'
            ? '今日のダックを読み込めませんでした。'
            : "Today's duck could not be loaded.";
      }
      return;
    }

    const image = $('#todayImage');
    image.src = todayData.image;
    image.alt = lang === 'ja' ? todayData.imageAltJa : todayData.imageAltEn;

    const date = $('#todayDate');
    date.textContent = todayData.displayDate;
    date.dateTime = todayData.date;

    $('#todayTitle').textContent = todayData.title;
    $('#todayStory').textContent =
      lang === 'ja' ? todayData.storyJa : todayData.storyEn;
    $('#todayDuck').textContent =
      lang === 'ja' ? todayData.duckJa : todayData.duckEn;

    const source = $('#sourceLink');
    source.href = todayData.sourceUrl;
    $('#sourceText').textContent =
      (lang === 'ja' ? '出典：' : 'Source: ') + todayData.sourceLabel;

    document.title = `The Daily Duck — ${todayData.title}`;
  }

  function renderArchive() {
    const grid = $('#archiveGrid');
    if (!grid) return;

    grid.innerHTML = '';

    archiveData.forEach((item) => {
      const card = document.createElement('article');
      card.className = 'duck-card';

      const imageAlt = lang === 'ja' ? item.imageAltJa : item.imageAltEn;
      const summary =
        lang === 'ja' ? item.archiveSummaryJa : item.archiveSummaryEn;

      card.innerHTML = `
        <div class="duck-card-image">
          <img src="${item.image}" alt="${imageAlt}" loading="lazy">
        </div>
        <div>
          <time datetime="${item.date}">${item.date.replaceAll('-', '.')}</time>
          <h3>${item.title}</h3>
          <p>${summary}</p>
        </div>
      `;

      grid.appendChild(card);
    });
  }

  const langToggle = $('#langToggle');
  if (langToggle) {
    langToggle.addEventListener('click', () => {
      lang = lang === 'ja' ? 'en' : 'ja';
      applyStaticLanguage();
      renderToday();
      renderArchive();
    });
  }

  const shareBtn = $('#shareBtn');
  if (shareBtn) {
    shareBtn.addEventListener('click', async () => {
      if (!todayData) return;

      const shareData = {
        title: `The Daily Duck — ${todayData.title}`,
        text:
          lang === 'ja'
            ? `今日のダックは ${todayData.title} 🐤`
            : `Today's duck is ${todayData.title} 🐤`,
        url: 'https://www.thedailyduck.ai/'
      };

      try {
        if (navigator.share) {
          await navigator.share(shareData);
        } else if (navigator.clipboard) {
          await navigator.clipboard.writeText(shareData.url);
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
  renderToday();
  renderArchive();
})();
