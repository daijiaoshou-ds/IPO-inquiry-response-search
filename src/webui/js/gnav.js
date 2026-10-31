/**
 * 题号跳转条（结果条下面那一排「题1-1 题1-2 …」）。
 *
 * 两个职责：
 *   wireGnav()        每次重绘后重新绑定：箭头、拖拽、滚轮、两端渐隐
 *   initGnavScroll()  全局只注册一次：页面滚动时高亮「当前在哪一组」
 *
 * 为什么不能只靠 overflow-x：原生横向滚动只认触控板手势和 Shift+滚轮，
 * 用普通鼠标的同事滚不动，右边几十个题就永远点不到。
 */
import { $ } from './dom.js';

var gnavSync = null;

export function wireGnav() {
  var g = $('gnav');
  if (!g) { gnavSync = null; return; }
  var L = $('gnavL'), R = $('gnavR');

  function maxLeft() { return Math.max(0, g.scrollWidth - g.clientWidth); }
  function sync() {
    var m = maxLeft(), has = m > 4, atL = g.scrollLeft <= 2, atR = g.scrollLeft >= m - 2;
    if (L) { L.hidden = !has || atL; }
    if (R) { R.hidden = !has || atR; }
    g.classList.toggle('ov-l', has && !atL);
    g.classList.toggle('ov-r', has && !atR);
  }
  gnavSync = sync;

  function step(dir) {
    g.scrollBy({ left: dir * Math.max(160, g.clientWidth * 0.7), behavior: 'smooth' });
    setTimeout(sync, 380);            // 平滑滚动没有 scroll 事件也要能更新箭头
  }
  if (L) { L.onclick = function () { step(-1); }; }
  if (R) { R.onclick = function () { step(1); }; }
  g.addEventListener('scroll', sync, { passive: true });

  // 按住拖（只认鼠标：触屏的横向滑动浏览器自己就管，别再抢一遍）
  var drag = null, moved = false;
  g.addEventListener('pointerdown', function (e) {
    if (e.pointerType !== 'mouse' || e.button !== 0) { return; }
    drag = { x: e.clientX, left: g.scrollLeft };
    moved = false;
  });
  g.addEventListener('pointermove', function (e) {
    if (!drag) { return; }
    var dx = e.clientX - drag.x;
    if (!moved && Math.abs(dx) < 4) { return; }
    if (!moved) { moved = true; g.classList.add('dragging'); }
    g.scrollLeft = drag.left - dx;
    sync();
  });
  function endDrag() {
    if (!drag) { return; }
    drag = null;
    g.classList.remove('dragging');
    sync();
    setTimeout(function () { moved = false; }, 0);   // 让紧随其后的 click 先读到 moved
  }
  g.addEventListener('pointerup', endDrag);
  g.addEventListener('pointerleave', endDrag);
  g.addEventListener('pointercancel', endDrag);
  // 拖完手一松别顺手把某一题点开
  g.addEventListener('click', function (e) {
    if (moved) { e.preventDefault(); e.stopPropagation(); }
  }, true);

  // 竖向滚轮转横向：还能往那个方向滚才接管，到头就放行给页面继续上下滑
  g.addEventListener('wheel', function (e) {
    if (Math.abs(e.deltaX) > Math.abs(e.deltaY)) { return; }
    var m = maxLeft();
    if (m <= 0) { return; }
    if ((e.deltaY < 0 && g.scrollLeft <= 0) || (e.deltaY > 0 && g.scrollLeft >= m)) {
      sync();                                   // 到头了，箭头该换边了
      return;
    }
    e.preventDefault();
    g.scrollLeft = Math.max(0, Math.min(m, g.scrollLeft + e.deltaY));
    sync();
  }, { passive: false });

  sync();
  requestAnimationFrame(sync);
}
window.addEventListener('resize', function () { if (gnavSync) { gnavSync(); } });

/**
 * 页面滚动时把「当前组」的题号高亮，并把它滚到条中间。
 * 只注册一次（main.js 启动时），因为条本身每次重绘都会重建，监听挂在 window 上。
 */
export function initGnavScroll() {
  let ticking = false;
  window.addEventListener('scroll', () => {
    if (ticking) { return; }
    ticking = true;
    requestAnimationFrame(() => {
      ticking = false;
      const chips = document.querySelectorAll('.gnav a');
      if (!chips.length) { return; }
      let cur = null;
      document.querySelectorAll('.group').forEach((g) => {
        if (g.getBoundingClientRect().top < 150) { cur = g.id.replace('g-', ''); }
      });
      chips.forEach((c) => {
        const on = cur === c.dataset.g;
        if (on && !c.classList.contains('on')) {
          c.classList.add('on');
          // 手动滚容器，别用 scrollIntoView —— 那会把整页也带着跳一下。
          // 位置用 rect 算，不用 offsetLeft：offsetParent 是 sticky 的 .rbar，
          // offsetLeft 里混着箭头和 padding 的宽度，算出来会偏。
          const box = c.parentNode;
          const cr = c.getBoundingClientRect(), br = box.getBoundingClientRect();
          const delta = (cr.left - br.left) - (box.clientWidth - c.offsetWidth) / 2;
          box.scrollTo({ left: Math.max(0, box.scrollLeft + delta), behavior: 'smooth' });
        } else if (!on) {
          c.classList.remove('on');
        }
      });
    });
  }, { passive: true });
}
