const QUANTS = ["Q4_0","Q4_1","Q5_0","Q5_1","Q8_0","Q3_K_M","Q4_K_M","Q5_K_M","Q6_K","IQ4_XS"];

function renderModelsList() {
  const modelNames = S.models.map(m => m.name);
  const currentOptions = Array.from($('qModel').options).map(o => o.value);
  if (JSON.stringify(modelNames) === JSON.stringify(currentOptions)) return;

  const modelOptions = S.models.map(m => `<option value="${esc(m.name)}">${esc(m.name)}</option>`).join('');
  $('qModel').innerHTML = modelOptions;

  const modelLines = S.models.map(m => {
    const sizeGb = (m.size / 1e9).toFixed(2);
    const expected = m.expected;
    const extra = expected && m.size < expected ? ` ▮${Math.round((m.size / expected) * 100)}%` : '';
    return `<div class="label mono">${esc(m.name)} (${sizeGb} GB)${extra}</div>`;
  }).join('');
  $('modelsList').innerHTML = modelLines;

  if ($('qTarget').innerHTML === '') {
    const quantOptions = QUANTS.map(q => `<option value="${q}">${q}</option>`).join('');
    $('qTarget').innerHTML = quantOptions;
  }
}

function renderJobs() {
  if (S.jobs.length === 0) {
    $('jobsList').innerHTML = '<span class="label">none yet</span>';
    return;
  }

  const jobElements = S.jobs.map(job => {
    const header = `<div class="job-header"><b>${esc(job.label)}</b> — ${esc(job.status)} (${job.elapsed}s)</div>`;
    const logLine = job.log && job.log.length ? `<pre>${esc(job.log.slice(-2).join('\n'))}</pre>` : '';
    const cancelButton = job.status === 'running' ? `<button class="btn sm job-cancel" data-id="${job.id}">cancel</button>` : '';
    return `<div class="job ${job.status}">${header}${logLine}${cancelButton}</div>`;
  }).join('');

  $('jobsList').innerHTML = jobElements;
}

async function cancelJob(id) {
  try {
    await api('/jobs/' + id + '/cancel', {method: 'POST'});
    toast('cancelled');
  } catch (e) {
    toast('ERR ' + e.message);
  }
}

async function doDownload() {
  const url = $('dlUrl').value.trim();
  if (!url) {
    toast('paste a URL first');
    return;
  }
  try {
    const d = await api('/download?url=' + encodeURIComponent(url));
    toast('download started: ' + d.file);
  } catch (e) {
    toast('ERR ' + e.message, 6000);
  }
}

async function doQuantize() {
  const model = $('qModel').value;
  const quant = $('qTarget').value;
  if (!model) {
    toast('select a model');
    return;
  }
  try {
    const d = await api('/quantize?model=' + encodeURIComponent(model) + '&quant=' + quant);
    toast('quantize started → ' + d.out);
  } catch (e) {
    toast('ERR ' + e.message, 6000);
  }
}

async function doNpuPull() {
  const repo = $('npRepo').value.trim();
  if (!repo) {
    toast('enter org/name');
    return;
  }
  try {
    await api('/npu/pull?repo=' + encodeURIComponent(repo));
    toast('NPU pull started');
  } catch (e) {
    toast('ERR ' + e.message, 6000);
  }
}

$('btnDl').addEventListener('click', doDownload);
$('btnQuantize').addEventListener('click', doQuantize);
$('btnNpuPull').addEventListener('click', doNpuPull);
$('jobsList').addEventListener('click', (e) => {
  const button = e.target.closest('.job-cancel');
  if (button) {
    cancelJob(button.dataset.id);
  }
});
