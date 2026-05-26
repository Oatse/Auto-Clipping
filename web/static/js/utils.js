/**
 * utils.js — Stateless helpers shared by every frontend module.
 *
 * Centralises everything that has no place inside a feature module:
 *   - HTTP wrapper      (apiFetch)
 *   - Time formatting   (fmtTime, fmtTimeShort, timeAgo, parseTime)
 *   - Sizes / durations (formatBytes, formatClipDuration)
 *   - Markup escaping   (escHtml)
 *   - Screen routing    (showScreen, switchTab, setupNavTabs)
 *   - User feedback     (toast, confirmDialog, promptDialog)
 *
 * All functions are pure (no module-level mutable state) and safe to call
 * before DOMContentLoaded — DOM-dependent helpers create their own host
 * elements lazily on first use.
 */

// ── HTTP ─────────────────────────────────────────────────────────────
const API = '';

/**
 * Thin fetch wrapper that always returns parsed JSON and throws an Error
 * carrying the server-supplied detail message on non-2xx responses.
 */
export async function apiFetch(url, options = {}) {
  const res = await fetch(API + url, options);
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail || res.statusText);
  }
  return res.json();
}

// ── Time / size formatting ───────────────────────────────────────────
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

/** "1:23.4" — minute / second / decisecond, used by the transcript panel. */
export function fmtTime(secs) {
  const m = Math.floor(secs / 60);
  const s = Math.floor(secs % 60);
  const ms = Math.round((secs % 1) * 10);
  return `${m}:${String(s).padStart(2, '0')}.${ms}`;
}

/** "1:23" — minute / second, used by the timeline ruler. */
export function fmtTimeShort(secs) {
  const m = Math.floor(secs / 60);
  const s = Math.floor(secs % 60);
  return `${m}:${String(s).padStart(2, '0')}`;
}

/** Human-friendly clip length: "5s" / "1m 5s". */
export function formatClipDuration(seconds) {
  if (!Number.isFinite(seconds) || seconds <= 0) return '0s';
  const total = Math.round(seconds);
  if (total < 60) return `${total}s`;
  const m = Math.floor(total / 60);
  const s = total % 60;
  return s ? `${m}m ${s}s` : `${m}m`;
}

/**
 * Parse a user-typed timestamp into seconds.
 * Accepts: plain seconds ("82.5"), "1:22", "1:02:30", or "1m 22s".
 * Returns NaN if the string can't be interpreted.
 */
export function parseTime(str) {
  if (str == null) return NaN;
  const raw = String(str).trim();
  if (!raw) return NaN;

  // M-and-S text: "1m 22s" / "30s" / "2m"
  const ms = /^(?:(\d+)\s*m)?\s*(?:(\d+(?:\.\d+)?)\s*s)?$/i.exec(raw);
  if (ms && (ms[1] || ms[2])) {
    return (parseInt(ms[1] || 0, 10) * 60) + parseFloat(ms[2] || 0);
  }

  // Colon form: HH:MM:SS or MM:SS
  if (raw.includes(':')) {
    const parts = raw.split(':').map(p => p.trim());
    if (parts.some(p => p === '' || Number.isNaN(parseFloat(p)))) return NaN;
    if (parts.length === 3) {
      return (
        parseInt(parts[0], 10) * 3600
        + parseInt(parts[1], 10) * 60
        + parseFloat(parts[2])
      );
    }
    if (parts.length === 2) {
      return parseInt(parts[0], 10) * 60 + parseFloat(parts[1]);
    }
    return NaN;
  }

  const n = Number(raw);
  return Number.isFinite(n) ? n : NaN;
}

// ── HTML escaping ────────────────────────────────────────────────────
export function escHtml(text) {
  const d = document.createElement('div');
  d.textContent = text == null ? '' : String(text);
  return d.innerHTML;
}

// ── Screen routing ───────────────────────────────────────────────────
//
// The dashboard is a single-page UI with multiple ".app-screen" panes
// gated by an ".active" class.  showScreen flips which pane is visible
// and toggles a body-level "preview-active" flag for CSS hooks; the back
// button is a UI affordance that hides itself on the upload screen.

export function showScreen(name) {
  document.querySelectorAll('.app-screen').forEach(s =>
    s.classList.remove('active')
  );
  const target = document.getElementById('screen-' + name);
  if (target) {
    // Editor templates ship non-default screens with class="hidden" as
    // their initial state. The global `.hidden { display:none !important }`
    // rule (reset.css) overrides `.app-screen.active { display: block }`,
    // so adding `.active` alone leaves the screen invisible — which is
    // why "Start render" appeared to blank the page even though the job
    // kept running. Strip `hidden` on activation so `.active` wins.
    target.classList.remove('hidden');
    target.classList.add('active');
  }

  // Hide hero on every screen except the upload landing page.
  const hero = document.querySelector('.hero');
  if (hero) hero.style.display = name === 'upload' ? '' : 'none';

  // Lock outer scroll while the editor preview is active.
  document.body.classList.toggle('preview-active', name === 'preview');

  // Back button visible everywhere except the dashboard.
  const navBackBtn = document.getElementById('navBackBtn');
  if (navBackBtn) navBackBtn.classList.toggle('hidden', name === 'upload');
}

/**
 * Swap the top-level workspace tab.  Tab IDs are the data-tab attribute
 * on each ``.nav-tab`` button: "subtitle" | "clipfinder" | "shortmaker".
 */
export function switchTab(tab) {
  document.querySelectorAll('.nav-tab').forEach(btn =>
    btn.classList.toggle('active', btn.dataset.tab === tab)
  );

  if (tab === 'clipfinder' || tab === 'shortmaker') {
    document.querySelectorAll('.app-screen').forEach(s =>
      s.classList.remove('active')
    );
    const target = document.getElementById('screen-' + tab);
    if (target) target.classList.add('active');

    const hero = document.querySelector('.hero');
    if (hero) hero.style.display = 'none';
    document.body.classList.remove('preview-active');

    const navBackBtn = document.getElementById('navBackBtn');
    if (navBackBtn) navBackBtn.classList.add('hidden');
    return;
  }

  // Default: show the upload / dashboard screen.
  showScreen('upload');
}

export function setupNavTabs() {
  document.querySelectorAll('.nav-tab').forEach(btn => {
    btn.addEventListener('click', () => switchTab(btn.dataset.tab));
  });
}

// ── Toast notifications ──────────────────────────────────────────────
//
// Drop-in replacement for the legacy native ``alert()`` / ``confirm()``.
// Markup matches the rules already shipped in ``css/polish.css`` so this
// module needs no new styles.  Toasts auto-dismiss after a configurable
// timeout and can be dismissed early via the close button.

const _TOAST_DEFAULT_DURATION_MS = 4500;

function _ensureToastContainer() {
  let host = document.querySelector('.toast-container');
  if (host) return host;
  host = document.createElement('div');
  host.className = 'toast-container';
  host.setAttribute('role', 'region');
  host.setAttribute('aria-live', 'polite');
  document.body.appendChild(host);
  return host;
}

function _showToast(message, kind, duration) {
  const host = _ensureToastContainer();

  const el = document.createElement('div');
  el.className = `toast toast-${kind}`;
  el.setAttribute('role', kind === 'error' ? 'alert' : 'status');

  const icon = document.createElement('span');
  icon.className = `toast-icon toast-icon-${kind}`;
  const iconChar =
    kind === 'success' ? '\u2713' :
    kind === 'error'   ? '\u2715' :
    kind === 'warn'    ? '!' : 'i';
  icon.textContent = iconChar;

  const text = document.createElement('div');
  text.className = 'toast-text';
  text.textContent = String(message);

  const close = document.createElement('button');
  close.className = 'toast-close';
  close.type = 'button';
  close.setAttribute('aria-label', 'Dismiss notification');
  close.textContent = '\u00D7';

  el.append(icon, text, close);
  host.appendChild(el);

  // Force a reflow before adding the enter class so the transition runs.
  // eslint-disable-next-line no-unused-expressions
  el.offsetWidth;
  el.classList.add('toast-enter');

  let dismissed = false;
  const dismiss = () => {
    if (dismissed) return;
    dismissed = true;
    el.classList.remove('toast-enter');
    el.classList.add('toast-leave');
    el.addEventListener('transitionend', () => el.remove(), { once: true });
    // Safety net in case transitionend never fires (display:none, etc).
    setTimeout(() => el.remove(), 600);
  };

  close.addEventListener('click', dismiss);
  setTimeout(dismiss, Math.max(1200, Number(duration) || _TOAST_DEFAULT_DURATION_MS));
  return dismiss;
}

export const toast = {
  info:    (msg, ms) => _showToast(msg, 'info', ms),
  success: (msg, ms) => _showToast(msg, 'success', ms),
  warn:    (msg, ms) => _showToast(msg, 'warn', ms),
  error:   (msg, ms) => _showToast(msg, 'error', ms),
};

// ── Confirm / prompt dialogs ────────────────────────────────────────
//
// Promise-based replacements for window.confirm / window.prompt.  The
// modal markup is built on demand so the helper has no DOM dependency
// at module load.  Dialogs trap focus on the primary button and resolve
// when the user clicks Confirm/Cancel, presses Enter/Escape, or clicks
// the backdrop.

function _buildDialog({ title, message, confirmText, cancelText, danger, isPrompt, defaultValue }) {
  const overlay = document.createElement('div');
  overlay.className = 'modal-overlay confirm-dialog-overlay';
  overlay.setAttribute('role', 'dialog');
  overlay.setAttribute('aria-modal', 'true');

  const modal = document.createElement('div');
  modal.className = 'modal confirm-dialog';

  if (title) {
    const h = document.createElement('div');
    h.className = 'modal-title confirm-dialog-title';
    h.textContent = title;
    modal.appendChild(h);
  }

  const body = document.createElement('div');
  body.className = 'modal-body confirm-dialog-body';
  body.textContent = message || '';
  modal.appendChild(body);

  let input = null;
  if (isPrompt) {
    input = document.createElement('input');
    input.type = 'text';
    input.className = 'confirm-dialog-input';
    input.value = defaultValue == null ? '' : String(defaultValue);
    modal.appendChild(input);
  }

  const actions = document.createElement('div');
  actions.className = 'confirm-dialog-actions';

  const cancelBtn = document.createElement('button');
  cancelBtn.type = 'button';
  cancelBtn.className = 'btn-secondary';
  cancelBtn.textContent = cancelText || 'Cancel';

  const confirmBtn = document.createElement('button');
  confirmBtn.type = 'button';
  confirmBtn.className = danger ? 'btn-danger' : 'btn-success';
  confirmBtn.textContent = confirmText || 'OK';

  actions.append(cancelBtn, confirmBtn);
  modal.appendChild(actions);
  overlay.appendChild(modal);

  return { overlay, modal, cancelBtn, confirmBtn, input };
}

function _showDialog(opts) {
  return new Promise(resolve => {
    const { overlay, cancelBtn, confirmBtn, input } = _buildDialog(opts);
    document.body.appendChild(overlay);

    const cleanup = (value) => {
      document.removeEventListener('keydown', onKey);
      overlay.remove();
      resolve(value);
    };

    const onKey = (e) => {
      if (e.key === 'Escape') {
        e.preventDefault();
        cleanup(opts.isPrompt ? null : false);
      } else if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        cleanup(opts.isPrompt ? (input ? input.value : '') : true);
      }
    };

    cancelBtn.addEventListener('click', () => cleanup(opts.isPrompt ? null : false));
    confirmBtn.addEventListener('click', () =>
      cleanup(opts.isPrompt ? (input ? input.value : '') : true)
    );
    overlay.addEventListener('click', (e) => {
      if (e.target === overlay) cleanup(opts.isPrompt ? null : false);
    });
    document.addEventListener('keydown', onKey);

    // Focus management: input first when prompting, else the primary action.
    requestAnimationFrame(() => {
      if (input) {
        input.focus();
        input.select();
      } else {
        confirmBtn.focus();
      }
    });
  });
}

export function confirmDialog(message, options = {}) {
  return _showDialog({
    message,
    title: options.title || 'Confirm',
    confirmText: options.confirmText,
    cancelText: options.cancelText,
    danger: !!options.danger,
    isPrompt: false,
  });
}

export function promptDialog(message, options = {}) {
  return _showDialog({
    message,
    title: options.title || 'Input required',
    confirmText: options.confirmText,
    cancelText: options.cancelText,
    danger: !!options.danger,
    isPrompt: true,
    defaultValue: options.defaultValue,
  });
}
