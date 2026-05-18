/**
 * effects.js — Effects & Filters system: tab switching, preset drag-drop to timeline,
 *              FX block manipulation (move, resize, delete), per-effect settings panel,
 *              crop position selector, filter controls, audio preview (Web Audio),
 *              preview overlay.
 */

import * as S from './state.js';
import { renderTimeline } from './timeline.js';
import { scheduleAutoSave } from './preview.js';
import { confirmDialog } from './utils.js';

// ── FX preset metadata ─────────────────────────────────────────────────────
const FX_PRESETS = {
  'red-flash':      { label: 'Red Flash',   defaultDuration: 0.5,  category: 'visual' },
  'zoom-vtuber':    { label: 'Auto Zoom',   defaultDuration: 3.0,  category: 'visual' },
  'shake':          { label: 'Shake',        defaultDuration: 0.4,  category: 'visual' },
  'flash-white':    { label: 'Flash White', defaultDuration: 0.3,  category: 'visual' },
  'zoom-in-center': { label: 'Zoom Center', defaultDuration: 2.0,  category: 'visual' },
  'vignette':       { label: 'Vignette',    defaultDuration: 5.0,  category: 'visual' },
  'volume-boost':   { label: 'Vol Boost',   defaultDuration: 3.0,  category: 'audio' },
  'bass-boost':     { label: 'Bass Boost',  defaultDuration: 3.0,  category: 'audio' },
};

/** Return default params for a given FX type */
function defaultParams(type) {
  switch (type) {
    case 'red-flash':      return { intensity: 60 };
    case 'flash-white':    return { intensity: 70 };
    case 'shake':          return { intensity: 50 };
    case 'vignette':       return { intensity: 60 };
    case 'zoom-vtuber':    return { zoomLevel: 130, transition: 'smooth', cropX: 0.5, cropY: 0.5 };
    case 'zoom-in-center': return { zoomLevel: 130, transition: 'smooth', cropX: 0.5, cropY: 0.5 };
    case 'volume-boost':   return { gain: 2.0 };
    case 'bass-boost':     return { gain: 6 };
    default:               return {};
  }
}

// ── DOM Refs ────────────────────────────────────────────────────────────────
const previewVideo       = document.getElementById('previewVideo');
const timelineTrack      = document.getElementById('timelineTrack');
const timelineScrollArea = document.getElementById('timelineScrollArea');

// ── Web Audio API nodes ─────────────────────────────────────────────────────
let _audioCtx = null;
let _audioSource = null;
let _gainNode = null;
let _bassFilter = null;
let _audioConnected = false;

function ensureAudioGraph() {
  if (_audioConnected) return;
  const video = document.getElementById('previewVideo');
  if (!video) return;
  try {
    _audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    _audioSource = _audioCtx.createMediaElementSource(video);
    _gainNode = _audioCtx.createGain();
    _gainNode.gain.value = 1.0;
    _bassFilter = _audioCtx.createBiquadFilter();
    _bassFilter.type = 'lowshelf';
    _bassFilter.frequency.value = 150;
    _bassFilter.gain.value = 0;
    _audioSource.connect(_gainNode).connect(_bassFilter).connect(_audioCtx.destination);
    _audioConnected = true;
  } catch (e) {
    console.warn('Web Audio init failed:', e);
  }
}

// ── Tab Switching ───────────────────────────────────────────────────────────
function setupTabs() {
  const tabBar = document.getElementById('stylePanelTabs');
  if (!tabBar) return;
  tabBar.addEventListener('click', (e) => {
    const btn = e.target.closest('.sp-tab');
    if (!btn) return;
    const target = btn.dataset.spTab;
    tabBar.querySelectorAll('.sp-tab').forEach(t => t.classList.toggle('active', t === btn));
    document.querySelectorAll('.sp-tab-pane').forEach(p => {
      p.classList.toggle('active', p.id === `spPane-${target}`);
    });
  });
}

// ── Drag from Preset Cards to Timeline ──────────────────────────────────────
function setupPresetDrag() {
  document.querySelectorAll('.fx-preset-card[draggable="true"]').forEach(card => {
    card.addEventListener('dragstart', (e) => {
      e.dataTransfer.setData('text/plain', card.dataset.fxType);
      e.dataTransfer.effectAllowed = 'copy';
      card.classList.add('dragging');
    });
    card.addEventListener('dragend', () => {
      card.classList.remove('dragging');
    });
  });
}

function setupTimelineFxDrop() {
  const trackArea = document.getElementById('timelineTrackArea');
  if (!trackArea) return;

  trackArea.addEventListener('dragover', (e) => {
    // Accept drop on any FX track layer
    const fxTrack = e.target.closest('.timeline-fx-track');
    if (!fxTrack) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = 'copy';
    // Highlight only the hovered layer
    document.querySelectorAll('.timeline-fx-track.drag-over').forEach(el => el.classList.remove('drag-over'));
    fxTrack.classList.add('drag-over');
  });

  trackArea.addEventListener('dragleave', (e) => {
    const fxTrack = e.target.closest('.timeline-fx-track');
    if (fxTrack) fxTrack.classList.remove('drag-over');
  });

  trackArea.addEventListener('drop', (e) => {
    document.querySelectorAll('.timeline-fx-track.drag-over').forEach(el => el.classList.remove('drag-over'));

    const fxTrack = e.target.closest('.timeline-fx-track');
    if (!fxTrack) return;

    const fxType = e.dataTransfer.getData('text/plain');
    if (!fxType || !FX_PRESETS[fxType]) return;

    e.preventDefault();

    const layer = parseInt(fxTrack.dataset.fxLayer || '0', 10);

    const trackRect = timelineTrack.getBoundingClientRect();
    const x = e.clientX - trackRect.left;
    const trackWidth = timelineScrollArea.clientWidth * S.timelineZoom;
    const dropTime = Math.max(0, (x / trackWidth) * S.videoDuration);

    const preset = FX_PRESETS[fxType];
    const fx = {
      id: S.nextFxId(),
      type: fxType,
      start: dropTime,
      end: Math.min(dropTime + preset.defaultDuration, S.videoDuration),
      label: preset.label,
      params: defaultParams(fxType),
      layer: layer,
    };

    S.effectsData.push(fx);
    S.setSelectedFxId(fx.id);
    updateFxCount();
    renderTimeline();
    openFxSettings(fx.id);
    scheduleAutoSave();
  });
}

// ── FX Block Interaction (click, drag-move, resize, right-click delete) ────
function setupFxBlockInteraction() {
  const trackArea = document.getElementById('timelineTrackArea');
  if (!trackArea) return;

  // Click to select or start drag
  trackArea.addEventListener('mousedown', (e) => {
    const block = e.target.closest('.fx-block');
    if (!block) return;

    const fxId = parseInt(block.dataset.fxId);
    S.setSelectedFxId(fxId);
    highlightFxBlock(fxId);
    openFxSettings(fxId);

    // Handle resize
    if (e.target.classList.contains('fx-handle')) {
      const edge = e.target.classList.contains('fx-handle-left') ? 'start' : 'end';
      S.setDraggingFxEdge({ id: fxId, edge });
      e.preventDefault();
      return;
    }

    // Handle body drag
    const trackRect = timelineTrack.getBoundingClientRect();
    const x = e.clientX - trackRect.left;
    const trackWidth = timelineScrollArea.clientWidth * S.timelineZoom;
    const clickTime = (x / trackWidth) * S.videoDuration;
    const fx = S.effectsData.find(f => f.id === fxId);
    if (fx) {
      S.setDraggingFx({ id: fxId, offsetTime: clickTime - fx.start });
      block.classList.add('dragging');
    }
    e.preventDefault();
  });

  // Mouse move for FX drag/resize
  document.addEventListener('mousemove', (e) => {
    if (S.draggingFx) {
      const trackRect = timelineTrack.getBoundingClientRect();
      const x = e.clientX - trackRect.left;
      const trackWidth = timelineScrollArea.clientWidth * S.timelineZoom;
      const mouseTime = (x / trackWidth) * S.videoDuration;
      const fx = S.effectsData.find(f => f.id === S.draggingFx.id);
      if (fx) {
        const dur = fx.end - fx.start;
        let newStart = mouseTime - S.draggingFx.offsetTime;
        newStart = Math.max(0, Math.min(S.videoDuration - dur, newStart));
        fx.start = newStart;
        fx.end = newStart + dur;
        renderTimeline();
        syncSettingsTime(fx);
      }
    }
    if (S.draggingFxEdge) {
      const trackRect = timelineTrack.getBoundingClientRect();
      const x = e.clientX - trackRect.left;
      const trackWidth = timelineScrollArea.clientWidth * S.timelineZoom;
      const t = Math.max(0, Math.min(S.videoDuration, (x / trackWidth) * S.videoDuration));
      const fx = S.effectsData.find(f => f.id === S.draggingFxEdge.id);
      if (fx) {
        if (S.draggingFxEdge.edge === 'start') {
          fx.start = Math.min(t, fx.end - 0.05);
        } else {
          fx.end = Math.max(t, fx.start + 0.05);
        }
        renderTimeline();
        syncSettingsTime(fx);
      }
    }
  });

  // Mouse up
  document.addEventListener('mouseup', () => {
    if (S.draggingFx) {
      document.querySelectorAll('.fx-block.dragging').forEach(el => el.classList.remove('dragging'));
      S.setDraggingFx(null);
      scheduleAutoSave();
    }
    if (S.draggingFxEdge) {
      S.setDraggingFxEdge(null);
      scheduleAutoSave();
    }
  });

  // Right-click context menu on FX blocks
  trackArea.addEventListener('contextmenu', (e) => {
    const block = e.target.closest('.fx-block');
    if (!block) return;
    e.preventDefault();
    const fxId = parseInt(block.dataset.fxId);
    showFxContextMenu(e.clientX, e.clientY, fxId);
  });

  // Delete key for selected FX
  document.addEventListener('keydown', (e) => {
    if (S.selectedFxId !== null && (e.key === 'Delete' || e.key === 'Backspace')) {
      // Only if not editing text
      if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA' || e.target.isContentEditable) return;
      // Don't delete if a subtitle segment is also selected (timeline.js handles that)
      if (S.selectedSegIdx !== null) return;
      deleteFx(S.selectedFxId);
    }
  });
}

function highlightFxBlock(fxId) {
  document.querySelectorAll('.fx-block').forEach(b => {
    b.classList.toggle('selected', parseInt(b.dataset.fxId) === fxId);
  });
  // Deselect subtitle segments when FX is selected
  S.setSelectedSegIdx(null);
  document.querySelectorAll('.timeline-segment.selected').forEach(el => el.classList.remove('selected'));
}

// ── FX Context Menu ─────────────────────────────────────────────────────────
function showFxContextMenu(x, y, fxId) {
  removeFxContextMenu();

  const menu = document.createElement('div');
  menu.className = 'fx-context-menu';
  menu.style.left = x + 'px';
  menu.style.top = y + 'px';
  menu.id = 'fxContextMenu';

  const settingsBtn = document.createElement('button');
  settingsBtn.className = 'fx-context-menu-item';
  settingsBtn.textContent = '\u2699 Settings';
  settingsBtn.addEventListener('click', () => { openFxSettings(fxId); removeFxContextMenu(); });
  menu.appendChild(settingsBtn);

  // Move to Layer — show sub-menu if more than 1 layer
  if (S.fxLayerCount > 1) {
    const fx = S.effectsData.find(f => f.id === fxId);
    const currentLayer = fx ? (fx.layer || 0) : 0;
    const moveGroup = document.createElement('div');
    moveGroup.className = 'fx-context-submenu';
    const moveLabel = document.createElement('span');
    moveLabel.className = 'fx-context-menu-label';
    moveLabel.textContent = 'Move to Layer';
    moveGroup.appendChild(moveLabel);
    for (let li = 0; li < S.fxLayerCount; li++) {
      const layerBtn = document.createElement('button');
      layerBtn.className = 'fx-context-menu-item' + (li === currentLayer ? ' active' : '');
      layerBtn.textContent = `Layer ${li + 1}`;
      layerBtn.addEventListener('click', () => {
        if (fx) { fx.layer = li; renderTimeline(); scheduleAutoSave(); }
        removeFxContextMenu();
      });
      moveGroup.appendChild(layerBtn);
    }
    menu.appendChild(moveGroup);
  }

  const dupBtn = document.createElement('button');
  dupBtn.className = 'fx-context-menu-item';
  dupBtn.textContent = 'Duplicate';
  dupBtn.addEventListener('click', () => { duplicateFx(fxId); removeFxContextMenu(); });
  menu.appendChild(dupBtn);

  const delBtn = document.createElement('button');
  delBtn.className = 'fx-context-menu-item danger';
  delBtn.textContent = 'Delete';
  delBtn.addEventListener('click', () => { deleteFx(fxId); removeFxContextMenu(); });
  menu.appendChild(delBtn);

  document.body.appendChild(menu);

  // Close on outside click
  setTimeout(() => {
    document.addEventListener('click', removeFxContextMenu, { once: true });
  }, 10);
}

function removeFxContextMenu() {
  const m = document.getElementById('fxContextMenu');
  if (m) m.remove();
}

function deleteFx(fxId) {
  S.setEffectsData(S.effectsData.filter(f => f.id !== fxId));
  if (S.selectedFxId === fxId) {
    S.setSelectedFxId(null);
    closeFxSettings();
  }
  updateFxCount();
  renderTimeline();
  scheduleAutoSave();
}

function duplicateFx(fxId) {
  const fx = S.effectsData.find(f => f.id === fxId);
  if (!fx) return;
  const dup = {
    id: S.nextFxId(),
    type: fx.type,
    start: Math.min(fx.end, S.videoDuration - 0.1),
    end: Math.min(fx.end + (fx.end - fx.start), S.videoDuration),
    label: fx.label,
    params: { ...(fx.params || defaultParams(fx.type)) },
    layer: fx.layer || 0,
  };
  S.effectsData.push(dup);
  S.setSelectedFxId(dup.id);
  updateFxCount();
  renderTimeline();
  openFxSettings(dup.id);
  scheduleAutoSave();
}

// ── Clear All Button ────────────────────────────────────────────────────────
function setupClearAll() {
  const btn = document.getElementById('clearAllFxBtn');
  if (!btn) return;
  btn.addEventListener('click', async () => {
    if (S.effectsData.length === 0) return;
    const ok = await confirmDialog('Clear all timeline effects?', {
      title: 'Clear effects',
      confirmText: 'Clear all',
      danger: true,
    });
    if (!ok) return;
    S.setEffectsData([]);
    S.setSelectedFxId(null);
    closeFxSettings();
    updateFxCount();
    renderTimeline();
    scheduleAutoSave();
  });
}

// ── FX Count Display ────────────────────────────────────────────────────────
function updateFxCount() {
  const el = document.getElementById('fxTrackCount');
  if (el) el.textContent = S.effectsData.length;
}

// ══════════════════════════════════════════════════════════════════════════════
//  FX SETTINGS PANEL — per-effect controls
// ══════════════════════════════════════════════════════════════════════════════
let _cropDragging = false;

function openFxSettings(fxId) {
  const fx = S.effectsData.find(f => f.id === fxId);
  if (!fx) return;
  if (!fx.params) fx.params = defaultParams(fx.type);

  const panel = document.getElementById('fxSettingsPanel');
  const title = document.getElementById('fxSettingsTitle');
  const body  = document.getElementById('fxSettingsBody');
  if (!panel || !body) return;

  title.textContent = '\u2699 ' + fx.label + ' Settings';
  body.innerHTML = '';

  // ── Common: Start / End time ──────────────────────────────────────────
  body.innerHTML += `
    <div class="fx-settings-row">
      <label>Start</label>
      <input type="number" id="fxSetStart" step="0.1" min="0" value="${fx.start.toFixed(2)}" />
      <label style="min-width:auto;">End</label>
      <input type="number" id="fxSetEnd" step="0.1" min="0" value="${fx.end.toFixed(2)}" />
    </div>
  `;

  // ── Layer selector ────────────────────────────────────────────────────
  if (S.fxLayerCount > 1) {
    let layerOpts = '';
    for (let i = 0; i < S.fxLayerCount; i++) {
      layerOpts += `<option value="${i}" ${(fx.layer || 0) === i ? 'selected' : ''}>Layer ${i + 1}</option>`;
    }
    body.innerHTML += `
      <div class="fx-settings-row">
        <label>Layer</label>
        <select id="fxSetLayer">${layerOpts}</select>
      </div>
    `;
  }

  // ── Per-type controls ─────────────────────────────────────────────────
  const p = fx.params;

  if (fx.type === 'zoom-vtuber' || fx.type === 'zoom-in-center') {
    body.innerHTML += `
      <div class="fx-settings-row">
        <label>Zoom Level</label>
        <input type="range" id="fxSetZoomLevel" min="100" max="300" value="${p.zoomLevel || 130}" class="style-range" />
        <span class="fx-val" id="fxSetZoomLevelVal">${p.zoomLevel || 130}%</span>
      </div>
      <div class="fx-settings-row">
        <label>Transition</label>
        <select id="fxSetTransition">
          <option value="smooth" ${p.transition === 'smooth' ? 'selected' : ''}>Smooth (gradual)</option>
          <option value="instant" ${p.transition === 'instant' ? 'selected' : ''}>Instant (snap)</option>
          <option value="ease-in" ${p.transition === 'ease-in' ? 'selected' : ''}>Ease In</option>
          <option value="ease-out" ${p.transition === 'ease-out' ? 'selected' : ''}>Ease Out</option>
        </select>
      </div>
      <div class="fx-settings-row" style="flex-direction:column; align-items:stretch;">
        <label style="margin-bottom:4px;">Crop Position <span style="font-weight:400;color:var(--text-3);">(click to set zoom center)</span></label>
        <div class="fx-crop-wrap" id="fxCropWrap">
          <canvas id="fxCropCanvas"></canvas>
        </div>
        <span class="fx-crop-help">Click or drag on the preview to choose zoom center point</span>
      </div>
    `;
  } else if (fx.type === 'red-flash' || fx.type === 'flash-white') {
    body.innerHTML += `
      <div class="fx-settings-row">
        <label>Intensity</label>
        <input type="range" id="fxSetIntensity" min="10" max="100" value="${p.intensity || 60}" class="style-range" />
        <span class="fx-val" id="fxSetIntensityVal">${p.intensity || 60}%</span>
      </div>
    `;
  } else if (fx.type === 'shake') {
    body.innerHTML += `
      <div class="fx-settings-row">
        <label>Intensity</label>
        <input type="range" id="fxSetIntensity" min="10" max="100" value="${p.intensity || 50}" class="style-range" />
        <span class="fx-val" id="fxSetIntensityVal">${p.intensity || 50}%</span>
      </div>
    `;
  } else if (fx.type === 'vignette') {
    body.innerHTML += `
      <div class="fx-settings-row">
        <label>Intensity</label>
        <input type="range" id="fxSetIntensity" min="10" max="100" value="${p.intensity || 60}" class="style-range" />
        <span class="fx-val" id="fxSetIntensityVal">${p.intensity || 60}%</span>
      </div>
    `;
  } else if (fx.type === 'volume-boost') {
    body.innerHTML += `
      <div class="fx-settings-row">
        <label>Gain</label>
        <input type="range" id="fxSetGain" min="1" max="5" step="0.1" value="${p.gain || 2}" class="style-range" />
        <span class="fx-val" id="fxSetGainVal">${(p.gain || 2).toFixed(1)}\u00d7</span>
      </div>
    `;
  } else if (fx.type === 'bass-boost') {
    body.innerHTML += `
      <div class="fx-settings-row">
        <label>Bass Gain</label>
        <input type="range" id="fxSetGain" min="1" max="12" step="0.5" value="${p.gain || 6}" class="style-range" />
        <span class="fx-val" id="fxSetGainVal">${(p.gain || 6).toFixed(1)} dB</span>
      </div>
    `;
  }

  panel.style.display = '';

  // ── Bind event listeners ──────────────────────────────────────────────
  bindSettingsEvents(fx);
}

function closeFxSettings() {
  const panel = document.getElementById('fxSettingsPanel');
  if (panel) panel.style.display = 'none';
  _cropDragging = false;
}

function setupFxSettingsClose() {
  const btn = document.getElementById('fxSettingsClose');
  if (btn) btn.addEventListener('click', closeFxSettings);
}

/** Keep time inputs in sync when FX is dragged on timeline */
function syncSettingsTime(fx) {
  if (!fx || S.selectedFxId !== fx.id) return;
  const startEl = document.getElementById('fxSetStart');
  const endEl   = document.getElementById('fxSetEnd');
  if (startEl) startEl.value = fx.start.toFixed(2);
  if (endEl)   endEl.value   = fx.end.toFixed(2);
}

function bindSettingsEvents(fx) {
  // Start / End
  const startEl = document.getElementById('fxSetStart');
  const endEl   = document.getElementById('fxSetEnd');
  if (startEl) startEl.addEventListener('change', () => {
    fx.start = Math.max(0, parseFloat(startEl.value) || 0);
    if (fx.start >= fx.end) fx.start = fx.end - 0.05;
    renderTimeline();
    scheduleAutoSave();
  });
  if (endEl) endEl.addEventListener('change', () => {
    fx.end = Math.min(S.videoDuration, parseFloat(endEl.value) || 0);
    if (fx.end <= fx.start) fx.end = fx.start + 0.05;
    renderTimeline();
    scheduleAutoSave();
  });

  // Intensity slider (red-flash, flash-white, shake, vignette)
  const intSlider = document.getElementById('fxSetIntensity');
  const intVal    = document.getElementById('fxSetIntensityVal');
  if (intSlider) intSlider.addEventListener('input', () => {
    const v = parseInt(intSlider.value, 10);
    fx.params.intensity = v;
    if (intVal) intVal.textContent = v + '%';
    scheduleAutoSave();
  });

  // Zoom level
  const zoomSlider = document.getElementById('fxSetZoomLevel');
  const zoomVal    = document.getElementById('fxSetZoomLevelVal');
  if (zoomSlider) zoomSlider.addEventListener('input', () => {
    const v = parseInt(zoomSlider.value, 10);
    fx.params.zoomLevel = v;
    if (zoomVal) zoomVal.textContent = v + '%';
    drawCropPreview(fx);
    scheduleAutoSave();
  });

  // Transition select
  const transSel = document.getElementById('fxSetTransition');
  if (transSel) transSel.addEventListener('change', () => {
    fx.params.transition = transSel.value;
    scheduleAutoSave();
  });

  // Gain slider (volume-boost, bass-boost)
  const gainSlider = document.getElementById('fxSetGain');
  const gainVal    = document.getElementById('fxSetGainVal');
  if (gainSlider) gainSlider.addEventListener('input', () => {
    const v = parseFloat(gainSlider.value);
    fx.params.gain = v;
    if (gainVal) {
      gainVal.textContent = fx.type === 'bass-boost' ? v.toFixed(1) + ' dB' : v.toFixed(1) + '\u00d7';
    }
    scheduleAutoSave();
  });

  // Layer selector
  const layerSel = document.getElementById('fxSetLayer');
  if (layerSel) layerSel.addEventListener('change', () => {
    fx.layer = parseInt(layerSel.value, 10) || 0;
    renderTimeline();
    scheduleAutoSave();
  });

  // Crop position canvas (zoom effects)
  setupCropCanvas(fx);
}

// ══════════════════════════════════════════════════════════════════════════════
//  CROP POSITION SELECTOR (mini canvas in settings panel)
// ══════════════════════════════════════════════════════════════════════════════
function drawCropPreview(fx) {
  const wrap   = document.getElementById('fxCropWrap');
  const canvas = document.getElementById('fxCropCanvas');
  if (!wrap || !canvas) return;

  const video = document.getElementById('previewVideo');
  const rect  = wrap.getBoundingClientRect();
  canvas.width  = rect.width;
  canvas.height = rect.height;
  const ctx = canvas.getContext('2d');
  ctx.clearRect(0, 0, canvas.width, canvas.height);

  // Draw video thumbnail or dark fill
  if (video && video.readyState >= 2) {
    ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
  } else {
    ctx.fillStyle = '#1a1a2e';
    ctx.fillRect(0, 0, canvas.width, canvas.height);
  }

  // Calculate visible crop rect
  const zoomScale = (fx.params.zoomLevel || 130) / 100;
  const cropW = canvas.width / zoomScale;
  const cropH = canvas.height / zoomScale;
  const cx = (fx.params.cropX || 0.5) * canvas.width;
  const cy = (fx.params.cropY || 0.5) * canvas.height;
  let rx = cx - cropW / 2;
  let ry = cy - cropH / 2;
  rx = Math.max(0, Math.min(canvas.width - cropW, rx));
  ry = Math.max(0, Math.min(canvas.height - cropH, ry));

  // Dim outside crop
  ctx.fillStyle = 'rgba(0,0,0,0.5)';
  ctx.fillRect(0, 0, canvas.width, ry);
  ctx.fillRect(0, ry + cropH, canvas.width, canvas.height - (ry + cropH));
  ctx.fillRect(0, ry, rx, cropH);
  ctx.fillRect(rx + cropW, ry, canvas.width - (rx + cropW), cropH);

  // Border
  ctx.strokeStyle = '#6366f1';
  ctx.lineWidth = 2;
  ctx.setLineDash([6, 3]);
  ctx.strokeRect(rx, ry, cropW, cropH);
  ctx.setLineDash([]);

  // Center crosshair
  ctx.strokeStyle = 'rgba(255,255,255,0.6)';
  ctx.lineWidth = 1;
  const midX = rx + cropW / 2;
  const midY = ry + cropH / 2;
  ctx.beginPath();
  ctx.moveTo(midX - 12, midY); ctx.lineTo(midX + 12, midY);
  ctx.moveTo(midX, midY - 12); ctx.lineTo(midX, midY + 12);
  ctx.stroke();
}

function setupCropCanvas(fx) {
  const wrap   = document.getElementById('fxCropWrap');
  const canvas = document.getElementById('fxCropCanvas');
  if (!wrap || !canvas) return;

  drawCropPreview(fx);

  const setCropFromEvent = (e) => {
    const rect = wrap.getBoundingClientRect();
    const x = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
    const y = Math.max(0, Math.min(1, (e.clientY - rect.top) / rect.height));
    fx.params.cropX = x;
    fx.params.cropY = y;
    drawCropPreview(fx);
    scheduleAutoSave();
  };

  wrap.addEventListener('mousedown', (e) => {
    _cropDragging = true;
    setCropFromEvent(e);
    e.preventDefault();
  });
  const onMove = (e) => { if (_cropDragging) setCropFromEvent(e); };
  const onUp   = ()  => { _cropDragging = false; };
  document.addEventListener('mousemove', onMove);
  document.addEventListener('mouseup', onUp);
}

// ── Filter Controls ─────────────────────────────────────────────────────────
function setupFilterControls() {
  // Filter preset grid
  const filterGrid = document.getElementById('filterGrid');
  if (filterGrid) {
    filterGrid.addEventListener('click', (e) => {
      const btn = e.target.closest('.filter-btn');
      if (!btn) return;
      filterGrid.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      S.setActiveFilter(btn.dataset.filter);
      applyFilterPreset(btn.dataset.filter);
    });
  }

  // Brightness / Contrast / Saturation sliders
  setupFilterSlider('brightnessRange', 'brightnessVal', v => S.setFilterBrightness(v));
  setupFilterSlider('contrastRange', 'contrastVal', v => S.setFilterContrast(v));
  setupFilterSlider('saturationRange', 'saturationVal', v => S.setFilterSaturation(v));
}

function setupFilterSlider(sliderId, labelId, setter) {
  const slider = document.getElementById(sliderId);
  const label  = document.getElementById(labelId);
  if (!slider) return;
  slider.addEventListener('input', () => {
    const v = parseInt(slider.value, 10);
    if (label) label.textContent = v;
    setter(v);
    applyPreviewFilter();
  });
}

function applyFilterPreset(name) {
  const brSlider = document.getElementById('brightnessRange');
  const coSlider = document.getElementById('contrastRange');
  const saSlider = document.getElementById('saturationRange');

  const presets = {
    'none':          { brightness: 0,  contrast: 0,  saturation: 0 },
    'warm':          { brightness: 5,  contrast: 5,  saturation: 20 },
    'cool':          { brightness: 0,  contrast: 5,  saturation: -10 },
    'vintage':       { brightness: -5, contrast: 10, saturation: -30 },
    'high-contrast': { brightness: 0,  contrast: 40, saturation: 10 },
    'anime-boost':   { brightness: 5,  contrast: 15, saturation: 40 },
  };
  const p = presets[name] || presets['none'];

  S.setFilterBrightness(p.brightness);
  S.setFilterContrast(p.contrast);
  S.setFilterSaturation(p.saturation);

  if (brSlider) { brSlider.value = p.brightness; document.getElementById('brightnessVal').textContent = p.brightness; }
  if (coSlider) { coSlider.value = p.contrast; document.getElementById('contrastVal').textContent = p.contrast; }
  if (saSlider) { saSlider.value = p.saturation; document.getElementById('saturationVal').textContent = p.saturation; }

  applyPreviewFilter();
}

// ── Preview Filter (CSS) ────────────────────────────────────────────────────
function applyPreviewFilter() {
  const video = document.getElementById('previewVideo');
  if (!video) return;
  const b = 100 + S.filterBrightness;
  const c = 100 + S.filterContrast;
  const s = 100 + S.filterSaturation;
  video.style.filter = `brightness(${b}%) contrast(${c}%) saturate(${s}%)`;
}

// ── Preview FX Overlay (real-time during playback) ──────────────────────────
let _fxOverlay = null;

function ensureFxOverlay() {
  if (_fxOverlay) return _fxOverlay;
  const wrap = document.querySelector('.video-preview-wrap');
  if (!wrap) return null;
  _fxOverlay = document.createElement('div');
  _fxOverlay.id = 'fxPreviewOverlay';
  _fxOverlay.style.cssText = `
    position: absolute; inset: 0; pointer-events: none;
    z-index: 15; transition: opacity 0.1s;
  `;
  wrap.appendChild(_fxOverlay);
  return _fxOverlay;
}

export function updateFxPreview(currentTime) {
  const overlay = ensureFxOverlay();
  if (!overlay) return;

  const active = S.effectsData.filter(fx =>
    currentTime >= fx.start && currentTime < fx.end
  );

  let overlayStyle = '';
  let hasTransform = false;

  for (const fx of active) {
    const progress = (currentTime - fx.start) / (fx.end - fx.start); // 0..1
    const p = fx.params || {};

    switch (fx.type) {
      case 'red-flash': {
        const intensity = (p.intensity || 60) / 100;
        const alpha = Math.max(0, intensity * 0.55 * (1 - progress));
        overlayStyle += `background: rgba(255,0,0,${alpha.toFixed(3)});`;
        break;
      }
      case 'flash-white': {
        const intensity = (p.intensity || 70) / 100;
        const alpha = Math.max(0, intensity * 0.7 * (1 - progress));
        overlayStyle += `background: rgba(255,255,255,${alpha.toFixed(3)});`;
        break;
      }
      case 'vignette': {
        const intensity = (p.intensity || 60) / 100;
        const spread = 40 + intensity * 40;
        const blur   = 60 + intensity * 40;
        overlayStyle += `box-shadow: inset 0 0 ${blur}px ${spread}px rgba(0,0,0,${(0.4 + intensity * 0.3).toFixed(2)});`;
        break;
      }
      case 'zoom-vtuber':
      case 'zoom-in-center': {
        hasTransform = true;
        const zoomLevel = (p.zoomLevel || 130) / 100;
        const transition = p.transition || 'smooth';
        const cropXOff = ((p.cropX || 0.5) - 0.5) * 100;
        const cropYOff = ((p.cropY || 0.5) - 0.5) * 100;
        let scale;
        if (transition === 'instant') {
          scale = zoomLevel;
        } else if (transition === 'ease-in') {
          scale = 1 + (zoomLevel - 1) * (progress * progress);
        } else if (transition === 'ease-out') {
          const inv = 1 - progress;
          scale = 1 + (zoomLevel - 1) * (1 - inv * inv);
        } else { // smooth (linear)
          scale = 1 + (zoomLevel - 1) * progress;
        }
        const vid = document.getElementById('previewVideo');
        if (vid) {
          vid.style.transform = `scale(${scale.toFixed(4)}) translate(${-cropXOff * (scale - 1)}%, ${-cropYOff * (scale - 1)}%)`;
          vid.style.transformOrigin = `${(p.cropX || 0.5) * 100}% ${(p.cropY || 0.5) * 100}%`;
        }
        break;
      }
      case 'shake': {
        hasTransform = true;
        const shakeInt = ((p.intensity || 50) / 100) * 8;
        const decay = 1 - progress;
        const rx = (Math.random() - 0.5) * shakeInt * decay;
        const ry = (Math.random() - 0.5) * shakeInt * decay;
        const vid = document.getElementById('previewVideo');
        if (vid) vid.style.transform = `translate(${rx.toFixed(1)}px, ${ry.toFixed(1)}px)`;
        break;
      }
    }
  }

  // Apply overlay
  if (overlayStyle) {
    overlay.setAttribute('style', `
      position: absolute; inset: 0; pointer-events: none;
      z-index: 15; transition: opacity 0.1s; ${overlayStyle}
    `);
  } else {
    overlay.setAttribute('style', `
      position: absolute; inset: 0; pointer-events: none;
      z-index: 15; background: transparent;
    `);
  }

  if (!hasTransform) {
    const vid = document.getElementById('previewVideo');
    if (vid) {
      vid.style.transform = '';
      vid.style.transformOrigin = '';
    }
  }

  // ── Audio FX preview via Web Audio API ────────────────────────────────
  updateAudioPreview(currentTime);
}

// ── Audio FX preview via Web Audio API ──────────────────────────────────────
function updateAudioPreview(currentTime) {
  if (!_audioConnected) {
    ensureAudioGraph();
    if (!_audioConnected) return;
  }

  const activeAudio = S.effectsData.filter(fx =>
    currentTime >= fx.start && currentTime < fx.end &&
    (fx.type === 'volume-boost' || fx.type === 'bass-boost')
  );

  let volGain = 1.0;
  let bassGain = 0;
  for (const fx of activeAudio) {
    const p = fx.params || {};
    if (fx.type === 'volume-boost') volGain = Math.max(volGain, p.gain || 2.0);
    if (fx.type === 'bass-boost')   bassGain = Math.max(bassGain, p.gain || 6);
  }

  if (_gainNode) _gainNode.gain.setTargetAtTime(volGain, _audioCtx.currentTime, 0.05);
  if (_bassFilter) _bassFilter.gain.setTargetAtTime(bassGain, _audioCtx.currentTime, 0.05);
}

// ── Collect effects data for render API ─────────────────────────────────────
export function collectEffectsConfig() {
  return {
    effects: S.effectsData.map(fx => ({
      type: fx.type,
      start: fx.start,
      end: fx.end,
      params: fx.params || {},
      layer: fx.layer || 0,
    })),
    filter: {
      name: S.activeFilter,
      brightness: S.filterBrightness,
      contrast: S.filterContrast,
      saturation: S.filterSaturation,
    },
  };
}

// ══════════════════════════════════════════════════════════════════════════════
//  LAYER MANAGEMENT
// ══════════════════════════════════════════════════════════════════════════════
function addFxLayer() {
  if (S.fxLayerCount >= 8) return;
  S.setFxLayerCount(S.fxLayerCount + 1);
  renderTimeline();
  updateLayerCountDisplay();
}

function removeFxLayer() {
  if (S.fxLayerCount <= 1) return;
  const removedIdx = S.fxLayerCount - 1;
  // Move FX from removed layer down to the last remaining layer
  S.effectsData.forEach(fx => {
    if ((fx.layer || 0) >= removedIdx) fx.layer = removedIdx - 1;
  });
  S.setFxLayerCount(S.fxLayerCount - 1);
  renderTimeline();
  updateLayerCountDisplay();
  scheduleAutoSave();
}

function updateLayerCountDisplay() {
  const el = document.getElementById('fxLayerCountDisplay');
  if (el) el.textContent = S.fxLayerCount;
  const rmBtn = document.getElementById('removeFxLayerBtn');
  if (rmBtn) rmBtn.disabled = S.fxLayerCount <= 1;
  const addBtn = document.getElementById('addFxLayerBtn');
  if (addBtn) addBtn.disabled = S.fxLayerCount >= 8;
}

function setupLayerButtons() {
  const addBtn = document.getElementById('addFxLayerBtn');
  const rmBtn  = document.getElementById('removeFxLayerBtn');
  if (addBtn) addBtn.addEventListener('click', addFxLayer);
  if (rmBtn)  rmBtn.addEventListener('click', removeFxLayer);
  updateLayerCountDisplay();
}

// ── Setup ───────────────────────────────────────────────────────────────────
export function setupEffects() {
  setupTabs();
  setupPresetDrag();
  setupTimelineFxDrop();
  setupFxBlockInteraction();
  setupClearAll();
  setupFilterControls();
  setupFxSettingsClose();
  setupLayerButtons();

  // Connect Web Audio on first user interaction
  document.addEventListener('click', () => ensureAudioGraph(), { once: true });
}
