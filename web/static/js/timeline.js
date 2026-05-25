/**
 * timeline.js — Timeline component: ruler, segments, playhead, split/merge, keyboard shortcuts
 */

import { fmtTime, fmtTimeShort, parseTime, toast, confirmDialog } from './utils.js';
import * as S from './state.js';
import { pushUndoSnapshot, popUndoSnapshot, pushRedoSnapshot, popRedoSnapshot } from './state.js';
import { renderTranscriptList, setActiveSeg, scheduleAutoSave } from './preview.js';
import { onStyleChange } from './subtitleEngine.js';

// ── DOM Refs ───────────────────────────────────────────────────────────────
const previewVideo      = document.getElementById('previewVideo');
const subtitleContainer = document.getElementById('subtitleContainer');
const timelineRuler     = document.getElementById('timelineRuler');
const timelineTrackArea = document.getElementById('timelineTrackArea');
const timelineTrack     = document.getElementById('timelineTrack');
const timelineScrollArea = document.getElementById('timelineScrollArea');
const timelineLabelsCol  = document.getElementById('timelineLabelsCol');
const tlTimeDisplay     = document.getElementById('tlTimeDisplay');
const addSegmentBtn     = document.getElementById('addSegmentBtn');
const deleteSegmentBtn  = document.getElementById('deleteSegmentBtn');
const duplicateSegmentBtn = document.getElementById('duplicateSegmentBtn');
const timelineBody       = document.getElementById('timelineBody');
const timelineResizeHandle = document.getElementById('timelineResizeHandle');
const videoPreviewWrap   = document.querySelector('.video-preview-wrap');

// ── Timeline Resize State ─────────────────────────────────────────────────
const TL_HEIGHT_MIN     = 60;
const TL_HEIGHT_MAX     = 600;
const TL_HEIGHT_DEFAULT = 180;
const TL_STORAGE_KEY    = 'clipauto_timeline_height';

let _tlResizing        = false;
let _tlResizeStartY    = 0;
let _tlResizeStartH    = 0;

// Segment dialog
const segmentDialog    = document.getElementById('segmentDialog');
const newSegStart      = document.getElementById('newSegStart');
const newSegEnd        = document.getElementById('newSegEnd');
const newSegText       = document.getElementById('newSegText');
const newSegSpeaker    = document.getElementById('newSegSpeaker');
const cancelNewSeg     = document.getElementById('cancelNewSeg');
const confirmNewSeg    = document.getElementById('confirmNewSeg');

// Split dialog
const splitDialog      = document.getElementById('splitDialog');
const splitDialogWords = document.getElementById('splitDialogWords');
const splitPart1Time   = document.getElementById('splitPart1Time');
const splitPart1Text   = document.getElementById('splitPart1Text');
const splitPart2Time   = document.getElementById('splitPart2Time');
const splitPart2Text   = document.getElementById('splitPart2Text');
const cancelSplitSeg   = document.getElementById('cancelSplitSeg');
const confirmSplitSeg  = document.getElementById('confirmSplitSeg');

// ── Timeline Resize ──────────────────────────────────────────────────────
function initTimelineResize() {
  // Restore saved height or use default
  const saved = parseInt(localStorage.getItem(TL_STORAGE_KEY), 10);
  const initH = (saved >= TL_HEIGHT_MIN && saved <= TL_HEIGHT_MAX) ? saved : TL_HEIGHT_DEFAULT;
  timelineBody.style.height = initH + 'px';

  timelineResizeHandle.addEventListener('mousedown', (e) => {
    _tlResizing = true;
    _tlResizeStartY = e.clientY;
    _tlResizeStartH = timelineBody.getBoundingClientRect().height;
    timelineResizeHandle.classList.add('is-resizing');
    document.body.style.cursor = 'ns-resize';
    document.body.style.userSelect = 'none';
    e.preventDefault();
    e.stopPropagation();
  });
}

// ── Word-Timestamp Rescaling on Edge Drag ────────────────────────────────
//
// When the user drags a segment's left or right edge, the segment box
// changes (seg.start / seg.end), but seg.words[].start/end are still
// anchored to the original ElevenLabs timestamps.  Without rescaling, the
// karaoke / narration-pop highlight drifts out of the segment box because
// the engine matches words against the ORIGINAL word times.
//
// Strategy: linear rescale every word's [start, end] from the original
// segment span [origStart, origEnd] into the new span [seg.start, seg.end].
// This is approximate (it doesn't preserve true speech timing inside a
// shrunk window) but it keeps the visual/audio sync consistent with what
// the user just edited.  When the user wants exact ElevenLabs timing back
// they can undo (Ctrl+Z) or reload the original transcript.
function _rescaleWordsForEdgeDrag(seg, dragState) {
  if (!Array.isArray(seg.words) || seg.words.length === 0) return;
  if (!dragState || !Array.isArray(dragState.origWords)) return;

  const origSpan = dragState.origEnd - dragState.origStart;
  const newSpan  = seg.end - seg.start;
  if (origSpan <= 0 || newSpan <= 0) return;

  const origStart = dragState.origStart;
  const newStart  = seg.start;

  seg.words.forEach((w, i) => {
    const ow = dragState.origWords[i];
    if (!ow) return;
    const wsRel = (ow.start - origStart) / origSpan;
    const weRel = (ow.end   - origStart) / origSpan;
    w.start = newStart + wsRel * newSpan;
    w.end   = newStart + weRel * newSpan;
  });
}

// ── Setup ──────────────────────────────────────────────────────────────────
export function setupTimeline() {
  // Initialize timeline height resize
  initTimelineResize();
  // Add segment button
  addSegmentBtn.addEventListener('click', () => openSegmentDialog(previewVideo.currentTime));

  // Ruler click / drag → seek the video (Current Time Indicator).
  // Clicking the ruler moves the playhead; click-and-drag scrubs.
  timelineRuler.addEventListener('mousedown', (e) => {
    if (!S.videoDuration) return;
    e.preventDefault();
    const seekFromEvent = (ev) => {
      const trackRect = timelineTrack.getBoundingClientRect();
      const x = ev.clientX - trackRect.left;
      const trackWidth = getTrackWidth();
      const t = Math.max(0, Math.min(S.videoDuration, (x / trackWidth) * S.videoDuration));
      previewVideo.currentTime = t;
    };
    seekFromEvent(e);
    S.setDraggingPlayhead(true);
  });

  // Delete segment button
  deleteSegmentBtn.addEventListener('click', async () => {
    if (S.selectedSegIdx !== null && S.selectedSegIdx < S.transcriptData.length) {
      const ok = await confirmDialog('Delete this subtitle segment?', {
        title: 'Delete segment',
        confirmText: 'Delete',
        danger: true,
      });
      if (!ok) return;
      pushUndoSnapshot();
      S.transcriptData.splice(S.selectedSegIdx, 1);
      S.setSelectedSegIdx(null);
      deleteSegmentBtn.disabled = true;
      duplicateSegmentBtn.disabled = true;
      renderTimeline();
      renderTranscriptList();
      onStyleChange();
      scheduleAutoSave();
    }
  });

  // Duplicate segment button
  duplicateSegmentBtn.addEventListener('click', duplicateSelectedSegment);

  // Segment dialog
  cancelNewSeg.addEventListener('click', closeSegmentDialog);
  confirmNewSeg.addEventListener('click', confirmAddSegment);

  // Playhead drag on track area
  timelineTrackArea.addEventListener('mousedown', (e) => {
    if (e.target.classList.contains('playhead-handle')) {
      S.setDraggingPlayhead(true);
      e.preventDefault();
      return;
    }
    if (e.target.classList.contains('seg-handle')) {
      const segEl = e.target.parentElement;
      const segIdx = parseInt(segEl.dataset.idx);
      const edge = e.target.classList.contains('seg-handle-left') ? 'start' : 'end';
      pushUndoSnapshot();
      const seg = S.transcriptData[segIdx];
      // Capture original boundaries so we can scale words proportionally
      // during drag.  Without this, only seg.start/end change while
      // seg.words[].start/end stay frozen, which makes karaoke /
      // narration-pop highlighting drift out of sync with the segment box
      // the user just edited.
      S.setDraggingSegEdge({
        segIdx,
        edge,
        origStart: seg.start,
        origEnd: seg.end,
        origWords: (seg.words || []).map(w => ({ start: w.start, end: w.end })),
      });
      e.preventDefault();
      return;
    }
    if (e.target.classList.contains('timeline-segment') || e.target.closest('.timeline-segment')) {
      const segEl = e.target.classList.contains('timeline-segment') ? e.target : e.target.closest('.timeline-segment');
      const segIdx = parseInt(segEl.dataset.idx);
      selectSegment(segIdx);
      previewVideo.currentTime = S.transcriptData[segIdx].start;
      const trackRect = timelineTrack.getBoundingClientRect();
      const x = e.clientX - trackRect.left;
      const trackWidth = getTrackWidth();
      const clickTime = (x / trackWidth) * S.videoDuration;
      const seg = S.transcriptData[segIdx];
      pushUndoSnapshot();
      S.setDraggingSegBody({
        segIdx,
        offsetTime: clickTime - seg.start,
        segDuration: seg.end - seg.start,
        origSpeaker: seg.speaker || 'SPEAKER_00',
      });
      segEl.classList.add('dragging-body');
      e.preventDefault();
      return;
    }
    // Click on empty area — seek playhead (and start drag-to-scrub)
    const trackRect = timelineTrack.getBoundingClientRect();
    const x = e.clientX - trackRect.left;
    const trackWidth = getTrackWidth();
    const t = (x / trackWidth) * S.videoDuration;
    if (t >= 0 && t <= S.videoDuration) {
      previewVideo.currentTime = t;
      S.setDraggingPlayhead(true);
      e.preventDefault();
    }
  });

  // Mouse move for playhead and segment edge dragging
  document.addEventListener('mousemove', (e) => {
    if (S.draggingPlayhead) {
      const trackRect = timelineTrack.getBoundingClientRect();
      const x = e.clientX - trackRect.left;
      const trackWidth = getTrackWidth();
      const t = Math.max(0, Math.min(S.videoDuration, (x / trackWidth) * S.videoDuration));
      previewVideo.currentTime = t;
    }
    if (S.draggingSegEdge) {
      const trackRect = timelineTrack.getBoundingClientRect();
      const x = e.clientX - trackRect.left;
      const trackWidth = getTrackWidth();
      const t = Math.max(0, Math.min(S.videoDuration, (x / trackWidth) * S.videoDuration));
      const seg = S.transcriptData[S.draggingSegEdge.segIdx];
      if (S.draggingSegEdge.edge === 'start') {
        seg.start = Math.min(t, seg.end - 0.1);
      } else {
        seg.end = Math.max(t, seg.start + 0.1);
      }
      // Linearly rescale word timestamps so the karaoke / narration-pop
      // highlight stays in sync with the new segment boundaries.
      _rescaleWordsForEdgeDrag(seg, S.draggingSegEdge);
      renderTimeline();
    }
    if (S.draggingSegBody) {
      const trackRect = timelineTrack.getBoundingClientRect();
      const x = e.clientX - trackRect.left;
      const trackWidth = getTrackWidth();
      const mouseTime = (x / trackWidth) * S.videoDuration;
      const newStart = Math.max(0, Math.min(S.videoDuration - S.draggingSegBody.segDuration, mouseTime - S.draggingSegBody.offsetTime));
      const seg = S.transcriptData[S.draggingSegBody.segIdx];
      const dt = newStart - seg.start;
      seg.start = newStart;
      seg.end = newStart + S.draggingSegBody.segDuration;
      // Shift every word by the same delta so word-level timestamps stay
      // anchored to the segment box being dragged.
      if (dt !== 0 && Array.isArray(seg.words)) {
        seg.words.forEach(w => {
          w.start += dt;
          w.end += dt;
        });
      }

      // Cross-speaker drag: detect which speaker row the mouse Y is over
      const ROW_HEIGHT = 36;
      const hasFxTrack = true;
      const fxRowCount = hasFxTrack ? S.fxLayerCount : 0;
      const speakerYOffset = fxRowCount * ROW_HEIGHT;
      const y = e.clientY - trackRect.top;
      const speakerRowIdx = Math.floor((y - speakerYOffset) / ROW_HEIGHT);
      if (speakerRowIdx >= 0 && speakerRowIdx < S.timelineSpeakers.length) {
        const targetSpeaker = S.timelineSpeakers[speakerRowIdx];
        if (seg.speaker !== targetSpeaker) {
          seg.speaker = targetSpeaker;
        }
      }

      renderTimeline();
    }
    if (S.subtitleDragState) {
      const ds = S.subtitleDragState;
      const dx = e.clientX - ds.startX;
      const dy = e.clientY - ds.startY;
      const newPosX = Math.max(5, Math.min(95, ds.origPosX + (dx / ds.overlayRect.width) * 100));
      const newPosY = Math.max(5, Math.min(95, ds.origPosY + (dy / ds.overlayRect.height) * 100));
      const seg = S.transcriptData[ds.segIdx];
      seg.posX = newPosX;
      seg.pos_x = newPosX;
      seg.posY = newPosY;
      seg.pos_y = newPosY;
      seg.posOverride = true;
      seg.pos_override = true;
      const lineEl = subtitleContainer.querySelector(`[data-seg-idx="${ds.segIdx}"]`);
      if (lineEl) {
        lineEl.style.left = newPosX + '%';
        lineEl.style.top = newPosY + '%';
      }
    }
    // Timeline vertical resize
    if (_tlResizing) {
      const dy = _tlResizeStartY - e.clientY; // drag UP = positive = taller
      const newH = Math.max(TL_HEIGHT_MIN, Math.min(TL_HEIGHT_MAX, _tlResizeStartH + dy));
      timelineBody.style.height = newH + 'px';
    }
  });

  // Mouse up — stop all dragging
  document.addEventListener('mouseup', () => {
    if (S.draggingPlayhead) S.setDraggingPlayhead(false);
    if (S.draggingSegEdge) {
      S.setDraggingSegEdge(null);
      renderTranscriptList();
      scheduleAutoSave();
    }
    if (S.draggingSegBody) {
      timelineTrack.querySelectorAll('.timeline-segment.dragging-body').forEach(el => el.classList.remove('dragging-body'));
      S.setDraggingSegBody(null);
      renderTranscriptList();
      onStyleChange();
      scheduleAutoSave();
    }
    if (S.subtitleDragState) {
      document.querySelectorAll('.subtitle-line.dragging').forEach(el => el.classList.remove('dragging'));
      S.setSubtitleDragState(null);
      onStyleChange();
    }
    if (_tlResizing) {
      _tlResizing = false;
      timelineResizeHandle.classList.remove('is-resizing');
      document.body.style.cursor = '';
      document.body.style.userSelect = '';
      // Persist height preference
      const h = Math.round(timelineBody.getBoundingClientRect().height);
      localStorage.setItem(TL_STORAGE_KEY, h);
    }
  });

  // Keyboard shortcuts
  document.addEventListener('keydown', (e) => {
    const previewScreen = document.getElementById('screen-preview');
    if (!previewScreen.classList.contains('active')) return;
    if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA' || e.target.isContentEditable) return;

    const isMod = e.ctrlKey || e.metaKey;

    // ── Undo (Ctrl/Cmd + Z) ──
    if (isMod && !e.shiftKey && (e.key === 'z' || e.key === 'Z')) {
      e.preventDefault();
      const snap = popUndoSnapshot();
      if (!snap) { toast('Nothing to undo', 'info'); return; }
      // Save current state to redo before restoring
      pushRedoSnapshot();
      S.setTranscriptData(snap);
      S.setSelectedSegIdx(null);
      deleteSegmentBtn.disabled = true;
      duplicateSegmentBtn.disabled = true;
      renderTimeline();
      renderTranscriptList();
      onStyleChange();
      scheduleAutoSave();
      toast('Undo', 'info');
      return;
    }

    // ── Redo (Ctrl/Cmd + Y  OR  Ctrl/Cmd + Shift + Z) ──
    if (isMod && ((e.key === 'y' || e.key === 'Y') || (e.shiftKey && (e.key === 'z' || e.key === 'Z')))) {
      e.preventDefault();
      const snap = popRedoSnapshot();
      if (!snap) { toast('Nothing to redo', 'info'); return; }
      // Save current onto undo stack WITHOUT clearing redo (manual push to avoid reset)
      S.undoStack.push(JSON.parse(JSON.stringify(S.transcriptData)));
      S.setTranscriptData(snap);
      S.setSelectedSegIdx(null);
      deleteSegmentBtn.disabled = true;
      duplicateSegmentBtn.disabled = true;
      renderTimeline();
      renderTranscriptList();
      onStyleChange();
      scheduleAutoSave();
      toast('Redo', 'info');
      return;
    }

    if (e.key === ' ') {
      e.preventDefault();
      if (previewVideo.paused) previewVideo.play();
      else previewVideo.pause();
    } else if (e.key === 'ArrowLeft') {
      e.preventDefault();
      previewVideo.currentTime = Math.max(0, previewVideo.currentTime - (e.shiftKey ? 5 : 1));
    } else if (e.key === 'ArrowRight') {
      e.preventDefault();
      previewVideo.currentTime = Math.min(S.videoDuration, previewVideo.currentTime + (e.shiftKey ? 5 : 1));
    } else if (e.key === 'Delete' || e.key === 'Backspace') {
      if (S.selectedSegIdx !== null && S.selectedSegIdx < S.transcriptData.length) {
        e.preventDefault();
        pushUndoSnapshot();
        S.transcriptData.splice(S.selectedSegIdx, 1);
        S.setSelectedSegIdx(null);
        deleteSegmentBtn.disabled = true;
        duplicateSegmentBtn.disabled = true;
        renderTimeline();
        renderTranscriptList();
        onStyleChange();
        scheduleAutoSave();
      }
    } else if (isMod && (e.key === 'd' || e.key === 'D')) {
      e.preventDefault();
      duplicateSelectedSegment();
    } else if (e.key === 'f' || e.key === 'F') {
      e.preventDefault();
      // Fullscreen handled in preview.js
      const videoWrap = document.querySelector('.video-preview-wrap');
      if (!document.fullscreenElement) {
        videoWrap.requestFullscreen().catch(() => {});
      } else {
        document.exitFullscreen();
      }
    }
  });

  // ── Wheel Zoom (Ctrl+Scroll) — like After Effects / Premiere Pro ──────────
  // Zooms timeline content width without changing the card/panel size.
  // Zoom anchor = mouse cursor position so the point under the cursor stays fixed.
  timelineScrollArea.addEventListener('wheel', (e) => {
    if (!S.videoDuration) return;

    if (e.ctrlKey) {
      // Ctrl + Wheel → zoom in / out
      e.preventDefault();
      const ZOOM_STEP   = 1.18;
      const zoomFactor  = e.deltaY < 0 ? ZOOM_STEP : 1 / ZOOM_STEP;
      const newZoom     = Math.max(0.3, Math.min(40, S.timelineZoom * zoomFactor));

      // Keep the timeline point under the mouse cursor stationary
      const rect           = timelineScrollArea.getBoundingClientRect();
      const mouseX         = e.clientX - rect.left;
      const oldContentW    = timelineScrollArea.clientWidth * S.timelineZoom;
      const cursorRatio    = (timelineScrollArea.scrollLeft + mouseX) / oldContentW;

      S.setTimelineZoom(newZoom);
      renderTimeline();

      const newContentW    = timelineScrollArea.clientWidth * newZoom;
      timelineScrollArea.scrollLeft = Math.max(0, cursorRatio * newContentW - mouseX);

      updateZoomDisplay();
    } else if (!e.shiftKey) {
      // Plain vertical wheel → horizontal pan (no Ctrl, no Shift)
      e.preventDefault();
      timelineScrollArea.scrollLeft += e.deltaY * 1.2;
    }
    // Shift + Wheel: browser handles as horizontal scroll natively
  }, { passive: false });

  // Click zoom badge → reset zoom to 1×
  const tlZoomBadge = document.getElementById('tlZoomBadge');
  if (tlZoomBadge) {
    tlZoomBadge.addEventListener('click', () => {
      S.setTimelineZoom(1.0);
      renderTimeline();
      updateZoomDisplay();
    });
  }

  // Split dialog
  setupSplitDialog();
}

// Update the zoom % badge in the toolbar
function updateZoomDisplay() {
  const badge = document.getElementById('tlZoomBadge');
  if (badge) badge.textContent = Math.round(S.timelineZoom * 100) + '%';
}

function getTrackWidth() {
  return timelineScrollArea.clientWidth * S.timelineZoom;
}

export function selectSegment(idx) {
  S.setSelectedSegIdx(idx);
  deleteSegmentBtn.disabled = false;
  duplicateSegmentBtn.disabled = false;
  timelineTrack.querySelectorAll('.timeline-segment').forEach(el => {
    el.classList.toggle('selected', parseInt(el.dataset.idx) === idx);
  });
  setActiveSeg(idx);
}

export function renderTimeline() {
  if (!S.videoDuration || S.videoDuration <= 0) return;

  const ROW_HEIGHT   = 36;
  const ROW_PADDING  = 5;

  const trackWidth = getTrackWidth();
  timelineTrack.style.width    = trackWidth + 'px';
  timelineRuler.style.width    = trackWidth + 'px';
  timelineTrackArea.style.width = trackWidth + 'px';

  // Collect unique speakers
  const speakers = [];
  S.transcriptData.forEach(seg => {
    const sp = seg.speaker || 'SPEAKER_00';
    if (!speakers.includes(sp)) speakers.push(sp);
  });
  if (speakers.length === 0) speakers.push('SPEAKER_00');
  speakers.sort();

  // Cache speakers in state for cross-speaker drag detection
  S.setTimelineSpeakers(speakers);

  // Total rows: N FX track layers + speaker rows
  const hasFxTrack = true;
  const fxRowCount = hasFxTrack ? S.fxLayerCount : 0;
  const rowCount   = fxRowCount + speakers.length;
  const trackHeight = rowCount * ROW_HEIGHT;

  timelineTrackArea.style.height = trackHeight + 'px';
  timelineTrack.style.height     = trackHeight + 'px';

  // Update labels column — FX labels first, then speaker labels
  if (timelineLabelsCol) {
    timelineLabelsCol.innerHTML = '<div class="timeline-ruler-spacer"></div>';
    if (hasFxTrack) {
      for (let li = 0; li < S.fxLayerCount; li++) {
        const fxLabel = document.createElement('div');
        fxLabel.className = 'timeline-label-row fx-label';
        fxLabel.innerHTML = `<span>\uD83C\uDFAC FX ${li + 1}</span>`;
        timelineLabelsCol.appendChild(fxLabel);
      }
    }
    speakers.forEach(sp => {
      const spIdx = parseInt((sp.match(/\d+$/) || ['0'])[0], 10);
      const color = S.getSpeakerColor(sp);
      const labelRow = document.createElement('div');
      labelRow.className = 'timeline-label-row';
      labelRow.innerHTML = `<span style="color:${color}">S${spIdx}</span>`;
      timelineLabelsCol.appendChild(labelRow);
    });
  }

  // Clear old content
  timelineTrack.innerHTML = '';
  timelineRuler.innerHTML = '';

  // ── FX Tracks (one per layer, all are drop zones) ────────────────
  if (hasFxTrack) {
    for (let li = 0; li < S.fxLayerCount; li++) {
      const fxTrack = document.createElement('div');
      fxTrack.className = 'timeline-fx-track';
      // First layer keeps the legacy id for backward compat; all get data-layer
      if (li === 0) fxTrack.id = 'timelineFxTrack';
      fxTrack.dataset.fxLayer = li;
      fxTrack.style.height = ROW_HEIGHT + 'px';
      fxTrack.style.position = 'absolute';
      fxTrack.style.top = (li * ROW_HEIGHT) + 'px';
      fxTrack.style.left = '0';
      fxTrack.style.width = '100%';

      // Render FX blocks belonging to this layer
      S.effectsData.forEach(fx => {
        const fxLayer = fx.layer || 0;
        if (fxLayer !== li) return;

        const left  = (fx.start / S.videoDuration) * trackWidth;
        const width = Math.max(20, ((fx.end - fx.start) / S.videoDuration) * trackWidth);
        const block = document.createElement('div');
        block.className = 'fx-block' + (S.selectedFxId === fx.id ? ' selected' : '');
        block.dataset.fxId = fx.id;
        block.dataset.fxType = fx.type;
        block.dataset.fxLayer = fxLayer;
        block.style.left  = left + 'px';
        block.style.width = width + 'px';
        block.title = `${fx.label} [${fmtTime(fx.start)} - ${fmtTime(fx.end)}] Layer ${fxLayer + 1}`;
        block.textContent = width > 40 ? fx.label : '';

        const hL = document.createElement('div');
        hL.className = 'fx-handle fx-handle-left';
        block.appendChild(hL);
        const hR = document.createElement('div');
        hR.className = 'fx-handle fx-handle-right';
        block.appendChild(hR);

        fxTrack.appendChild(block);
      });

      timelineTrack.appendChild(fxTrack);
    }
  }

  // Draw row separator lines (including after FX track)
  for (let i = 0; i < rowCount; i++) {
    if (i > 0) {
      const sep = document.createElement('div');
      sep.className = 'timeline-row-line';
      sep.style.top = (i * ROW_HEIGHT) + 'px';
      timelineTrack.appendChild(sep);
    }
  }

  // Draw ruler ticks
  const pxPerSec = trackWidth / S.videoDuration;
  let tickInterval;
  if (pxPerSec > 50)      tickInterval = 1;
  else if (pxPerSec > 20) tickInterval = 2;
  else if (pxPerSec > 10) tickInterval = 5;
  else if (pxPerSec > 4)  tickInterval = 10;
  else                    tickInterval = 30;

  const majorEvery = tickInterval >= 10 ? 1 : (tickInterval >= 5 ? 2 : 5);

  for (let t = 0; t <= S.videoDuration; t += tickInterval) {
    const x = (t / S.videoDuration) * trackWidth;
    const tickCount = Math.round(t / tickInterval);
    const isMajor = tickCount % majorEvery === 0;

    const tick = document.createElement('div');
    tick.className = 'ruler-tick' + (isMajor ? ' major' : '');
    tick.style.left = x + 'px';
    timelineRuler.appendChild(tick);

    if (isMajor) {
      const label = document.createElement('span');
      label.className = 'ruler-label';
      label.style.left = x + 'px';
      label.textContent = fmtTimeShort(t);
      timelineRuler.appendChild(label);
    }
  }

  // Draw segment blocks — offset Y by fxRowCount
  const speakerYOffset = fxRowCount * ROW_HEIGHT;
  S.transcriptData.forEach((seg, idx) => {
    const sp     = seg.speaker || 'SPEAKER_00';
    const rowIdx = speakers.indexOf(sp);
    const top    = speakerYOffset + rowIdx * ROW_HEIGHT + ROW_PADDING;
    const height = ROW_HEIGHT - ROW_PADDING * 2;

    const left  = (seg.start / S.videoDuration) * trackWidth;
    const width = Math.max(4, ((seg.end - seg.start) / S.videoDuration) * trackWidth);
    const color = S.getSpeakerColor(sp);

    const block = document.createElement('div');
    block.className = 'timeline-segment' + (S.selectedSegIdx === idx ? ' selected' : '');
    block.dataset.idx = idx;
    block.style.left   = left + 'px';
    block.style.width  = width + 'px';
    block.style.top    = top + 'px';
    block.style.height = height + 'px';
    block.style.background = color;
    block.title = `[${fmtTime(seg.start)} - ${fmtTime(seg.end)}] ${seg.text}`;

    if (width > 30) {
      block.textContent = seg.text.length > Math.floor(width / 6)
        ? seg.text.slice(0, Math.floor(width / 6)) + '…'
        : seg.text;
    }

    const handleL = document.createElement('div');
    handleL.className = 'seg-handle seg-handle-left';
    block.appendChild(handleL);

    const handleR = document.createElement('div');
    handleR.className = 'seg-handle seg-handle-right';
    block.appendChild(handleR);

    timelineTrack.appendChild(block);
  });

  // Re-add playhead element
  const ph = document.createElement('div');
  ph.className = 'timeline-playhead';
  ph.id = 'timelinePlayhead';
  ph.innerHTML = '<div class="playhead-handle"></div><div class="playhead-line"></div>';
  timelineTrack.appendChild(ph);

  updatePlayhead(previewVideo.currentTime);
}

export function updatePlayhead(t) {
  const ph = document.getElementById('timelinePlayhead');
  if (!ph || !S.videoDuration) return;
  const trackWidth = getTrackWidth();
  const x = (t / S.videoDuration) * trackWidth;
  ph.style.left = x + 'px';
}

export function updateTimeDisplay(t) {
  if (tlTimeDisplay) {
    tlTimeDisplay.textContent = `${fmtTimeShort(t)} / ${fmtTimeShort(S.videoDuration)}`;
  }
}

// ── Segment Dialog ─────────────────────────────────────────────────────────
function openSegmentDialog(startTime) {
  const endTime = Math.min(startTime + 2, S.videoDuration);
  newSegStart.value = fmtTime(startTime);
  newSegEnd.value = fmtTime(endTime);
  newSegText.value = '';
  newSegSpeaker.value = 'SPEAKER_00';
  segmentDialog.classList.remove('hidden');
  newSegText.focus();
}

function closeSegmentDialog() {
  segmentDialog.classList.add('hidden');
}

function confirmAddSegment() {
  const start = parseTimeTL(newSegStart.value);
  const end = parseTimeTL(newSegEnd.value);
  const text = newSegText.value.trim();
  const speaker = newSegSpeaker.value;

  if (isNaN(start) || isNaN(end) || start >= end) {
    toast.warn('Invalid time range.');
    return;
  }
  if (!text) {
    toast.warn('Please enter subtitle text.');
    return;
  }

  const newSeg = { start, end, text, speaker, words: [] };

  let insertIdx = S.transcriptData.findIndex(s => s.start > start);
  if (insertIdx === -1) insertIdx = S.transcriptData.length;
  pushUndoSnapshot();
  S.transcriptData.splice(insertIdx, 0, newSeg);

  closeSegmentDialog();
  renderTimeline();
  renderTranscriptList();
  selectSegment(insertIdx);
  scheduleAutoSave();
}

// parseTimeTL replaced by parseTime from utils.js
const parseTimeTL = parseTime;

// ── Merge & Split ──────────────────────────────────────────────────────────
export function mergeSegmentWithNext(idx) {
  if (idx >= S.transcriptData.length - 1) return;
  pushUndoSnapshot();
  const seg  = S.transcriptData[idx];
  const next = S.transcriptData[idx + 1];

  const merged = {
    start:   seg.start,
    end:     next.end,
    text:    seg.text.trim() + ' ' + next.text.trim(),
    speaker: seg.speaker,
    words:   [...(seg.words || []), ...(next.words || [])],
  };

  S.transcriptData.splice(idx, 2, merged);
  renderTimeline();
  renderTranscriptList();
  selectSegment(idx);
  onStyleChange();
  scheduleAutoSave();
}

export function openSplitDialog(idx) {
  const seg   = S.transcriptData[idx];
  const words = seg.text.trim().split(/\s+/).filter(Boolean);
  if (words.length < 2) {
    toast.warn('Cannot split a segment that contains only one word.');
    return;
  }
  S.setSplitDialogIdx(idx);
  S.setSplitDialogWordIdx(Math.ceil(words.length / 2));
  renderSplitDialogContent();
  splitDialog.classList.remove('hidden');
}

function closeSplitDialog() {
  splitDialog.classList.add('hidden');
  S.setSplitDialogIdx(null);
  S.setSplitDialogWordIdx(1);
}

function renderSplitDialogContent() {
  if (S.splitDialogIdx === null) return;
  const seg   = S.transcriptData[S.splitDialogIdx];
  const words = seg.text.trim().split(/\s+/).filter(Boolean);

  splitDialogWords.innerHTML = '';
  words.forEach((w, i) => {
    const span = document.createElement('span');
    span.className = 'split-word ' + (i < S.splitDialogWordIdx ? 'split-first' : 'split-second');
    span.textContent = w;
    span.title = 'Click to split before this word';
    span.addEventListener('click', () => {
      const newIdx = i < S.splitDialogWordIdx ? i + 1 : i;
      if (newIdx <= 0 || newIdx >= words.length) return;
      S.setSplitDialogWordIdx(newIdx);
      renderSplitDialogContent();
    });
    splitDialogWords.appendChild(span);

    if (i === S.splitDialogWordIdx - 1) {
      const sep = document.createElement('span');
      sep.className = 'split-seam';
      sep.textContent = '|';
      splitDialogWords.appendChild(sep);
    }
  });

  const splitTime = computeSplitTime(seg, S.splitDialogWordIdx);
  splitPart1Time.textContent = `${fmtTime(seg.start)} – ${fmtTime(splitTime)}`;
  splitPart1Text.textContent = words.slice(0, S.splitDialogWordIdx).join(' ');
  splitPart2Time.textContent = `${fmtTime(splitTime)} – ${fmtTime(seg.end)}`;
  splitPart2Text.textContent = words.slice(S.splitDialogWordIdx).join(' ');
}

function computeSplitTime(seg, wordIdx) {
  const words          = seg.text.trim().split(/\s+/).filter(Boolean);
  const wordTimestamps = seg.words || [];

  if (wordTimestamps.length >= wordIdx && wordTimestamps[wordIdx]) {
    const t = wordTimestamps[wordIdx].start;
    if (t !== undefined && t > seg.start) return t;
  }
  if (wordTimestamps.length >= wordIdx && wordTimestamps[wordIdx - 1]) {
    const t = wordTimestamps[wordIdx - 1].end;
    if (t !== undefined && t > seg.start) return t;
  }
  const ratio = wordIdx / Math.max(words.length, 1);
  return seg.start + (seg.end - seg.start) * ratio;
}

function confirmSplit() {
  if (S.splitDialogIdx === null) return;
  pushUndoSnapshot();
  const seg   = S.transcriptData[S.splitDialogIdx];
  const words = seg.text.trim().split(/\s+/).filter(Boolean);

  const firstText  = words.slice(0, S.splitDialogWordIdx).join(' ');
  const secondText = words.slice(S.splitDialogWordIdx).join(' ');
  if (!firstText || !secondText) {
    toast.warn('Cannot create an empty segment. Adjust the split point.');
    return;
  }

  const splitTime      = computeSplitTime(seg, S.splitDialogWordIdx);
  const wordTimestamps = seg.words || [];

  const seg1 = {
    start:   seg.start,
    end:     splitTime,
    text:    firstText,
    speaker: seg.speaker,
    words:   wordTimestamps.slice(0, S.splitDialogWordIdx),
  };
  const seg2 = {
    start:   splitTime,
    end:     seg.end,
    text:    secondText,
    speaker: seg.speaker,
    words:   wordTimestamps.slice(S.splitDialogWordIdx),
  };

  S.transcriptData.splice(S.splitDialogIdx, 1, seg1, seg2);
  closeSplitDialog();
  renderTimeline();
  renderTranscriptList();
  selectSegment(S.splitDialogIdx);
  onStyleChange();
  scheduleAutoSave();
}

function setupSplitDialog() {
  cancelSplitSeg.addEventListener('click', closeSplitDialog);
  confirmSplitSeg.addEventListener('click', confirmSplit);
}

// ── Duplicate Segment ──────────────────────────────────────────────────────
function duplicateSelectedSegment() {
  if (S.selectedSegIdx === null || S.selectedSegIdx >= S.transcriptData.length) return;
  pushUndoSnapshot();
  const original = S.transcriptData[S.selectedSegIdx];
  const clone = JSON.parse(JSON.stringify(original));
  // Place the duplicate right after the original (same timing)
  const insertIdx = S.selectedSegIdx + 1;
  S.transcriptData.splice(insertIdx, 0, clone);
  renderTimeline();
  renderTranscriptList();
  selectSegment(insertIdx);
  onStyleChange();
  scheduleAutoSave();
}
