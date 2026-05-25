/**
 * all_in.js — Workspace 04 (All In) front-end logic.
 *
 * Runs the one-shot pipeline:
 *   download source → analyse moments → per-clip render
 *
 * Per design grilling Q2/Q10/Q12: Clip Cards stream as they finish,
 * each card has its own status pill + retry button, and Job deletion
 * removes the source video from disk.
 */

import { escHtml, toast, parseTime } from '../utils.js';

// ── State ────────────────────────────────────────────────────────────────────
let aiJobId = null;
let aiPollHandle = null;
let aiLogSSE = null;

// Form state — driven by segmented controls.
let aiAspectRatio = '9:16';
let aiCaptionPreset = 'bold';
let aiMode = 'single-shot';
let aiSortBy = 'score';

const TERMINAL = new Set(['completed', 'failed', 'cancelled']);

// ── Setup ────────────────────────────────────────────────────────────────────
export function setupAllIn() {
  const root = document.getElementById('screen-allin');
  if (!root) return;

  const $ = (id) => document.getElementById(id);

  const aiUrl              = $('aiUrl');
  const aiInstructions     = $('aiInstructions');
  const aiAnalysisLang     = $('aiAnalysisLang');
  const aiCaptionLang      = $('aiCaptionLang');
  const aiTightenSilence   = $('aiTightenSilence');
  const aiSpeakerTinting   = $('aiSpeakerTinting');
  const aiAutoSubtitle     = $('aiAutoSubtitle');
  const aiStartOffset      = $('aiStartOffset');
  const aiMaxClips         = $('aiMaxClips');
  const aiEnableAudio      = $('aiEnableAudio');
  const aiEnableChat       = $('aiEnableChat');
  const aiRunBtn           = $('aiRunBtn');
  const aiPresetVtuber     = $('aiPresetVtuber');
  const aiPresetClear      = $('aiPresetClear');
  const aiDeleteJobBtn     = $('aiDeleteJobBtn');

  function updateRunBtn() {
    aiRunBtn.disabled = !aiUrl.value.trim();
  }
  aiUrl.addEventListener('input', updateRunBtn);
  updateRunBtn();

  // ── Segmented controls ────────────────────────────────────────────────
  wireSegmented($('aiAspectRatio'), 'ratio', (v) => { aiAspectRatio = v; });
  wireSegmented($('aiCaptionPreset'), 'preset', (v) => { aiCaptionPreset = v; });
  wireSegmented($('aiMode'), 'mode', (v) => { aiMode = v; });
  wireSegmented($('aiSortSegmented'), 'sort', (v) => {
    aiSortBy = v;
    rerenderClipsFromState();
  });

  // ── Caption preset is disabled when auto-subtitle is off (Q7) ─────────
  aiAutoSubtitle.addEventListener('change', () => {
    const presetSeg = $('aiCaptionPreset');
    if (!presetSeg) return;
    presetSeg.querySelectorAll('button').forEach((b) => {
      b.disabled = !aiAutoSubtitle.checked;
    });
    presetSeg.style.opacity = aiAutoSubtitle.checked ? '1' : '0.4';
  });
  aiAutoSubtitle.dispatchEvent(new Event('change'));

  // ── Instruction presets (mirror Clip Finder) ──────────────────────────
  aiPresetClear.addEventListener('click', () => {
    aiInstructions.value = '';
    aiInstructions.focus();
  });
  aiPresetVtuber.addEventListener('click', () => {
    aiInstructions.value =
      "Find high-engagement VTuber highlights — peak reactions, " +
      "karma arcs, clutch plays. Include 15–45s of buildup before each peak.";
    aiInstructions.focus();
  });

  // ── Run button ────────────────────────────────────────────────────────
  aiRunBtn.addEventListener('click', async () => {
    const url = aiUrl.value.trim();
    if (!url) { toast('Please paste a YouTube URL'); return; }

    // Q13 — start_offset accepts mm:ss / hh:mm:ss / raw seconds.
    // The HTML input is type="text" so a colon character is allowed;
    // parseTime() in utils.js handles every supported shape.
    const offsetRaw = aiStartOffset.value.trim();
    let startOffset = 0;
    if (offsetRaw && offsetRaw !== '0') {
      startOffset = parseTime(offsetRaw);
      if (Number.isNaN(startOffset) || startOffset < 0) {
        toast('Invalid start offset. Use mm:ss (e.g. 03:54), hh:mm:ss, or raw seconds.');
        return;
      }
    }

    aiRunBtn.disabled = true;

    const payload = {
      url,
      instructions: aiInstructions.value.trim(),
      analysis_lang: aiAnalysisLang.value,
      caption_lang: aiCaptionLang.value,
      aspect_ratio: aiAspectRatio,
      tighten_silence: aiTightenSilence.checked,
      speaker_tinting: aiSpeakerTinting.checked,
      auto_subtitle: aiAutoSubtitle.checked,
      caption_preset: aiCaptionPreset,
      mode: aiMode,
      enable_audio_signals: aiEnableAudio.checked,
      enable_chat_signals: aiEnableChat.checked,
      start_offset: startOffset,
      max_clips: Math.max(1, Math.min(50, Number(aiMaxClips.value) || 12)),
    };

    try {
      const res = await fetch('/api/all-in/jobs', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      if (!res.ok) {
        const detail = await safeError(res);
        toast(`Failed: ${detail}`);
        aiRunBtn.disabled = false;
        return;
      }
      const job = await res.json();
      aiJobId = job.id;
      showProgress();
      aiDeleteJobBtn.style.display = '';
      attachLogStream(aiJobId);
      startPolling(aiJobId);
    } catch (exc) {
      toast(`Error: ${exc.message || exc}`);
      aiRunBtn.disabled = false;
    }
  });

  // ── Delete Job (Q12 — cleanup source + clips on disk) ────────────────
  aiDeleteJobBtn.addEventListener('click', async () => {
    if (!aiJobId) return;
    if (!confirm('Delete this Job? Source video and all clips will be removed.')) return;
    try {
      const res = await fetch(`/api/all-in/jobs/${aiJobId}`, { method: 'DELETE' });
      if (!res.ok) {
        toast(`Failed to delete: ${await safeError(res)}`);
        return;
      }
      toast('Job deleted');
      resetUI();
    } catch (exc) {
      toast(`Error: ${exc.message || exc}`);
    }
  });
}

// ── Helpers ──────────────────────────────────────────────────────────────────
function wireSegmented(root, dataKey, onChange) {
  if (!root) return;
  root.addEventListener('click', (e) => {
    const btn = e.target.closest(`button[data-${dataKey}]`);
    if (!btn || btn.disabled) return;
    root.querySelectorAll('button').forEach((b) => b.classList.remove('is-active'));
    btn.classList.add('is-active');
    onChange(btn.dataset[dataKey]);
  });
}

async function safeError(res) {
  try {
    const data = await res.json();
    return data.detail || `HTTP ${res.status}`;
  } catch {
    return `HTTP ${res.status}`;
  }
}

function showProgress() {
  const p = document.getElementById('aiProgress');
  if (p) p.style.display = '';
}

function resetUI() {
  aiJobId = null;
  if (aiPollHandle) { clearTimeout(aiPollHandle); aiPollHandle = null; }
  if (aiLogSSE) { aiLogSSE.close(); aiLogSSE = null; }
  const grid = document.getElementById('aiClipsGrid');
  if (grid) {
    grid.innerHTML = `
      <div class="state state--empty">
        <div class="state__icon"><svg width="22" height="22" viewBox="0 0 22 22"><path d="M3 6h16v12H3z M8 9l5 3-5 3z" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/></svg></div>
        <h4 class="state__title">No clips yet</h4>
        <p class="state__desc">Run All In to stream finished Clip Cards here.</p>
      </div>`;
  }
  const progress = document.getElementById('aiProgress');
  if (progress) progress.style.display = 'none';
  document.getElementById('aiResultCount').textContent = '0';
  document.getElementById('aiFailedCount').textContent = '0';
  document.getElementById('aiDeleteJobBtn').style.display = 'none';
  document.getElementById('aiRunBtn').disabled = !document.getElementById('aiUrl').value.trim();
}

// ── State cache for re-sort without re-fetch ────────────────────────────────
let aiClipsCache = [];

function rerenderClipsFromState() {
  if (aiClipsCache.length === 0) return;
  renderClips(aiClipsCache);
}

// Auto-init when page loads (mirror Clip Finder behaviour).
function attachLogStream(jobId) {
  if (aiLogSSE) aiLogSSE.close();
  const logEl = document.getElementById('aiLog');
  if (logEl) logEl.textContent = '';
  aiLogSSE = new EventSource(`/api/all-in/jobs/${jobId}/log`);
  aiLogSSE.onmessage = (ev) => {
    try {
      const data = JSON.parse(ev.data);
      if (data.line && logEl) {
        const line = document.createElement('div');
        line.textContent = data.line;
        logEl.appendChild(line);
        logEl.scrollTop = logEl.scrollHeight;
      }
      if (data.done) {
        aiLogSSE.close();
        aiLogSSE = null;
      }
    } catch { /* keepalive / non-JSON */ }
  };
  aiLogSSE.onerror = () => {
    if (aiLogSSE) { aiLogSSE.close(); aiLogSSE = null; }
  };
}

function startPolling(jobId) {
  async function poll() {
    if (!aiJobId || aiJobId !== jobId) return;
    try {
      const res = await fetch(`/api/all-in/jobs/${jobId}`);
      if (!res.ok) {
        aiPollHandle = setTimeout(poll, 2000);
        return;
      }
      const job = await res.json();
      applyJobToUI(job);
      if (TERMINAL.has(job.status)) {
        document.getElementById('aiRunBtn').disabled =
          !document.getElementById('aiUrl').value.trim();
        return;
      }
    } catch { /* network blip — keep polling */ }
    aiPollHandle = setTimeout(poll, 1500);
  }
  if (aiPollHandle) clearTimeout(aiPollHandle);
  poll();
}

function applyJobToUI(job) {
  // Phase label + progress bar.
  document.getElementById('aiPhaseLabel').textContent =
    job.phase_label || job.status || '';
  const bar = document.getElementById('aiProgressBar');
  if (bar) bar.style.width = `${Math.max(0, Math.min(100, job.progress_pct || 0))}%`;

  // Step indicators.
  const stepEls = document.querySelectorAll('.ai-step-line');
  stepEls.forEach((el) => {
    el.classList.remove('is-active', 'is-done');
  });
  const stepOrder = ['downloading', 'analyzing', 'rendering', 'completed'];
  const idx = stepOrder.indexOf(job.status);
  stepEls.forEach((el) => {
    const step = el.dataset.step;
    const stepIdx = stepOrder.indexOf(step);
    if (stepIdx < 0 || idx < 0) return;
    if (stepIdx < idx) el.classList.add('is-done');
    else if (stepIdx === idx) el.classList.add('is-active');
  });
  if (job.status === 'completed') {
    stepEls.forEach((el) => el.classList.add('is-done'));
  }

  // Clip cards.
  aiClipsCache = job.clips || [];
  renderClips(aiClipsCache);
}

// ── Clip Card rendering (Q14 — numeric badge + colour band) ────────────────
function renderClips(clips) {
  const grid = document.getElementById('aiClipsGrid');
  if (!grid) return;

  const doneCount = clips.filter((c) => c.status === 'done').length;
  const failCount = clips.filter((c) => c.status === 'failed').length;
  document.getElementById('aiResultCount').textContent = String(doneCount);
  document.getElementById('aiFailedCount').textContent = String(failCount);

  if (clips.length === 0) {
    grid.innerHTML = `
      <div class="state state--empty">
        <div class="state__icon"><svg width="22" height="22" viewBox="0 0 22 22"><path d="M3 6h16v12H3z M8 9l5 3-5 3z" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/></svg></div>
        <h4 class="state__title">No clips yet</h4>
        <p class="state__desc">Waiting on Gemini analysis…</p>
      </div>`;
    return;
  }

  // Sort (Q14 — score desc default, time asc as alternative).
  const sorted = [...clips].sort((a, b) => {
    if (aiSortBy === 'time') return (a.start || 0) - (b.start || 0);
    return (b.score || 0) - (a.score || 0);
  });

  grid.innerHTML = sorted.map((clip, displayIdx) => clipCardHTML(clip, displayIdx)).join('');

  // Wire per-card buttons.
  grid.querySelectorAll('.ai-card').forEach((card) => {
    const idx = Number(card.dataset.clipIdx);
    const retryBtn = card.querySelector('[data-action="retry"]');
    const downloadBtn = card.querySelector('[data-action="download"]');
    if (retryBtn) retryBtn.addEventListener('click', () => retryClip(idx));
    if (downloadBtn) downloadBtn.addEventListener('click', () => downloadClip(idx));
  });
}

function clipCardHTML(clip, displayIdx) {
  const sourceIdx = clip.index;            // index into job.clips on the server
  const score = Number(clip.score || 0);
  const scoreClass =
    score >= 8.0 ? 'is-lime' :
    score >= 6.0 ? 'is-cream' :
    '';

  const status = clip.status || 'pending';
  const statusClass =
    status === 'done'      ? 'is-done' :
    status === 'failed'    ? 'is-failed' :
    status === 'rendering' ? 'is-rendering' :
    '';
  const cardClass =
    status === 'rendering' ? 'is-rendering' :
    status === 'failed'    ? 'is-failed' :
    '';

  const isDone = status === 'done';
  const stageLabel = clip.stage_label || statusLabel(status);

  const thumbHTML = isDone
    ? `<video preload="metadata" src="/api/all-in/jobs/${aiJobId}/clips/${sourceIdx}/stream#t=0.5" muted></video>`
    : `<span>${escHtml(formatTime(clip.start))}</span>`;

  const errorBlock = (status === 'failed' && clip.error)
    ? `<div class="ai-card__error">${escHtml(clip.error)}</div>`
    : '';

  return `
    <div class="ai-card ${cardClass}" data-clip-idx="${sourceIdx}">
      <div class="ai-card__thumb">${thumbHTML}</div>
      <div class="ai-card__body">
        <div class="ai-card__title">${escHtml(clip.title || 'Untitled clip')}</div>
        <div class="ai-card__reason">${escHtml(clip.reason || '')}</div>
        <div class="ai-card__meta">
          <span class="ai-score ${scoreClass}">${score.toFixed(1)}/10</span>
          <span class="ai-card__pill ${statusClass}">${escHtml(stageLabel)}</span>
          <span class="ai-card__pill" title="Source time range">
            ${escHtml(formatTime(clip.start))} → ${escHtml(formatTime(clip.end))}
          </span>
          <div class="ai-card__actions">
            <button type="button" class="ai-card__icon-btn" data-action="download"
                    title="Download clip" ${isDone ? '' : 'disabled'}>
              <svg width="14" height="14" viewBox="0 0 14 14"><path d="M7 1v8M4 6l3 3 3-3M2 12h10" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round" fill="none"/></svg>
            </button>
            <button type="button" class="ai-card__icon-btn" data-action="retry"
                    title="Retry this clip" ${status === 'failed' ? '' : 'disabled'}>
              <svg width="14" height="14" viewBox="0 0 14 14"><path d="M11 6a4 4 0 1 1-1.2-2.8M11 2v3h-3" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round" fill="none"/></svg>
            </button>
          </div>
        </div>
        ${errorBlock}
      </div>
    </div>`;
}

function statusLabel(status) {
  switch (status) {
    case 'done':      return 'Ready';
    case 'failed':    return 'Failed';
    case 'rendering': return 'Rendering';
    case 'pending':   return 'Pending';
    default:          return status || '';
  }
}

function formatTime(seconds) {
  const s = Math.max(0, Math.floor(Number(seconds) || 0));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  if (h > 0) return `${h}:${String(m).padStart(2, '0')}:${String(sec).padStart(2, '0')}`;
  return `${m}:${String(sec).padStart(2, '0')}`;
}

async function retryClip(clipIdx) {
  if (!aiJobId) return;
  try {
    const res = await fetch(
      `/api/all-in/jobs/${aiJobId}/clips/${clipIdx}/retry`,
      { method: 'POST' },
    );
    if (!res.ok) {
      toast(`Retry failed: ${await safeError(res)}`);
      return;
    }
    toast('Retrying clip…');
    startPolling(aiJobId);
  } catch (exc) {
    toast(`Error: ${exc.message || exc}`);
  }
}

function downloadClip(clipIdx) {
  if (!aiJobId) return;
  window.location.href = `/api/all-in/jobs/${aiJobId}/clips/${clipIdx}/download`;
}

// ── Auto-init ──────────────────────────────────────────────────────────────
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', setupAllIn);
} else {
  setupAllIn();
}
