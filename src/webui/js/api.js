/**
 * 后端接口的唯一入口。
 *
 * 视图和交互层只认这里的函数，不直接写 fetch —— 换路径、加错误处理、
 * 以后要加校验或埋点，都只改这一个文件。
 * 后端报错是 FastAPI 的 {detail: "..."}，这里统一翻成 Error。
 */

/** 从失败响应里抠出人话。不是 JSON（502、代理页）就回退到兜底文案。 */
async function errText(res, fallback) {
  let detail = fallback;
  try {
    const body = await res.json();
    if (body && body.detail) { detail = body.detail; }
  } catch (e) { /* 保持兜底文案 */ }
  return detail;
}

async function toJson(res, fallback) {
  if (!res.ok) { throw new Error(await errText(res, fallback)); }
  return res.json();
}

async function toBlob(res, fallback) {
  if (!res.ok) { throw new Error(await errText(res, fallback)); }
  return res.blob();
}

/** 库内统计：函数 / 公司数 / query 数 / 板块分布 / 轮次 */
export function fetchStats() {
  return fetch('/api/stats').then((r) => toJson(r, '取库统计失败'));
}

/** 单段问题检索。body: {q, board, round, top, expand, boilerplate} */
export function search(body) {
  return fetch('/api/search', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body)
  }).then((r) => toJson(r, '检索失败'));
}

/** 整份问询函上传。返回同 /api/search，多 source / label / export */
export function upload(file, top) {
  const fd = new FormData();
  fd.append('file', file);
  fd.append('board', '');
  fd.append('top', String(top));
  return fetch('/api/upload', { method: 'POST', body: fd })
    .then((r) => toJson(r, '上传失败'));
}

/** 某个问题节下的全部小问（点「完整小问」时才拉） */
export function fetchSiblings(questionId) {
  return fetch('/api/siblings?question_id=' + encodeURIComponent(questionId))
    .then((r) => toJson(r, '取小问失败'));
}

/** 导出 Excel 指引，拿到的是二进制 */
export function exportGuide(token) {
  return fetch('/api/export/' + encodeURIComponent(token))
    .then((r) => toBlob(r, '导出失败'));
}
