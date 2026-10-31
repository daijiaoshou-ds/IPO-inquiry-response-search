/**
 * 检索 / 上传 / 导出三条流程的「编排」 —— 管按钮状态、加载态、报错提示，
 * 具体取数在 api.js，具体画在 render.js，这里只负责串起来。
 */
import { state, ICON } from './state.js';
import { $, esc, toast } from './dom.js';
import { search, upload, exportGuide } from './api.js';
import { render, skeleton } from './render.js';

/* 检索完把结果条推到顶栏下面：控制台占 400 px，小屏幕上结果全在折线以下，
   不滚一下会让人以为"点了没反应"。只在结果条还没进入视野上半部分时才滚，
   避免用户已经手动滚到某个位置时又被拽走。 */
function focusResults() {
  const el = $('resultbar');
  if (!el.firstChild) { return; }
  const r = el.getBoundingClientRect();
  if (r.top > window.innerHeight * 0.55) {
    const head = parseInt(getComputedStyle(document.documentElement)
      .getPropertyValue('--head-h'), 10) || 62;
    window.scrollTo({ top: Math.max(0, r.top + window.scrollY - head - 14), behavior: 'smooth' });
  }
}

function goIdle() {
  const btn = $('go');
  btn.disabled = false;
  btn.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true">' +
    '<circle cx="10.5" cy="10.5" r="6.2"/><path d="M15.2 15.2 20 20"/></svg>检索';
}

/** 单段问题检索 */
export function doSearch() {
  const q = $('q').value.trim();
  if (!q) { $('q').focus(); return; }
  const btn = $('go');
  btn.disabled = true;
  btn.innerHTML = '<span class="spin"></span>检索中';
  $('resultbar').innerHTML = '';
  state.data = null;
  $('results').innerHTML = skeleton(3);

  search({
    q: q,
    board: $('expand').checked ? null : state.board,
    round: $('round').value ? parseInt($('round').value, 10) : null,
    top: parseInt($('top').value, 10),
    expand: $('expand').checked,
    boilerplate: $('boiler').checked
  }).then((data) => {
    state.filter = 'all';
    state.collapsed = {};
    render(data);
    focusResults();
  }).catch((e) => {
    $('results').innerHTML = '<div class="banner warn">出错了：' + esc(e.message) + '</div>';
  }).then(goIdle);
}

/** 上传整份问询函：解析 → 拆题 → 逐小问检索，通常 20~40 秒 */
export function uploadFile(file) {
  if (!file || state.busy) { return; }
  const mb = file.size / 1048576;
  state.busy = true;
  state.export = null;
  state.data = null;
  $('filemeta').innerHTML = '已选 <b>' + esc(file.name) + '</b>（' + mb.toFixed(1) + ' MB）';
  $('dropzone').classList.add('busy');
  $('resultbar').innerHTML = '';

  const t0 = Date.now();
  $('results').innerHTML =
    '<div class="loading-line"><span class="spin dark"></span>' +
    '正在解析文件、拆题、逐个小问检索… 已用 <b id="el">0</b> 秒' +
    '<span style="color:var(--ink-3)">（整份函通常 20~40 秒）</span></div>' + skeleton(4);
  const timer = setInterval(() => {
    const el = $('el');
    if (el) { el.textContent = ((Date.now() - t0) / 1000).toFixed(0); }
  }, 300);

  upload(file, $('top').value).then((data) => {
    state.filter = 'all';
    state.collapsed = {};
    render(data);
    focusResults();
    $('filemeta').innerHTML = '来自 <b>' + esc(data.source) + '</b>：' +
      (data.questions || []).length + ' 道题 / ' + (data.groups || []).length +
      ' 个小问，用时 ' + (data.elapsed || 0) + ' 秒';
    toast('拆出 ' + (data.questions || []).length + ' 道题，可以导出 Excel 了');
  }).catch((e) => {
    $('results').innerHTML = '<div class="banner warn">出错了：' + esc(e.message) + '</div>';
    $('filemeta').textContent = '';
    toast('上传失败：' + e.message, true);
  }).then(() => {
    clearInterval(timer);
    state.busy = false;
    $('dropzone').classList.remove('busy');
  });
}

/** 下载 Excel 指引（后端把上次上传的结果存了 2 小时，这里是秒回） */
export function downloadExport() {
  const ex = state.export;
  if (!ex) { return; }
  const btn = $('exportbtn');
  if (btn) { btn.disabled = true; btn.innerHTML = '生成中…'; }
  exportGuide(ex.token).then((blob) => {
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = ex.name;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 4000);
    toast('已开始下载：' + ex.name);
  }).catch((e) => {
    toast(e.message, true);
  }).then(() => {
    if (btn) { btn.disabled = false; btn.innerHTML = ICON.dl + ' 导出 Excel 指引'; }
  });
}
