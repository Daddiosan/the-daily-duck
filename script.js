let lang='ja';
const toggle=document.getElementById('langToggle');
function setLang(next){lang=next;document.documentElement.lang=lang;document.querySelectorAll('[data-ja][data-en]').forEach(el=>{el.textContent=el.dataset[lang]});toggle.textContent=lang==='ja'?'EN':'JP';}
toggle.addEventListener('click',()=>setLang(lang==='ja'?'en':'ja'));
document.getElementById('shareBtn').addEventListener('click',async()=>{const data={title:'The Daily Duck — QUACKSTRONAUT',text:'One day. One story. One duck. Today: QUACKSTRONAUT 🐤🚀',url:location.href};const status=document.getElementById('shareStatus');try{if(navigator.share){await navigator.share(data)}else{await navigator.clipboard.writeText(location.href);status.textContent=lang==='ja'?'URLをコピーしました':'Link copied';setTimeout(()=>status.textContent='',2200)}}catch(e){}});
