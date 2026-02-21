/**
 * preview.js — Preview screen: video playback, subtitle rendering, style controls, save
 */

import { apiFetch, showScreen, switchTab, fmtTime, escHtml, parseTime } from './utils.js';
import * as S from './state.js';
import { renderTimeline, updatePlayhead, updateTimeDisplay } from './timeline.js';
import { openSplitDialog, mergeSegmentWithNext } from './timeline.js';
import { loadJobs } from './jobs.js';
import { clearFile } from './upload.js';

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

// Style controls
const fontFamilyEl    = document.getElementById('fontFamily');
const fontSizeEl      = document.getElementById('fontSize');
const fontSizeVal     = document.getElementById('fontSizeVal');
const fontColorEl     = document.getElementById('fontColor');
const strokeEnabledEl = document.getElementById('strokeEnabled');
const strokeControls  = document.getElementById('strokeControls');
const strokeColorEl   = document.getElementById('strokeColor');
const strokeWidthEl   = document.getElementById('strokeWidth');
const strokeWidthVal  = document.getElementById('strokeWidthVal');
const glowEnabledEl   = document.getElementById('glowEnabled');
const glowControls    = document.getElementById('glowControls');
const glowColorEl     = document.getElementById('glowColor');
const glowBlurEl      = document.getElementById('glowBlur');
const glowBlurVal     = document.getElementById('glowBlurVal');
const bgBoxEnabledEl  = document.getElementById('bgBoxEnabled');
const bgBoxControls   = document.getElementById('bgBoxControls');
const bgBoxColorEl    = document.getElementById('bgBoxColor');
const bgOpacityEl     = document.getElementById('bgOpacity');
const bgOpacityVal    = document.getElementById('bgOpacityVal');
const animGrid        = document.getElementById('animGrid');
const positionGrid    = document.getElementById('positionGrid');
const presetGrid      = document.getElementById('presetGrid');
const colorSwatches   = document.getElementById('colorSwatches');

// Speaker style panel
const speakerStylesSection = document.getElementById('speakerStylesSection');
const speakerStylesPanel   = document.getElementById('speakerStylesPanel');

// ── Setup ──────────────────────────────────────────────────────────────────
export function setupPreview() {
  setupPreviewControls();
  setupStyleControls();
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
      textEl.addEventListener('input', () => {
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

  toggleInput.addEventListener('change', () => {
    S.setShowingOriginal(toggleInput.checked);
    if (S.showingOriginal) {
      label.textContent = 'ElevenLabs';
      editBtn.style.display = 'none';
      renderOriginalTranscriptList();
    } else {
      label.textContent = 'Refined';
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

// ── Time Editor Popup ──────────────────────────────────────────────────────
let activeTimeEditor = null;

function closeTimeEditor() {
  if (activeTimeEditor) { activeTimeEditor.remove(); activeTimeEditor = null; }
}

function openTimeEditor(anchorEl, segIdx) {
  closeTimeEditor();
  closeSpeakerPicker();

  const seg = S.transcriptData[segIdx];
  const popup = document.createElement('div');
  popup.className = 'time-editor-popup';
  popup.innerHTML = `
    <div class="time-editor-header">Edit Timing</div>
    <div class="time-editor-fields">
      <div class="time-editor-field">
        <label class="time-editor-label">Start</label>
        <div class="time-editor-spin">
          <button class="time-spin-btn" data-field="start" data-dir="-1">−</button>
          <input class="time-editor-input" id="teStartInput" type="text" value="${fmtTime(seg.start)}" />
          <button class="time-spin-btn" data-field="start" data-dir="1">+</button>
        </div>
      </div>
      <div class="time-editor-field">
        <label class="time-editor-label">End</label>
        <div class="time-editor-spin">
          <button class="time-spin-btn" data-field="end" data-dir="-1">−</button>
          <input class="time-editor-input" id="teEndInput" type="text" value="${fmtTime(seg.end)}" />
          <button class="time-spin-btn" data-field="end" data-dir="1">+</button>
        </div>
      </div>
    </div>
    <div class="time-editor-error hidden" id="teError">Invalid values</div>
    <div class="time-editor-actions">
      <button class="btn-secondary btn-sm" id="teCancelBtn">Cancel</button>
      <button class="btn-primary btn-sm" id="teConfirmBtn">Apply</button>
    </div>
  `;

  document.body.appendChild(popup);
  const rect = anchorEl.getBoundingClientRect();
  let left = rect.left;
  let top = rect.bottom + 4;
  const popW = 220;
  if (left + popW > window.innerWidth - 8) left = window.innerWidth - popW - 8;
  if (top < 0) top = 0;
  popup.style.left = `${left}px`;
  popup.style.top  = `${top}px`;

  activeTimeEditor = popup;

  const startInput = popup.querySelector('#teStartInput');
  const endInput   = popup.querySelector('#teEndInput');
  const errEl      = popup.querySelector('#teError');

  popup.querySelectorAll('.time-spin-btn').forEach(btn => {
    btn.addEventListener('mousedown', (e) => { e.preventDefault(); });
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      const field = btn.dataset.field;
      const dir   = parseFloat(btn.dataset.dir);
      const inp   = field === 'start' ? startInput : endInput;
      const val   = parseTime(inp.value);
      if (val !== null) {
        inp.value = fmtTime(Math.max(0, parseFloat((val + dir * 0.1).toFixed(1))));
      }
    });
  });

  function applyChanges() {
    const newStart = parseTime(startInput.value);
    const newEnd   = parseTime(endInput.value);
    errEl.classList.add('hidden');
    if (newStart === null || newEnd === null) {
      errEl.textContent = 'Invalid time format (use M:SS.d)';
      errEl.classList.remove('hidden'); return;
    }
    if (newStart < 0) {
      errEl.textContent = 'Start cannot be negative'; errEl.classList.remove('hidden'); return;
    }
    if (newEnd <= newStart) {
      errEl.textContent = 'End must be after Start'; errEl.classList.remove('hidden'); return;
    }
    S.transcriptData[segIdx].start = parseFloat(newStart.toFixed(1));
    S.transcriptData[segIdx].end   = parseFloat(newEnd.toFixed(1));
    closeTimeEditor();
    renderTranscriptList();
    renderTimeline();
    onStyleChange();
    scheduleAutoSave();
  }

  popup.querySelector('#teConfirmBtn').addEventListener('click', (e) => { e.stopPropagation(); applyChanges(); });
  popup.querySelector('#teCancelBtn').addEventListener('click', (e) => { e.stopPropagation(); closeTimeEditor(); });
  startInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') applyChanges(); if (e.key === 'Escape') closeTimeEditor(); });
  endInput.addEventListener('keydown',   (e) => { if (e.key === 'Enter') applyChanges(); if (e.key === 'Escape') closeTimeEditor(); });

  startInput.addEventListener('focus', () => startInput.select());
  endInput.addEventListener('focus',   () => endInput.select());
  startInput.focus();

  setTimeout(() => {
    document.addEventListener('click', closeTimeEditor, { once: true });
  }, 0);
}

// ── Speaker Picker ─────────────────────────────────────────────────────────
let activeSpeakerPicker = null;

function closeSpeakerPicker() {
  if (activeSpeakerPicker) {
    activeSpeakerPicker.remove();
    activeSpeakerPicker = null;
  }
}

function openSpeakerPicker(anchorEl, segIdx) {
  closeSpeakerPicker();
  closeTimeEditor();

  const seen = [];
  S.transcriptData.forEach(s => {
    const sp = s.speaker || 'SPEAKER_00';
    if (!seen.includes(sp)) seen.push(sp);
  });

  const currentSpeaker = S.transcriptData[segIdx].speaker || 'SPEAKER_00';

  const picker = document.createElement('div');
  picker.className = 'speaker-picker-popup';
  picker.innerHTML = `
    <div class="speaker-picker-header">Change Speaker</div>
    <div class="speaker-picker-list">
      ${seen.map(sp => {
        const idx = parseInt((sp.match(/\d+$/) || ['0'])[0], 10);
        const color = S.getSpeakerColor(sp);
        const label = `S${idx}`;
        const active = sp === currentSpeaker ? ' speaker-picker-item-active' : '';
        return `<button class="speaker-picker-item${active}" data-speaker="${sp}" style="--sp-color:${color}">
          <span class="speaker-picker-badge" style="color:${color};border-color:${color}">${label}</span>
          <span class="speaker-picker-name">Speaker ${idx}</span>
          ${sp === currentSpeaker ? '<span class="speaker-picker-check">✓</span>' : ''}
        </button>`;
      }).join('')}
      <div class="speaker-picker-divider"></div>
      <button class="speaker-picker-item speaker-picker-new" data-speaker="__new__">
        <span class="speaker-picker-badge" style="color:var(--text-2);border-color:var(--border-2)">+</span>
        <span class="speaker-picker-name">New Speaker…</span>
      </button>
    </div>
  `;

  document.body.appendChild(picker);
  const rect = anchorEl.getBoundingClientRect();
  const pickerW = 180;
  let left = rect.left;
  let top = rect.bottom + 4;
  if (left + pickerW > window.innerWidth - 8) left = window.innerWidth - pickerW - 8;
  if (top < 0) top = 0;
  picker.style.left = `${left}px`;
  picker.style.top = `${top}px`;

  activeSpeakerPicker = picker;

  picker.querySelectorAll('.speaker-picker-item').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      const sp = btn.dataset.speaker;
      if (sp === '__new__') {
        const input = prompt('Enter new speaker ID (e.g. SPEAKER_02):', `SPEAKER_0${seen.length}`);
        if (!input || !input.trim()) { closeSpeakerPicker(); return; }
        const newSp = input.trim().toUpperCase().replace(/\s+/g, '_');
        if (!S.speakerStyles[newSp]) {
          const nIdx = parseInt((newSp.match(/\d+$/) || ['0'])[0], 10);
          S.speakerStyles[newSp] = { color: S.SPEAKER_COLORS[nIdx % S.SPEAKER_COLORS.length] };
        }
        S.transcriptData[segIdx].speaker = newSp;
      } else {
        S.transcriptData[segIdx].speaker = sp;
      }
      closeSpeakerPicker();
      renderTranscriptList();
      buildSpeakerStylePanel();
      onStyleChange();
      scheduleAutoSave();
    });
  });

  setTimeout(() => {
    document.addEventListener('click', closeSpeakerPicker, { once: true });
  }, 0);
}

// ── Subtitle Sync Loop ─────────────────────────────────────────────────────
function startSubtitleSync() {
  if (S.subtitleTimer) cancelAnimationFrame(S.subtitleTimer);

  let lastActiveKey = '';

  function tick() {
    const t = previewVideo.currentTime;

    updatePlayhead(t);
    updateTimeDisplay(t);

    const activeSegs = S.transcriptData
      .map((s, i) => ({ ...s, _idx: i }))
      .filter(s => t >= s.start && t <= s.end);

    const activeKey = activeSegs.map(s => s._idx).join(',');

    if (activeKey !== lastActiveKey) {
      lastActiveKey = activeKey;
      setActiveSeg(activeSegs.length > 0 ? activeSegs[0]._idx : -1);
      renderActiveSubtitles(activeSegs, t);
    } else if (activeSegs.length > 0 && (S.currentAnim === 'karaoke' || S.currentAnim === 'narration-pop')) {
      activeSegs.forEach(seg => updateKaraokeHighlight(seg, t, seg._idx));
    }

    S.setSubtitleTimer(requestAnimationFrame(tick));
  }

  S.setSubtitleTimer(requestAnimationFrame(tick));
}

export function renderActiveSubtitles(activeSegs, currentTime) {
  subtitleContainer.innerHTML = '';

  subtitleOverlay.style.alignItems = '';
  subtitleOverlay.style.justifyContent = '';

  if (activeSegs.length === 0) return;

  const style = collectStyle();

  const isMultiSpeaker = new Set(S.transcriptData.map(s => s.speaker || 'SPEAKER_00')).size > 1;

  activeSegs.forEach((seg, layerIdx) => {
    const speakerColor       = isMultiSpeaker ? S.getSpeakerColor(seg.speaker) : null;
    const speakerStrokeColor = isMultiSpeaker ? S.getSpeakerStrokeColor(seg.speaker) : null;
    const words = seg.text.split(' ');

    const wordsHtml = words.map((w) => {
      return `<span class="sub-word" style="${buildWordStyle(style, speakerColor, speakerStrokeColor)}">${escHtml(w)}</span>`;
    }).join('');

    const lineDiv = document.createElement('div');
    lineDiv.className = `subtitle-line anim-${S.currentAnim} speaker-layer-${layerIdx} draggable`;
    lineDiv.dataset.segIdx = seg._idx;
    lineDiv.dataset.speaker = seg.speaker || 'SPEAKER_00';
    lineDiv.style.cssText = buildLineStyle(style, speakerColor, layerIdx);

    if (seg.posOverride || seg.pos_override) {
      const px = seg.posX ?? seg.pos_x ?? 50;
      const py = seg.posY ?? seg.pos_y ?? 85;
      lineDiv.style.position = 'absolute';
      lineDiv.style.left = px + '%';
      lineDiv.style.top = py + '%';
      lineDiv.style.transform = 'translate(-50%, -50%)';
    }

    lineDiv.innerHTML = wordsHtml;

    // Drag to reposition subtitle
    lineDiv.addEventListener('mousedown', (e) => {
      if (e.button !== 0) return;
      e.preventDefault();
      const segIdx = parseInt(lineDiv.dataset.segIdx);
      const overlayRect = subtitleOverlay.getBoundingClientRect();
      const lineRect = lineDiv.getBoundingClientRect();
      const actualPosX = ((lineRect.left + lineRect.width / 2) - overlayRect.left) / overlayRect.width * 100;
      const actualPosY = ((lineRect.top + lineRect.height / 2) - overlayRect.top) / overlayRect.height * 100;

      const segData = S.transcriptData[segIdx];
      segData.posX = actualPosX;
      segData.pos_x = actualPosX;
      segData.posY = actualPosY;
      segData.pos_y = actualPosY;
      segData.posOverride = true;
      segData.pos_override = true;

      lineDiv.style.position = 'absolute';
      lineDiv.style.left = actualPosX + '%';
      lineDiv.style.top = actualPosY + '%';
      lineDiv.style.transform = 'translate(-50%, -50%)';

      S.setSubtitleDragState({
        segIdx,
        startX: e.clientX,
        startY: e.clientY,
        overlayRect,
        origPosX: actualPosX,
        origPosY: actualPosY,
      });
      lineDiv.classList.add('dragging');
    });

    subtitleContainer.appendChild(lineDiv);

    if ((S.currentAnim === 'karaoke' || S.currentAnim === 'narration-pop') && seg.words) {
      updateKaraokeHighlight(seg, currentTime, seg._idx);
    }
  });
}

function updateKaraokeHighlight(seg, currentTime, segIdx) {
  if (!seg.words || !seg.words.length) return;
  const lineDiv = subtitleContainer.querySelector(`[data-seg-idx="${segIdx}"]`);
  if (!lineDiv) return;
  const wordEls = lineDiv.querySelectorAll('.sub-word');
  if (!wordEls.length) return;

  seg.words.forEach((w, i) => {
    if (!wordEls[i]) return;
    const isActive = currentTime >= w.start && currentTime <= w.end;
    wordEls[i].classList.toggle('karaoke-active', isActive);
    if (isActive) {
      wordEls[i].style.color = fontColorEl.value;
    } else {
      wordEls[i].style.color = '';
    }
  });
}

// ── Style Computation ──────────────────────────────────────────────────────
export function collectStyle() {
  return {
    fontFamily:    fontFamilyEl.value,
    fontSize:      parseInt(fontSizeEl.value),
    fontColor:     fontColorEl.value,
    strokeEnabled: strokeEnabledEl.checked,
    strokeColor:   strokeColorEl.value,
    strokeWidth:   parseInt(strokeWidthEl.value),
    glowEnabled:   glowEnabledEl.checked,
    glowColor:     glowColorEl.value,
    glowBlur:      parseInt(glowBlurEl.value),
    bgBoxEnabled:  bgBoxEnabledEl.checked,
    bgBoxColor:    bgBoxColorEl ? bgBoxColorEl.value : '#000000',
    bgOpacity:     bgOpacityEl ? parseInt(bgOpacityEl.value) : 60,
    speakerStyles: Object.fromEntries(
      Object.entries(S.speakerStyles).map(([k, v]) => [k, { color: v.color, strokeColor: v.strokeColor || null }])
    ),
  };
}

function buildWordStyle(style, speakerColor, speakerStrokeColor) {
  let parts = [];
  let shadows = [];

  if (style.strokeEnabled && style.strokeWidth > 0) {
    const w = Math.round(style.strokeWidth * S.fsScale);
    const c = speakerStrokeColor || style.strokeColor;
    shadows.push(
      `${w}px ${w}px 0 ${c}`,
      `-${w}px ${w}px 0 ${c}`,
      `${w}px -${w}px 0 ${c}`,
      `-${w}px -${w}px 0 ${c}`,
      `${w}px 0 0 ${c}`,
      `-${w}px 0 0 ${c}`,
      `0 ${w}px 0 ${c}`,
      `0 -${w}px 0 ${c}`
    );
  }

  if (style.glowEnabled && style.glowBlur > 0) {
    const gb = Math.round(style.glowBlur * S.fsScale);
    const gc = style.glowColor;
    shadows.push(`0 0 ${gb}px ${gc}`, `0 0 ${gb * 2}px ${gc}`);
  }

  if (shadows.length > 0) {
    parts.push(`text-shadow: ${shadows.join(', ')}`);
  }

  if (style.bgBoxEnabled) {
    const hex = style.bgBoxColor;
    const r = parseInt(hex.slice(1,3),16);
    const g = parseInt(hex.slice(3,5),16);
    const b = parseInt(hex.slice(5,7),16);
    const a = (style.bgOpacity / 100).toFixed(2);
    parts.push(`background: rgba(${r},${g},${b},${a})`);
    parts.push('padding: 4px 10px');
    parts.push('border-radius: 6px');
  }

  return parts.join('; ');
}

function buildLineStyle(style, speakerColor, layerIdx) {
  const color = speakerColor || style.fontColor;
  const marginBottom = layerIdx > 0 ? `${layerIdx * 2.8}em` : '0';

  let parts = [
    `font-family: ${style.fontFamily}`,
    `font-size: ${Math.round(style.fontSize * S.fsScale)}px`,
    `color: ${color}`,
    `font-weight: 800`,
    `line-height: 1.2`,
    `margin-bottom: ${marginBottom}`,
  ];

  return parts.join('; ');
}

// Trigger re-render when style changes
export function onStyleChange() {
  if (subtitleContainer.children.length > 0) {
    const t = previewVideo.currentTime;
    const activeSegs = S.transcriptData
      .map((s, i) => ({ ...s, _idx: i }))
      .filter(s => t >= s.start && t <= s.end);
    if (activeSegs.length > 0) renderActiveSubtitles(activeSegs, t);
  }
}

// ── Style Controls Setup ──────────────────────────────────────────────────
function setupStyleControls() {
  fontSizeEl.addEventListener('input', () => {
    fontSizeVal.textContent = fontSizeEl.value;
    onStyleChange();
  });
  strokeWidthEl.addEventListener('input', () => {
    strokeWidthVal.textContent = strokeWidthEl.value;
    onStyleChange();
  });
  glowBlurEl.addEventListener('input', () => {
    glowBlurVal.textContent = glowBlurEl.value;
    onStyleChange();
  });
  bgOpacityEl.addEventListener('input', () => {
    bgOpacityVal.textContent = bgOpacityEl.value;
    onStyleChange();
  });

  fontColorEl.addEventListener('input', onStyleChange);
  strokeColorEl.addEventListener('input', onStyleChange);
  glowColorEl.addEventListener('input', onStyleChange);
  bgBoxColorEl.addEventListener('input', onStyleChange);
  fontFamilyEl.addEventListener('change', onStyleChange);

  strokeEnabledEl.addEventListener('change', () => {
    strokeControls.classList.toggle('hidden', !strokeEnabledEl.checked);
    onStyleChange();
  });
  glowEnabledEl.addEventListener('change', () => {
    glowControls.classList.toggle('hidden', !glowEnabledEl.checked);
    onStyleChange();
  });
  bgBoxEnabledEl.addEventListener('change', () => {
    bgBoxControls.classList.toggle('hidden', !bgBoxEnabledEl.checked);
    onStyleChange();
  });

  colorSwatches.querySelectorAll('.swatch').forEach(btn => {
    btn.addEventListener('click', () => {
      const color = btn.dataset.color;
      fontColorEl.value = color;
      colorSwatches.querySelectorAll('.swatch').forEach(s => s.classList.remove('active'));
      btn.classList.add('active');
      onStyleChange();
    });
  });

  animGrid.querySelectorAll('.anim-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      animGrid.querySelectorAll('.anim-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      S.setCurrentAnim(btn.dataset.anim);
      onStyleChange();
    });
  });

  positionGrid.querySelectorAll('.pos-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      positionGrid.querySelectorAll('.pos-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      S.setCurrentPos(btn.dataset.pos);
      subtitleOverlay.className = 'subtitle-overlay pos-' + S.currentPos;
    });
  });

  presetGrid.querySelectorAll('.preset-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      presetGrid.querySelectorAll('.preset-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      applyPreset(btn.dataset.preset);
    });
  });
}

export function applyPreset(name) {
  const p = S.PRESETS[name];
  if (!p) return;

  fontFamilyEl.value = p.fontFamily;
  fontSizeEl.value = p.fontSize;
  fontSizeVal.textContent = p.fontSize;
  fontColorEl.value = p.fontColor;
  strokeEnabledEl.checked = p.strokeEnabled;
  strokeControls.classList.toggle('hidden', !p.strokeEnabled);
  strokeColorEl.value = p.strokeColor;
  strokeWidthEl.value = p.strokeWidth;
  strokeWidthVal.textContent = p.strokeWidth;
  glowEnabledEl.checked = p.glowEnabled;
  glowControls.classList.toggle('hidden', !p.glowEnabled);
  glowColorEl.value = p.glowColor;
  glowBlurEl.value = p.glowBlur;
  glowBlurVal.textContent = p.glowBlur;
  bgBoxEnabledEl.checked = p.bgBoxEnabled || false;
  bgBoxControls.classList.toggle('hidden', !p.bgBoxEnabled);
  if (p.bgBoxColor) bgBoxColorEl.value = p.bgBoxColor;
  if (p.bgOpacity !== undefined) {
    bgOpacityEl.value = p.bgOpacity;
    bgOpacityVal.textContent = p.bgOpacity;
  }

  S.setCurrentAnim(p.anim);
  animGrid.querySelectorAll('.anim-btn').forEach(b => {
    b.classList.toggle('active', b.dataset.anim === p.anim);
  });

  S.setCurrentPos(p.pos);
  positionGrid.querySelectorAll('.pos-btn').forEach(b => {
    b.classList.toggle('active', b.dataset.pos === p.pos);
  });
  subtitleOverlay.className = 'subtitle-overlay pos-' + S.currentPos;

  onStyleChange();
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

function toggleFullscreen() {
  if (!document.fullscreenElement) {
    S.setPreviewWidthBeforeFs(previewVideo.clientWidth);
    videoWrap.requestFullscreen().catch(() => {});
  } else {
    document.exitFullscreen();
  }
}
