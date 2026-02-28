/**
 * timeline.js — Timeline component: ruler, segments, playhead, split/merge, keyboard shortcuts
 */

import { fmtTime, fmtTimeShort, parseTime } from './utils.js';
import * as S from './state.js';
import { renderTranscriptList, setActiveSeg, onStyleChange, scheduleAutoSave } from './preview.js';

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

// ── Setup ──────────────────────────────────────────────────────────────────
export function setupTimeline() {
  // Add segment button
  addSegmentBtn.addEventListener('click', () => openSegmentDialog(previewVideo.currentTime));

  // Delete segment button
  deleteSegmentBtn.addEventListener('click', () => {
    if (S.selectedSegIdx !== null && S.selectedSegIdx < S.transcriptData.length) {
      if (confirm('Delete this subtitle segment?')) {
        S.transcriptData.splice(S.selectedSegIdx, 1);
        S.setSelectedSegIdx(null);
        deleteSegmentBtn.disabled = true;
        renderTimeline();
        renderTranscriptList();
        onStyleChange();
        scheduleAutoSave();
      }
    }
  });

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
      S.setDraggingSegEdge({ segIdx, edge });
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
      S.setDraggingSegBody({
        segIdx,
        offsetTime: clickTime - seg.start,
        segDuration: seg.end - seg.start,
      });
      segEl.classList.add('dragging-body');
      e.preventDefault();
      return;
    }
    // Click on empty area — seek playhead
    const trackRect = timelineTrack.getBoundingClientRect();
    const x = e.clientX - trackRect.left;
    const trackWidth = getTrackWidth();
    const t = (x / trackWidth) * S.videoDuration;
    if (t >= 0 && t <= S.videoDuration) {
      previewVideo.currentTime = t;
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
      renderTimeline();
    }
    if (S.draggingSegBody) {
      const trackRect = timelineTrack.getBoundingClientRect();
      const x = e.clientX - trackRect.left;
      const trackWidth = getTrackWidth();
      const mouseTime = (x / trackWidth) * S.videoDuration;
      const newStart = Math.max(0, Math.min(S.videoDuration - S.draggingSegBody.segDuration, mouseTime - S.draggingSegBody.offsetTime));
      const seg = S.transcriptData[S.draggingSegBody.segIdx];
      seg.start = newStart;
      seg.end = newStart + S.draggingSegBody.segDuration;
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
  });

  // Keyboard shortcuts
  document.addEventListener('keydown', (e) => {
    const previewScreen = document.getElementById('screen-preview');
    if (!previewScreen.classList.contains('active')) return;
    if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA' || e.target.isContentEditable) return;

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
        S.transcriptData.splice(S.selectedSegIdx, 1);
        S.setSelectedSegIdx(null);
        deleteSegmentBtn.disabled = true;
        renderTimeline();
        renderTranscriptList();
        onStyleChange();
        scheduleAutoSave();
      }
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

  const rowCount    = speakers.length;
  const trackHeight = rowCount * ROW_HEIGHT;

  timelineTrackArea.style.height = trackHeight + 'px';
  timelineTrack.style.height     = trackHeight + 'px';

  // Update labels column
  if (timelineLabelsCol) {
    timelineLabelsCol.innerHTML = '<div class="timeline-ruler-spacer"></div>';
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

  // Draw row separator lines
  speakers.forEach((_sp, rowIdx) => {
    if (rowIdx > 0) {
      const sep = document.createElement('div');
      sep.className = 'timeline-row-line';
      sep.style.top = (rowIdx * ROW_HEIGHT) + 'px';
      timelineTrack.appendChild(sep);
    }
  });

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

  // Draw segment blocks
  S.transcriptData.forEach((seg, idx) => {
    const sp     = seg.speaker || 'SPEAKER_00';
    const rowIdx = speakers.indexOf(sp);
    const top    = rowIdx * ROW_HEIGHT + ROW_PADDING;
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
    alert('Invalid time range.');
    return;
  }
  if (!text) {
    alert('Please enter subtitle text.');
    return;
  }

  const newSeg = { start, end, text, speaker, words: [] };

  let insertIdx = S.transcriptData.findIndex(s => s.start > start);
  if (insertIdx === -1) insertIdx = S.transcriptData.length;
  S.transcriptData.splice(insertIdx, 0, newSeg);

  closeSegmentDialog();
  renderTimeline();
  renderTranscriptList();
  selectSegment(insertIdx);
  scheduleAutoSave();
}

// Timeline-local parseTime (slightly different from the one in utils.js)
function parseTimeTL(str) {
  str = str.trim();
  const parts = str.split(':');
  if (parts.length === 2) {
    const m = parseInt(parts[0]) || 0;
    const s = parseFloat(parts[1]) || 0;
    return m * 60 + s;
  }
  return parseFloat(str) || 0;
}

// ── Merge & Split ──────────────────────────────────────────────────────────
export function mergeSegmentWithNext(idx) {
  if (idx >= S.transcriptData.length - 1) return;
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
    alert('Cannot split a segment that contains only one word.');
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
  const seg   = S.transcriptData[S.splitDialogIdx];
  const words = seg.text.trim().split(/\s+/).filter(Boolean);

  const firstText  = words.slice(0, S.splitDialogWordIdx).join(' ');
  const secondText = words.slice(S.splitDialogWordIdx).join(' ');
  if (!firstText || !secondText) {
    alert('Cannot create an empty segment. Adjust the split point.');
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
