/**
 * 结果渲染：把一份 /api/search | /api/upload 的返回变成 DOM。
 *
 * 这一层是纯函数式的「数据 → HTML 字符串」，只在最后统一塞进 #results。
 * 它不 import 交互层（search.js / console.js），需要用户操作时只发自定义事件：
 *   qb:export  —— 点「导出 Excel 指引」
 *   qb:mode    —— 点「上传整份函」跳模式（detail 是目标模式）
 * 这样依赖方向永远是 交互 → 渲染，不会绕回来。
 */
import { state, ICON, bandOf } from './state.js';
import { $, esc, num } from './dom.js';
import { wireGnav } from './gnav.js';

/* ---------- 渲染：标黄 ---------- */
function markSpans(t, spans) {
  t = t || '';
  var out = [], pos = 0;
  (spans || []).forEach(function (s) {
    var a = s[0], b = s[1];
    if (a < pos || b > t.length || b <= a) { return; }
    if (a > pos) { out.push(esc(t.slice(pos, a))); }
    out.push('<mark>' + esc(t.slice(a, b)) + '</mark>');
    pos = b;
  });
  out.push(esc(t.slice(pos)));
  return out.join('');
}
function mark(h) {
  var spans = h.hl_spans;
  if (!spans || !spans.length) {
    spans = (h.hl_end > h.hl_start) ? [[h.hl_start, h.hl_end]] : [];
  }
  return markSpans(h.raw_text, spans);
}

/* ---------- 渲染：落点 ---------- */
function coverBlock(g) {
  var c = g.cover, t = g.text || '';
  if (!c || !t || !c.strong || !c.weak) { return ''; }
  if (!c.strong.length && !c.weak.length) { return ''; }
  var lvl = [], i, k;
  for (i = 0; i < t.length; i++) { lvl[i] = 0; }
  c.strong.forEach(function (s) { for (k = s[0]; k < s[1] && k < t.length; k++) { lvl[k] = 2; } });
  c.weak.forEach(function (s) {
    for (k = s[0]; k < s[1] && k < t.length; k++) { if (!lvl[k]) { lvl[k] = 1; } }
  });
  var out = [], nStrong = 0, nWeak = 0;
  i = 0;
  while (i < t.length) {
    var v = lvl[i], j = i;
    while (j < t.length && lvl[j] === v) { j++; }
    var seg = esc(t.slice(i, j));
    var blank = !/[0-9A-Za-z\u4e00-\u9fff]/.test(t.slice(i, j));
    if (v === 2) { out.push('<mark>' + seg + '</mark>'); nStrong += j - i; }
    else if (v === 1) { out.push('<u class="weak">' + seg + '</u>'); nWeak += j - i; }
    else if (blank) { out.push(seg); }
    else { out.push('<span class="miss">' + seg + '</span>'); }
    i = j;
  }
  var pct = function (n) { return Math.round(n / Math.max(1, t.length) * 100); };
  return '<div class="attn">' +
    '<div class="attn-head"><b>你的问题在这个库里的落点</b>' +
      '<span class="attn-stats">' +
        '<span><span class="dot" style="background:#ffd666"></span>逐字 ' + pct(nStrong) + '%</span>' +
        '<span><span class="dot" style="background:var(--b-mid)"></span>2 字级 ' + pct(nWeak) + '%</span>' +
        '<span><span class="dot" style="background:var(--line-2)"></span>无对应 ' +
          pct(t.length - nStrong - nWeak) + '%</span>' +
      '</span></div>' +
    '<div class="attn-text">' + out.join('') + '</div>' +
    '<div class="attn-tip"><b>黄底</b> 逐字相同，4-gram 打分吃得到； ' +
      '<b>虚线下划线</b> 只有 2 字级对得上（对方换了措辞，分数会偏低）； ' +
      '<b>灰色</b> 返回的这几条里没有对应说法。</div>' +
  '</div>';
}

/* ---------- 渲染：单选卡 ---------- */
function card(h, rank) {
  var b = bandOf(h.score);
  var meta = ['<span>' + esc(h.board) + '</span>', '<i>·</i>', '<span>第 ' + h.round + ' 轮</span>'];
  if (['北交所', '新三板', '科创板'].indexOf(h.board) >= 0 && h.chapter_std) {
    meta.push('<i>·</i><span>' + esc(h.chapter_std) + '</span>');
  }
  if (h.exact) { meta.push('<i>·</i><span>逐字命中</span>'); }
  if (h.is_boilerplate) { meta.push('<i>·</i><span>套话</span>'); }

  var kv = [];
  if (h.sub_question) { kv.push('<span>小问：' + esc(h.sub_question) + '</span>'); }
  if (h.asker) { kv.push('<span>提问主体：' + esc(h.asker) + '</span>'); }
  kv.push('<span>原文 ' + esc(h.pages) + ' 页</span>');
  kv.push('<code>' + esc(h.q_id) + '</code>');

  return '<article class="hit ' + b.cls + '" data-qid="' + esc(h.question_id) + '">' +
    '<div class="hit-top">' +
      '<span class="hit-rank">' + (rank < 10 ? '0' + rank : rank) + '</span>' +
      '<span class="hit-co">' + esc(h.company) + '</span>' +
      '<span class="hit-meta">' + meta.join('') + '</span>' +
      '<span class="band ' + b.cls + '" title="相似度档位：' + b.name + '">' +
        '<i></i><i></i><i></i><em>' + b.name + '</em></span>' +
    '</div>' +
    (h.question ? '<p class="hit-q">' + esc(h.question) + '</p>' : '') +
    '<blockquote class="quote">' + mark(h) + '</blockquote>' +
    '<div class="hit-foot">' +
      '<span class="kvline">' + kv.join('<i>·</i>') + '</span>' +
      '<span class="acts">' +
        '<button class="act" data-act="copy" title="复制这段原文">' + ICON.copy + '复制</button>' +
        '<button class="act" data-act="sib" aria-expanded="false">' + ICON.chev + '完整小问</button>' +
      '</span>' +
    '</div>' +
    '<div class="sib-wrap"><div class="sib-in"><div class="sib-list"></div></div></div>' +
  '</article>';
}

/* ---------- 渲染：结果条 ---------- */
function resultBar(data, visible) {
  var nHit = 0, dist = { high: 0, mid: 0, low: 0, min: 0 };
  visible.forEach(function (v) {
    (v.g.hits || []).forEach(function (h) { nHit++; dist[bandOf(h.score).cls]++; });
  });
  var m = [];
  if (data.source) { m.push('<span class="metric">题<b>' + (data.questions || []).length + '</b></span>'); }
  m.push('<span class="metric">小问<b>' + visible.length + '</b></span>');
  m.push('<span class="metric">参考<b>' + num(nHit) + '</b>条</span>');
  if (data.elapsed) { m.push('<span class="metric">用时<b>' + data.elapsed + '</b>秒</span>'); }

  var total = nHit || 1;
  var bar = [['high', '高'], ['mid', '中'], ['low', '低'], ['min', '极低']].map(function (x) {
    return dist[x[0]] ? '<i class="' + x[0] + '" style="width:' + (dist[x[0]] / total * 100) + '%"></i>' : '';
  }).join('');

  var f = [['all', '全部'], ['high', '只看高'], ['highmid', '高+中']].map(function (x) {
    return '<button data-f="' + x[0] + '" class="' + (state.filter === x[0] ? 'on' : '') + '">' + x[1] + '</button>';
  }).join('');

  var right = '';
  if (data.export) {
    right = '<button class="btn-export" id="exportbtn" title="四张表：说明 / 命中排行 / 明细TOP2 / 明细TOP5">' +
            ICON.dl + '导出 Excel 指引</button>';
  } else if (!data.source) {
    right = '<span class="rb-note">要导出 Excel？<a id="tofile">上传整份函</a></span>';
  }

  var title = data.source ? '整份函检索结果' : '检索结果';
  var src = data.source ? '<span class="rb-src">' + esc(data.source) + '</span>' : '';

  var nav = '';
  if (visible.length > 4) {
    nav = '<div class="gnav-wrap" id="gnavwrap">' +
      '<button class="gnav-arrow" id="gnavL" hidden aria-label="往前翻题号">' +
        '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M14.5 6 8.5 12l6 6"/></svg></button>' +
      '<div class="gnav" id="gnav">' + visible.map(function (v) {
        var g = v.g, top = 'min';
        (g.hits || []).forEach(function (h) {
          var c = bandOf(h.score).cls;
          if (top === 'min' && c !== 'min') { top = c; }
          if (c === 'high') { top = 'high'; }
          else if (c === 'mid' && top !== 'high') { top = 'mid'; }
          else if (c === 'low' && top === 'min') { top = 'low'; }
        });
        return '<a href="#g-' + v.i + '" data-g="' + v.i + '"><i style="background:var(--b-' + top + ')"></i>' +
               (g.label ? esc(g.label.replace(' · 小问', '-')) : (v.i + 1)) + '</a>';
      }).join('') + '</div>' +
      '<button class="gnav-arrow" id="gnavR" hidden aria-label="往后翻题号">' +
        '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9.5 6 15.5 12l-6 6"/></svg></button>' +
      '</div>';
  }

  $('resultbar').innerHTML =
    '<div class="rbar">' +
      '<div class="rb-main">' +
        '<span class="rb-title">' + title + '</span>' + src +
        '<div class="metrics">' + m.join('<span class="rb-sep"></span>') + '</div>' +
        '<div class="rb-right"><div class="filters">' + f + '</div>' + right + '</div>' +
      '</div>' +
      (nHit ? '<div class="dist">' + bar + '</div>' : '') +
      nav +
    '</div>';

  if (data.export) {
    state.export = data.export;
    $('exportbtn').onclick = () => document.dispatchEvent(new CustomEvent('qb:export'));
  } else {
    state.export = null;
    const tf = $('tofile');
    if (tf) { tf.onclick = () => document.dispatchEvent(new CustomEvent('qb:mode', { detail: 'file' })); }
  }
  Array.prototype.forEach.call(document.querySelectorAll('.filters button'), function (btn) {
    btn.onclick = function () {
      state.filter = btn.dataset.f;
      render(state.data);
    };
  });
}

/* ---------- 渲染：主流程 ---------- */
function pass(g) {
  if (state.filter === 'all') { return true; }
  return (g.hits || []).some(function (h) {
    var c = bandOf(h.score).cls;
    return state.filter === 'high' ? c === 'high' : (c === 'high' || c === 'mid');
  });
}
function keep(h) {
  if (state.filter === 'all') { return true; }
  var c = bandOf(h.score).cls;
  return state.filter === 'high' ? c === 'high' : (c === 'high' || c === 'mid');
}

export function skeleton(n) {
  var out = [];
  for (var i = 0; i < (n || 3); i++) {
    out.push('<div class="sk"><i class="w30"></i><i class="w95"></i><i class="w70"></i><i class="w95"></i><i class="w45"></i></div>');
  }
  return out.join('');
}

export function render(data) {
  if (!data) { return; }
  state.data = data;
  // 带上原始序号：筛选后组号、锚点、收起状态都按它在整份函里的位置算，
  // 否则切一次筛选，收起的就变成别的组了。
  var groups = [];
  (data.groups || []).forEach(function (g, i) {
    if (pass(g)) { groups.push({ g: g, i: i }); }
  });
  var out = [];
  var best = 0;
  (data.groups || []).forEach(function (g) {
    (g.hits || []).forEach(function (h) { if (h.score > best) { best = h.score; } });
  });

  if (data.source) {
    // 文件名、题数、小问数、用时结果条上都有了，这里不再重复一遍横幅
  } else if (data.parts && data.parts.length > 1) {
    // 只留一句"拆成了几段"。具体拆成什么，下面每组的标题就是那一段，
    // 再列一遍等于把同一段话在屏幕上放两遍。
    out.push('<div class="banner split">已把输入拆成 ' + data.parts.length +
             ' 个最小 query，分别检索后汇总</div>');
  }
  if (best > 0 && best < 0.30) {
    out.push('<div class="banner warn">库里没有特别接近的问题，下面是最接近的几条 —— ' +
             '建议打开「扩大搜索范围」，或换一种问法。</div>');
  }
  if (state.filter !== 'all' && !groups.length) {
    out.push('<div class="empty">当前筛选下没有命中，<a id="resetf" style="color:var(--brand);cursor:pointer">' +
             '看全部结果</a>。</div>');
  }

  // 落点面板只在"自己粘的一段问题"这种单条场景给：拆成 4 段或整份函 99 组时，
  // 每组再画一遍同一句话的落点，既重复又占屏，直接省略。
  var showCover = (data.groups || []).length <= 2;

  groups.forEach(function (v) {
    var g = v.g, gi = v.i;
    var hits = (g.hits || []).filter(keep);
    var collapsed = !!state.collapsed[gi];
    var title = g.question || g.text || '';
    var sub = g.question ? (g.text || '') : '';
    out.push('<div class="group' + (collapsed ? ' collapsed' : '') + '" id="g-' + gi + '">');
    out.push('<div class="g-head">' +
      '<span class="g-no">' + (gi + 1 < 10 ? '0' : '') + (gi + 1) + '</span>' +
      '<div class="g-body">' +
        '<div class="g-title">' + (g.label ? '<span class="g-label">' + esc(g.label) + '</span>' : '') +
          '<span class="g-name">' + esc(title) + '</span></div>' +
        (sub ? '<p class="g-text" data-clamp="2">' + esc(sub) + '</p>' +
               '<button class="g-more" hidden>展开全文</button>' : '') +
      '</div>' +
      '<div class="g-right">' +
        '<span class="g-count">' + (hits.length ? hits.length + ' 条' : '无参考') + '</span>' +
        '<button class="g-toggle" data-g="' + gi + '" aria-label="收起/展开这一组" aria-expanded="' +
          (collapsed ? 'false' : 'true') + '">' + ICON.chev + '</button>' +
      '</div>' +
    '</div>');
    if (showCover) { out.push(coverBlock(g)); }
    if (!hits.length) {
      out.push('<div class="empty">' +
        (state.filter === 'all' ? '没有找到相似的问题，试试打开「扩大搜索范围」。' : '这一组没有该档位的参考。') +
        '</div>');
    }
    out.push('<div class="g-cards">');
    hits.forEach(function (h, hi) { out.push(card(h, hi + 1)); });
    out.push('</div>');
    out.push('</div>');
  });

  $('results').innerHTML = out.join('');
  resultBar(data, groups);
  wireGnav();
  requestAnimationFrame(function () {
    // 文本被 clamp 住的才给「展开全文」，没截断的不给（不然点了没反应）
    Array.prototype.forEach.call(document.querySelectorAll('.g-text'), function (p) {
      if (p.scrollHeight > p.clientHeight + 2) {
        var b = p.nextElementSibling;
        if (b && b.classList.contains('g-more')) { b.hidden = false; }
      }
    });
    // 卡片入场错峰：一屏内依次浮现，超过 6 张就不再推迟（不然最后一张要等 1 秒）
    Array.prototype.forEach.call(document.querySelectorAll('.hit'), function (el, i) {
      if (i < 6) { el.style.animationDelay = (i * 26) + 'ms'; }
      else { el.style.animation = 'none'; }
    });
  });
}
