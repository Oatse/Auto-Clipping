/**
 * jobs.js — Job list, job detail modal, SSE log streaming, system info
 */

import { apiFetch, timeAgo, escHtml, showScreen } from './utils.js';
import * as S from './state.js';
import { fetchTranscript } from './upload.js';
import { openPreviewScreen } from './preview.js';

// ── DOM Refs ───────────────────────────────────────────────────────────────
const jobsList        = document.getElementById('jobsList');
const clipJobsList    = document.getElementById('clipJobsList');
const jobsPane        = document.getElementById('jobsPane');
const clipsPane       = document.getElementById('clipsPane');
const jobsToggleBtns  = document.querySelectorAll('.jobs-toggle-btn');
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
    const dot  = systemStatus.querySelector('.status-dot');
    const text = systemStatus.querySelector('.status-text');

    const allOk = data.packages.ffmpeg && data.packages.whisperx;
    dot.className = 'status-dot ' + (allOk ? 'ok' : 'warn');
    text.textContent = allOk
      ? `Ready · ${data.cuda_available ? data.gpu_name || 'GPU' : 'CPU'} · Torch ${data.torch_version}`
      : 'Setup required — FFmpeg or WhisperX missing';

    const items = [
      { label: 'FFmpeg',    ok: data.packages.ffmpeg },
      { label: 'WhisperX', ok: data.packages.whisperx },
      { label: 'Pycaps',   ok: data.packages.pycaps },
      { label: 'CUDA',     ok: data.cuda_available, warn: !data.cuda_available },
    ];
    sysGrid.innerHTML = items.map(i => `
      <div class="sys-item ${i.ok ? 'ok' : i.warn ? 'warn' : 'err'}">
        <span class="dot"></span>${i.label}
      </div>
    `).join('');

    // Populate whisper model selector dynamically from server
    if (data.whisper_models && whisperModel) {
      const currentVal = whisperModel.value;
      whisperModel.innerHTML = '';
      const modelIcons = { 'whisperx': '🧠', 'faster-whisper': '🎌', 'elevenlabs': '🔊' };
      for (const [key, m] of Object.entries(data.whisper_models)) {
        const opt = document.createElement('option');
        opt.value = key;
        const icon = modelIcons[m.type] || '🧠';
        opt.textContent = `${icon} ${m.label} — ${m.description}`;
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
  jobsToggleBtns.forEach(btn => btn.classList.toggle('active', btn.dataset.jobsTab === tab));

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
  try {
    const jobs = await apiFetch('/api/jobs');
    renderJobs(jobs);
  } catch (e) {
    console.error('Load jobs failed:', e);
  }
}

function renderJobs(jobs) {
  if (!jobs.length) {
    jobsList.innerHTML = `
      <div class="empty-state">
        <div class="empty-icon">📋</div>
        <p>No jobs yet. Upload a video to get started.</p>
      </div>`;
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
  try {
    const transcript = await fetchTranscript(jobId);
    if (!transcript || !transcript.length) {
      alert('Transcript belum tersedia untuk job ini.');
      return;
    }
    S.setActiveJobId(jobId);
    S.setTranscriptData(transcript);
    openPreviewScreen(jobId);
  } catch (e) {
    console.error('Resume job failed:', e);
    alert('Gagal membuka job untuk editing: ' + (e.message || e));
  }
}

// ── Clip Jobs List ─────────────────────────────────────────────────────────
async function loadClipJobsList() {
  if (!clipJobsList) return;

  clipJobsList.innerHTML = '<div class="clip-picker-loading">Loading clips...</div>';
  try {
    const data = await apiFetch('/api/clip-finder/available-clips');

    if (!data || data.length === 0) {
      clipJobsList.innerHTML = `
        <div class="empty-state">
          <div class="empty-icon">◻</div>
          <p>No clips available. Use Clip Finder to download clips first.</p>
        </div>`;
      return;
    }

    const clipCards = [];
    data.forEach(job => {
      (job.clips || []).forEach(clip => {
        const start = typeof clip.start === 'number' ? clip.start : 0;
        const end = typeof clip.end === 'number' ? clip.end : 0;
        const duration = Math.max(0, end - start);
        const durationLabel = duration > 0 ? formatClipDuration(duration) : '--';

        const title = clip.title || clip.filename || `Clip ${clip.index + 1}`;
        const sourceTitle = job.video_title || job.url || job.job_id;

        clipCards.push(`
          <div class="clip-job-card">
            <div class="clip-job-video-wrap">
              <video class="clip-job-video" preload="metadata" muted playsinline
                src="/api/clip-finder/clips/${encodeURIComponent(job.job_id)}/${clip.index}/stream"></video>
              <span class="clip-job-duration">${durationLabel}</span>
            </div>
            <div class="clip-job-info">
              <div class="clip-job-title">${escHtml(title)}</div>
              <div class="clip-job-meta">${escHtml(sourceTitle)}</div>
              <div class="clip-job-meta">${escHtml(clip.filename || '')}</div>
            </div>
            <button class="clip-job-use" data-path="${escHtml(clip.path)}" data-file="${escHtml(clip.filename || title)}">Use clip</button>
          </div>
        `);
      });
    });

    if (clipCards.length === 0) {
      clipJobsList.innerHTML = `
        <div class="empty-state">
          <div class="empty-icon">◻</div>
          <p>No clips available. Use Clip Finder to download clips first.</p>
        </div>`;
      return;
    }

    clipJobsList.innerHTML = clipCards.join('');

    clipJobsList.querySelectorAll('.clip-job-card').forEach(card => {
      const video = card.querySelector('.clip-job-video');
      const durationEl = card.querySelector('.clip-job-duration');
      if (video) {
        video.addEventListener('loadedmetadata', () => {
          if (!durationEl) return;
          const seconds = Number(video.duration);
          if (Number.isFinite(seconds) && seconds > 0) {
            durationEl.textContent = formatClipDuration(seconds);
          }
        });
        video.addEventListener('mouseenter', () => {
          video.play().catch(() => {});
        });
        video.addEventListener('mouseleave', () => {
          video.pause();
          video.currentTime = 0;
        });
      }
    });

    clipJobsList.querySelectorAll('.clip-job-use').forEach(btn => {
      btn.addEventListener('click', () => {
        import('./upload.js').then(m => m.startJobFromClip(btn.dataset.path, btn.dataset.file || 'clip.mp4'));
      });
    });

  } catch (err) {
    clipJobsList.innerHTML = `<div class="clip-picker-empty">Failed to load clips: ${escHtml(err.message)}</div>`;
  }
}

function formatClipDuration(seconds) {
  const total = Math.max(0, Math.floor(seconds));
  const mins = Math.floor(total / 60);
  const secs = total % 60;
  if (mins > 0) return `${mins}m ${secs}s`;
  return `${secs}s`;
}

// ── Modal ──────────────────────────────────────────────────────────────────
function setupModal() {
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
  if (!confirm('Cancel this job?')) return;
  await apiFetch(`/api/jobs/${jobId}`, { method: 'DELETE' });
  closeModal();
  loadJobs();
}

async function deleteJob(jobId) {
  if (!confirm('Remove this job from the list?')) return;
  await apiFetch(`/api/jobs/${jobId}`, { method: 'DELETE' });
  closeModal();
  loadJobs();
}
