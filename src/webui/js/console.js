/**
 * 工作台（顶部那个控制台）：模式切换、输入框、拖拽上传、检索 / 上传入口。
 *
 * 它负责"用户在这里能做什么"，具体怎么检索交给 search.js。
 * 模式状态只存在 state.mode，别再在 DOM class 上另存一份。
 */
import { state } from './state.js';
import { $, toast } from './dom.js';
import { doSearch, uploadFile } from './search.js';

/** 'paste' | 'file' */
export function setMode(m) {
  state.mode = m;
  $('pane-paste').hidden = m !== 'paste';
  $('pane-file').hidden = m !== 'file';
  $('mode-paste').classList.toggle('on', m === 'paste');
  $('mode-file').classList.toggle('on', m === 'file');
  $('mode-paste').setAttribute('aria-selected', String(m === 'paste'));
  $('mode-file').setAttribute('aria-selected', String(m === 'file'));
  $('modes').style.setProperty('--i', m === 'paste' ? 0 : 1);
  if (m === 'paste') { $('q').focus(); }
}

export function countChars() {
  const n = ($('q').value || '').replace(/\s/g, '').length;
  $('tacount').textContent = n ? n + ' 字' : '';
}

/** 拖文件到输入框上也能上传：高亮 + 接管落点 */
function wireDrop(el) {
  const isText = el.id === 'q';
  ['dragover', 'dragenter'].forEach((ev) => {
    el.addEventListener(ev, (e) => {
      if (e.dataTransfer && e.dataTransfer.types.indexOf('Files') >= 0) {
        e.preventDefault();
        el.classList.add(isText ? 'dragging' : 'hot');
      }
    });
  });
  ['dragleave', 'dragend'].forEach((ev) => {
    el.addEventListener(ev, () => el.classList.remove('hot', 'dragging'));
  });
  el.addEventListener('drop', (e) => {
    el.classList.remove('hot', 'dragging');
    const f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
    if (!f) { return; }
    e.preventDefault();
    setMode('file');
    uploadFile(f);
  });
}

export function initConsole() {
  $('mode-paste').onclick = () => setMode('paste');
  $('mode-file').onclick = () => setMode('file');
  $('go').onclick = doSearch;
  $('q').addEventListener('input', countChars);

  $('dropzone').addEventListener('click', (e) => {
    if (e.target && e.target.id === 'file') { return; }
    $('file').click();
  });
  $('dropzone').addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); $('file').click(); }
  });
  $('file').addEventListener('change', (e) => {
    if (e.target.files && e.target.files[0]) { uploadFile(e.target.files[0]); }
    e.target.value = '';
  });
  wireDrop($('dropzone'));
  wireDrop($('q'));

  // 改筛选条件后：粘贴模式下有结果就自动重搜；整份函要重跑上传，只提示
  ['round', 'top', 'expand', 'boiler'].forEach((id) => {
    $(id).addEventListener('change', () => {
      if (state.mode === 'paste' && $('q').value.trim() && state.data) { doSearch(); }
      else if (state.mode === 'file' && state.data && id === 'top') {
        toast('重新上传后按新条数检索');
      }
    });
  });

  if (/[?&]mode=file\b/.test(location.search)) { setMode('file'); }
}
