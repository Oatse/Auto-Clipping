/**
 * subtitleEngine.js — Subtitle rendering engine: sync loop, multi-speaker overlay, karaoke, style computation
 */

import * as S from './state.js';
import { escHtml } from './utils.js';
import { updatePlayhead, updateTimeDisplay } from './timeline.js';
import { updateFxPreview } from './effects.js';

// ── DOM Refs ───────────────────────────────────────────────────────────────
const previewVideo      = document.getElementById('previewVideo');
const subtitleOverlay   = document.getElementById('subtitleOverlay');
const subtitleContainer = document.getElementById('subtitleContainer');

// Style control refs (read-only, for collectStyle / buildWordStyle)
const fontFamilyEl    = document.getElementById('fontFamily');
const fontSizeEl      = document.getElementById('fontSize');
const fontColorEl     = document.getElementById('fontColor');
const strokeEnabledEl = document.getElementById('strokeEnabled');
const strokeColorEl   = document.getElementById('strokeColor');
const strokeWidthEl   = document.getElementById('strokeWidth');
const glowEnabledEl   = document.getElementById('glowEnabled');
const glowColorEl     = document.getElementById('glowColor');
const glowBlurEl      = document.getElementById('glowBlur');
const bgBoxEnabledEl  = document.getElementById('bgBoxEnabled');
const bgBoxColorEl    = document.getElementById('bgBoxColor');
const bgOpacityEl     = document.getElementById('bgOpacity');

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

export function buildWordStyle(style, speakerColor, speakerStrokeColor) {
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

export function buildLineStyle(style, speakerColor) {
  const color = speakerColor || style.fontColor;

  let parts = [
    `font-family: ${style.fontFamily}`,
    `font-size: ${Math.round(style.fontSize * S.fsScale)}px`,
    `color: ${color}`,
    `font-weight: 800`,
    `line-height: 1.2`,
  ];

  return parts.join('; ');
}

// ── Trigger Re-render ──────────────────────────────────────────────────────
export function onStyleChange() {
  if (subtitleContainer.children.length > 0) {
    const t = previewVideo.currentTime;
    const activeSegs = S.transcriptData
      .map((s, i) => ({ ...s, _idx: i }))
      .filter(s => t >= s.start && t <= s.end + 0.03);
    if (activeSegs.length > 0) renderActiveSubtitles(activeSegs, t, true);
  }
}

// ── Subtitle Sync Loop ─────────────────────────────────────────────────────
// setActiveSeg is injected to avoid circular dep with preview.js
let _setActiveSeg = () => {};
export function injectSetActiveSeg(fn) { _setActiveSeg = fn; }

export function startSubtitleSync() {
  if (S.subtitleTimer) cancelAnimationFrame(S.subtitleTimer);

  let lastActiveKey = '';

  function tick() {
    const t = previewVideo.currentTime;

    updatePlayhead(t);
    updateTimeDisplay(t);
    updateFxPreview(t);

    const activeSegs = S.transcriptData
      .map((s, i) => ({ ...s, _idx: i }))
      .filter(s => t >= s.start && t <= s.end + 0.03);

    const activeKey = activeSegs.map(s => s._idx).join(',');

    if (activeKey !== lastActiveKey) {
      lastActiveKey = activeKey;
      _setActiveSeg(activeSegs.length > 0 ? activeSegs[0]._idx : -1);
      renderActiveSubtitles(activeSegs, t);
    } else if (activeSegs.length > 0 && (S.currentAnim === 'karaoke' || S.currentAnim === 'narration-pop')) {
      activeSegs.forEach(seg => updateKaraokeHighlight(seg, t, seg._idx));
    }

    S.setSubtitleTimer(requestAnimationFrame(tick));
  }

  S.setSubtitleTimer(requestAnimationFrame(tick));
}

// ── Active Subtitle Rendering ──────────────────────────────────────────────
// Helper: create a new subtitle line DOM element for a segment
function _createSubtitleLine(seg, layerIdx, style, isMultiSpeaker) {
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
  lineDiv.style.cssText = buildLineStyle(style, speakerColor);

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

  return lineDiv;
}

/**
 * Render active subtitle overlays using speaker-based DOM tracking.
 *
 * Each speaker gets ONE persistent DOM element.  When the same speaker
 * moves to a new segment (very common — same-speaker consecutive segs
 * with zero-gap), the existing element's text is updated IN PLACE:
 *   - No element destruction / recreation  → no layout shift
 *   - No animation restart                → no flash / glitch
 *   - Other speakers' elements stay put   → no shifting
 *
 * A full entry animation only plays when a speaker FIRST appears on
 * screen.  Subsequent text changes for the same speaker are instant.
 *
 * The subtitle-container uses CSS flex-direction: column-reverse
 * (bottom position) so child[0] = bottom speaker, child[1] = above, etc.
 *
 * @param {Array}   activeSegs   - Currently active segments with _idx
 * @param {number}  currentTime  - Current video playback time
 * @param {boolean} forceRebuild - If true, clear+rebuild all (used by onStyleChange)
 */
export function renderActiveSubtitles(activeSegs, currentTime, forceRebuild = false) {
  subtitleOverlay.style.alignItems = '';
  subtitleOverlay.style.justifyContent = '';

  if (activeSegs.length === 0) {
    subtitleContainer.innerHTML = '';
    return;
  }

  if (forceRebuild) {
    subtitleContainer.innerHTML = '';
  }

  const style = collectStyle();
  const isMultiSpeaker = new Set(S.transcriptData.map(s => s.speaker || 'SPEAKER_00')).size > 1;

  const spIdx = (sp) => {
    const m = (sp || '').match(/\d+$/);
    return m ? parseInt(m[0]) : 0;
  };

  // ── Group by speaker: one DOM element per speaker ──────────────────
  // If a speaker has multiple overlapping segments, keep the latest one.
  const speakerSegMap = new Map();
  activeSegs.forEach(seg => {
    const sp = seg.speaker || 'SPEAKER_00';
    const prev = speakerSegMap.get(sp);
    if (!prev || seg.start > prev.start || (seg.start === prev.start && seg._idx > prev._idx)) {
      speakerSegMap.set(sp, seg);
    }
  });

  // Sort by speaker index for consistent visual stacking
  const sortedEntries = [...speakerSegMap.entries()]
    .sort((a, b) => spIdx(a[0]) - spIdx(b[0]));

  const desiredSpeakers = new Set(sortedEntries.map(([sp]) => sp));

  // ── Remove lines for speakers no longer active ─────────────────────
  if (!forceRebuild) {
    [...subtitleContainer.querySelectorAll('.subtitle-line')].forEach(el => {
      const sp = el.dataset.speaker || 'SPEAKER_00';
      if (!desiredSpeakers.has(sp)) {
        el.remove();
      }
    });
  }

  // Map existing elements by speaker
  const existingBySpeaker = new Map();
  [...subtitleContainer.querySelectorAll('.subtitle-line')].forEach(el => {
    existingBySpeaker.set(el.dataset.speaker || 'SPEAKER_00', el);
  });

  // ── Create / update one element per speaker ────────────────────────
  sortedEntries.forEach(([speaker, seg], layerIdx) => {
    const speakerColor       = isMultiSpeaker ? S.getSpeakerColor(seg.speaker) : null;
    const speakerStrokeColor = isMultiSpeaker ? S.getSpeakerStrokeColor(seg.speaker) : null;

    let lineDiv = existingBySpeaker.get(speaker);

    if (lineDiv) {
      // ── Speaker already on screen ────────────────────────────────
      const currentSegIdx = parseInt(lineDiv.dataset.segIdx);

      if (currentSegIdx !== seg._idx) {
        // Different segment for same speaker → update text in place
        lineDiv.dataset.segIdx = seg._idx;
        lineDiv.style.cssText = buildLineStyle(style, speakerColor);

        // Handle position override change
        if (seg.posOverride || seg.pos_override) {
          const px = seg.posX ?? seg.pos_x ?? 50;
          const py = seg.posY ?? seg.pos_y ?? 85;
          lineDiv.style.position = 'absolute';
          lineDiv.style.left = px + '%';
          lineDiv.style.top = py + '%';
          lineDiv.style.transform = 'translate(-50%, -50%)';
        }

        // Rebuild word spans with new text
        const words = seg.text.split(' ');
        lineDiv.innerHTML = words.map(w =>
          `<span class="sub-word" style="${buildWordStyle(style, speakerColor, speakerStrokeColor)}">${escHtml(w)}</span>`
        ).join('');

        // Skip entry animation — speaker is already visible on screen
        [...lineDiv.querySelectorAll('.sub-word')].forEach(el => {
          el.style.animation = 'none';
        });
      }

      // Update layer class if stacking position changed
      const layerClass = `speaker-layer-${layerIdx}`;
      if (!lineDiv.classList.contains(layerClass)) {
        lineDiv.className = lineDiv.className.replace(/speaker-layer-\d+/g, layerClass);
      }

      // Ensure correct DOM ordering
      const currentChild = subtitleContainer.children[layerIdx];
      if (currentChild !== lineDiv) {
        subtitleContainer.insertBefore(lineDiv, currentChild || null);
      }
    } else {
      // ── New speaker → create with full entry animation ───────────
      lineDiv = _createSubtitleLine(seg, layerIdx, style, isMultiSpeaker);

      const currentChild = subtitleContainer.children[layerIdx];
      if (currentChild) {
        subtitleContainer.insertBefore(lineDiv, currentChild);
      } else {
        subtitleContainer.appendChild(lineDiv);
      }
    }

    if ((S.currentAnim === 'karaoke' || S.currentAnim === 'narration-pop') && seg.words) {
      updateKaraokeHighlight(seg, currentTime, seg._idx);
    }
  });
}

// ── Karaoke / Narration-Pop Highlight ──────────────────────────────────────
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
