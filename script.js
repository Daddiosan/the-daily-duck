(() => {
  const toggle = document.getElementById('langToggle');
  const shareBtn = document.getElementById('shareBtn');
  const shareStatus = document.getElementById('shareStatus');
  const translatable = [...document.querySelectorAll('[data-ja][data-en]')];

  let lang = localStorage.getItem('dailyDuckLang') || 'ja';

  function applyLanguage(nextLang) {
    lang = nextLang;
    document.documentElement.lang = lang;

    translatable.forEach((el) => {
      el.textContent = el.dataset[lang];
    });

    toggle.textContent = lang === 'ja' ? 'EN' : 'JP';
    toggle.setAttribute(
      'aria-label',
      lang === 'ja' ? 'Switch to English' : '日本語に切り替える'
    );

    localStorage.setItem('dailyDuckLang', lang);
  }

  toggle.addEventListener('click', () => {
    applyLanguage(lang === 'ja' ? 'en' : 'ja');
  });

  applyLanguage(lang);

  shareBtn.addEventListener('click', async () => {
    const shareData = {
      title: 'The Daily Duck — QUACKSTRONAUT',
      text: lang === 'ja'
        ? '今日のダックは QUACKSTRONAUT 🐤🚀'
        : "Today's duck is QUACKSTRONAUT 🐤🚀",
      url: 'https://www.thedailyduck.ai/'
    };

    try {
      if (navigator.share) {
        await navigator.share(shareData);
        shareStatus.textContent = '';
      } else {
        await navigator.clipboard.writeText(shareData.url);
        shareStatus.textContent =
          lang === 'ja' ? 'URLをコピーしました' : 'URL copied';
      }
    } catch (error) {
      if (error?.name !== 'AbortError') {
        shareStatus.textContent =
          lang === 'ja' ? 'シェアできませんでした' : 'Could not share';
      }
    }
  });
})();
