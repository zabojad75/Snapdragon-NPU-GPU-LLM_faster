async function gpuStart() {
  const model = $('model').value;
  const ctx = $('ctx').value;
  if (!model) {
    toast('no model selected');
    return;
  }
  toast('loading ' + model + ' …');
  try {
    await post('/gpu/start?model=' + encodeURIComponent(model) + '&ctx=' + ctx);
    toast('GPU starting — model loads ~15-60 s');
  } catch (e) {
    toast('ERR ' + e.message, 6000);
  }
}

async function gpuStop() {
  try {
    await post('/gpu/stop');
    toast('GPU stopping…');
  } catch (e) {
    toast('ERR ' + e.message);
  }
}

async function gpuRestart() {
  const model = $('model').value;
  const ctx = $('ctx').value;
  try {
    await post('/gpu/restart?model=' + encodeURIComponent(model) + '&ctx=' + ctx);
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
    const d = await post('/models/delete?model=' + encodeURIComponent(model));
    toast('deleted ' + d.deleted);
  } catch (e) {
    toast('ERR ' + e.message, 6000);
  }
}

async function npuStart() {
  try {
    await post('/npu/start');
    toast('NPU starting…');
  } catch (e) {
    toast('ERR ' + e.message);
  }
}

async function npuStop() {
  try {
    await post('/npu/stop');
    toast('NPU stopping…');
  } catch (e) {
    toast('ERR ' + e.message);
  }
}

async function npuRestart() {
  try {
    await post('/npu/restart');
    toast('NPU restarting…');
  } catch (e) {
    toast('ERR ' + e.message);
  }
}

let estSeq = 0;

function ctxChanged(force = false) {
  const model = $('model').value;
  const ctx = $('ctx').value;
  if (!model) return;
  const key = model + '@' + ctx;
  if (key === lastEstKey && !force) return;
  lastEstKey = key;
  const sel = $('ctx');
  const hint = $('ctxHint');
  sel.className = '';
  hint.className = '';
  hint.textContent = '~';
  const seq = ++estSeq;
  async function run() {
    try {
      const e = await api('/ctx/estimate?model=' + encodeURIComponent(model) + '&ctx=' + ctx);
      // Check if we're still the latest sequence
      if (seq !== estSeq) return;
      
      if (e.need_gb == null) {
        sel.className = '';
        hint.className = '';
        hint.textContent = '~';
        // Clear all option classes when no estimate is available
        const options = $('ctx').options;
        for (let i = 0; i < options.length; i++) {
          options[i].className = '';
        }
        return;
      }
      
      const free = (S && S.ram_free != null) ? S.ram_free : null;
      const ctxVal = parseInt(ctx, 10);
      const gpuHolds = !!(S && S.gpu
        && (S.gpu.state === 'ready' || S.gpu.state === 'busy'));
      // RAM usable for this ctx: on a switch the running instance is stopped
      // first, so its (observed) footprint is added back.
      const avail = free == null ? null
        : (gpuHolds ? free + (S.gpu_ram || 0) : free);
      // gpu.model is the served alias from the log; gpu.model_file is the GGUF filename
      // that the model select holds — accept either so identity actually matches.
      const loadedName = S && S.gpu
        ? (S.gpu.model_file || S.gpu.model || '')
        : '';
      const sameModel = loadedName && (loadedName === model
        || S.gpu.model === model
        || model.startsWith(loadedName) || loadedName.startsWith(model));
      const alreadyLoaded = sameModel && S.gpu.ctx === ctxVal && gpuHolds;

      let cls, txt;
      if (alreadyLoaded) {
        cls = 'ok';
        txt = '✓ loaded' + (free != null ? ' — ' + free.toFixed(0) + ' GB available' : '');
      } else if (free == null) {
        cls = '';
        txt = 'needs ~' + e.need_gb + ' GB';
      } else {
        const margin = avail - e.need_gb;
        const freed = gpuHolds && (S.gpu_ram || 0) > 0;
        if (margin < 1.5) {
          cls = 'err';
          txt = freed
            ? '✗ needs ~' + e.need_gb + ' GB — will not fit (available ' + free.toFixed(0) + ' GB, swap frees ~' + (S.gpu_ram || 0).toFixed(0) + ' GB)'
            : '✗ needs ~' + e.need_gb + ' GB — will not fit (available ' + free.toFixed(0) + ' GB)';
        } else if (margin < 6) {
          cls = 'warn';
          txt = '▲ needs ~' + e.need_gb + ' GB — tight, ~' + margin.toFixed(0) + ' GB left';
        } else {
          cls = 'ok';
          txt = '✓ needs ~' + e.need_gb + ' GB — fits';
        }
      }

      // One colorizer for the whole list: every option is sized on its own
      // (weights + overhead + kv*ctx vs avail), so green/orange/red can all
      // appear at once. Never leave an option class-less: class-less options
      // inherit the select's colour and the list looks single-coloured.
      (function paintOptions() {
        const options = $('ctx').options;
        const haveParts = e.weights_gb != null && e.kv_gb_per_tok != null;
        for (let i = 0; i < options.length; i++) {
          const v = parseInt(options[i].value, 10);
          if (alreadyLoaded && v === ctxVal) { options[i].className = 'ok'; continue; }
          if (free == null || !haveParts) { options[i].className = ''; continue; }
          const need = e.weights_gb + e.overhead_gb + e.kv_gb_per_tok * v;
          const margin = avail - need;
          options[i].className = margin < 1.5 ? 'err' : (margin < 6 ? 'warn' : 'ok');
        }
      })();

      sel.className = cls;
      hint.className = cls;
      hint.textContent = txt;
    } catch (e) {
      sel.className = '';
      hint.textContent = '';
      // Clear all option classes on error
      const options = $('ctx').options;
      for (let i = 0; i < options.length; i++) {
        options[i].className = '';
      }
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
  const maxGpu = parseInt(($('ctx') && $('ctx').value) || '16384', 10) || 16384;
  const gpu = est <= maxGpu ? 'fits GPU (' + maxGpu + ')' : 'exceeds GPU (' + maxGpu + ')';
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
