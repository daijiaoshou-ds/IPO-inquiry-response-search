/**
 * 页面外壳的行为：主题切换、侧栏宽度拖拽 / 收起、回到顶部、顶栏滚动投影。
 *
 * 这几件事都只跟 <body> 和固定几个元素打交道，不碰结果数据，所以单独一层。
 * 入口是 initChrome()，由 main.js 在启动时调一次。
 */
import { $ } from './dom.js';
import { ICON } from './state.js';

/* ---------- 主题：跟随系统 → 浅色 → 深色 ----------
   首屏的 data-theme 已经在 <head> 的内联脚本里定好了（避免刷新白闪），
   这里只负责把按钮画对、把切换接上。 */
function initTheme() {
  const order = ['auto', 'light', 'dark'];
  const btn = $('theme');
  const LABEL = { auto: '跟随系统', light: '浅色', dark: '深色' };

  function paint() {
    const pref = document.documentElement.dataset.pref || 'auto';
    btn.innerHTML = pref === 'auto' ? ICON.auto : (pref === 'dark' ? ICON.moon : ICON.sun);
    btn.title = '主题：' + LABEL[pref] + '（点一下切换）';
  }
  function apply(pref) {
    const dark = pref === 'dark' ||
      (pref === 'auto' && window.matchMedia('(prefers-color-scheme: dark)').matches);
    document.documentElement.dataset.theme = dark ? 'dark' : 'light';
    document.documentElement.dataset.pref = pref;
    try { localStorage.setItem('theme', pref); } catch (e) { /* 隐私模式 */ }
    paint();
  }

  btn.onclick = () => {
    const cur = document.documentElement.dataset.pref || 'auto';
    apply(order[(order.indexOf(cur) + 1) % order.length]);
  };
  window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
    if ((document.documentElement.dataset.pref || 'auto') === 'auto') { apply('auto'); }
  });
  paint();
}

/* ---------- 侧栏：宽度可拖 / 可收起 ----------
   键名 asideW2 / asideOff 是老版本留下的，别改名，否则用户上次拖的宽度会丢一次。 */
function initRail() {
  const MIN = 232, MAX = 520, DEF = 292;
  const root = document.documentElement;
  const gut = $('gutter');
  const rail = document.querySelector('.rail');

  let saved = null;
  try { saved = parseInt(localStorage.getItem('asideW2'), 10); } catch (e) { /* 忽略 */ }
  if (saved >= MIN && saved <= MAX) { root.style.setProperty('--rail-w', saved + 'px'); }

  function setW(v) {
    v = Math.max(MIN, Math.min(MAX, Math.round(v)));
    root.style.setProperty('--rail-w', v + 'px');
    try { localStorage.setItem('asideW2', String(v)); } catch (e) { /* 忽略 */ }
  }
  function setOff(off) {
    document.body.classList.toggle('rail-off', off);
    gut.title = off ? '点一下展开侧栏' : '拖动调整宽度；双击复位';
    try { localStorage.setItem('asideOff', off ? '1' : '0'); } catch (e) { /* 忽略 */ }
  }
  try { setOff(localStorage.getItem('asideOff') === '1'); } catch (e) { /* 忽略 */ }
  $('hideRail').onclick = () => setOff(true);

  let drag = null;
  gut.addEventListener('pointerdown', (e) => {
    if (document.body.classList.contains('rail-off')) { return; }
    drag = { x: e.clientX, w: rail.getBoundingClientRect().width };
    gut.setPointerCapture(e.pointerId);
    gut.classList.add('dragging');
    document.body.classList.add('resizing');
    e.preventDefault();
  });
  gut.addEventListener('pointermove', (e) => {
    if (drag) { setW(drag.w + (e.clientX - drag.x)); }
  });
  function end(e) {
    if (!drag) { return; }
    drag = null;
    gut.classList.remove('dragging');
    document.body.classList.remove('resizing');
    try { gut.releasePointerCapture(e.pointerId); } catch (err) { /* 已释放 */ }
  }
  gut.addEventListener('pointerup', end);
  gut.addEventListener('pointercancel', end);
  gut.addEventListener('click', () => {
    if (document.body.classList.contains('rail-off')) { setOff(false); }
  });
  gut.addEventListener('keydown', (e) => {
    if (document.body.classList.contains('rail-off') &&
        (e.key === 'Enter' || e.key === ' ' || e.key === 'ArrowLeft')) {
      setOff(false);
      e.preventDefault();
      return;
    }
    const cur = rail.getBoundingClientRect().width;
    if (e.key === 'ArrowLeft') { setW(cur - 20); e.preventDefault(); }
    else if (e.key === 'ArrowRight') { setW(cur + 20); e.preventDefault(); }
  });
  gut.addEventListener('dblclick', () => {
    root.style.setProperty('--rail-w', DEF + 'px');
    try { localStorage.removeItem('asideW2'); } catch (e) { /* 忽略 */ }
  });
}

/* ---------- 回到顶部 + 顶栏投影 ---------- */
function initScrollChrome() {
  const top = $('totop');
  const bar = document.querySelector('.topbar');
  let ticking = false;
  window.addEventListener('scroll', () => {
    if (ticking) { return; }
    ticking = true;
    requestAnimationFrame(() => {
      ticking = false;
      const y = window.scrollY;
      top.classList.toggle('on', y > 620);
      bar.classList.toggle('scrolled', y > 6);
    });
  }, { passive: true });
  top.onclick = () => window.scrollTo({ top: 0, behavior: 'smooth' });
}

export function initChrome() {
  initTheme();
  initRail();
  initScrollChrome();
}
