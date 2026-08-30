/**
 * preview.js — Preview screen: video playback, transcript list, speaker styles, save
 * Subtitle rendering delegated to subtitleEngine.js
 * Style controls delegated to styleControls.js
 * Popup editors delegated to popups.js
 */

import { apiFetch, showScreen, switchTab, fmtTime, escHtml, toast } from './utils.js';
import * as S from './state.js';
import { pushUndoSnapshot, popUndoSnapshot } from './state.js';
import { renderTimeline } from './timeline.js';
import { openSplitDialog, mergeSegmentWithNext } from './timeline.js';
import { loadJobs } from './jobs.js';
import { clearFile } from './upload.js';
import { onStyleChange, startSubtitleSync, renderActiveSubtitles, injectSetActiveSeg, collectStyle } from './subtitleEngine.js';
import { setupStyleControls, applyStyleConfig, applyPreset } from './styleControls.js';
import { openTimeEditor, openSpeakerPicker, injectPopupCallbacks } from './popups.js';
import { injectSegmentEffectPreview, setActiveSegmentEffectIndex, setupSegmentEffects } from './segmentEffects.js';
import { enhanceColorInput } from './colorPicker.js';

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
const glowColorEl          = document.getElementById('glowColor');

// ── Setup ──────────────────────────────────────────────────────────────────
export function setupPreview() {
  setupPreviewControls();
  setupStyleControls();
  setupSegmentEffects();
  injectSegmentEffectPreview(() => {
    onStyleChange();
    renderTimeline();
    renderTranscriptList();
  });
  // Inject setActiveSeg into subtitle engine (avoids circular dep)
  injectSetActiveSeg(setActiveSeg);
  // Inject callbacks into popups module
  injectPopupCallbacks({ renderTranscriptList, buildSpeakerStylePanel, scheduleAutoSave });

  // Set up auto-save request binding
  S.setOnSaveRequest(scheduleAutoSave);

  window.addEventListener('beforeunload', (e) => {
    if (S.autoSaveTimer) {
      e.preventDefault();
      e.returnValue = 'You have unsaved changes. Are you sure you want to leave?';
      return e.returnValue;
    }
  });
}

// ── Save / Auto-Save ───────────────────────────────────────────────────────
export function scheduleAutoSave() {
  if (S.autoSaveTimer) clearTimeout(S.autoSaveTimer);
  showAutoSaveIndicator('pending');
  S.setAutoSaveTimer(setTimeout(() => {
    S.setAutoSaveTimer(null);
    saveTranscript(true);
  }, S.AUTOSAVE_DELAY));
}

export async function saveTranscript(isAutoSave = false) {
  if (!S.activeJobId || !S.transcriptData.length || S.isSaving) return;
  S.setIsSaving(true);
  showAutoSaveIndicator('saving');
  // Manual saves get a button-level spinner — auto-saves don't, since
  // the autosave-indicator chip already covers them.
  const saveBtn = document.getElementById('saveTranscriptBtn');
  if (!isAutoSave && saveBtn) saveBtn.classList.add('is-saving');
  try {
    await apiFetch(`/api/jobs/${S.activeJobId}/transcript`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        segments: S.transcriptData,
        style_config: collectStyle()
      }),
    });
    showAutoSaveIndicator('saved');
    if (!isAutoSave) {
      toast.success('Transcript saved');
    }
  } catch (err) {
    console.error('Save failed:', err);
    showAutoSaveIndicator('error');
    if (!isAutoSave) {
      toast.error('Save failed: ' + (err.message || 'unknown error'));
    }
  } finally {
    S.setIsSaving(false);
    if (!isAutoSave && saveBtn) saveBtn.classList.remove('is-saving');
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

  // Reset and load styling configuration
  S.setSpeakerStyles({});
  if (S.loadedStyleConfig && Object.keys(S.loadedStyleConfig).length > 0) {
    applyStyleConfig(S.loadedStyleConfig);
  } else {
    applyPreset('vtuber-pop');
  }

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
      S.speakerStyles[sp] = {
        color:       S.SPEAKER_COLORS[idx % S.SPEAKER_COLORS.length],
        strokeColor: null,
        glowColor:   null,
      };
    } else {
      if (!('strokeColor' in S.speakerStyles[sp])) S.speakerStyles[sp].strokeColor = null;
      if (!('glowColor'   in S.speakerStyles[sp])) S.speakerStyles[sp].glowColor   = null;
    }
  });

  if (seen.length <= 1) {
    speakerStylesSection.style.display = 'none';
    return;
  }
  speakerStylesSection.style.display = '';

  speakerStylesPanel.innerHTML = '';
  seen.forEach(sp => {
    const match      = sp.match(/\d+$/);
    const label      = match ? `Speaker ${parseInt(match[0], 10)}` : sp;
    const badgeLabel = match ? `S${parseInt(match[0], 10)}` : sp;
    const color      = S.speakerStyles[sp].color;
    const strokeOverride = S.speakerStyles[sp].strokeColor;
    const strokeVal  = strokeOverride || strokeColorEl.value || '#000000';
    const strokeActive = !!strokeOverride;
    const glowOverride = S.speakerStyles[sp].glowColor;
    const glowVal    = glowOverride || (glowColorEl ? glowColorEl.value : '#ffffff') || '#ffffff';
    const glowActive = !!glowOverride;

    const row = document.createElement('div');
    row.className = 'speaker-style-row';
    row.innerHTML = `
      <span class="speaker-style-badge" style="color:${color};border-color:${color}">${badgeLabel}</span>
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
      <div class="speaker-color-group">
        <span class="speaker-color-label">Glow</span>
        <input type="color" class="color-input speaker-glow-input ${glowActive ? 'active-override' : ''}" value="${glowVal}" data-speaker="${sp}" title="Glow / shadow color for ${sp} (click × to reset)" />
        <button class="speaker-glow-clear ${glowActive ? '' : 'hidden'}" data-speaker="${sp}" title="Reset to global glow">×</button>
      </div>
    `;

    const fillInput = row.querySelector('.speaker-color-input');
    enhanceColorInput(fillInput);
    fillInput.addEventListener('input', () => {
      S.speakerStyles[sp] = { ...S.speakerStyles[sp], color: fillInput.value };
      row.querySelector('.speaker-style-badge').style.color = fillInput.value;
      row.querySelector('.speaker-style-badge').style.borderColor = fillInput.value;
      renderTranscriptList();
      renderTimeline();
      onStyleChange();
      scheduleAutoSave();
    });

    const strokeInput = row.querySelector('.speaker-stroke-input');
    const strokeClear = row.querySelector('.speaker-stroke-clear');
    enhanceColorInput(strokeInput);
    strokeInput.addEventListener('input', () => {
      S.speakerStyles[sp] = { ...S.speakerStyles[sp], strokeColor: strokeInput.value };
      strokeInput.classList.add('active-override');
      strokeClear.classList.remove('hidden');
      renderTimeline();
      onStyleChange();
      scheduleAutoSave();
    });
    strokeClear.addEventListener('click', (e) => {
      e.stopPropagation();
      S.speakerStyles[sp] = { ...S.speakerStyles[sp], strokeColor: null };
      strokeInput.value = strokeColorEl.value || '#000000';
      strokeInput._syncColorPicker?.();
      strokeInput.classList.remove('active-override');
      strokeClear.classList.add('hidden');
      renderTimeline();
      onStyleChange();
      scheduleAutoSave();
    });

    const glowInput = row.querySelector('.speaker-glow-input');
    const glowClear = row.querySelector('.speaker-glow-clear');
    enhanceColorInput(glowInput);
    glowInput.addEventListener('input', () => {
      S.speakerStyles[sp] = { ...S.speakerStyles[sp], glowColor: glowInput.value };
      glowInput.classList.add('active-override');
      glowClear.classList.remove('hidden');
      renderTimeline();
      onStyleChange();
      scheduleAutoSave();
    });
    glowClear.addEventListener('click', (e) => {
      e.stopPropagation();
      S.speakerStyles[sp] = { ...S.speakerStyles[sp], glowColor: null };
      glowInput.value = (glowColorEl ? glowColorEl.value : '#ffffff') || '#ffffff';
      glowInput._syncColorPicker?.();
      glowInput.classList.remove('active-override');
      glowClear.classList.add('hidden');
      renderTimeline();
      onStyleChange();
      scheduleAutoSave();
    });

    speakerStylesPanel.appendChild(row);
  });
}

// ── Transcript List ────────────────────────────────────────────────────────
export function renderTranscriptList() {
  transcriptBody.innerHTML = '';
  S.transcriptData.forEach((seg, idx) => {
    const speakerColor = S.getSpeakerColor(seg.speaker);
    const match = seg.speaker ? seg.speaker.match(/\d+$/) : null;
    const speakerLabel = match ? `S${parseInt(match[0], 10)}` : (seg.speaker || 'S0');
    const isLast = idx === S.transcriptData.length - 1;
    const effectLabel = seg.effect?.type === 'wave'
      ? `Wave ${seg.effect.axis === 'vertical' ? 'V' : 'H'}`
      : (seg.effect?.type === 'shake' ? 'Shake' : '');
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
        ${effectLabel ? `<span class="seg-effect-badge">${effectLabel}</span>` : ''}
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
    const match = seg.speaker ? seg.speaker.match(/\d+$/) : null;
    const speakerLabel = match ? `S${parseInt(match[0], 10)}` : (seg.speaker || 'S0');
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
  setActiveSegmentEffectIndex(idx);
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

  const isTextEntryTarget = (target) => {
    if (!target) return false;
    if (target.isContentEditable) return true;
    const tagName = target.tagName;
    return tagName === 'INPUT' || tagName === 'TEXTAREA' || tagName === 'SELECT';
  };

  const togglePreviewFromSpace = (e) => {
    if (e.code !== 'Space' || isTextEntryTarget(e.target)) return;
    const previewScreen = document.getElementById('screen-preview');
    if (!previewScreen || !previewScreen.classList.contains('active')) return;
    if (e.type === 'keydown') {
      if (e.repeat) return;
      if (previewVideo.paused) previewVideo.play();
      else previewVideo.pause();
    }
    e.preventDefault();
    e.stopImmediatePropagation();
  };

  document.addEventListener('keydown', togglePreviewFromSpace, true);
  document.addEventListener('keyup', togglePreviewFromSpace, true);

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

  // Play / Pause button has structured markup (SVG icon + <span> label).
  // Setting `previewPlayBtn.textContent = '⏸ Pause'` previously WIPED the
  // SVG and replaced the entire button with a text node, after which the
  // play/pause UI looked stuck (no icon, no clear visual state). Update
  // only the label span and only swap the SVG `<path>` for the play /
  // pause glyph so the button keeps its layout.
  const PLAY_PATH  = 'M3 2v8l7-4z';
  const PAUSE_PATH = 'M3 2h2v8H3zM7 2h2v8H7z';

  function setPlayBtnState(isPlaying) {
    if (!previewPlayBtn) return;
    const labelEl = previewPlayBtn.querySelector('span');
    const pathEl  = previewPlayBtn.querySelector('svg path');
    if (labelEl) labelEl.textContent = isPlaying ? 'Pause' : 'Play';
    if (pathEl)  pathEl.setAttribute('d', isPlaying ? PAUSE_PATH : PLAY_PATH);
  }

  previewPlayBtn.addEventListener('click', () => {
    if (previewVideo.paused) {
      // play() returns a promise — if it rejects (autoplay block, codec
      // issue) we want the button to reflect the actual paused state
      // instead of going out of sync.
      const p = previewVideo.play();
      if (p && typeof p.catch === 'function') {
        p.catch(() => setPlayBtnState(false));
      }
      setPlayBtnState(true);
    } else {
      previewVideo.pause();
      setPlayBtnState(false);
    }
  });

  previewRestartBtn.addEventListener('click', () => {
    previewVideo.currentTime = 0;
    const p = previewVideo.play();
    if (p && typeof p.catch === 'function') p.catch(() => setPlayBtnState(false));
    setPlayBtnState(true);
  });

  previewVideo.addEventListener('ended', () => {
    setPlayBtnState(false);
    subtitleContainer.innerHTML = '';
  });

  // The native `pause` / `play` events are the source of truth — bind the
  // button label to them so external triggers (keyboard shortcut, ended,
  // programmatic pause) keep the icon in sync.
  previewVideo.addEventListener('pause', () => setPlayBtnState(false));
  previewVideo.addEventListener('play',  () => setPlayBtnState(true));

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
    navBackBtn.addEventListener('click', async () => {
      if (S.autoSaveTimer) {
        clearTimeout(S.autoSaveTimer);
        S.setAutoSaveTimer(null);
      }
      if (S.activeJobId && S.transcriptData.length && !S.isSaving) {
        try {
          showAutoSaveIndicator('saving');
          await saveTranscript(true);
        } catch (err) {
          console.error('Auto-save on back failed:', err);
        }
      }
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
      window.location.href = '/auto-subtitle';
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
