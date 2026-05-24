/**
 * jobs.js — Job list, job detail modal, SSE log streaming, system info
 */

import { apiFetch, timeAgo, escHtml, showScreen, formatClipDuration, toast, confirmDialog } from './utils.js';
import * as S from './state.js';
import { fetchTranscript, loadClipJobsList } from './upload.js';
import { openPreviewScreen } from './preview.js';

// ── DOM Refs ───────────────────────────────────────────────────────────────
const jobsList        = document.getElementById('jobsList');
const clipJobsList    = document.getElementById('clipJobsList');
const jobsPane        = document.getElementById('jobsPane');
const clipsPane       = document.getElementById('clipsPane');
const jobsToggleBtns  = document.querySelectorAll('#jobsToggle button[data-jobs-tab]');
const systemStatus    = document.getElementById('systemStatus');
const sysGrid         = document.getElementById('sysGrid');
const modalOverlay    = document.getElementById('modalOverlay');
const modalClose      = document.getElementById('modalClose');
const modalTitle      = document.getElementById('modalTitle');
const modalId         = document.getElementById('modalId');
const modalPhase      = document.getElementById('modalPhase');
const modalPct        = document.getElementById('modalPct');
const modalProgressBar = document.getElementById('modalProgressBar');
const phaseDots       = document.getElementById('phaseDots');
const logBody         = document.getElementById('logBody');
const logBadge        = document.getElementById('logBadge');
const modalActions    = document.getElementById('modalActions');
const whisperModel    = document.getElementById('whisperModel');

// ── Setup ──────────────────────────────────────────────────────────────────
export function setupJobs() {
  setupModal();
  setupJobsPanelToggle();
}

// ── System Info ────────────────────────────────────────────────────────────
export async function loadSystemInfo() {
  try {
    const data = await apiFetch('/api/system');
    // The floating-pill nav status is owned by nav.js (it renders the
    // clickable "All systems online" pill + popup). Older legacy markup
    // exposed `.status-dot` / `.status-text` on this same element — we
    // explicitly skip touching them here so we don't fight nav.js for
    // the pill text.
    const ffmpegOk     = !!data.packages.ffmpeg;
    const elevenlabsOk = !!data.packages.elevenlabs;
    const geminiOk     = !!(data.env && data.env.gemini_keys_set);
    const deeplOk      = !!(data.env && data.env.deepl_key_set);
    const allOk        = ffmpegOk && elevenlabsOk && geminiOk;
    if (dot) dot.className = 'status-dot ' + (allOk ? 'ok' : 'warn');
    if (text) {
    if (allOk) {
      const gpuLabel = data.cuda_available
        ? (data.gpu_name || 'GPU')
        : 'CPU';
      const torchPart = data.torch_version
        ? ` · Torch ${data.torch_version}`
        : '';
      text.textContent = `Ready · ${gpuLabel}${torchPart}`;
    } else if (!elevenlabsOk) {
      text.textContent = 'Setup required — ELEVENLABS_API_KEY missing';
    } else if (!geminiOk) {
      text.textContent = 'Setup required — GEMINI_API_KEY missing (translate disabled)';
    } else {
      text.textContent = 'Setup required — FFmpeg missing';
    }
    } /* end if(text) */

    // System status grid surfaces the same backend env signals so the
    // user sees Gemini + DeepL availability at a glance.  DeepL is
    // optional (fallback only) so its missing state is "warn" rather
    // than "err".
    const items = [
      { label: 'FFmpeg',     ok: ffmpegOk },
      { label: 'ElevenLabs', ok: elevenlabsOk },
      { label: 'Gemini',     ok: geminiOk },
      { label: 'DeepL',      ok: deeplOk, warn: !deeplOk,
        title: deeplOk
          ? 'DeepL fallback ready'
          : 'DeepL fallback disabled (optional). Translate falls back to source-language text if Gemini fails.' },
      { label: 'Pycaps',     ok: !!data.packages.pycaps },
      { label: 'GPU',        ok: !!data.cuda_available, warn: !data.cuda_available },
    ];
    if (sysGrid) {
      sysGrid.innerHTML = items.map(i => `
        <div class="def-list__row sys-item ${i.ok ? 'ok' : i.warn ? 'warn' : 'err'}"${i.title ? ` title="${i.title}"` : ''}>
          <span class="def-list__key"><span class="dot" style="display:inline-block;width:6px;height:6px;border-radius:999px;background:${i.ok ? 'var(--c-success)' : i.warn ? 'var(--c-warning)' : 'var(--c-danger)'};margin-right:8px;"></span>${i.label}</span>
          <span class="def-list__val">${i.ok ? 'OK' : (i.warn ? 'optional' : 'missing')}</span>
        </div>
      `).join('');
    }

    // Populate transcription engine dropdown.
    // The server now returns a single ElevenLabs entry, but we keep the
    // populate-from-server logic so future engines can be added without
    // changing the frontend.
    const engines = data.stt_engines || data.whisper_models;
    if (engines && whisperModel) {
      const currentVal = whisperModel.value;
      whisperModel.innerHTML = '';
      const engineIcons = { 'elevenlabs': '🔊' };
      for (const [key, m] of Object.entries(engines)) {
        const opt = document.createElement('option');
        opt.value = key;
        const icon = engineIcons[m.type] || '🔊';
        opt.textContent = `${icon} ${m.label}${m.description ? ' — ' + m.description : ''}`;
        whisperModel.appendChild(opt);
      }
      if ([...whisperModel.options].some(o => o.value === currentVal)) {
        whisperModel.value = currentVal;
      }
    }
  } catch (e) {
    console.error('System info failed:', e);
  }
}

// ── Jobs Panel Toggle ──────────────────────────────────────────────────────
function setupJobsPanelToggle() {
  if (!jobsToggleBtns || jobsToggleBtns.length === 0) return;

  jobsToggleBtns.forEach(btn => {
    btn.addEventListener('click', () => {
      const tab = btn.dataset.jobsTab;
      if (!tab || tab === S.jobsPanelTab) return;
      switchJobsPanelTab(tab);
    });
  });
}

function switchJobsPanelTab(tab) {
  S.setJobsPanelTab(tab);
  jobsToggleBtns.forEach(btn => btn.classList.toggle('is-active', btn.dataset.jobsTab === tab));

  if (jobsPane) jobsPane.classList.toggle('hidden', tab !== 'jobs');
  if (clipsPane) clipsPane.classList.toggle('hidden', tab !== 'clips');

  if (tab === 'clips') {
    loadClipJobsList();
  } else {
    loadJobs();
  }
}

// ── Jobs List ──────────────────────────────────────────────────────────────
export async function loadJobs() {
  // Show skeleton placeholders during the first paint so the panel
  // doesn't snap from "empty" to "filled".  Reusing renderJobs would
  // require ferrying state, so we paint inline once and let the API
  // response replace the markup.
  if (jobsList && !jobsList.dataset.jobsHydrated) {
    jobsList.innerHTML = _renderJobsSkeleton();
  }
  try {
    const jobs = await apiFetch('/api/jobs');
    if (jobsList) jobsList.dataset.jobsHydrated = '1';
    renderJobs(jobs);
  } catch (e) {
    console.error('Load jobs failed:', e);
  }
}

function _renderJobsSkeleton(count = 3) {
  const card = `
    <div class="skeleton-job-card">
      <div class="skeleton-row">
        <div class="skeleton skeleton-line title"></div>
        <div class="skeleton skeleton-line badge"></div>
      </div>
      <div class="skeleton skeleton-line meta"></div>
      <div class="skeleton skeleton-line bar"></div>
    </div>
  `;
  return Array.from({ length: count }, () => card).join('');
}

function renderJobs(jobs) {
  if (!jobs.length) {
    jobsList.innerHTML = `
      <div class="empty-state">
        <div class="empty-icon">📋</div>
        <p class="empty-title">No jobs yet</p>
        <p class="empty-sub">Upload a video below to start your first transcription. Jobs you create will show up here for resume + edit.</p>
        <button type="button" class="empty-cta" id="emptyJobsCta">
          ↓ Jump to upload
        </button>
      </div>`;
    // Wire the CTA — scroll to the drop-zone and emphasise it briefly.
    const cta = document.getElementById('emptyJobsCta');
    if (cta) {
      cta.addEventListener('click', () => {
        const drop = document.getElementById('dropZone');
        if (!drop) return;
        drop.scrollIntoView({ behavior: 'smooth', block: 'center' });
        drop.classList.add('drop-zone--highlight');
        setTimeout(() => drop.classList.remove('drop-zone--highlight'), 1400);
      });
    }
    return;
  }

  jobsList.innerHTML = jobs.map(job => {
    const barClass = job.status === 'completed' ? 'done' : job.status === 'failed' ? 'failed' : '';
    const elapsed = job.completed_at && job.started_at
      ? ` · ${Math.round(job.completed_at - job.started_at)}s`
      : '';
    const hasTranscript = job.has_transcript === true;
    const actionsBtnHtml = hasTranscript ? `
      <div class="job-actions">
        <button class="btn-resume-job" data-job-id="${job.id}">
          ▶ Continue Editing
        </button>
        <button class="btn-view-job" data-job-id="${job.id}">
          Details
        </button>
      </div>` : '';
    return `
      <div class="job-card status-${job.status}" data-job-id="${job.id}">
        <div class="job-top">
          <div class="job-name" title="${job.filename}">${job.filename}</div>
          <span class="job-badge badge-${job.status}">${job.status}</span>
        </div>
        <div class="job-meta">
          <span>🌐 ${job.target_language.toUpperCase()}</span>
          <span>📁 ${job.id.slice(0,8)}</span>
          <span>${timeAgo(job.created_at)}${elapsed}</span>
        </div>
        <div class="job-progress-wrap">
          <div class="job-progress-bar-bg">
            <div class="job-progress-bar ${barClass}" style="width:${job.progress_pct}%"></div>
          </div>
          <span class="job-progress-pct">${Math.round(job.progress_pct)}%</span>
        </div>
        ${actionsBtnHtml}
      </div>`;
  }).join('');

  // Attach event listeners using event delegation instead of inline onclick
  jobsList.querySelectorAll('.job-card').forEach(card => {
    const jobId = card.dataset.jobId;

    card.addEventListener('click', (e) => {
      // Don't open modal if clicking action buttons
      if (e.target.closest('.job-actions')) return;
      openModal(jobId);
    });

    const resumeBtn = card.querySelector('.btn-resume-job');
    if (resumeBtn) {
      resumeBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        resumeJob(resumeBtn.dataset.jobId);
      });
    }

    const viewBtn = card.querySelector('.btn-view-job');
    if (viewBtn) {
      viewBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        openModal(viewBtn.dataset.jobId);
      });
    }
  });
}

// ── Resume Job ─────────────────────────────────────────────────────────────
async function resumeJob(jobId) {
  // The editor lives on its own route now. If we're on a non-editor page,
  // hop to /editor/{id} so the editor templates + DOM are loaded fresh.
  const onEditor = document.body && document.body.classList.contains('p-editor');
  if (!onEditor) {
    window.location.href = `/editor/${jobId}`;
    return;
  }
  try {
    const transcript = await fetchTranscript(jobId);
    if (!transcript || !transcript.length) {
      toast.warn('Transcript belum tersedia untuk job ini.');
      return;
    }
    S.setActiveJobId(jobId);
    S.setTranscriptData(transcript);
    openPreviewScreen(jobId);
  } catch (e) {
    console.error('Resume job failed:', e);
    toast.error('Gagal membuka job untuk editing: ' + (e.message || e));
  }
}

// ── Clip Jobs List ─────────────────────────────────────────────────────────
// loadClipJobsList imported from upload.js

// formatClipDuration imported from utils.js

// ── Modal ──────────────────────────────────────────────────────────────────
function setupModal() {
  // Modal markup is editor-only. Skip wiring on pages that don't ship it.
  if (!modalClose || !modalOverlay) return;
  modalClose.addEventListener('click', closeModal);
  modalOverlay.addEventListener('click', (e) => {
    if (e.target === modalOverlay) closeModal();
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') closeModal();
  });
}

async function openModal(jobId) {
  S.setActiveJobId(jobId);
  modalOverlay.classList.remove('hidden');
  logBody.innerHTML = '';

  phaseDots.innerHTML = Array.from({length: 4}, (_, i) =>
    `<div class="phase-dot" id="dot-${i+1}"></div>`
  ).join('');

  await refreshModal(jobId);
  startSSE(jobId);
  S.setPollInterval(setInterval(() => refreshModal(jobId), 2000));
}

function closeModal() {
  modalOverlay.classList.add('hidden');
  S.setActiveJobId(null);
  if (S.sseSource)    { S.sseSource.close(); S.setSseSource(null); }
  if (S.pollInterval) { clearInterval(S.pollInterval); S.setPollInterval(null); }
}

async function refreshModal(jobId) {
  try {
    const job = await apiFetch(`/api/jobs/${jobId}`);
    updateModalUI(job);
    await loadJobs();
  } catch (e) {
    console.error('Modal refresh failed:', e);
  }
}

function updateModalUI(job) {
  modalTitle.textContent = job.filename;
  modalId.textContent = `Job ID: ${job.id}`;
  modalPhase.textContent = job.phase_label;
  modalPct.textContent = `${Math.round(job.progress_pct)}%`;

  modalProgressBar.style.width = `${job.progress_pct}%`;
  modalProgressBar.className = 'progress-bar' +
    (job.status === 'completed' ? ' done' : job.status === 'failed' ? ' failed' : '');

  for (let i = 1; i <= 7; i++) {
    const dot = document.getElementById(`dot-${i}`);
    if (!dot) continue;
    dot.className = 'phase-dot';
    if (i < job.current_phase) dot.classList.add('done');
    else if (i === job.current_phase) dot.classList.add('active');
  }

  logBadge.className = 'log-badge';
  if (job.status === 'completed') { logBadge.textContent = 'DONE'; logBadge.classList.add('done'); }
  else if (job.status === 'failed') { logBadge.textContent = 'FAILED'; logBadge.classList.add('failed'); }
  else { logBadge.textContent = 'LIVE'; }

  renderModalActions(job);
}

function renderModalActions(job) {
  let html = '';
  if (job.status === 'completed' && job.output_file) {
    html += `<a href="/api/download/${job.id}" class="btn-success" download>⬇ Download Output</a>`;
  }
  if (job.status === 'running' || job.status === 'queued') {
    html += `<button class="btn-danger" data-action="cancel" data-job-id="${job.id}">✕ Cancel</button>`;
  }
  html += `<button class="btn-secondary" data-action="delete" data-job-id="${job.id}">🗑 Remove</button>`;
  modalActions.innerHTML = html;

  // Attach event listeners
  modalActions.querySelectorAll('button[data-action]').forEach(btn => {
    btn.addEventListener('click', () => {
      const action = btn.dataset.action;
      const jid = btn.dataset.jobId;
      if (action === 'cancel') cancelJob(jid);
      else if (action === 'delete') deleteJob(jid);
    });
  });
}

// ── SSE Log Streaming ──────────────────────────────────────────────────────
function startSSE(jobId) {
  if (S.sseSource) S.sseSource.close();
  S.setSseSource(new EventSource(`/api/jobs/${jobId}/log`));

  S.sseSource.onmessage = (e) => {
    const data = JSON.parse(e.data);
    if (data.line) appendLog(data.line);
    if (data.done) { S.sseSource.close(); S.setSseSource(null); }
  };
  S.sseSource.onerror = () => { S.sseSource.close(); S.setSseSource(null); };
}

function appendLog(line) {
  const div = document.createElement('div');
  div.className = 'log-line';
  if (line.includes('✗') || line.includes('Error') || line.includes('failed')) div.classList.add('error');
  else if (line.includes('✓') || line.includes('complete')) div.classList.add('success');
  else if (line.includes('WARNING') || line.includes('warn')) div.classList.add('warn');
  div.textContent = line;
  logBody.appendChild(div);
  logBody.scrollTop = logBody.scrollHeight;
}

// ── Job Actions ────────────────────────────────────────────────────────────
async function cancelJob(jobId) {
  const ok = await confirmDialog('Cancel this job?', {
    title: 'Cancel job',
    confirmText: 'Cancel job',
    cancelText: 'Keep running',
    danger: true,
  });
  if (!ok) return;
  await apiFetch(`/api/jobs/${jobId}`, { method: 'DELETE' });
  closeModal();
  loadJobs();
}

async function deleteJob(jobId) {
  const ok = await confirmDialog('Remove this job from the list?', {
    title: 'Remove job',
    confirmText: 'Remove',
    danger: true,
  });
  if (!ok) return;
  await apiFetch(`/api/jobs/${jobId}`, { method: 'DELETE' });
  closeModal();
  loadJobs();
}
