/**
 * 全局状态与领域常量。
 *
 * 这里只放"是什么"，不放"怎么画"。视图层（render.js）读 state 出 HTML，
 * 交互层（search.js / main.js）改 state 再触发重绘。
 * state 是一个可变对象而不是 setState：页面只有这一处状态，没有并发写，
 * 引入发布订阅只会多一层跳转。
 */
export const state = {
  stats: null, board: '北交所', mode: 'paste', export: null, busy: false,
  data: null, filter: 'all', collapsed: {}
};

/** 相似度分档：[下限, 档位字, class 后缀]。顺序必须从高到低。 */
export const BANDS = [[0.60, '高', 'high'], [0.42, '中', 'mid'], [0.30, '低', 'low']];

export function bandOf(score) {
  for (let i = 0; i < BANDS.length; i++) {
    if (score >= BANDS[i][0]) { return { name: BANDS[i][1], cls: BANDS[i][2] }; }
  }
  return { name: '极低', cls: 'min' };
}

/** 内联 SVG 图标（stroke 图标，不引外链——离线包里不能联网取图标库）。 */
export const ICON = {
  dl: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 4v11"/><path d="M7.5 10.5 12 15l4.5-4.5"/><path d="M4.5 19.5h15"/></svg>',
  copy: '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="9" y="9" width="10" height="11" rx="2"/><path d="M15 6.5V5a1.5 1.5 0 0 0-1.5-1.5H6A1.5 1.5 0 0 0 4.5 5v8A1.5 1.5 0 0 0 6 14.5h1.5"/></svg>',
  chev: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 9.5 12 15.5 18 9.5"/></svg>',
  sun: '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="4"/><path d="M12 2.5v2M12 19.5v2M2.5 12h2M19.5 12h2M5.2 5.2l1.4 1.4M17.4 17.4l1.4 1.4M18.8 5.2l-1.4 1.4M6.6 17.4l-1.4 1.4"/></svg>',
  moon: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M20 13.5A8.2 8.2 0 0 1 10.5 4a8.2 8.2 0 1 0 9.5 9.5z"/></svg>',
  auto: '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="8.4"/><path d="M12 3.6v16.8" /><path d="M12 3.6a8.4 8.4 0 0 1 0 16.8" fill="currentColor" stroke="none"/></svg>'
};
