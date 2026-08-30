/**
 * styleControls.js — Subtitle style UI controls: sliders, color pickers, presets, animation/position selectors
 */

import * as S from './state.js';
import { onStyleChange } from './subtitleEngine.js';
import { refreshColorPickers, setColorInputValue, setupColorPickers } from './colorPicker.js';

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
  setupColorPickers();

  fontSizeEl.addEventListener('input', () => {
    fontSizeVal.textContent = fontSizeEl.value;
    onStyleChange();
    S.triggerAutoSave();
  });

  strokeWidthEl.addEventListener('input', () => {
    strokeWidthVal.textContent = strokeWidthEl.value;
    onStyleChange();
    S.triggerAutoSave();
  });

  glowBlurEl.addEventListener('input', () => {
    glowBlurVal.textContent = glowBlurEl.value;
    onStyleChange();
    S.triggerAutoSave();
  });

  bgOpacityEl.addEventListener('input', () => {
    bgOpacityVal.textContent = bgOpacityEl.value;
    onStyleChange();
    S.triggerAutoSave();
  });

  fontColorEl.addEventListener('input', () => {
    onStyleChange();
    S.triggerAutoSave();
  });

  strokeColorEl.addEventListener('input', () => {
    onStyleChange();
    S.triggerAutoSave();
  });

  glowColorEl.addEventListener('input', () => {
    onStyleChange();
    S.triggerAutoSave();
  });

  bgBoxColorEl.addEventListener('input', () => {
    onStyleChange();
    S.triggerAutoSave();
  });

  fontFamilyEl.addEventListener('change', () => {
    onStyleChange();
    S.triggerAutoSave();
  });

  strokeEnabledEl.addEventListener('change', () => {
    strokeControls.classList.toggle('hidden', !strokeEnabledEl.checked);
    onStyleChange();
    S.triggerAutoSave();
  });
  glowEnabledEl.addEventListener('change', () => {
    glowControls.classList.toggle('hidden', !glowEnabledEl.checked);
    onStyleChange();
    S.triggerAutoSave();
  });
  bgBoxEnabledEl.addEventListener('change', () => {
    bgBoxControls.classList.toggle('hidden', !bgBoxEnabledEl.checked);
    onStyleChange();
    S.triggerAutoSave();
  });

  colorSwatches.querySelectorAll('.swatch').forEach(btn => {
    btn.addEventListener('click', () => {
      const color = btn.dataset.color;
      setColorInputValue(fontColorEl, color);
      colorSwatches.querySelectorAll('.swatch').forEach(s => s.classList.remove('active'));
      btn.classList.add('active');
    });
  });

  // Animation grid uses .tag.is-active in CSS (editor.css), so toggle the
  // matching class — previously this set `.active` and the lime highlight
  // never appeared even though setCurrentAnim ran.
  animGrid.querySelectorAll('.anim-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      animGrid.querySelectorAll('.anim-btn').forEach(b => b.classList.remove('is-active'));
      btn.classList.add('is-active');
      S.setCurrentAnim(btn.dataset.anim);
      onStyleChange();
      S.triggerAutoSave();
    });
  });

  // Position selector is a `.segmented` group — its CSS keys off `is-active`.
  // Same root cause as the animation grid: bumping the wrong class meant the
  // pill highlight never followed the click, even though state.currentPos
  // and the overlay class did update.
  positionGrid.querySelectorAll('.pos-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      positionGrid.querySelectorAll('.pos-btn').forEach(b => b.classList.remove('is-active'));
      btn.classList.add('is-active');
      S.setCurrentPos(btn.dataset.pos);
      subtitleOverlay.className = 'subtitle-overlay pos-' + S.currentPos;
      onStyleChange();
      S.triggerAutoSave();
    });
  });

  // Quick-preset cards: the markup uses `.preset` (not `.preset-btn`) and
  // the CSS active state is `.preset.is-active`. The previous selector
  // `.preset-btn` matched zero elements, so clicks did nothing.
  presetGrid.querySelectorAll('.preset').forEach(btn => {
    btn.addEventListener('click', () => {
      presetGrid.querySelectorAll('.preset').forEach(b => b.classList.remove('is-active'));
      btn.classList.add('is-active');
      applyPreset(btn.dataset.preset);
      S.triggerAutoSave();
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
  // Use `.is-active` to match the click handler + the CSS rule
  // `.editor .tag.is-active`. Previously this set `.active` and the
  // lime highlight didn't follow when a preset was applied.
  animGrid.querySelectorAll('.anim-btn').forEach(b => {
    b.classList.toggle('is-active', b.dataset.anim === p.anim);
  });

  S.setCurrentPos(p.pos);
  positionGrid.querySelectorAll('.pos-btn').forEach(b => {
    b.classList.toggle('is-active', b.dataset.pos === p.pos);
  });
  subtitleOverlay.className = 'subtitle-overlay pos-' + S.currentPos;

  refreshColorPickers();
  onStyleChange();
}

// ── Style Configuration Application ─────────────────────────────────────────
export function applyStyleConfig(config) {
  if (!config) return;

  if (config.fontFamily !== undefined && fontFamilyEl) fontFamilyEl.value = config.fontFamily;
  if (config.fontSize !== undefined && fontSizeEl) {
    fontSizeEl.value = config.fontSize;
    fontSizeVal.textContent = config.fontSize;
  }
  if (config.fontColor !== undefined && fontColorEl) {
    fontColorEl.value = config.fontColor;
    if (colorSwatches) {
      colorSwatches.querySelectorAll('.swatch').forEach(s => {
        s.classList.toggle('active', s.dataset.color.toLowerCase() === config.fontColor.toLowerCase());
      });
    }
  }

  if (config.strokeEnabled !== undefined && strokeEnabledEl) {
    strokeEnabledEl.checked = config.strokeEnabled;
    strokeControls.classList.toggle('hidden', !config.strokeEnabled);
  }
  if (config.strokeColor !== undefined && strokeColorEl) strokeColorEl.value = config.strokeColor;
  if (config.strokeWidth !== undefined && strokeWidthEl) {
    strokeWidthEl.value = config.strokeWidth;
    strokeWidthVal.textContent = config.strokeWidth;
  }

  if (config.glowEnabled !== undefined && glowEnabledEl) {
    glowEnabledEl.checked = config.glowEnabled;
    glowControls.classList.toggle('hidden', !config.glowEnabled);
  }
  if (config.glowColor !== undefined && glowColorEl) glowColorEl.value = config.glowColor;
  if (config.glowBlur !== undefined && glowBlurEl) {
    glowBlurEl.value = config.glowBlur;
    glowBlurVal.textContent = config.glowBlur;
  }

  if (config.bgBoxEnabled !== undefined && bgBoxEnabledEl) {
    bgBoxEnabledEl.checked = config.bgBoxEnabled;
    bgBoxControls.classList.toggle('hidden', !config.bgBoxEnabled);
  }
  if (config.bgBoxColor !== undefined && bgBoxColorEl) bgBoxColorEl.value = config.bgBoxColor;
  if (config.bgOpacity !== undefined && bgOpacityEl) {
    bgOpacityEl.value = config.bgOpacity;
    bgOpacityVal.textContent = config.bgOpacity;
  }

  if (config.anim !== undefined) {
    S.setCurrentAnim(config.anim);
    if (animGrid) {
      animGrid.querySelectorAll('.anim-btn').forEach(b => {
        b.classList.toggle('is-active', b.dataset.anim === config.anim);
      });
    }
  }

  if (config.pos !== undefined) {
    S.setCurrentPos(config.pos);
    if (positionGrid) {
      positionGrid.querySelectorAll('.pos-btn').forEach(b => {
        b.classList.toggle('is-active', b.dataset.pos === config.pos);
      });
    }
    if (subtitleOverlay) {
      subtitleOverlay.className = 'subtitle-overlay pos-' + S.currentPos;
    }
  }

  if (config.speakerStyles !== undefined) {
    S.setSpeakerStyles(JSON.parse(JSON.stringify(config.speakerStyles)));
  }

  if (presetGrid) {
    presetGrid.querySelectorAll('.preset').forEach(b => b.classList.remove('is-active'));
  }

  refreshColorPickers();
  onStyleChange();
}
