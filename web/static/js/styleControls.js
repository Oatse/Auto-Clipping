/**
 * styleControls.js — Subtitle style UI controls: sliders, color pickers, presets, animation/position selectors
 */

import * as S from './state.js';
import { onStyleChange } from './subtitleEngine.js';

// ── DOM Refs ───────────────────────────────────────────────────────────────
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
const subtitleOverlay = document.getElementById('subtitleOverlay');

// ── Setup ──────────────────────────────────────────────────────────────────
export function setupStyleControls() {
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

// ── Preset Application ─────────────────────────────────────────────────────
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
