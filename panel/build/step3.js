const $ = id => document.getElementById(id);
function esc(s) { return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;'); }
async function api(path, opts) { const r = await fetch('/api'+path, opts); const d = await r.json(); if (!r.ok) throw new Error(d.detail || r.statusText); return d; }
function toast(msg, ms=3500) { $('toast').innerHTML = esc(msg); $('toast').style.display = 'block'; setTimeout(() => $('toast').style.display = 'none', ms); }
function age(ts) { const s = Math.floor((Date.now() - ts*1000)/1000); return s < 60 ? s+'s' : Math.floor(s/60)+'m'; }
let S = null;
function chip(el, state, label) { el.className = 'chip ' + state; el.textContent = label; }

let lastState = {gpu:'?', npu:'?'};
let lastReflected = null;
let lastRamSeen = null;
let lastEstKey = '';

async function poll() {
  try { S = await api('/status'); } catch { return; }
  const g = S.gpu, n = S.npu;
  
  let gpuLabel = g.state;
  if (g.state === 'ready') gpuLabel = g.model ? g.model.split('/').pop().slice(0,22) : 'ready';
  else if (g.state === 'busy') gpuLabel = (g.model ? g.model.split('/').pop().slice(0,22) + ' · ' : '') + (g.toks ? g.toks.toFixed(0)+' t/s' : 'busy');
  else if (g.state === 'starting') gpuLabel = 'starting… '+age(g.since);
  else if (g.state === 'stopping') gpuLabel = 'stopping…';
  else if (g.state === 'crashed') gpuLabel = 'CRASHED';
  chip($('gpuChip'), g.state, gpuLabel);
  
  let npuLabel = n.state;
  if (n.state === 'ready') npuLabel = 'ready';
  else if (n.state === 'busy') npuLabel = n.detail === 'busy · health slow' ? 'busy · health slow' : 'generating…';
  else if (n.state === 'starting') npuLabel = 'starting… '+age(n.since);
  else if (n.state === 'stopping') npuLabel = 'stopping…';
  else if (n.state === 'crashed') npuLabel = 'CRASHED';
  chip($('npuChip'), n.state, npuLabel);
  
  $('dWeb').className = 'dot' + (S.webui ? ' on' : ' err');
  
  if (S.ram_free !== null) {
    $('ramInfo').textContent = 'RAM ' + S.ram_free.toFixed(1) + ' GB free';
    $('ramFreeText').textContent = 'RAM ' + S.ram_free.toFixed(1) + ' GB free';
    const ramPercent = Math.min(100, S.ram_free/48*100);
    $('ramBarMiniFill').style.width = ramPercent + '%';
    $('ramBarFill').style.width = ramPercent + '%';
    $('ramBarMiniFill').className = (S.ram_free < 6 ? 'err' : S.ram_free < 12 ? 'warn' : 'ok');
    $('ramBarFill').className = (S.ram_free < 6 ? 'err' : S.ram_free < 12 ? 'warn' : 'ok');
  }
  
  $('clock').textContent = S.now;
  $('gwInfo').textContent = 'gateway ' + S.gateway + ' · NPU ' + (S.npu.loaded ? 'model loaded' : 'on-demand');
  
  $('gpuDetail').textContent = (g.model ? g.model : 'no model') + (g.toks ? ' · '+g.toks.toFixed(1)+' t/s' : '');
  $('npuDetail').textContent = n.detail || '–';
  $('gpuLog').textContent = (g.log_tail || []).join('\n');
  $('npuLog').textContent = (n.log_tail || []).join('\n');
  
  const modelNames = S.models.map(m => m.name);
  const currentModelNames = Array.from($('model').options).map(o => o.value);
  if (JSON.stringify(modelNames) !== JSON.stringify(currentModelNames)) {
    const selected = $('model').value;
    $('model').innerHTML = '';
    S.models.forEach(m => {
      const opt = document.createElement('option');
      opt.value = m.name;
      let text = `${m.name} (${(m.size/1e9).toFixed(1)} GB)`;
      if (m.expected && m.size < m.expected) text += ` ▮${Math.round(100*m.size/m.expected)}%`;
      opt.textContent = text;
      $('model').appendChild(opt);
    });
    if (modelNames.includes(selected)) $('model').value = selected;
  }
  
  if (g.model && g.model !== lastReflected) {
    const modelSelect = $('model');
    for (let i = 0; i < modelSelect.options.length; i++) {
      if (modelSelect.options[i].value.includes(g.model)) {
        modelSelect.value = modelSelect.options[i].value;
        break;
      }
    }
    lastReflected = g.model;
  }
  
  const estKey = $('model').value + '@' + $('ctx').value;
  if (estKey !== lastEstKey || (S.ram_free != null && lastRamSeen !== S.ram_free)) {
    lastRamSeen = S.ram_free;
    ctxChanged();
  }
  
  renderJobs();
  renderModelsList();
  
  const gpuLive = (g.state==='ready'||g.state==='busy');
  $('btnStart').disabled = (g.state==='starting'||g.state==='stopping');
  $('btnStart').textContent = gpuLive ? 'Switch' : 'Start';
  $('btnStop').disabled = !(g.state==='ready'||g.state==='busy'||g.state==='starting');
  $('btnGpuRestart').disabled = !(g.state==='ready'||g.state==='busy'||g.state==='crashed');
  
  $('btnNpuStart').disabled = !(n.state==='off'||n.state==='crashed');
  $('btnNpuStop').disabled = !(n.state==='ready'||n.state==='busy'||n.state==='starting');
  $('btnNpuRestart').disabled = !(n.state==='ready'||n.state==='busy'||n.state==='crashed');
  
  if (lastState.gpu !== '?' && lastState.gpu !== g.state) {
    if (g.state === 'crashed') toast('⚠ GPU server crashed', 6000);
    else if (g.state === 'ready') toast('GPU ready: '+ (g.model||''), 3000);
  }
  if (lastState.npu !== '?' && lastState.npu !== n.state) {
    if (n.state === 'crashed') toast('⚠ NPU server crashed', 6000);
    else if (n.state === 'ready') toast('NPU ready: '+ (n.detail||''), 3000);
  }
  
  lastState.gpu = g.state;
  lastState.npu = n.state;
}

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  if (theme === 'light') {
    $('iconSun').style.display = 'block';
    $('iconMoon').style.display = 'none';
  } else {
    $('iconSun').style.display = 'none';
    $('iconMoon').style.display = 'block';
  }
  localStorage.setItem('llmnpu-theme', theme);
}

function toggleTheme() {
  const theme = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
  applyTheme(theme);
}

function switchView(view) {
  if (view === 'chat') {
    $('view-chat').classList.remove('hidden');
    $('view-dash').classList.add('hidden');
    $('btnChat').classList.add('active');
    $('btnDash').classList.remove('active');
  } else {
    $('view-chat').classList.add('hidden');
    $('view-dash').classList.remove('hidden');
    $('btnChat').classList.remove('active');
    $('btnDash').classList.add('active');
  }
}

function saveSel() {
  localStorage.setItem('lastModel', $('model').value);
  ctxChanged();
}

$('themeBtn').addEventListener('click', toggleTheme);
$('btnChat').addEventListener('click', () => switchView('chat'));
$('btnDash').addEventListener('click', () => switchView('dash'));
$('model').addEventListener('change', saveSel);
$('ctx').addEventListener('change', ctxChanged);

const initial = localStorage.getItem('llmnpu-theme') || (window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark');
applyTheme(initial);
setInterval(poll, 2000);

let lastModelRestored = false;
const restoreLastModel = () => {
  if (!lastModelRestored && S && S.models.length > 0) {
    const last = localStorage.getItem('lastModel');
    if (last && S.models.some(m => m.name === last)) {
      $('model').value = last;
      ctxChanged();
    }
    lastModelRestored = true;
  }
};
poll().then(restoreLastModel);
