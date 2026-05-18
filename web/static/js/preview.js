/**
 * preview.js — Preview screen: video playback, transcript list, speaker styles, save
 * Subtitle rendering delegated to subtitleEngine.js
 * Style controls delegated to styleControls.js
 * Popup editors delegated to popups.js
 */

import { apiFetch, showScreen, switchTab, fmtTime, escHtml } from './utils.js';
import * as S from './state.js';
import { pushUndoSnapshot, popUndoSnapshot } from './state.js';
import { renderTimeline } from './timeline.js';
import { openSplitDialog, mergeSegmentWithNext } from './timeline.js';
import { loadJobs } from './jobs.js';
import { clearFile } from './upload.js';
import { onStyleChange, startSubtitleSync, renderActiveSubtitles, injectSetActiveSeg } from './subtitleEngine.js';
import { setupStyleControls } from './styleControls.js';
import { openTimeEditor, openSpeakerPicker, injectPopupCallbacks } from './popups.js';

// ── DOM Refs ───────────────────────────────────────────────────────────────
const previewVideo     = document.getElementById('previewVideo');
const subtitleOverlay  = document.getElementById('subtitleOverlay');
const subtitleContainer = document.getElementById('subtitleContainer');
const transcriptBody   = document.getElementById('transcriptBody');
const previewPlayBtn   = document.getElementById('previewPlayBtn');
const previewRestartBtn = document.getElementById('previewRestartBtn');
const editTranscriptBtn = document.getElementById('editTranscriptBtn');
const backToUploadBtn  = document.getElementById('backToUploadBtn');
const startRenderBtn   = document.getElementById('startRenderBtn');
const fullscreenBtn    = document.getElementById('fullscreenBtn');
const videoWrap        = document.querySelector('.video-preview-wrap');

// Speaker style panel DOM refs
const speakerStylesSection = document.getElementById('speakerStylesSection');
const speakerStylesPanel   = document.getElementById('speakerStylesPanel');
const strokeColorEl        = document.getElementById('strokeColor');

// ── Setup ──────────────────────────────────────────────────────────────────
export function setupPreview() {
  setupPreviewControls();
  setupStyleControls();
  // Inject setActiveSeg into subtitle engine (avoids circular dep)
  injectSetActiveSeg(setActiveSeg);
  // Inject callbacks into popups module
  injectPopupCallbacks({ renderTranscriptList, buildSpeakerStylePanel, scheduleAutoSave });
}

// ── Save / Auto-Save ───────────────────────────────────────────────────────
export function scheduleAutoSave() {
  if (S.autoSaveTimer) clearTimeout(S.autoSaveTimer);
  showAutoSaveIndicator('pending');
  S.setAutoSaveTimer(setTimeout(() => saveTranscript(true), S.AUTOSAVE_DELAY));
}

export async function saveTranscript(isAutoSave = false) {
  if (!S.activeJobId || !S.transcriptData.length || S.isSaving) return;
  S.setIsSaving(true);
  showAutoSaveIndicator('saving');
  try {
    await apiFetch(`/api/jobs/${S.activeJobId}/transcript`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ segments: S.transcriptData }),
    });
    showAutoSaveIndicator('saved');
  } catch (err) {
    console.error('Save failed:', err);
    showAutoSaveIndicator('error');
  } finally {
    S.setIsSaving(false);
  }
}

function showAutoSaveIndicator(state) {
  const el = document.getElementById('autosaveIndicator');
  if (!el) return;
  el.className = 'autosave-indicator';
  switch (state) {
    case 'pending':  el.textContent = ''; break;
    case 'saving':   el.textContent = 'Saving…'; el.classList.add('saving'); break;
    case 'saved':    el.textContent = '✓ Saved'; el.classList.add('saved'); break;
    case 'error':    el.textContent = '✗ Save failed'; el.classList.add('error'); break;
  }
}

// ── Open Preview Screen ────────────────────────────────────────────────────
export function openPreviewScreen(jobId) {
  const videoUrl = `/api/jobs/${jobId}/video`;
  previewVideo.src = videoUrl;
  previewVideo.load();

  renderTranscriptList();
  buildSpeakerStylePanel();
  initOriginalTranscriptToggle();
  checkOriginalTranscriptAvailable(jobId);

  previewVideo.addEventListener('loadedmetadata', function onMeta() {
    S.setVideoDuration(previewVideo.duration);
    renderTimeline();
    previewVideo.removeEventListener('loadedmetadata', onMeta);
  });

  startSubtitleSync();
  showScreen('preview');
  loadJobs();
}

// ── Speaker Style Panel ────────────────────────────────────────────────────
function buildSpeakerStylePanel() {
  const seen = [];
  S.transcriptData.forEach(seg => {
    const sp = seg.speaker || 'SPEAKER_00';
    if (!seen.includes(sp)) seen.push(sp);
  });

  seen.forEach(sp => {
    if (!S.speakerStyles[sp]) {
      const idx = parseInt((sp.match(/\d+$/) || ['0'])[0], 10);
      S.speakerStyles[sp] = { color: S.SPEAKER_COLORS[idx % S.SPEAKER_COLORS.length], strokeColor: null };
    } else if (!('strokeColor' in S.speakerStyles[sp])) {
      S.speakerStyles[sp].strokeColor = null;
    }
  });

  if (seen.length <= 1) {
    speakerStylesSection.style.display = 'none';
    return;
  }
  speakerStylesSection.style.display = '';

  speakerStylesPanel.innerHTML = '';
  seen.forEach(sp => {
    const idx        = parseInt((sp.match(/\d+$/) || ['0'])[0], 10);
    const label      = `Speaker ${idx}`;
    const color      = S.speakerStyles[sp].color;
    const strokeOverride = S.speakerStyles[sp].strokeColor;
    const strokeVal  = strokeOverride || strokeColorEl.value || '#000000';
    const strokeActive = !!strokeOverride;

    const row = document.createElement('div');
    row.className = 'speaker-style-row';
    row.innerHTML = `
      <span class="speaker-style-badge" style="color:${color};border-color:${color}">S${idx}</span>
      <span class="speaker-style-name">${label}</span>
      <div class="speaker-color-group">
        <span class="speaker-color-label">Fill</span>
        <input type="color" class="color-input speaker-color-input" value="${color}" data-speaker="${sp}" title="Text color for ${sp}" />
      </div>
      <div class="speaker-color-group">
        <span class="speaker-color-label">Stroke</span>
        <input type="color" class="color-input speaker-stroke-input ${strokeActive ? 'active-override' : ''}" value="${strokeVal}" data-speaker="${sp}" title="Stroke color for ${sp} (click × to reset)" />
        <button class="speaker-stroke-clear ${strokeActive ? '' : 'hidden'}" data-speaker="${sp}" title="Reset to global stroke">×</button>
      </div>
    `;

    const fillInput = row.querySelector('.speaker-color-input');
    fillInput.addEventListener('input', () => {
      S.speakerStyles[sp] = { ...S.speakerStyles[sp], color: fillInput.value };
      row.querySelector('.speaker-style-badge').style.color = fillInput.value;
      row.querySelector('.speaker-style-badge').style.borderColor = fillInput.value;
      renderTranscriptList();
      onStyleChange();
    });

    const strokeInput = row.querySelector('.speaker-stroke-input');
    const strokeClear = row.querySelector('.speaker-stroke-clear');
    strokeInput.addEventListener('input', () => {
      S.speakerStyles[sp] = { ...S.speakerStyles[sp], strokeColor: strokeInput.value };
      strokeInput.classList.add('active-override');
      strokeClear.classList.remove('hidden');
      onStyleChange();
    });
    strokeClear.addEventListener('click', (e) => {
      e.stopPropagation();
      S.speakerStyles[sp] = { ...S.speakerStyles[sp], strokeColor: null };
      strokeInput.value = strokeColorEl.value || '#000000';
      strokeInput.classList.remove('active-override');
      strokeClear.classList.add('hidden');
      onStyleChange();
    });

    speakerStylesPanel.appendChild(row);
  });
}

// ── Transcript List ────────────────────────────────────────────────────────
export function renderTranscriptList() {
  transcriptBody.innerHTML = '';
  S.transcriptData.forEach((seg, idx) => {
    const speakerColor = S.getSpeakerColor(seg.speaker);
    const speakerNum = seg.speaker
      ? (seg.speaker.match(/\d+$/) || ['0'])[0]
      : '0';
    const speakerLabel = `S${parseInt(speakerNum, 10)}`;
    const isLast = idx === S.transcriptData.length - 1;
    const div = document.createElement('div');
    div.className = 'transcript-seg';
    div.dataset.idx = idx;
    div.innerHTML = `
      <div class="seg-row-time">
        <span class="seg-time seg-time-btn" data-idx="${idx}" title="Click to edit timing">${fmtTime(seg.start)}<span class="seg-time-sep"> › </span>${fmtTime(seg.end)}<span class="seg-time-edit-icon">✎</span></span>
        <span class="seg-actions">
          <button class="seg-btn seg-split-btn" data-idx="${idx}" title="Split segment">✂</button>
          ${!isLast ? `<button class="seg-btn seg-merge-btn" data-idx="${idx}" title="Merge with next">⊕</button>` : ''}
        </span>
      </div>
      <div class="seg-row-content">
        <span class="seg-speaker seg-speaker-btn" style="color:${speakerColor};border-color:${speakerColor}" data-idx="${idx}" title="Click to change speaker">${speakerLabel}<span class="seg-speaker-edit-icon">✎</span></span>
        <span class="seg-text" ${S.editMode ? 'contenteditable="true"' : ''}>${escHtml(seg.text)}</span>
      </div>
    `;
    div.addEventListener('click', (e) => {
      if (e.target.closest('.seg-actions')) return;
      if (e.target.closest('.seg-speaker-btn')) return;
      if (e.target.closest('.seg-time-btn')) return;
      previewVideo.currentTime = seg.start;
      previewVideo.play();
      setActiveSeg(idx);
    });
    const timeBtn = div.querySelector('.seg-time-btn');
    timeBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      openTimeEditor(timeBtn, idx);
    });
    const speakerBtn = div.querySelector('.seg-speaker-btn');
    speakerBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      openSpeakerPicker(speakerBtn, idx);
    });
    if (S.editMode) {
      const textEl = div.querySelector('.seg-text');
      let _undoPushed = false;
      textEl.addEventListener('focus', () => { _undoPushed = false; });
      textEl.addEventListener('input', () => {
        if (!_undoPushed) { pushUndoSnapshot(); _undoPushed = true; }
        S.transcriptData[idx].text = textEl.textContent;
        scheduleAutoSave();
      });
    }
    div.querySelector('.seg-split-btn').addEventListener('click', (e) => {
      e.stopPropagation();
      openSplitDialog(idx);
    });
    const mergeBtn = div.querySelector('.seg-merge-btn');
    if (mergeBtn) {
      mergeBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        mergeSegmentWithNext(idx);
      });
    }
    transcriptBody.appendChild(div);
  });
}

// ── Original Transcript Toggle ────────────────────────────────────────────
function renderOriginalTranscriptList() {
  if (!S.originalTranscriptData) return;
  transcriptBody.innerHTML = '';
  S.originalTranscriptData.forEach((seg, idx) => {
    const speakerColor = S.getSpeakerColor(seg.speaker);
    const speakerNum = seg.speaker
      ? (seg.speaker.match(/\d+$/) || ['0'])[0]
      : '0';
    const speakerLabel = `S${parseInt(speakerNum, 10)}`;
    const div = document.createElement('div');
    div.className = 'transcript-seg transcript-seg-original';
    div.dataset.idx = idx;
    div.innerHTML = `
      <div class="seg-row-time">
        <span class="seg-time">${fmtTime(seg.start)}<span class="seg-time-sep"> › </span>${fmtTime(seg.end)}</span>
      </div>
      <div class="seg-row-content">
        <span class="seg-speaker" style="color:${speakerColor};border-color:${speakerColor}">${speakerLabel}</span>
        <span class="seg-text">${escHtml(seg.text)}</span>
      </div>
    `;
    div.addEventListener('click', () => {
      previewVideo.currentTime = seg.start;
      previewVideo.play();
    });
    transcriptBody.appendChild(div);
  });
}

async function fetchOriginalTranscript(jobId) {
  try {
    const data = await apiFetch(`/api/jobs/${jobId}/transcript/original`);
    return data.segments || null;
  } catch {
    return null;
  }
}

function initOriginalTranscriptToggle() {
  const toggleWrap = document.getElementById('transcriptSourceToggle');
  const toggleInput = document.getElementById('showOriginalTranscript');
  const label = document.getElementById('transcriptSourceLabel');
  const editBtn = document.getElementById('editTranscriptBtn');

  if (!toggleWrap || !toggleInput) return;

  // Labels reflect the audit's terminology: the "raw" view shows the
  // pre-sanitization snapshot from ElevenLabs Scribe (saved by the STT
  // engine before sanitize_timestamps mutates anything), while the
  // "refined" view shows the user-edited / Gemini-regrouped transcript.
  toggleInput.addEventListener('change', () => {
    S.setShowingOriginal(toggleInput.checked);
    if (S.showingOriginal) {
      label.textContent = 'Raw ElevenLabs';
      label.title = 'Word-level data exactly as ElevenLabs Scribe returned it (pre-sanitization).';
      editBtn.style.display = 'none';
      renderOriginalTranscriptList();
    } else {
      label.textContent = 'Refined';
      label.title = 'Sanitized + Gemini-regrouped transcript with your edits.';
      editBtn.style.display = '';
      renderTranscriptList();
    }
  });
}

async function checkOriginalTranscriptAvailable(jobId) {
  const toggleWrap = document.getElementById('transcriptSourceToggle');
  const toggleInput = document.getElementById('showOriginalTranscript');
  if (!toggleWrap) return;

  S.setOriginalTranscriptData(await fetchOriginalTranscript(jobId));
  if (S.originalTranscriptData && S.originalTranscriptData.length > 0) {
    toggleWrap.style.display = 'flex';
  } else {
    toggleWrap.style.display = 'none';
  }

  S.setShowingOriginal(false);
  if (toggleInput) toggleInput.checked = false;
  const label = document.getElementById('transcriptSourceLabel');
  if (label) label.textContent = 'Refined';
}

export function setActiveSeg(idx) {
  transcriptBody.querySelectorAll('.transcript-seg').forEach((el, i) => {
    el.classList.toggle('active', i === idx);
  });
  const active = transcriptBody.querySelector('.transcript-seg.active');
  if (active) active.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

// ── Preview Controls ───────────────────────────────────────────────────────
function setupPreviewControls() {
  const saveBtn = document.getElementById('saveTranscriptBtn');
  if (saveBtn) {
    saveBtn.addEventListener('click', () => saveTranscript(false));
  }

  document.addEventListener('keydown', (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === 's') {
      const previewScreen = document.getElementById('screen-preview');
      if (previewScreen && previewScreen.classList.contains('active')) {
        e.preventDefault();
        saveTranscript(false);
      }
    }
    // Ctrl+Z — Undo last transcript change
    if ((e.ctrlKey || e.metaKey) && e.key === 'z' && !e.shiftKey) {
      const previewScreen = document.getElementById('screen-preview');
      if (previewScreen && previewScreen.classList.contains('active')) {
        e.preventDefault();
        performUndo();
      }
    }
  });

  previewPlayBtn.addEventListener('click', () => {
    if (previewVideo.paused) {
      previewVideo.play();
      previewPlayBtn.textContent = '⏸ Pause';
    } else {
      previewVideo.pause();
      previewPlayBtn.textContent = '▶ Play';
    }
  });

  previewRestartBtn.addEventListener('click', () => {
    previewVideo.currentTime = 0;
    previewVideo.play();
    previewPlayBtn.textContent = '⏸ Pause';
  });

  previewVideo.addEventListener('ended', () => {
    previewPlayBtn.textContent = '▶ Play';
    subtitleContainer.innerHTML = '';
  });

  previewVideo.addEventListener('pause', () => {
    previewPlayBtn.textContent = '▶ Play';
  });
  previewVideo.addEventListener('play', () => {
    previewPlayBtn.textContent = '⏸ Pause';
  });

  if (backToUploadBtn) {
    backToUploadBtn.addEventListener('click', () => {
      previewVideo.pause();
      if (S.subtitleTimer) cancelAnimationFrame(S.subtitleTimer);
      S.setSelectedSegIdx(null);
      S.setVideoDuration(0);
      S.setTimelineZoom(1.0);
      showScreen('upload');
      clearFile();
    });
  }

  const navBackBtn = document.getElementById('navBackBtn');
  if (navBackBtn) {
    navBackBtn.addEventListener('click', () => {
      const activeScreen = document.querySelector('.app-screen.active');
      const screenId = activeScreen ? activeScreen.id : '';
      if (screenId === 'screen-preview') {
        previewVideo.pause();
        if (S.subtitleTimer) cancelAnimationFrame(S.subtitleTimer);
        S.setSelectedSegIdx(null);
        S.setVideoDuration(0);
        S.setTimelineZoom(1.0);
        clearFile();
      }
      showScreen('upload');
      switchTab('subtitle');
    });
  }

  editTranscriptBtn.addEventListener('click', () => {
    S.setEditMode(!S.editMode);
    editTranscriptBtn.textContent = S.editMode ? '✅ Done' : '✏️ Edit';
    renderTranscriptList();
  });

  startRenderBtn.addEventListener('click', () => {
    // Import render module dynamically to avoid circular dependency
    import('./render.js').then(m => m.openRenderOptionsModal());
  });

  fullscreenBtn.addEventListener('click', toggleFullscreen);
  document.addEventListener('fullscreenchange', () => {
    const isFs = !!document.fullscreenElement;
    fullscreenBtn.textContent = isFs ? '✕' : '⛶';
    fullscreenBtn.title = isFs ? 'Exit Fullscreen (F or Esc)' : 'Fullscreen (F)';

    if (isFs) {
      requestAnimationFrame(() => {
        const cW = videoWrap.offsetWidth;
        const cH = videoWrap.offsetHeight;
        const vAspect = previewVideo.videoWidth > 0
          ? previewVideo.videoWidth / previewVideo.videoHeight
          : 16 / 9;
        const cAspect = cW / cH;

        let vW, vH, vX, vY;
        if (cAspect >= vAspect) {
          vH = cH;  vW = cH * vAspect;
          vX = (cW - vW) / 2;  vY = 0;
        } else {
          vW = cW;  vH = cW / vAspect;
          vX = 0;   vY = (cH - vH) / 2;
        }

        subtitleOverlay.style.left   = vX + 'px';
        subtitleOverlay.style.top    = vY + 'px';
        subtitleOverlay.style.right  = 'auto';
        subtitleOverlay.style.bottom = 'auto';
        subtitleOverlay.style.width  = vW + 'px';
        subtitleOverlay.style.height = vH + 'px';

        S.setFsScale(S.previewWidthBeforeFs > 0 ? vW / S.previewWidthBeforeFs : 1);
        onStyleChange();
      });
    } else {
      subtitleOverlay.style.left   = '';
      subtitleOverlay.style.top    = '';
      subtitleOverlay.style.right  = '';
      subtitleOverlay.style.bottom = '';
      subtitleOverlay.style.width  = '';
      subtitleOverlay.style.height = '';
      S.setFsScale(1);
      onStyleChange();
    }
  });
}

// ── Undo ───────────────────────────────────────────────────────────────────
function performUndo() {
  const snapshot = popUndoSnapshot();
  if (!snapshot) {
    showUndoToast('Nothing to undo');
    return;
  }
  S.setTranscriptData(snapshot);
  renderTranscriptList();
  renderTimeline();
  onStyleChange();
  scheduleAutoSave();
  showUndoToast('Undo successful');
}

function showUndoToast(msg) {
  let toast = document.getElementById('undoToast');
  if (!toast) {
    toast = document.createElement('div');
    toast.id = 'undoToast';
    toast.className = 'undo-toast';
    document.body.appendChild(toast);
  }
  toast.textContent = msg;
  toast.classList.remove('undo-toast-hide');
  toast.classList.add('undo-toast-show');
  clearTimeout(toast._hideTimer);
  toast._hideTimer = setTimeout(() => {
    toast.classList.remove('undo-toast-show');
    toast.classList.add('undo-toast-hide');
  }, 1500);
}

function toggleFullscreen() {
  if (!document.fullscreenElement) {
    S.setPreviewWidthBeforeFs(previewVideo.clientWidth);
    videoWrap.requestFullscreen().catch(() => {});
  } else {
    document.exitFullscreen();
  }
}
