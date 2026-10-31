/**
 * DOM 小工具。没有依赖，谁都可以引。
 */

/** 按 id 取元素。页面里所有元素 id 都是唯一的，这个足够。 */
export const $ = (id) => document.getElementById(id);

/** 转义成能塞进 innerHTML 的文本。所有拼字符串的地方都必须过一遍。 */
export function esc(s) {
  return String(s === null || s === undefined ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

/** 1234567 → 1,234,567 */
export function num(n) {
  return String(n === null || n === undefined ? 0 : n).replace(/\B(?=(\d{3})+(?!\d))/g, ',');
}

/** 底部一闪而过的提示。不抢焦点、不阻塞，2 秒后自己消失。 */
export function toast(msg, bad) {
  const t = document.createElement('div');
  t.className = 'toast' + (bad ? ' bad' : '');
  t.textContent = msg;
  document.body.appendChild(t);
  requestAnimationFrame(() => t.classList.add('on'));
  setTimeout(() => {
    t.classList.remove('on');
    setTimeout(() => t.remove(), 240);
  }, bad ? 4400 : 2100);
}
