/**
 * utils.js — Shared utility functions and screen navigation
 */

const API = '';

// ── API Helper ─────────────────────────────────────────────────────────────
export async function apiFetch(url, options = {}) {
  const res = await fetch(API + url, options);
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail || res.statusText);
  }
  return res.json();
}

// ── Formatting Helpers ─────────────────────────────────────────────────────
export function formatBytes(bytes) {
  if (bytes < 1024) return bytes + ' B';
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
  return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
}

export function timeAgo(ts) {
  const diff = Math.floor(Date.now() / 1000 - ts);
  if (diff < 60) return `${diff}s ago`;
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  return `${Math.floor(diff / 3600)}h ago`;
}

export function fmtTime(secs) {
  const m = Math.floor(secs / 60);
  const s = Math.floor(secs % 60);
  const ms = Math.round((secs % 1) * 10);
  return `${m}:${String(s).padStart(2,'0')}.${ms}`;
}

export function fmtTimeShort(secs) {
  if (!secs || isNaN(secs)) return '0:00';
  const m = Math.floor(secs / 60);
  const s = Math.floor(secs % 60);
  return `${m}:${String(s).padStart(2, '0')}`;
}

export function escHtml(text) {
  const d = document.createElement('div');
  d.textContent = text;
  return d.innerHTML;
}

// Alias used in clip finder
export { escHtml as escapeHtml };

export function parseTime(str) {
  // Accept "M:SS.d", "M:SS", or "SS.d" formats
  str = str.trim();
  const full = str.match(/^(\d+):(\d{1,2})(?:\.(\d))?$/);
  if (full) {
    return parseInt(full[1], 10) * 60 + parseInt(full[2], 10) + (full[3] ? parseInt(full[3], 10) / 10 : 0);
  }
  const secs = str.match(/^(\d+(?:\.\d)?)$/);
  if (secs) return parseFloat(secs[1]);
  return null;
}

export function formatClipDuration(seconds) {
  const total = Math.max(0, Math.floor(seconds));
  const mins = Math.floor(total / 60);
  const secs = total % 60;
  if (mins > 0) return `${mins}m ${secs}s`;
  return `${secs}s`;
}

// ── Screen Navigation ──────────────────────────────────────────────────────
let activeTab = 'subtitle'; // 'subtitle' or 'clipfinder'

export function showScreen(name) {
  document.querySelectorAll('.app-screen').forEach(s => s.classList.remove('active'));
  document.getElementById('screen-' + name).classList.add('active');

  // Hide hero section on all screens except upload
  const hero = document.querySelector('.hero');
  if (hero) hero.style.display = (name === 'upload') ? '' : 'none';

  // Prevent outer scrolling when preview screen is active
  document.body.classList.toggle('preview-active', name === 'preview');

  // Show back button on all screens except upload
  const navBackBtn = document.getElementById('navBackBtn');
  if (navBackBtn) navBackBtn.classList.toggle('hidden', name === 'upload');
}

export function switchTab(tab) {
  activeTab = tab;
  document.querySelectorAll('.nav-tab').forEach(t => t.classList.toggle('active', t.dataset.tab === tab));

  if (tab === 'clipfinder' || tab === 'shortmaker') {
    // Hide all subtitle screens, show the selected screen
    document.querySelectorAll('.app-screen').forEach(s => s.classList.remove('active'));
    document.getElementById('screen-' + tab).classList.add('active');
    const hero = document.querySelector('.hero');
    if (hero) hero.style.display = 'none';
    document.body.classList.remove('preview-active');
    const navBackBtn = document.getElementById('navBackBtn');
    if (navBackBtn) navBackBtn.classList.add('hidden');
  } else {
    // Show upload screen (default)
    showScreen('upload');
  }
}

export function setupNavTabs() {
  document.querySelectorAll('.nav-tab').forEach(btn => {
    btn.addEventListener('click', () => switchTab(btn.dataset.tab));
  });
}

// ── Toast Notifications ──────────────────────────────────────────────────
//
// Replacement for native alert().  Native alert() is modal-blocking,
// looks like a system dialog, and breaks the visual rhythm of the app.
// This toast system shows a non-blocking, dismissable banner anchored
// to the bottom-right of the viewport.
//
// Usage:
//   import { toast } from './utils.js';
//   toast('Saved');
//   toast.error('Upload failed: ' + err.message);
//   toast.warn('Cannot split a single-word segment');
//   toast.success('Render complete');
//
// The container is created lazily on first call.  Each toast lives for
// 4 s by default, with the timer paused on hover so users can read
// long messages without rushing.

let _toastContainer = null;
const _TOAST_DEFAULT_MS = 4000;

function _ensureToastContainer() {
  if (_toastContainer && document.body.contains(_toastContainer)) {
    return _toastContainer;
  }
  _toastContainer = document.createElement('div');
  _toastContainer.id = 'toastContainer';
  _toastContainer.className = 'toast-container';
  // aria-live=polite so screen readers announce new toasts without
  // interrupting the user.
  _toastContainer.setAttribute('aria-live', 'polite');
  _toastContainer.setAttribute('aria-atomic', 'false');
  document.body.appendChild(_toastContainer);
  return _toastContainer;
}

function _showToast(message, variant = 'info', durationMs = _TOAST_DEFAULT_MS) {
  if (!message) return null;
  const container = _ensureToastContainer();

  const el = document.createElement('div');
  el.className = `toast toast--${variant}`;
  el.setAttribute('role', variant === 'error' ? 'alert' : 'status');

  const iconMap = {
    info: 'ℹ',
    success: '✓',
    warn: '⚠',
    error: '✕',
  };
  el.innerHTML = `
    <span class="toast-icon" aria-hidden="true">${iconMap[variant] || iconMap.info}</span>
    <span class="toast-text"></span>
    <button class="toast-close" aria-label="Dismiss">×</button>
  `;
  // Use textContent to neutralise any HTML in user/error messages.
  el.querySelector('.toast-text').textContent = String(message);

  let timer = null;
  let remaining = durationMs;
  let openedAt = Date.now();

  const dismiss = () => {
    if (!el.isConnected) return;
    el.classList.add('toast-leave');
    // Animation duration must match polish.css .toast leave transition.
    setTimeout(() => el.remove(), 200);
    if (timer) clearTimeout(timer);
  };

  const startTimer = () => {
    openedAt = Date.now();
    timer = setTimeout(dismiss, remaining);
  };

  // Pause timer while the user is hovering — prevents fast flickers
  // from being unreadable.
  el.addEventListener('mouseenter', () => {
    if (timer) {
      clearTimeout(timer);
      remaining -= Date.now() - openedAt;
      timer = null;
    }
  });
  el.addEventListener('mouseleave', () => {
    if (!timer && remaining > 0) startTimer();
  });

  el.querySelector('.toast-close').addEventListener('click', dismiss);

  container.appendChild(el);
  // Trigger entrance transition on next frame.
  requestAnimationFrame(() => el.classList.add('toast-enter'));
  startTimer();

  return { dismiss };
}

/**
 * Show a non-blocking toast.  Default variant is 'info'.
 * Static methods .success/.error/.warn/.info are provided for clarity.
 */
export function toast(message, opts = {}) {
  const variant = opts.variant || 'info';
  const duration = opts.duration ?? _TOAST_DEFAULT_MS;
  return _showToast(message, variant, duration);
}
toast.info    = (msg, opts = {}) => _showToast(msg, 'info',    opts.duration);
toast.success = (msg, opts = {}) => _showToast(msg, 'success', opts.duration);
toast.warn    = (msg, opts = {}) => _showToast(msg, 'warn',    opts.duration);
toast.error   = (msg, opts = {}) => _showToast(msg, 'error',   opts.duration ?? 6000);
