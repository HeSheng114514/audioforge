/* 音匠 AudioForge · 前端逻辑 */
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];

const state = {
  meta: null,
  files: [],
  active: null,
  fmt: 'mp3',
  quality: 'standard',
  normalize: 'none',
  channels: 'keep',
  chipStereo: 'mono',
  eq: new Array(10).fill(0),
  chipEnabled: false,
  preset: 'nes',
  pollTimer: null,
  autoUnlock: true,
};

const fmtBytes = n => {
  if (!n) return '0 B';
  const u = ['B', 'KB', 'MB', 'GB'];
  let i = 0; while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return (i === 0 ? n : n.toFixed(1)) + ' ' + u[i];
};
const fmtTime = s => {
  if (!isFinite(s) || s <= 0) return '0:00';
  const m = Math.floor(s / 60), r = Math.floor(s % 60);
  return m + ':' + String(r).padStart(2, '0');
};

let toastTimer;
function toast(msg, isErr = false) {
  const el = $('#toast');
  el.textContent = msg;
  el.className = 'toast show' + (isErr ? ' err' : '');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (el.className = 'toast'), 2800);
}

async function api(path, body) {
  const opt = body === undefined ? {} : {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  };
  const res = await fetch(path, opt);
  const data = await res.json().catch(() => ({ ok: false, error: '响应解析失败' }));
  if (!res.ok || data.ok === false) throw new Error(data.error || ('HTTP ' + res.status));
  return data;
}

/* ---------------- 初始化 ---------------- */
async function init() {
  try {
    state.meta = await api('/api/meta');
    $('#enginePill').textContent = state.meta.engine + ' · v' + state.meta.version;
  } catch (e) {
    $('#enginePill').textContent = '引擎未就绪';
    toast('无法连接后端：' + e.message, true);
    return;
  }
  renderFormats();
  renderQualities();
  renderPresets();
  renderSegments();
  renderEq();
  renderRates();
  renderEncFormats();
  applyPreset(state.meta.presets.find(p => p.key === state.preset));
  bindEvents();
  updateBitrateForFormat(false);
}

function renderFormats() {
  const box = $('#fmtGrid');
  box.innerHTML = '';
  const shown = ['mp3', 'wav', 'flac', 'ogg', 'opus', 'm4a', 'aiff', 'wma', 'alac'];
  state.meta.formats.filter(f => shown.includes(f.key)).forEach(f => {
    const b = document.createElement('button');
    b.className = 'fmt' + (f.key === state.fmt ? ' active' : '');
    b.dataset.fmt = f.key;
    b.innerHTML = '' + f.label + '<small>' + f.tag + '</small>';
    b.onclick = () => {
      state.fmt = f.key;
      renderFormats();
      updateBitrateForFormat(true);
      refreshChain();
    };
    box.appendChild(b);
  });
}

function renderQualities() {
  const box = $('#qualitySeg');
  box.innerHTML = '';
  state.meta.qualities.forEach(q => {
    const b = document.createElement('button');
    b.textContent = q.label;
    b.dataset.q = q.key;
    if (q.key === state.quality) b.classList.add('active');
    b.onclick = () => { state.quality = q.key; renderQualities(); refreshChain(); };
    box.appendChild(b);
  });
}

function renderRates() {
  const sel = $('#rate');
  sel.innerHTML = '<option value="0">保持原样</option>';
  state.meta.sample_rates.forEach(r => {
    const o = document.createElement('option');
    o.value = r; o.textContent = (r / 1000) + ' kHz';
    sel.appendChild(o);
  });
}

function renderEncFormats() {
  const box = $('#encGrid');
  if (!box || !state.meta.encrypted_formats) return;
  box.innerHTML = '';
  const seen = new Set();
  state.meta.encrypted_formats.forEach(f => {
    if (seen.has(f.kind)) return;
    seen.add(f.kind);
    const d = document.createElement('div');
    d.className = 'enc';
    d.dataset.kind = f.kind;
    d.textContent = f.kind;
    d.title = f.platform + ' · ' + f.ext;
    box.appendChild(d);
  });
}

function markEncFormats(kind) {
  $$('.enc').forEach(e => e.classList.toggle('on', e.dataset.kind === kind));
}

function renderPresets() {
  const box = $('#presetGrid');
  box.innerHTML = '';
  state.meta.presets.filter(p => p.key !== 'custom').forEach(p => {
    const b = document.createElement('button');
    b.className = 'preset' + (p.key === state.preset ? ' active' : '');
    b.dataset.preset = p.key;
    b.innerHTML = '<strong>' + p.label + '</strong><small>' + p.desc + '</small>';
    b.onclick = () => {
      state.preset = p.key;
      applyPreset(p);
      renderPresets();
      refreshChain();
    };
    box.appendChild(b);
  });
  const cur = state.meta.presets.find(p => p.key === state.preset);
  $('#presetDesc').textContent = cur ? cur.label + ' · ' + cur.desc : '';
}

function applyPreset(p) {
  if (!p) return;
  const q = p.params || {};
  const set = (id, v) => {
    const el = $('#' + id);
    if (el && v !== undefined && v !== null) el.value = v;
  };
  set('bitDepth', q.bit_depth);
  set('chipRate', q.sample_rate);
  set('squareness', q.squareness);
  set('gate', q.gate);
  set('vibrato', q.vibrato_semitones);
  set('arp', q.arp_semitones);
  set('noise', q.noise);
  set('lowpass', q.lowpass);
  set('tapeStop', q.tape_stop || 0);
  if (q.stereo) { state.chipStereo = q.stereo; renderSegments(); }
  syncChipLabels();
}

function renderSegments() {
  const norms = [{ k: 'none', l: '关闭' }, { k: 'peak', l: '峰值' }, { k: 'rms', l: '响度 RMS' }];
  const nb = $('#normSeg'); nb.innerHTML = '';
  norms.forEach(n => {
    const b = document.createElement('button');
    b.textContent = n.l; if (n.k === state.normalize) b.classList.add('active');
    b.onclick = () => { state.normalize = n.k; renderSegments(); refreshChain(); };
    nb.appendChild(b);
  });
  const chans = [{ k: 'keep', l: '保持' }, { k: 'mono', l: '单声道' }, { k: 'stereo', l: '立体声' }, { k: 'swap', l: '左右互换' }];
  const cb = $('#chanSeg'); cb.innerHTML = '';
  chans.forEach(c => {
    const b = document.createElement('button');
    b.textContent = c.l; if (c.k === state.channels) b.classList.add('active');
    b.onclick = () => { state.channels = c.k; renderSegments(); refreshChain(); };
    cb.appendChild(b);
  });
  const cs = [{ k: 'mono', l: '单声道' }, { k: 'keep', l: '原样' }, { k: 'widen', l: '加宽' }, { k: 'pingpong', l: '乒乓' }];
  const sb = $('#chipStereo'); sb.innerHTML = '';
  cs.forEach(c => {
    const b = document.createElement('button');
    b.textContent = c.l; if (c.k === state.chipStereo) b.classList.add('active');
    b.onclick = () => { state.chipStereo = c.k; renderSegments(); refreshChain(); };
    sb.appendChild(b);
  });
}

function renderEq() {
  const box = $('#eqBox');
  box.innerHTML = '';
  state.meta.eq_bands.forEach((hz, i) => {
    const wrap = document.createElement('div');
    wrap.className = 'band';
    const r = document.createElement('input');
    r.type = 'range'; r.min = -12; r.max = 12; r.step = 1; r.value = state.eq[i];
    r.title = hz + ' Hz';
    r.oninput = () => { state.eq[i] = +r.value; scheduleChain(); };
    const lbl = document.createElement('small');
    lbl.textContent = hz >= 1000 ? (hz / 1000) + 'k' : hz;
    wrap.append(r, lbl);
    box.appendChild(wrap);
  });
}

/* ---------------- 参数收集 ---------------- */
function collectChip() {
  const val = id => +$('#' + id).value;
  return {
    bit_depth: val('bitDepth'),
    sample_rate: val('chipRate'),
    squareness: val('squareness'),
    gate: val('gate'),
    vibrato_semitones: val('vibrato'),
    arp_semitones: val('arp'),
    noise: val('noise'),
    lowpass: val('lowpass'),
    tape_stop: val('tapeStop'),
    stereo: state.chipStereo,
  };
}

function specFor(file) {
  const meta = {};
  const t = $('#metaTitle').value.trim(); if (t) meta.title = t;
  const ar = $('#metaArtist').value.trim(); if (ar) meta.artist = ar;
  const al = $('#metaAlbum').value.trim(); if (al) meta.album = al;
  return {
    input: file ? file.path : '',
    fmt: state.fmt,
    quality: state.quality,
    bitrate: +$('#bitrate').value || 0,
    rate: +$('#rate').value || 0,
    bits: +$('#bits').value || 16,
    volume_db: +$('#volume').value,
    normalize: state.normalize,
    normalize_db: -1,
    fade_in: +$('#fadeIn').value,
    fade_out: +$('#fadeOut').value,
    start: +$('#start').value || 0,
    end: +$('#end').value || 0,
    speed: +$('#speed').value,
    pitch: +$('#pitch').value,
    channels: state.channels,
    trim_silence: $('#trimSilence').checked,
    reverse: $('#reverse').checked,
    eq: state.eq,
    chip_enabled: state.chipEnabled,
    chip_preset: state.preset,
    chip: collectChip(),
    metadata: meta,
    keep_original_tags: $('#keepTags').checked,
    auto_unlock: state.autoUnlock,
  };
}

/* ---------------- 文件管理 ---------------- */
async function uploadFiles(fileList) {
  const arr = [...fileList].filter(f => f.size > 0);
  if (!arr.length) return;
  const fd = new FormData();
  arr.forEach(f => fd.append('files', f, f.name));
  toast('正在上传 ' + arr.length + ' 个文件…');
  try {
    const res = await fetch('/api/upload', { method: 'POST', body: fd });
    const data = await res.json();
    if (!data.ok) throw new Error(data.error);
    let added = 0;
    data.files.forEach(f => {
      if (f.ok) { state.files.push(f); added++; }
      else toast((f.name || '文件') + ' 无法解析：' + (f.error || ''), true);
    });
    renderFiles();
    if (!state.active && state.files.length) selectFile(state.files[0]);
    if (added) toast('已添加 ' + added + ' 个文件');
  } catch (e) {
    toast('上传失败：' + e.message, true);
  }
}

function renderFiles() {
  const ul = $('#filelist');
  ul.innerHTML = '';
  state.files.forEach((f, idx) => {
    const li = document.createElement('li');
    li.className = (state.active === f ? 'active ' : '')
      + (f.ok === false ? 'bad ' : '')
      + (f.encrypted ? 'locked' : '');
    const ext = (f.name.split('.').pop() || '?').toUpperCase();
    li.innerHTML =
      '<span class="fext">' + ext + '</span>' +
      '<span class="fname" title="' + f.name + '">' + f.name + '</span>' +
      '<span class="fmeta">' + (f.ok === false ? '—' : fmtTime(f.duration)) + '</span>' +
      '<button class="del" title="移除">✕</button>';
    li.onclick = ev => {
      if (ev.target.classList.contains('del')) { removeFile(idx); return; }
      selectFile(f);
    };
    ul.appendChild(li);
  });
  $('#fileCount').textContent = state.files.length;
  const dur = state.files.reduce((a, f) => a + (f.duration || 0), 0);
  const size = state.files.reduce((a, f) => a + (f.size || 0), 0);
  $('#statTotal').textContent = state.files.length;
  $('#statDur').textContent = fmtTime(dur);
  $('#statSize').textContent = fmtBytes(size);
  $('#btnConvert').disabled = state.files.length === 0;
}

async function removeFile(idx) {
  const f = state.files[idx];
  state.files.splice(idx, 1);
  if (state.active === f) {
    state.active = null;
    clearStage();
    if (state.files.length) selectFile(state.files[0]);
  }
  renderFiles();
  if (f && f.path) {
    fetch('/api/delete', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path: f.path }),
    });
  }
}

function selectFile(f) {
  state.active = f;
  renderFiles();
  $('#stageTitle').textContent = f.name;
  $('#btnPlay').disabled = false;
  $('#btnPreview').disabled = false;
  $('#btnCompare').disabled = false;
  $('#btnCompareImg').disabled = false;
  $('#compare').hidden = true;
  $('#compareImg').hidden = true;
  if (f.encrypted) {
    $('#metaRow').innerHTML =
      '<span class="tag" style="border-color:rgba(255,180,84,.5);color:#ffc978">加密 <b>' + f.kind + '</b></span>' +
      '<span class="tag">平台 <b>' + f.platform + '</b></span>' +
      '<span class="tag">大小 <b>' + fmtBytes(f.size) + '</b></span>' +
      (f.needs_key ? '<span class="tag" style="color:#ff9dbe">需要 <b>ekey</b></span>' : '');
    updateUnlockPanel(f);
    $('#btnPlay').disabled = true;
    $('#btnPreview').disabled = true;
    $('#btnCompare').disabled = true;
    $('#btnCompareImg').disabled = true;
    $('#compare').hidden = true;
    $('#spectrum').hidden = true;
    $('#vizEmpty').hidden = false;
    $('#vizEmpty').textContent = '加密文件，先解密才能试听';
    const cv = $('#wave');
    cv.getContext('2d').clearRect(0, 0, cv.width, cv.height);
    waveData = null;
    refreshChain();
    return;
  }
  $('#metaRow').innerHTML =
    '<span class="tag">编码 <b>' + (f.codec || '?') + '</b></span>' +
    '<span class="tag">时长 <b>' + fmtTime(f.duration) + '</b></span>' +
    '<span class="tag">采样率 <b>' + (f.rate || '?') + ' Hz</b></span>' +
    '<span class="tag">声道 <b>' + (f.channels || '?') + '</b></span>' +
    '<span class="tag">码率 <b>' + (f.bitrate ? Math.round(f.bitrate / 1000) + 'k' : '—') + '</b></span>' +
    '<span class="tag">大小 <b>' + fmtBytes(f.size) + '</b></span>' +
    (f.bit_depth ? '<span class="tag">位深 <b>' + f.bit_depth + 'bit</b></span>' : '');
  loadWave(f);
  refreshChain();
}

function clearStage() {
  $('#stageTitle').textContent = '未选择文件';
  $('#metaRow').innerHTML = '';
  $('#vizEmpty').hidden = false;
  $('#vizEmpty').textContent = '选择一个文件查看波形';
  $('#spectrum').hidden = true;
  $('#spectrum').src = '';
  $('#btnPlay').disabled = true;
  $('#btnPreview').disabled = true;
  $('#btnCompare').disabled = true;
  $('#btnCompareImg').disabled = true;
  $('#compare').hidden = true;
  $('#compareImg').hidden = true;
  const cv = $('#wave');
  const ctx = cv.getContext('2d');
  ctx.clearRect(0, 0, cv.width, cv.height);
  waveData = null;
  updateUnlockPanel(null);
  refreshChain();
}

let waveData = null;
async function loadWave(f) {
  try {
    $('#vizEmpty').textContent = '正在解析波形…';
    $('#vizEmpty').hidden = false;
    const res = await api('/api/waveform', { path: f.path });
    waveData = res.wave;
    drawWave(res.wave);
    $('#vizEmpty').hidden = true;
    $('#spectrum').src = res.spectrum;
    $('#spectrum').hidden = false;
  } catch (e) {
    $('#vizEmpty').hidden = false;
    $('#vizEmpty').textContent = '波形解析失败：' + e.message;
  }
}

function drawWave(w, progress = 0) {
  const cv = $('#wave');
  const dpr = window.devicePixelRatio || 1;
  const rect = cv.getBoundingClientRect();
  const H = 150;
  if (!rect.width) return;
  cv.width = Math.round(rect.width * dpr);
  cv.height = Math.round(H * dpr);
  const ctx = cv.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  const W = rect.width;
  ctx.clearRect(0, 0, W, H);

  ctx.strokeStyle = 'rgba(255,255,255,.045)';
  ctx.lineWidth = 1;
  for (let i = 1; i < 8; i++) { ctx.beginPath(); ctx.moveTo(W * i / 8, 0); ctx.lineTo(W * i / 8, H); ctx.stroke(); }
  for (let i = 1; i < 4; i++) { ctx.beginPath(); ctx.moveTo(0, H * i / 4); ctx.lineTo(W, H * i / 4); ctx.stroke(); }
  if (!w) return;

  const mid = H / 2, amp = H * 0.44;
  const grad = ctx.createLinearGradient(0, 0, 0, H);
  grad.addColorStop(0, 'rgba(92,226,197,.9)');
  grad.addColorStop(.5, 'rgba(92,226,197,.6)');
  grad.addColorStop(1, 'rgba(122,162,255,.55)');
  ctx.fillStyle = grad;
  const n = w.max.length;
  for (let x = 0; x < W; x++) {
    const i = Math.min(n - 1, Math.floor(x / W * n));
    const y1 = mid - w.max[i] * amp;
    const y2 = mid - w.min[i] * amp;
    ctx.fillRect(x, y1, 1, Math.max(1, y2 - y1));
  }
  ctx.strokeStyle = 'rgba(160,180,220,.35)';
  ctx.beginPath(); ctx.moveTo(0, mid); ctx.lineTo(W, mid); ctx.stroke();
  const g2 = ctx.createLinearGradient(0, mid, 0, H);
  g2.addColorStop(0, 'rgba(255,110,160,.14)');
  g2.addColorStop(1, 'rgba(255,110,160,0)');
  ctx.fillStyle = g2;
  ctx.fillRect(0, mid, W, H - mid);

  if (progress > 0) {
    ctx.fillStyle = 'rgba(6,8,13,.55)';
    ctx.fillRect(W * progress, 0, W * (1 - progress), H);
    ctx.strokeStyle = '#ff6ea0';
    ctx.lineWidth = 2;
    ctx.beginPath(); ctx.moveTo(W * progress, 0); ctx.lineTo(W * progress, H); ctx.stroke();
  }
}

/* ---------------- 处理链 ---------------- */
let chainTimer;
function scheduleChain() {
  clearTimeout(chainTimer);
  chainTimer = setTimeout(refreshChain, 220);
}

async function refreshChain() {
  const box = $('#chainItems');
  if (!state.active) { box.innerHTML = '<em class="muted">选择文件并设置参数后显示</em>'; return; }
  try {
    const res = await api('/api/chain', { spec: specFor(state.active) });
    box.innerHTML = res.chain.map(s => '<span>' + s + '</span>').join('');
  } catch (e) {
    box.innerHTML = '<em class="muted">' + e.message + '</em>';
  }
}

/* ---------------- 解锁 ---------------- */
function updateUnlockPanel(f) {
  const badge = $('#unlockBadge');
  const desc = $('#unlockDesc');
  if (!f) {
    badge.textContent = '未检测';
    badge.className = 'unlock-badge off';
    desc.textContent = '选中加密文件后，这里会显示平台与格式并允许一键解密。';
    $('#btnUnlock').disabled = true;
    $('#ekeyField').hidden = true;
    markEncFormats('');
    return;
  }
  badge.textContent = f.platform + ' · ' + f.kind;
  badge.className = 'unlock-badge';
  desc.textContent = '检测到加密文件，解密后可还原为 '
    + (f.kind.includes('v2') || f.needs_key ? '（需要 ekey）FLAC/OGG' : '原始音频')
    + '，不重编码、无音质损失。';
  $('#btnUnlock').disabled = false;
  $('#ekeyField').hidden = !f.needs_key;
  markEncFormats(f.kind);
}

async function unlockFiles() {
  const targets = state.files.filter(f => f.encrypted);
  if (!targets.length) { toast('队列里没有加密文件', true); return; }
  const btn = $('#btnUnlock');
  btn.disabled = true;
  btn.textContent = '解密中…';
  try {
    const res = await api('/api/unlock', {
      items: targets.map(f => ({ path: f.path })),
      ekey: $('#ekeyInput').value.trim(),
    });
    const okOnes = res.results.filter(r => r.ok);
    res.results.filter(r => !r.ok).forEach(r => {
      toast(r.error.split('\n')[0], true);
    });
    if (okOnes.length) {
      renderUnlockResults(okOnes);
      toast('已解密 ' + okOnes.length + ' 个文件');
      // 把解密结果加入队列，方便继续转换
      for (const r of okOnes) {
        const info = await api('/api/probe?path=' + encodeURIComponent(r.output)).catch(() => null);
        if (!info) continue;
        const f = { ...info.info, ok: true, path: r.output, id: r.name };
        if (!state.files.find(x => x.path === r.output)) state.files.push(f);
      }
      renderFiles();
    }
  } catch (e) {
    toast('解密失败：' + e.message, true);
  } finally {
    btn.disabled = false;
    btn.textContent = '解密并还原原始音频';
  }
}

function renderUnlockResults(results) {
  const panel = $('#resultsPanel');
  const ul = $('#resultsList');
  panel.hidden = false;
  results.forEach(r => {
    const li = document.createElement('li');
    li.innerHTML =
      '<span class="rname" title="' + r.output + '">' + r.name + '</span>' +
      '<span class="rmeta">' + r.codec + ' · ' + fmtBytes(r.size) + ' · ' + r.rate + 'Hz</span>' +
      '<audio controls preload="none" src="/media/' + encodeURIComponent(r.name) + '"></audio>' +
      '<span class="badge-ok" style="color:#ffc978">🔓 ' + r.kind.toUpperCase() + '</span>';
    ul.insertBefore(li, ul.firstChild);
  });
}

/* ---------------- 试听 ---------------- */
const audioEl = new Audio();
audioEl.preload = 'auto';

function updatePlayBtn() {
  $('#btnPlay').textContent = audioEl.paused ? '▶ 试听' : '⏸ 暂停';
}

function playSource() {
  if (!state.active) return;
  const want = '/media/' + encodeURIComponent(state.active.name);
  if (audioEl.dataset.src === want) {
    if (audioEl.paused) audioEl.play().catch(() => {}); else audioEl.pause();
    return;
  }
  audioEl.dataset.src = want;
  audioEl.src = want + '?t=' + Date.now();
  audioEl.play().catch(() => toast('浏览器拒绝播放，请再点一次', true));
  $('#compare').hidden = false;
  $('#audioSrc').src = want;
  $('#btnCompare').classList.add('on');
  updatePlayBtn();
}

audioEl.addEventListener('timeupdate', () => {
  if (!waveData || !audioEl.duration) return;
  drawWave(waveData, audioEl.currentTime / audioEl.duration);
});
audioEl.addEventListener('pause', () => { updatePlayBtn(); drawWave(waveData, 0); });
audioEl.addEventListener('play', () => { updatePlayBtn(); $('#btnPlay').textContent = '⏸ 暂停'; });
audioEl.addEventListener('ended', () => { drawWave(waveData, 0); updatePlayBtn(); });

async function previewProcessed() {
  if (!state.active) return;
  const btn = $('#btnPreview');
  btn.disabled = true;
  btn.textContent = '渲染中…';
  try {
    const res = await api('/api/preview', {
      path: state.active.path, spec: specFor(state.active), preview_length: 12,
    });
    $('#compare').hidden = false;
    $('#btnCompare').classList.add('on');
    $('#audioOut').src = res.url + '?t=' + Date.now();
    $('#compareHint').textContent =
      '试听 ' + res.info.duration.toFixed(1) + ' 秒 · ' +
      (res.chip ? res.chip.text : '') + ' · 用时 ' + res.elapsed + 's';
    $('#audioOut').play().catch(() => {});
    toast('已生成试听片段');
  } catch (e) {
    toast('试听失败：' + e.message, true);
  } finally {
    btn.disabled = false;
    btn.textContent = '试听处理后';
  }
}

async function exportCompare() {
  if (!state.active) return;
  const btn = $('#btnCompareImg');
  btn.disabled = true;
  btn.textContent = '生成中…';
  try {
    const res = await api('/api/compare', {
      path: state.active.path, spec: specFor(state.active), preview_length: 12,
    });
    const img = $('#compareImg');
    img.src = res.url + '?t=' + Date.now();
    img.hidden = false;
    toast('对比图已保存到输出目录');
  } catch (e) {
    toast('生成对比图失败：' + e.message, true);
  } finally {
    btn.disabled = false;
    btn.textContent = '导出对比图';
  }
}

/* ---------------- 转换 ---------------- */
async function convert() {
  if (!state.files.length) return;
  const items = state.files.filter(f => f.ok !== false).map(f => specFor(f));
  if (!items.length) { toast('没有可转换的文件', true); return; }
  try {
    $('#globalProgress').hidden = false;
    $('#barFill').style.width = '2%';
    $('#progressText').textContent = '提交任务…';
    const res = await api('/api/convert', {
      items,
      out_dir: $('#outDir').value.trim() || undefined,
      suffix: $('#suffix').value.trim(),
    });
    toast('已提交 ' + res.ids.length + ' 个任务');
    pollJobs(res.ids);
  } catch (e) {
    toast('提交失败：' + e.message, true);
    $('#globalProgress').hidden = true;
  }
}

function pollJobs(ids) {
  clearInterval(state.pollTimer);
  const results = [];
  const tick = async () => {
    try {
      const res = await api('/api/jobs?ids=' + ids.join(','));
      const jobs = res.jobs;
      const done = jobs.filter(j => j.status === 'done').length;
      const err = jobs.filter(j => j.status === 'error').length;
      const total = jobs.length;
      const avg = jobs.reduce((a, j) => a + (j.status === 'done' ? 1 : j.progress), 0) / Math.max(1, total);
      $('#barFill').style.width = (avg * 100).toFixed(1) + '%';
      const cur = jobs.find(j => j.status === 'running');
      $('#progressText').textContent =
        done + '/' + total + ' 完成' + (err ? ' · ' + err + ' 失败' : '') +
        (cur ? ' · ' + (cur.message || '处理中') + ' ' + Math.round(cur.progress * 100) + '%' : '');

      jobs.forEach(j => {
        if ((j.status === 'done' || j.status === 'error') && !results.find(r => r.id === j.id)) {
          results.push({ id: j.id, job: j });
        }
      });
      renderResults(results);
      if (done + err >= total) {
        clearInterval(state.pollTimer);
        $('#progressText').textContent = '全部完成：' + done + ' 成功' + (err ? ' / ' + err + ' 失败' : '');
        toast(err ? ('完成，' + err + ' 个失败') : '转换完成', !!err);
      }
    } catch (e) {
      clearInterval(state.pollTimer);
      $('#progressText').textContent = '轮询失败：' + e.message;
    }
  };
  tick();
  state.pollTimer = setInterval(tick, 550);
}

function renderResults(results) {
  const panel = $('#resultsPanel');
  const ul = $('#resultsList');
  panel.hidden = false;
  ul.innerHTML = '';
  results.slice().reverse().forEach(({ job }) => {
    const li = document.createElement('li');
    if (job.status === 'done') {
      const out = job.result.output || '';
      const name = out.split(/[\\/]/).pop();
      li.innerHTML =
        '<span class="rname" title="' + out + '">' + name + '</span>' +
        '<span class="rmeta">' + fmtBytes(job.result.out_info.size) + ' · ' + job.result.out_info.rate + 'Hz · ' + job.result.elapsed + 's</span>' +
        '<audio controls preload="none" src="/media/' + encodeURIComponent(name) + '"></audio>' +
        '<span class="badge-ok">✓ 完成</span>';
    } else {
      li.innerHTML =
        '<span class="rname">' + (job.spec.input.split(/[\\/]/).pop()) + '</span>' +
        '<span class="rmeta" style="color:#ff9dbe">' + (job.error || '失败').slice(0, 90) + '</span>' +
        '<span></span><span class="badge-ok" style="color:#ff9dbe">✕ 失败</span>';
    }
    ul.appendChild(li);
  });
}

/* ---------------- 标签同步 ---------------- */
function syncChipLabels() {
  const v = id => +$('#' + id).value;
  $('#bitDepthVal').textContent = v('bitDepth');
  $('#levelsVal').textContent = (1 << v('bitDepth')) - 1;
  $('#chipRateVal').textContent = v('chipRate');
  $('#chipNyq').textContent = (v('chipRate') / 2).toFixed(0);
  $('#squareVal').textContent = v('squareness').toFixed(2);
  $('#gateVal').textContent = v('gate').toFixed(2);
  $('#vibVal').textContent = v('vibrato').toFixed(2);
  $('#arpVal').textContent = v('arp');
  $('#noiseVal').textContent = v('noise') ? v('noise').toFixed(3) : '0';
  $('#lpVal').textContent = v('lowpass') ? v('lowpass') + ' Hz' : '自动';
  $('#stopVal').textContent = v('tapeStop').toFixed(1);
  $('#volVal').textContent = v('volume').toFixed(1);
  $('#speedVal').textContent = v('speed').toFixed(2);
  $('#pitchVal').textContent = v('pitch');
  $('#fiVal').textContent = v('fadeIn').toFixed(1);
  $('#foVal').textContent = v('fadeOut').toFixed(1);
  $('#bitrateVal').textContent = v('bitrate');
}

function updateBitrateForFormat(resetBitrate = true) {
  const spec = state.meta.formats.find(f => f.key === state.fmt);
  const hasBr = spec && spec.bitrate;
  $('#bitrateField').style.display = hasBr ? '' : 'none';
  $('#bitsField').style.display = (spec && (spec.kind === 'pcm' || spec.kind === 'lossless')) ? '' : 'none';
  if (hasBr && spec.default_bitrate && resetBitrate) $('#bitrate').value = spec.default_bitrate;
  const bitsSel = $('#bits');
  [...bitsSel.options].forEach(o => {
    const bits = (spec && spec.bits) || [16];
    o.hidden = !bits.includes(+o.value);
  });
  if (spec && spec.bits && !spec.bits.includes(+bitsSel.value)) bitsSel.value = spec.bits[0];
  syncChipLabels();
}

/* ---------------- 事件绑定 ---------------- */
function bindEvents() {
  $$('.tab').forEach(t => t.onclick = () => {
    $$('.tab').forEach(x => x.classList.remove('active'));
    t.classList.add('active');
    $$('.tab-body').forEach(p => p.hidden = p.dataset.panel !== t.dataset.tab);
  });

  $('#btnPick').onclick = e => { e.stopPropagation(); $('#fileInput').click(); };
  $('#btnSamples').onclick = async e => {
    e.stopPropagation();
    try {
      const res = await api('/api/samples');
      if (!res.files.length) { toast('没有内置示例', true); return; }
      const res2 = await api('/api/use_sample', { name: res.files[0].name });
      const f = res2.file;
      if (!state.files.find(x => x.path === f.path)) state.files.push(f);
      renderFiles();
      selectFile(f);
      toast('已载入示例：' + f.name);
    } catch (err) {
      toast('载入示例失败：' + err.message, true);
    }
  };
  $('#dropzone').onclick = () => $('#fileInput').click();
  $('#fileInput').onchange = e => { uploadFiles(e.target.files); e.target.value = ''; };

  const dz = $('#dropzone');
  ['dragenter', 'dragover'].forEach(ev => dz.addEventListener(ev, e => {
    e.preventDefault(); dz.classList.add('over');
  }));
  ['dragleave', 'drop'].forEach(ev => dz.addEventListener(ev, e => {
    e.preventDefault(); dz.classList.remove('over');
  }));
  dz.addEventListener('drop', e => {
    if (e.dataTransfer.files.length) uploadFiles(e.dataTransfer.files);
  });
  document.addEventListener('dragover', e => e.preventDefault());
  document.addEventListener('drop', e => e.preventDefault());

  $('#btnClear').onclick = () => {
    state.files = [];
    state.active = null;
    renderFiles();
    clearStage();
  };
  $('#btnPlay').onclick = playSource;
  $('#btnPreview').onclick = previewProcessed;
  $('#btnCompareImg').onclick = exportCompare;
  $('#btnCompare').onclick = () => {
    const box = $('#compare');
    box.hidden = !box.hidden;
    if (!box.hidden && state.active) $('#audioSrc').src = '/media/' + encodeURIComponent(state.active.name);
    $('#btnCompare').classList.toggle('on', !box.hidden);
  };
  $('#btnConvert').onclick = convert;
  $('#btnUnlock').onclick = unlockFiles;
  $('#autoUnlock').onchange = e => {
    state.autoUnlock = e.target.checked;
    refreshChain();
  };
  $('#btnOpenOut').onclick = $('#btnOpenOut2').onclick = () =>
    api('/api/open_folder', {}).catch(e => toast(e.message, true));

  $('#chipEnabled').onchange = e => {
    state.chipEnabled = e.target.checked;
    if (state.chipEnabled) {
      applyPreset(state.meta.presets.find(p => p.key === state.preset));
    }
    refreshChain();
  };

  const chipIds = ['bitDepth', 'chipRate', 'squareness', 'gate', 'vibrato', 'arp', 'noise', 'lowpass', 'tapeStop'];
  chipIds.forEach(id => $('#' + id).addEventListener('input', () => { syncChipLabels(); scheduleChain(); }));

  ['volume', 'speed', 'pitch', 'fadeIn', 'fadeOut'].forEach(id =>
    $('#' + id).addEventListener('input', () => { syncChipLabels(); scheduleChain(); }));

  $('#bitrate').addEventListener('input', () => syncChipLabels());
  $('#bitrate').addEventListener('change', () => { updateBitrateForFormat(false); scheduleChain(); });

  ['start', 'end', 'rate', 'bits', 'trimSilence', 'reverse'].forEach(id => {
    $('#' + id).addEventListener('change', scheduleChain);
  });
  ['metaTitle', 'metaArtist', 'metaAlbum', 'suffix', 'outDir'].forEach(id => {
    $('#' + id).addEventListener('input', scheduleChain);
  });

  document.addEventListener('keydown', e => {
    if (e.code === 'Space' && !/INPUT|TEXTAREA|SELECT/.test(e.target.tagName)) {
      e.preventDefault();
      playSource();
    }
  });
  window.addEventListener('resize', () => { if (waveData) drawWave(waveData); });
}

init();
