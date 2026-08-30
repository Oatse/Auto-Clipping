import * as S from './state.js';

let activePreviewIndex = null;
let refreshPreview = () => {};

export function injectSegmentEffectPreview(fn) {
  refreshPreview = fn;
}

export function setActiveSegmentEffectIndex(idx) {
  activePreviewIndex = idx >= 0 ? idx : null;
  refreshSegmentEffectPanel();
}

export function getSegmentEffect(segment) {
  const effect = segment?.effect;
  if (!effect || !['wave', 'shake'].includes(effect.type)) return null;
  if (effect.type === 'wave' && !['horizontal', 'vertical'].includes(effect.axis)) return null;
  if (!['soft', 'medium', 'expert'].includes(effect.strength)) return null;
  return effect;
}

export function getEffectForSegmentIndex(idx) {
  return idx === null || idx === undefined ? null : getSegmentEffect(S.transcriptData[idx]);
}

export function effectOffset(effect, localTime, duration) {
  const normalized = getSegmentEffect({ effect });
  if (!normalized || localTime < 0 || localTime > duration || duration <= 0) return [0, 0];
  const presets = {
    'wave-soft': [0.0025, 2.0],
    'wave-medium': [0.0045, 2.5],
    'wave-expert': [0.0075, 3.2],
    'shake-soft': [0.0015, 18.0],
    'shake-medium': [0.0035, 26.0],
    'shake-expert': [0.0065, 36.0],
  };
  const [amplitude, frequency] = presets[`${normalized.type}-${normalized.strength}`];
  if (normalized.type === 'wave') {
    const offset = amplitude * Math.sin(Math.PI * 2 * frequency * localTime);
    return normalized.axis === 'horizontal' ? [offset, 0] : [0, offset];
  }
  return [
    amplitude * Math.sin(Math.PI * 2 * frequency * localTime),
    amplitude * Math.cos(Math.PI * 2 * frequency * 0.83 * localTime),
  ];
}

function targetIndex() {
  return S.selectedSegIdx ?? activePreviewIndex;
}

function setEffect(effect) {
  const idx = targetIndex();
  if (idx === null || !S.transcriptData[idx]) return;
  if (effect === null) {
    delete S.transcriptData[idx].effect;
  } else {
    S.transcriptData[idx].effect = effect;
  }
  refreshPreview();
  refreshSegmentEffectPanel();
  S.triggerAutoSave();
}

function selectPreset(preset) {
  const current = getEffectForSegmentIndex(targetIndex());
  const strength = current?.strength || document.getElementById('segmentEffectStrength')?.value || 'medium';
  if (preset === 'none') return setEffect(null);
  if (preset === 'shake') return setEffect({ type: 'shake', strength });
  return setEffect({ type: 'wave', axis: preset === 'wave-vertical' ? 'vertical' : 'horizontal', strength });
}

function refreshSegmentEffectPanel() {
  const panel = document.getElementById('segmentEffectControls');
  if (!panel) return;
  const idx = targetIndex();
  const effect = getEffectForSegmentIndex(idx);
  const disabled = idx === null || !S.transcriptData[idx];
  panel.classList.toggle('is-disabled', disabled);
  panel.querySelectorAll('[data-segment-effect]').forEach((button) => {
    const preset = button.dataset.segmentEffect;
    const active = !disabled && (
      (preset === 'none' && !effect) ||
      (preset === 'shake' && effect?.type === 'shake') ||
      (preset === 'wave-horizontal' && effect?.type === 'wave' && effect.axis === 'horizontal') ||
      (preset === 'wave-vertical' && effect?.type === 'wave' && effect.axis === 'vertical')
    );
    button.classList.toggle('is-active', active);
  });
  const strength = document.getElementById('segmentEffectStrength');
  if (strength) {
    strength.disabled = disabled;
    strength.value = effect?.strength || 'medium';
  }
  const label = document.getElementById('segmentEffectTarget');
  if (label) label.textContent = disabled ? 'Select a subtitle segment' : `Segment ${idx + 1}`;
}

export function setupSegmentEffects() {
  const panel = document.getElementById('segmentEffectControls');
  if (!panel) return;
  panel.querySelectorAll('[data-segment-effect]').forEach((button) => {
    button.addEventListener('click', () => selectPreset(button.dataset.segmentEffect));
  });
  const strength = document.getElementById('segmentEffectStrength');
  strength?.addEventListener('change', () => {
    const current = getEffectForSegmentIndex(targetIndex());
    if (!current) return;
    setEffect({ ...current, strength: strength.value });
  });
  refreshSegmentEffectPanel();
}
