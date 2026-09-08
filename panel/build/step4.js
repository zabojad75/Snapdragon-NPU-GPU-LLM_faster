async function gpuStart() {
  const model = $('model').value;
  const ctx = $('ctx').value;
  if (!model) {
    toast('no model selected');
    return;
  }
  toast('loading ' + model + ' …');
  try {
    await api('/gpu/start?model=' + encodeURIComponent(model) + '&ctx=' + ctx);
    toast('GPU starting — model loads ~15-60 s');
  } catch (e) {
    toast('ERR ' + e.message, 6000);
  }
}

async function gpuStop() {
  try {
    await api('/gpu/stop');
    toast('GPU stopping…');
  } catch (e) {
    toast('ERR ' + e.message);
  }
}

async function gpuRestart() {
  const model = $('model').value;
  try {
    await api('/gpu/restart?model=' + encodeURIComponent(model));
    toast('GPU restarting…');
  } catch (e) {
    toast('ERR ' + e.message, 6000);
  }
}

async function modelDelete() {
  const model = $('model').value;
  if (!model) {
    toast('no model selected');
    return;
  }
  const info = (S.models || []).find(m => m.name === model);
  const size = info ? ' (' + (info.size / 1e9).toFixed(2) + ' GB)' : '';
  if (!confirm('Delete ' + model + size + ' from disk?\nThis cannot be undone.')) return;
  try {
    const d = await api('/models/delete?model=' + encodeURIComponent(model));
    toast('deleted ' + d.deleted);
  } catch (e) {
    toast('ERR ' + e.message, 6000);
  }
}

async function npuStart() {
  try {
    await api('/npu/start');
    toast('NPU starting…');
  } catch (e) {
    toast('ERR ' + e.message);
  }
}

async function npuStop() {
  try {
    await api('/npu/stop');
    toast('NPU stopping…');
  } catch (e) {
    toast('ERR ' + e.message);
  }
}

async function npuRestart() {
  try {
    await api('/npu/restart');
    toast('NPU restarting…');
  } catch (e) {
    toast('ERR ' + e.message);
  }
}

function ctxChanged() {
  const model = $('model').value;
  const ctx = $('ctx').value;
  if (!model) return;
  const key = model + '@' + ctx;
  if (key === lastEstKey) return;
  lastEstKey = key;
  const sel = $('ctx');
  const hint = $('ctxHint');
  sel.className = '';
  hint.className = '';
  hint.textContent = '~';
  async function run() {
    try {
      const e = await api('/ctx/estimate?model=' + encodeURIComponent(model) + '&ctx=' + ctx);
      if (e.need_gb == null) {
        sel.className = '';
        hint.className = '';
        hint.textContent = '~';
        return;
      }
      const free = (S && S.ram_free != null) ? S.ram_free : null;
      let cls, txt;
      if (free == null) {
        cls = '';
        txt = 'needs ~' + e.need_gb + ' GB';
      } else {
        const gpuHolds = (S.gpu.state === 'ready' || S.gpu.state === 'busy');
        const avail = gpuHolds ? free + (S.gpu_ram || 0) : free;
        const margin = avail - e.need_gb;
        if (margin < 1.5) {
          cls = 'err';
          txt = '✗ needs ~' + e.need_gb + ' GB — will not fit (free ' + free.toFixed(0) + ', swap frees ~' + (S.gpu_ram || 0).toFixed(0) + ' GB)';
        } else if (margin < 6) {
          cls = 'warn';
          txt = '▲ needs ~' + e.need_gb + ' GB — tight, ~' + margin.toFixed(0) + ' GB left';
        } else {
          cls = 'ok';
          txt = '✓ needs ~' + e.need_gb + ' GB — fits';
        }
      }
      sel.className = cls;
      hint.className = cls;
      hint.textContent = txt;
    } catch (e) {
      sel.className = '';
      hint.textContent = '';
    }
  }
  run();
}

function budgetCheck() {
  const t = ($('budgetText').value || '').trim();
  if (!t) {
    toast('paste some text first');
    return;
  }
  const est = Math.round(t.length / 4);
  const npu = est <= 3000 ? 'fits NPU' : 'exceeds NPU (4K)';
  const gpu = est <= 12000 ? 'fits GPU' : 'exceeds GPU (16K)';
  $('budgetOut').textContent = '~' + est + ' tokens → ' + npu + ' · ' + gpu;
}

$('btnStart').addEventListener('click', gpuStart);
$('btnStop').addEventListener('click', gpuStop);
$('btnGpuRestart').addEventListener('click', gpuRestart);
$('btnDel').addEventListener('click', modelDelete);
$('btnNpuStart').addEventListener('click', npuStart);
$('btnNpuStop').addEventListener('click', npuStop);
$('btnNpuRestart').addEventListener('click', npuRestart);
$('btnBudget').addEventListener('click', budgetCheck);
