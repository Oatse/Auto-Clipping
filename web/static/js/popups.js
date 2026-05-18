/**
 * popups.js — Inline popup components: Time Editor and Speaker Picker
 */

import * as S from './state.js';
import { pushUndoSnapshot } from './state.js';
import { fmtTime, parseTime, promptDialog } from './utils.js';
import { renderTimeline } from './timeline.js';
import { onStyleChange } from './subtitleEngine.js';

// ── Callbacks (injected to avoid circular deps) ────────────────────────────
let _renderTranscriptList = () => {};
let _buildSpeakerStylePanel = () => {};
let _scheduleAutoSave = () => {};

export function injectPopupCallbacks({ renderTranscriptList, buildSpeakerStylePanel, scheduleAutoSave }) {
  _renderTranscriptList = renderTranscriptList;
  _buildSpeakerStylePanel = buildSpeakerStylePanel;
  _scheduleAutoSave = scheduleAutoSave;
}

// ── Time Editor ────────────────────────────────────────────────────────────
let activeTimeEditor = null;

export function closeTimeEditor() {
  if (activeTimeEditor) { activeTimeEditor.remove(); activeTimeEditor = null; }
}

export function openTimeEditor(anchorEl, segIdx) {
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
    pushUndoSnapshot();
    S.transcriptData[segIdx].start = parseFloat(newStart.toFixed(1));
    S.transcriptData[segIdx].end   = parseFloat(newEnd.toFixed(1));
    closeTimeEditor();
    _renderTranscriptList();
    renderTimeline();
    onStyleChange();
    _scheduleAutoSave();
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

export function closeSpeakerPicker() {
  if (activeSpeakerPicker) {
    activeSpeakerPicker.remove();
    activeSpeakerPicker = null;
  }
}

export function openSpeakerPicker(anchorEl, segIdx) {
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
    btn.addEventListener('click', async (e) => {
      e.stopPropagation();
      const sp = btn.dataset.speaker;
      if (sp === '__new__') {
        const input = await promptDialog({
          title: 'New speaker',
          message: 'Enter a speaker ID (auto-uppercased, spaces become underscores).',
          defaultValue: `SPEAKER_0${seen.length}`,
          placeholder: 'e.g. SPEAKER_02',
          confirmText: 'Add speaker',
        });
        if (input === null || !input.trim()) { closeSpeakerPicker(); return; }
        const newSp = input.trim().toUpperCase().replace(/\s+/g, '_');
        if (!S.speakerStyles[newSp]) {
          const nIdx = parseInt((newSp.match(/\d+$/) || ['0'])[0], 10);
          S.speakerStyles[newSp] = { color: S.SPEAKER_COLORS[nIdx % S.SPEAKER_COLORS.length] };
        }
        pushUndoSnapshot();
        S.transcriptData[segIdx].speaker = newSp;
      } else {
        pushUndoSnapshot();
        S.transcriptData[segIdx].speaker = sp;
      }
      closeSpeakerPicker();
      _renderTranscriptList();
      _buildSpeakerStylePanel();
      onStyleChange();
      _scheduleAutoSave();
    });
  });

  setTimeout(() => {
    document.addEventListener('click', closeSpeakerPicker, { once: true });
  }, 0);
}
