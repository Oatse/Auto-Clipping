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

  if (tab === 'clipfinder') {
    // Hide all subtitle screens, show clip finder
    document.querySelectorAll('.app-screen').forEach(s => s.classList.remove('active'));
    document.getElementById('screen-clipfinder').classList.add('active');
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
