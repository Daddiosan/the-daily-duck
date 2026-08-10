(() => {
  const $ = (s) => document.querySelector(s);
  const $$ = (s) => [...document.querySelectorAll(s)];
  let lang = localStorage.getItem('dailyDuckLang') || 'ja';
  let todayData = null;
  let archiveData = [];

  const translatable = $$('[data-ja][data-en]');

  function applyStaticLanguage() {
    document.documentElement.lang = lang;
    translatable.forEach(el => el.textContent = el.dataset[lang]);
    $('#langToggle').textContent = lang === 'ja' ? 'EN' : 'JP';
    localStorage.setItem('dailyDuckLang', lang);
  }

  function renderToday() {
    if (!todayData) return;
    $('#todayImage').src = todayData.image;
    $('#todayImage').alt = lang === 'ja' ? todayData.imageAltJa : todayData.imageAltEn;
    $('#todayDate').textContent = todayData.displayDate;
    $('#todayDate').dateTime = todayData.date;
    $('#todayTitle').textContent = todayData.title;
    $('#todayStory').textContent = lang === 'ja' ? todayData.storyJa : todayData.storyEn;
    $('#todayDuck').textContent = lang === 'ja' ? todayData.duckJa : todayData.duckEn;
    $('#sourceLink').href = todayData.sourceUrl;
    $('#sourceText').textContent = (lang === 'ja' ? '出典：' : 'Source: ') + todayData.sourceLabel;
    document.title = `The Daily Duck — ${todayData.title}`;
  }

  function renderArchive() {
    const grid = $('#archiveGrid');
    grid.innerHTML = '';
    archiveData.forEach(item => {
      const card = document.createElement('article');
      card.className = 'duck-card';
      card.innerHTML = `
        <div class="duck-card-image">
          <img src="${item.image}" alt="${lang === 'ja' ? item.imageAltJa : item.imageAltEn}" loading="lazy">
        </div>
        <div>
          <time datetime="${item.date}">${item.date.replaceAll('-', '.')}</time>
          <h3>${item.title}</h3>
          <p>${lang === 'ja' ? item.archiveSummaryJa : item.archiveSummaryEn}</p>
        </div>`;
      grid.appendChild(card);
    });
  }

  async function loadData() {
    try {
      const [todayRes, archiveRes] = await Promise.all([
        fetch('data/today.json', { cache: 'no-store' }),
        fetch('data/archive.json', { cache: 'no-store' })
      ]);
      todayData = await todayRes.json();
      archiveData = await archiveRes.json();
      renderToday();
      renderArchive();
    } catch (e) {
      console.error('Could not load Daily Duck data', e);
      $('#todayStory').textContent = 'Could not load today’s duck.';
    }
  }

  $('#langToggle').addEventListener('click', () => {
    lang = lang === 'ja' ? 'en' : 'ja';
    applyStaticLanguage();
    renderToday();
    renderArchive();
  });

  $('#shareBtn').addEventListener('click', async () => {
    if (!todayData) return;
    const data = {
      title: `The Daily Duck — ${todayData.title}`,
      text: lang === 'ja' ? `今日のダックは ${todayData.title} 🐤` : `Today's duck is ${todayData.title} 🐤`,
      url: 'https://www.thedailyduck.ai/'
    };
    try {
      if (navigator.share) {
        await navigator.share(data);
      } else {
        await navigator.clipboard.writeText(data.url);
        $('#shareStatus').textContent = lang === 'ja' ? 'URLをコピーしました' : 'URL copied';
      }
    } catch (e) {}
  });

  applyStaticLanguage();
  loadData();
})();