/**
 * 入口：把各层装起来，挂全局事件。
 *
 * 依赖方向是单向的（越往下越不认识上面的东西）：
 *   main → console / search / chrome / gnav
 *   console / search → render → state / dom / api
 * 所以这里可以用 import 而不用担心循环引用；反过来不行，
 * 渲染层想触发动作时只能 document.dispatchEvent 发意图事件。
 */
import { state, ICON } from './state.js';
import { $, esc, num, toast } from './dom.js';
import { fetchStats, fetchSiblings } from './api.js';
import { render } from './render.js';
import { doSearch, uploadFile, downloadExport } from './search.js';
import { setMode, countChars, initConsole } from './console.js';
import { initChrome } from './chrome.js';
import { initGnavScroll } from './gnav.js';

/* ---------- 左栏 / 顶栏的库统计 ---------- */
function loadStats() {
  fetchStats().then((s) => {
    state.stats = s;
    $('ledger').innerHTML =
      '<span>问询函<b>' + num(s.letters) + '</b></span>' +
      '<span>公司<b>' + num(s.company_count) + '</b></span>' +
      '<span>最小 query<b>' + num(s.queries) + '</b></span>';

    const boards = {};
    (s.by_board || []).forEach((b) => { boards[b.board] = b; });

    $('round').innerHTML = ['<option value="">（全部轮次）</option>'].concat(
      (s.rounds || []).map((n) => '<option value="' + n + '">第 ' + n + ' 轮</option>')
    ).join('');

    $('boards').innerHTML = ['北交所', '新三板', '科创板', '沪主板', '创业板', '深主板']
      .map((b) => {
        const ok = (s.boards || []).indexOf(b) >= 0;
        const cnt = boards[b] ? boards[b].letters : 0;
        return '<button class="board' + (b === state.board ? ' on' : '') + '" data-b="' + b + '"' +
          (ok ? '' : ' disabled') + '>' + b + (cnt ? '<em>' + num(cnt) + '</em>' : '') + '</button>';
      }).join('');
    $('boards').querySelectorAll('.board').forEach((btn) => {
      btn.onclick = () => {
        if (btn.disabled) { return; }
        state.board = btn.dataset.b;
        $('boards').querySelectorAll('.board').forEach((o) => o.classList.remove('on'));
        btn.classList.add('on');
        if (state.mode === 'paste' && $('q').value.trim() && state.data) { doSearch(); }
      };
    });

    $('libinfo').innerHTML = [
      ['问询函', '<b>' + num(s.letters) + '</b> 份'],
      ['公司', '<b>' + num(s.company_count) + '</b> 家'],
      ['问题节', '<b>' + num(s.questions) + '</b> 个'],
      ['最小 query', '<b>' + num(s.queries) + '</b> 条'],
      ['原文页数', '<b>' + num(s.pages) + '</b> 页'],
      ['IDF 词表', '<b>' + num(s.grams) + '</b> 个 ' + (s.gram_n || 4) + '-gram']
    ].map((r) => '<div><dt>' + esc(r[0]) + '</dt><dd>' + r[1] + '</dd></div>').join('');

    // 示例问题：点一下就直接搜，省得用户自己想"该输什么"
    const chips = $('chips');
    chips.innerHTML = '<span class="chips-label">试试</span>';
    [
      '说明研发人员认定标准及划分依据，研发人员是否全职从事研发活动并作出实际贡献',
      '说明客户相应第三方回款金额与合同金额、销售收入、应收账款的勾稽关系，是否具备可验证性',
      '说明报告期各期末存货跌价准备的计提依据及充分性'
    ].forEach((t) => {
      const b = document.createElement('button');
      b.textContent = t.slice(0, 24) + '…';
      b.title = t;
      b.onclick = () => { $('q').value = t; countChars(); doSearch(); };
      chips.appendChild(b);
    });
  }).catch(() => {
    $('ledger').innerHTML = '<span style="color:var(--b-mid)">库未连接</span>';
  });
}

/* ---------- 点一下就能做的事：全部走事件委托 ----------
   结果区每次重绘都会换掉整块 DOM，逐个绑监听会泄漏；
   挂在 document 上只绑一次，靠 closest 判断点到谁。 */
function bindDelegates() {
  document.addEventListener('click', (e) => {
    const t = e.target;

    const gmore = t.closest('.g-more');
    if (gmore) {
      const p = gmore.previousElementSibling;
      const open = p.classList.toggle('open');
      gmore.textContent = open ? '收起' : '展开全文';
      return;
    }

    const gt = t.closest('.g-toggle');
    if (gt) {
      const grp = gt.closest('.group');
      const isCol = grp.classList.toggle('collapsed');
      state.collapsed[gt.dataset.g] = isCol;
      gt.setAttribute('aria-expanded', isCol ? 'false' : 'true');
      return;
    }

    const nav = t.closest('.gnav a');
    if (nav) {
      e.preventDefault();
      const target = document.getElementById('g-' + nav.dataset.g);
      if (target) {
        target.scrollIntoView({ behavior: 'smooth', block: 'start' });
        history.replaceState(null, '', '#' + target.id);
      }
      return;
    }

    if (t.id === 'resetf') { state.filter = 'all'; render(state.data); return; }

    const act = t.closest('.act');
    if (!act) { return; }
    const art = act.closest('.hit');

    if (act.dataset.act === 'copy') {
      const quote = art.querySelector('.quote');
      const txt = quote ? quote.textContent : '';
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(txt)
          .then(() => toast('已复制 ' + txt.length + ' 字原文'))
          .catch(() => toast('复制失败，请手动选中', true));
      } else {
        toast('这个浏览器不支持自动复制', true);
      }
      return;
    }

    // 展开 / 收起该问题节的全部小问
    const box = art.querySelector('.sib-wrap');
    const list = art.querySelector('.sib-list');
    if (box.classList.contains('open')) {
      box.classList.remove('open');
      act.classList.remove('on');
      act.setAttribute('aria-expanded', 'false');
      act.innerHTML = ICON.chev + '完整小问';
      return;
    }
    act.classList.add('on');
    act.setAttribute('aria-expanded', 'true');
    act.innerHTML = ICON.chev + '收起';
    if (list.dataset.loaded === '1') { box.classList.add('open'); return; }

    list.innerHTML = '<div class="empty">加载中…</div>';
    box.classList.add('open');
    fetchSiblings(art.dataset.qid).then((d) => {
      list.dataset.loaded = '1';
      list.innerHTML = d.items.length
        ? d.items.map((s) => '<div><b>#' + s.seq_in_question + ' ' + esc(s.asker || '') + '</b>' +
            esc(s.raw_text) + '</div>').join('')
        : '<div class="empty">没有其他小问</div>';
    }).catch(() => {
      list.innerHTML = '<div class="empty">加载失败</div>';
    });
  });

  // 渲染层发出来的意图事件（它不能反向 import 交互层，见 render.js 开头）
  document.addEventListener('qb:export', downloadExport);
  document.addEventListener('qb:mode', (e) => setMode(e.detail || 'paste'));
}

function bindKeyboard() {
  document.addEventListener('keydown', (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
      e.preventDefault();
      if (state.mode === 'paste') { doSearch(); } else { $('file').click(); }
      return;
    }
    const tag = (e.target.tagName || '').toLowerCase();
    const typing = tag === 'input' || tag === 'textarea' || tag === 'select';
    if (e.key === '/' && !typing) { e.preventDefault(); setMode('paste'); }
    if (e.key === 'Escape' && document.activeElement === $('q')) { $('q').blur(); }
  });
}

/* ---------- 装配 ---------- */
initChrome();
initConsole();
initGnavScroll();
bindDelegates();
bindKeyboard();
loadStats();

// 快捷键标注跟着平台走：Mac 上写 Ctrl 会让人多按一次多余的键
if (/Mac|iPhone|iPad/.test(navigator.platform || '')) { $('kbdmod').textContent = '⌘'; }

export { uploadFile };
