/**
 * render.js — Render pipeline: render options modal, start render, AE export
 */

import { apiFetch, showScreen, toast } from './utils.js';
import * as S from './state.js';
import { collectStyle } from './subtitleEngine.js';
import { loadJobs } from './jobs.js';
import { collectEffectsConfig } from './effects.js';

// ── DOM Refs ───────────────────────────────────────────────────────────────
const previewVideo   = document.getElementById('previewVideo');
const startRenderBtn = document.getElementById('startRenderBtn');
const renderPhases   = document.getElementById('renderPhases');
const renderFile     = document.getElementById('renderFile');
const renderStatus   = document.getElementById('renderStatus');
const renderLog      = document.getElementById('renderLog');
const renderBar      = document.getElementById('renderBar');
const exportAEBtn    = document.getElementById('exportAEBtn');

// ── Setup ──────────────────────────────────────────────────────────────────
export function setupRender() {
  setupAEExport();
}

// ── Render Options Modal ───────────────────────────────────────────────────
export function openRenderOptionsModal() {
  if (!S.activeJobId) return;

  const overlay  = document.getElementById('renderOptionsOverlay');
  const closeBtn = document.getElementById('renderOptionsClose');
  const cancelBtn = document.getElementById('renderOptionsCancel');
  const confirmBtn = document.getElementById('renderOptionsConfirm');
  const optRefined  = document.getElementById('renderOptRefined');
  const optOriginal = document.getElementById('renderOptOriginal');

  // Hide "original" option if original transcript is not available
  if (!S.originalTranscriptData || S.originalTranscriptData.length === 0) {
    optOriginal.style.display = 'none';
  } else {
    optOriginal.style.display = 'flex';
  }

  // Reset selection to refined
  optRefined.querySelector('input').checked = true;
  optRefined.classList.add('selected');
  optOriginal.classList.remove('selected');

  // Radio change → update card styling
  const radios = overlay.querySelectorAll('input[name="renderTranscriptSource"]');
  radios.forEach(r => {
    r.onchange = () => {
      optRefined.classList.toggle('selected', optRefined.querySelector('input').checked);
      optOriginal.classList.toggle('selected', optOriginal.querySelector('input').checked);
    };
  });

  overlay.classList.remove('hidden');

  const close = () => overlay.classList.add('hidden');
  closeBtn.onclick = close;
  cancelBtn.onclick = close;
  overlay.onclick = (e) => { if (e.target === overlay) close(); };

  confirmBtn.onclick = () => {
    const selected = overlay.querySelector('input[name="renderTranscriptSource"]:checked').value;
    close();
    startRender(selected);
  };
}

// ── Start Render ───────────────────────────────────────────────────────────
async function startRender(transcriptSource = 'refined') {
  if (!S.activeJobId) return;

  startRenderBtn.disabled = true;

  const chosenTranscript = (transcriptSource === 'original' && S.originalTranscriptData && S.originalTranscriptData.length > 0)
    ? S.originalTranscriptData
    : S.transcriptData;

  const styleConfig = collectStyle();
  styleConfig.animStyle = S.currentAnim;
  styleConfig.position  = S.currentPos;
  styleConfig.transcript = chosenTranscript;
  styleConfig.transcriptSource = transcriptSource;

  // Merge effects & filter config
  const fxConfig = collectEffectsConfig();
  styleConfig.effects = fxConfig.effects;
  styleConfig.filter  = fxConfig.filter;

  // Scale font sizes from display to native video pixels
  const nativeWidth  = previewVideo.videoWidth  || 0;
  const nativeHeight = previewVideo.videoHeight || 0;
  let displayedWidth = previewVideo.clientWidth;

  if (nativeWidth > 0 && nativeHeight > 0 && previewVideo.clientHeight > 0) {
    const vAspect = nativeWidth / nativeHeight;
    const cAspect = previewVideo.clientWidth / previewVideo.clientHeight;
    if (cAspect >= vAspect) {
      displayedWidth = previewVideo.clientHeight * vAspect;
    } else {
      displayedWidth = previewVideo.clientWidth;
    }
  }

  if (displayedWidth > 0 && nativeWidth > 0) {
    const scale = nativeWidth / displayedWidth;
    styleConfig.fontSize    = Math.round(styleConfig.fontSize * scale);
    styleConfig.strokeWidth = Math.round((styleConfig.strokeWidth || 0) * scale);
    styleConfig.glowBlur    = Math.round((styleConfig.glowBlur    || 0) * scale);
  }

  try {
    const phases = ['Translation', 'Subtitles', 'Mux'];
    renderPhases.innerHTML = phases.map((p, i) =>
      `<div class="render-phase-item" id="rphase-${i}">${p}</div>`
    ).join('');

    renderFile.textContent = S.selectedFile ? S.selectedFile.name : S.activeJobId;
    renderStatus.textContent = 'Starting render pipeline...';
    renderLog.textContent = '';

    showScreen('rendering');

    const job = await apiFetch(`/api/jobs/${S.activeJobId}/render`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ style_config: styleConfig }),
    });

    await watchRender(S.activeJobId);

  } catch (err) {
    renderStatus.textContent = 'Error: ' + err.message;
    toast.error('Render failed: ' + err.message);
    startRenderBtn.disabled = false;
    showScreen('preview');
  }
}

async function watchRender(jobId) {
  return new Promise((resolve, reject) => {
    const sse = new EventSource(`/api/jobs/${jobId}/log`);
    sse.onmessage = (e) => {
      const data = JSON.parse(e.data);
      if (data.line) {
        const line = document.createElement('div');
        line.textContent = data.line;
        renderLog.appendChild(line);
        renderLog.scrollTop = renderLog.scrollHeight;
      }
      if (data.done) sse.close();
    };
    sse.onerror = () => sse.close();

    const PHASE_LABELS = ['', 'Transcription', 'Translation', 'Subtitles', 'Mux'];

    const poll = setInterval(async () => {
      try {
        const job = await apiFetch(`/api/jobs/${jobId}`);

        if (job.phase_label) renderStatus.textContent = job.phase_label;

        const phaseIdx = (job.current_phase || 1) - 2;
        document.querySelectorAll('.render-phase-item').forEach((el, i) => {
          el.classList.remove('active', 'done');
          if (i < phaseIdx) el.classList.add('done');
          else if (i === phaseIdx) el.classList.add('active');
        });

        renderBar.style.animation = 'none';
        renderBar.style.width = job.progress_pct + '%';
        renderBar.style.marginLeft = '0';

        await loadJobs();

        if (job.status === 'completed') {
          clearInterval(poll);
          sse.close();
          renderStatus.textContent = 'Render complete!';
          renderBar.style.width = '100%';
          renderBar.style.background = 'var(--green)';

          const dlBtn = document.createElement('a');
          dlBtn.href = `/api/download/${jobId}`;
          dlBtn.className = 'btn-success';
          dlBtn.style.cssText = 'margin-top: 20px; display: inline-flex; align-items: center; gap: 8px;';
          dlBtn.textContent = '⬇ Download Output';
          dlBtn.download = true;
          document.querySelector('#screen-rendering .transcribing-card').appendChild(dlBtn);

          resolve();
        } else if (job.status === 'failed') {
          clearInterval(poll);
          sse.close();
          renderStatus.textContent = 'Render failed: ' + (job.error || 'Unknown error');
          renderBar.style.background = 'var(--red)';
          reject(new Error(job.error));
        }
      } catch (err) {
        clearInterval(poll);
        sse.close();
        reject(err);
      }
    }, 2000);
  });
}

// ── AE Export ──────────────────────────────────────────────────────────────
function setupAEExport() {
  if (!exportAEBtn) return;

  exportAEBtn.addEventListener('click', async () => {
    if (!S.activeJobId) {
      toast.warn('No active job to export.');
      return;
    }

    const styleConfig = collectStyle();
    styleConfig.animStyle = S.currentAnim;
    styleConfig.position = S.currentPos;
    styleConfig.transcript = S.transcriptData;
    styleConfig.videoDuration = S.videoDuration || 60;
    styleConfig.videoWidth = previewVideo.videoWidth || 1920;
    styleConfig.videoHeight = previewVideo.videoHeight || 1080;
    styleConfig.fps = 30;

    const nativeWidth  = previewVideo.videoWidth  || 0;
    const nativeHeight = previewVideo.videoHeight || 0;
    let displayedWidth = previewVideo.clientWidth;

    if (nativeWidth > 0 && nativeHeight > 0 && previewVideo.clientHeight > 0) {
      const vAspect = nativeWidth / nativeHeight;
      const cAspect = previewVideo.clientWidth / previewVideo.clientHeight;
      if (cAspect >= vAspect) {
        displayedWidth = previewVideo.clientHeight * vAspect;
      } else {
        displayedWidth = previewVideo.clientWidth;
      }
    }

    if (displayedWidth > 0 && nativeWidth > 0) {
      const scale = nativeWidth / displayedWidth;
      styleConfig.fontSize    = Math.round(styleConfig.fontSize * scale);
      styleConfig.strokeWidth = Math.round((styleConfig.strokeWidth || 0) * scale);
      styleConfig.glowBlur    = Math.round((styleConfig.glowBlur    || 0) * scale);
    }

    exportAEBtn.disabled = true;
    exportAEBtn.querySelector('.btn-text').textContent = 'Exporting...';

    try {
      const res = await fetch(`/api/jobs/${S.activeJobId}/export-ae`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ style_config: styleConfig }),
      });

      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: 'Export failed' }));
        throw new Error(err.detail || 'Export failed');
      }

      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `subtitles_${S.activeJobId}.jsx`;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);

    } catch (err) {
      toast.error('AE Export failed: ' + err.message);
    } finally {
      exportAEBtn.disabled = false;
      exportAEBtn.querySelector('.btn-text').textContent = 'Export .jsx';
    }
  });
}
