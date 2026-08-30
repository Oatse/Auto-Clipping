const RECENT_COLORS_KEY = 'clipAutomation.recentColors';
const MAX_RECENT_COLORS = 8;

let picker = null;
let activeInput = null;
let activeHsv = { h: 0, s: 0, v: 1 };
let globalListenersBound = false;

function normalizeHex(value) {
  const raw = String(value || '').trim().replace(/^#/, '');
  if (/^[0-9a-f]{3}$/i.test(raw)) {
    return `#${raw.split('').map(char => char + char).join('')}`.toLowerCase();
  }
  if (/^[0-9a-f]{6}$/i.test(raw)) return `#${raw.toLowerCase()}`;
  return null;
}

function hexToHsv(hex) {
  const safe = normalizeHex(hex) || '#ffffff';
  const r = parseInt(safe.slice(1, 3), 16) / 255;
  const g = parseInt(safe.slice(3, 5), 16) / 255;
  const b = parseInt(safe.slice(5, 7), 16) / 255;
  const max = Math.max(r, g, b);
  const min = Math.min(r, g, b);
  const delta = max - min;
  let h = 0;

  if (delta) {
    if (max === r) h = 60 * (((g - b) / delta) % 6);
    else if (max === g) h = 60 * ((b - r) / delta + 2);
    else h = 60 * ((r - g) / delta + 4);
  }

  return {
    h: h < 0 ? h + 360 : h,
    s: max === 0 ? 0 : delta / max,
    v: max,
  };
}

function hsvToHex({ h, s, v }) {
  const chroma = v * s;
  const x = chroma * (1 - Math.abs(((h / 60) % 2) - 1));
  const m = v - chroma;
  let rgb;

  if (h < 60) rgb = [chroma, x, 0];
  else if (h < 120) rgb = [x, chroma, 0];
  else if (h < 180) rgb = [0, chroma, x];
  else if (h < 240) rgb = [0, x, chroma];
  else if (h < 300) rgb = [x, 0, chroma];
  else rgb = [chroma, 0, x];

  return `#${rgb
    .map(channel => Math.round((channel + m) * 255).toString(16).padStart(2, '0'))
    .join('')}`;
}

function readRecentColors() {
  try {
    const colors = JSON.parse(localStorage.getItem(RECENT_COLORS_KEY) || '[]');
    return Array.isArray(colors) ? colors.map(normalizeHex).filter(Boolean).slice(0, MAX_RECENT_COLORS) : [];
  } catch {
    return [];
  }
}

function saveRecentColor(color) {
  const normalized = normalizeHex(color);
  if (!normalized) return;
  const colors = [normalized, ...readRecentColors().filter(item => item !== normalized)]
    .slice(0, MAX_RECENT_COLORS);
  try {
    localStorage.setItem(RECENT_COLORS_KEY, JSON.stringify(colors));
  } catch {
  }
}

function createPicker() {
  const root = document.createElement('div');
  root.className = 'modern-color-picker';
  root.setAttribute('role', 'dialog');
  root.setAttribute('aria-modal', 'false');
  root.setAttribute('aria-label', 'Choose colour');
  root.hidden = true;
  root.innerHTML = `
    <div class="modern-color-picker__header">
      <div>
        <span class="modern-color-picker__eyebrow">Colour</span>
        <strong class="modern-color-picker__title">Choose colour</strong>
      </div>
      <button type="button" class="modern-color-picker__close" aria-label="Close colour picker">
        <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M3 3l10 10M13 3L3 13"/></svg>
      </button>
    </div>
    <div class="modern-color-picker__saturation" role="slider" tabindex="0"
      aria-label="Saturation and brightness" aria-valuemin="0" aria-valuemax="100">
      <span class="modern-color-picker__cursor"></span>
    </div>
    <label class="modern-color-picker__hue-label">
      <span>Hue</span>
      <input class="modern-color-picker__hue" type="range" min="0" max="359" step="1" value="0" />
    </label>
    <div class="modern-color-picker__value-row">
      <span class="modern-color-picker__preview" aria-hidden="true"></span>
      <label class="modern-color-picker__hex-wrap">
        <span>HEX</span>
        <input class="modern-color-picker__hex" type="text" inputmode="text" maxlength="7"
          autocomplete="off" spellcheck="false" value="#ffffff" />
      </label>
      <button type="button" class="modern-color-picker__eyedropper">
        <svg viewBox="0 0 20 20" aria-hidden="true">
          <path d="M12.7 3.1l4.2 4.2M7.2 12.8l-3.5 3.5v1h1l3.5-3.5m-1-1l6.6-6.6 2 2-6.6 6.6-2-2z"/>
        </svg>
        <span>Pick from screen</span>
      </button>
    </div>
    <div class="modern-color-picker__recent-wrap">
      <span class="modern-color-picker__recent-label">Recent</span>
      <div class="modern-color-picker__recent"></div>
    </div>
  `;
  document.body.appendChild(root);

  const saturation = root.querySelector('.modern-color-picker__saturation');
  const hue = root.querySelector('.modern-color-picker__hue');
  const hex = root.querySelector('.modern-color-picker__hex');
  const eyedropper = root.querySelector('.modern-color-picker__eyedropper');

  const updateFromPointer = (event) => {
    const rect = saturation.getBoundingClientRect();
    activeHsv.s = Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width));
    activeHsv.v = 1 - Math.max(0, Math.min(1, (event.clientY - rect.top) / rect.height));
    applyColor(hsvToHex(activeHsv));
  };

  saturation.addEventListener('pointerdown', (event) => {
    saturation.setPointerCapture(event.pointerId);
    updateFromPointer(event);
  });
  saturation.addEventListener('pointermove', (event) => {
    if (saturation.hasPointerCapture(event.pointerId)) updateFromPointer(event);
  });
  saturation.addEventListener('keydown', (event) => {
    const step = event.shiftKey ? 0.1 : 0.02;
    if (event.key === 'ArrowLeft') activeHsv.s = Math.max(0, activeHsv.s - step);
    else if (event.key === 'ArrowRight') activeHsv.s = Math.min(1, activeHsv.s + step);
    else if (event.key === 'ArrowUp') activeHsv.v = Math.min(1, activeHsv.v + step);
    else if (event.key === 'ArrowDown') activeHsv.v = Math.max(0, activeHsv.v - step);
    else return;
    event.preventDefault();
    applyColor(hsvToHex(activeHsv));
  });

  hue.addEventListener('input', () => {
    activeHsv.h = Number(hue.value);
    applyColor(hsvToHex(activeHsv));
  });

  hex.addEventListener('input', () => {
    const color = normalizeHex(hex.value);
    hex.classList.toggle('is-invalid', !color);
    if (color) applyColor(color, { preserveHexFocus: true });
  });
  hex.addEventListener('blur', () => {
    hex.value = activeInput?.value || '#ffffff';
    hex.classList.remove('is-invalid');
  });

  eyedropper.addEventListener('click', async () => {
    if (!('EyeDropper' in window)) return;
    try {
      const result = await new window.EyeDropper().open();
      applyColor(result.sRGBHex);
      saveRecentColor(result.sRGBHex);
      renderRecentColors();
    } catch (error) {
      if (error?.name !== 'AbortError') console.warn('Screen colour picker failed:', error);
    }
  });

  root.querySelector('.modern-color-picker__close').addEventListener('click', closeColorPicker);
  root.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') {
      event.stopPropagation();
      const trigger = activeInput?._colorPickerTrigger;
      closeColorPicker();
      trigger?.focus();
    }
  });

  eyedropper.disabled = !('EyeDropper' in window);
  eyedropper.title = eyedropper.disabled
    ? 'Screen eyedropper is not available in this browser'
    : 'Pick a colour anywhere on your screen';

  return root;
}

function renderRecentColors() {
  if (!picker) return;
  const container = picker.querySelector('.modern-color-picker__recent');
  const colors = readRecentColors();
  container.innerHTML = '';
  picker.querySelector('.modern-color-picker__recent-wrap').classList.toggle('is-empty', colors.length === 0);

  colors.forEach(color => {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'modern-color-picker__recent-swatch';
    button.style.setProperty('--swatch-color', color);
    button.title = color;
    button.setAttribute('aria-label', `Use recent colour ${color}`);
    button.addEventListener('click', () => applyColor(color));
    container.appendChild(button);
  });
}

function syncPickerUi(color) {
  if (!picker) return;
  activeHsv = hexToHsv(color);
  picker.style.setProperty('--picker-hue', `hsl(${activeHsv.h} 100% 50%)`);
  picker.style.setProperty('--picker-color', color);
  picker.querySelector('.modern-color-picker__hue').value = String(Math.round(activeHsv.h));
  picker.querySelector('.modern-color-picker__cursor').style.left = `${activeHsv.s * 100}%`;
  picker.querySelector('.modern-color-picker__cursor').style.top = `${(1 - activeHsv.v) * 100}%`;
  picker.querySelector('.modern-color-picker__saturation')
    .setAttribute('aria-valuenow', String(Math.round(activeHsv.s * activeHsv.v * 100)));
}

function applyColor(color, { preserveHexFocus = false } = {}) {
  const normalized = normalizeHex(color);
  if (!normalized || !activeInput) return;
  activeInput.value = normalized;
  activeInput.dispatchEvent(new Event('input', { bubbles: true }));
  syncPickerUi(normalized);

  const hex = picker.querySelector('.modern-color-picker__hex');
  if (!preserveHexFocus || document.activeElement !== hex) hex.value = normalized;
  hex.classList.remove('is-invalid');
}

function positionPicker(trigger) {
  const rect = trigger.getBoundingClientRect();
  const pickerRect = picker.getBoundingClientRect();
  const gutter = 12;
  let left = rect.right - pickerRect.width;
  let top = rect.bottom + 8;

  left = Math.max(gutter, Math.min(left, window.innerWidth - pickerRect.width - gutter));
  if (top + pickerRect.height > window.innerHeight - gutter) {
    top = Math.max(gutter, rect.top - pickerRect.height - 8);
  }
  picker.style.left = `${Math.round(left)}px`;
  picker.style.top = `${Math.round(top)}px`;
}

function openColorPicker(input) {
  picker ||= createPicker();
  activeInput = input;
  picker.hidden = false;
  picker.querySelector('.modern-color-picker__title').textContent =
    input.dataset.colorLabel || input.title || 'Choose colour';
  picker.querySelector('.modern-color-picker__hex').value = input.value;
  syncPickerUi(input.value);
  renderRecentColors();
  positionPicker(input._colorPickerTrigger);
  requestAnimationFrame(() => picker.classList.add('is-open'));
}

function closeColorPicker() {
  if (!picker || picker.hidden) return;
  if (activeInput) saveRecentColor(activeInput.value);
  picker.classList.remove('is-open');
  picker.hidden = true;
  activeInput = null;
}

export function enhanceColorInput(input) {
  if (!input || input.dataset.colorPickerEnhanced === 'true') return input?._colorPickerTrigger || null;
  input.dataset.colorPickerEnhanced = 'true';
  input.classList.add('enhanced-color-input');
  input.setAttribute('aria-hidden', 'true');
  input.tabIndex = -1;

  const trigger = document.createElement('button');
  trigger.type = 'button';
  trigger.className = 'modern-color-trigger';
  if (input.closest('.speaker-color-group')) trigger.classList.add('modern-color-trigger--compact');
  trigger.innerHTML = `
    <span class="modern-color-trigger__swatch" aria-hidden="true"></span>
    <span class="modern-color-trigger__value">${input.value}</span>
  `;
  trigger.setAttribute('aria-label', input.title || input.dataset.colorLabel || 'Choose colour');
  trigger.setAttribute('aria-haspopup', 'dialog');
  input.insertAdjacentElement('afterend', trigger);
  input._colorPickerTrigger = trigger;

  const syncTrigger = () => {
    const color = normalizeHex(input.value) || '#ffffff';
    trigger.style.setProperty('--trigger-color', color);
    trigger.querySelector('.modern-color-trigger__value').textContent = color;
  };
  input._syncColorPicker = syncTrigger;
  input.addEventListener('input', syncTrigger);
  trigger.addEventListener('click', (event) => {
    event.stopPropagation();
    if (activeInput === input && picker && !picker.hidden) closeColorPicker();
    else openColorPicker(input);
  });
  syncTrigger();
  return trigger;
}

export function setupColorPickers(root = document) {
  root.querySelectorAll('input[type="color"].color-input').forEach(enhanceColorInput);
  if (globalListenersBound) return;
  globalListenersBound = true;

  document.addEventListener('pointerdown', (event) => {
    if (!picker || picker.hidden) return;
    if (picker.contains(event.target) || activeInput?._colorPickerTrigger?.contains(event.target)) return;
    closeColorPicker();
  });
  window.addEventListener('resize', closeColorPicker);
  window.addEventListener('scroll', closeColorPicker, true);
}

export function refreshColorPickers(root = document) {
  root.querySelectorAll('input[type="color"].color-input').forEach(input => {
    enhanceColorInput(input);
    input._syncColorPicker?.();
  });
}

export function setColorInputValue(input, color) {
  const normalized = normalizeHex(color);
  if (!input || !normalized) return;
  input.value = normalized;
  input.dispatchEvent(new Event('input', { bubbles: true }));
}
