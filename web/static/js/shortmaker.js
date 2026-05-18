/**
 * shortmaker.js — Short Maker module
 *
 * Converts landscape videos into YouTube Shorts (9:16 / 1080×1920)
 * with a 2-grid vertical layout:
 *   Top: center-cropped gameplay / scene
 *   Bottom: zoomed VTuber / face cam area
 */

import { apiFetch, formatBytes, fmtTimeShort, toast } from './utils.js';

// ── State ──────────────────────────────────────────────────────────────────

let smJobId = null;
let smVideoInfo = null;       // { width, height, duration, fps }
let smTopCrop = null;          // { x, y, w, h }
let smBottomCrop = null;       // { x, y, w, h }
let smDragging = null;         // { region: 'top'|'bottom', type: 'move'|'resize', ... }
let smPollTimer = null;

// ── DOM refs ───────────────────────────────────────────────────────────────

const $ = id => document.getElementById(id);

// ── Setup ──────────────────────────────────────────────────────────────────

export function setupShortMaker() {
  // Upload
  const dropZone = $('smDropZone');
  const fileInput = $('smFileInput');
  const browseLink = $('smBrowseLink');

  if (!dropZone) return; // guard if HTML not yet added

  browseLink?.addEventListener('click', e => { e.preventDefault(); fileInput?.click(); });
  fileInput?.addEventListener('change', () => {
    if (fileInput.files.length) handleSmUpload(fileInput.files[0]);
  });

  dropZone.addEventListener('dragover', e => { e.preventDefault(); dropZone.classList.add('sm-dragover'); });
  dropZone.addEventListener('dragleave', () => dropZone.classList.remove('sm-dragover'));
  dropZone.addEventListener('drop', e => {
    e.preventDefault();
    dropZone.classList.remove('sm-dragover');
    if (e.dataTransfer.files.length) handleSmUpload(e.dataTransfer.files[0]);
  });

  // Process button
  $('smGenerateBtn')?.addEventListener('click', startSmProcess);

  // Download button
  $('smDownloadBtn')?.addEventListener('click', () => {
    if (smJobId) window.open(`/api/short-maker/${smJobId}/download`, '_blank');
  });

  // New button (reset)
  $('smNewBtn')?.addEventListener('click', resetShortMaker);

  // Canvas interaction (crop region dragging)
  const canvas = $('smCanvas');
  if (canvas) {
    canvas.addEventListener('mousedown', onCanvasMouseDown);
    canvas.addEventListener('mousemove', onCanvasMouseMove);
    canvas.addEventListener('mouseup', onCanvasMouseUp);
    canvas.addEventListener('mouseleave', onCanvasMouseUp);
  }
}


// ── Upload ─────────────────────────────────────────────────────────────────

async function handleSmUpload(file) {
  const allowed = ['.mp4', '.mov', '.mkv', '.avi'];
  const ext = '.' + file.name.split('.').pop().toLowerCase();
  if (!allowed.includes(ext)) {
    toast.warn('Only video files are accepted (.mp4, .mov, .mkv, .avi)');
    return;
  }

  // Show loading state
  showSmPhase('uploading');

  const form = new FormData();
  form.append('video', file);

  try {
    const res = await fetch('/api/short-maker/upload', { method: 'POST', body: form });
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }));
      throw new Error(err.detail || 'Upload failed');
    }
    const data = await res.json();
    smJobId = data.job_id;

    $('smFileName').textContent = file.name;
    $('smFileSize').textContent = formatBytes(file.size);

    // Fetch video info + default crops
    await loadVideoInfo();
  } catch (err) {
    toast.error('Upload error: ' + err.message);
    showSmPhase('upload');
  }
}

async function loadVideoInfo() {
  showSmPhase('loading-info');

  try {
    const info = await apiFetch(`/api/short-maker/${smJobId}/video-info`);
    smVideoInfo = { width: info.width, height: info.height, duration: info.duration, fps: info.fps };
    smTopCrop = info.default_top_crop;
    smBottomCrop = info.default_bottom_crop;

    // Update info display
    $('smVideoRes').textContent = `${info.width}×${info.height}`;
    $('smVideoDur').textContent = formatDuration(info.duration);
    $('smVideoFps').textContent = `${info.fps.toFixed(1)} fps`;

    // Load video into preview
    const videoEl = $('smVideoPreview');
    if (videoEl) {
      videoEl.src = `/api/short-maker/${smJobId}/video`;
      videoEl.load();
      videoEl.addEventListener('loadeddata', () => {
        drawCropOverlay();
      }, { once: true });
    }

    // Show editor phase
    showSmPhase('editor');
    drawCropOverlay();
    drawShortPreview();
  } catch (err) {
    toast.error('Error loading video info: ' + err.message);
    showSmPhase('upload');
  }
}


// ── Canvas Crop Overlay ────────────────────────────────────────────────────

function drawCropOverlay() {
  const canvas = $('smCanvas');
  const videoEl = $('smVideoPreview');
  if (!canvas || !videoEl || !smVideoInfo) return;

  const rect = videoEl.getBoundingClientRect();
  canvas.width = rect.width;
  canvas.height = rect.height;
  canvas.style.width = rect.width + 'px';
  canvas.style.height = rect.height + 'px';

  const ctx = canvas.getContext('2d');
  ctx.clearRect(0, 0, canvas.width, canvas.height);

  const scaleX = rect.width / smVideoInfo.width;
  const scaleY = rect.height / smVideoInfo.height;

  // Dim entire video slightly
  ctx.fillStyle = 'rgba(0, 0, 0, 0.35)';
  ctx.fillRect(0, 0, canvas.width, canvas.height);

  // Draw top crop region
  if (smTopCrop) {
    const x = smTopCrop.x * scaleX;
    const y = smTopCrop.y * scaleY;
    const w = smTopCrop.w * scaleX;
    const h = smTopCrop.h * scaleY;

    // Clear the crop area (show video through)
    ctx.clearRect(x, y, w, h);

    // Border
    ctx.strokeStyle = '#6366f1';
    ctx.lineWidth = 2;
    ctx.setLineDash([6, 3]);
    ctx.strokeRect(x, y, w, h);
    ctx.setLineDash([]);

    // Label
    ctx.fillStyle = '#6366f1';
    ctx.fillRect(x, y - 22, 80, 22);
    ctx.fillStyle = '#fff';
    ctx.font = 'bold 11px Inter, sans-serif';
    ctx.fillText('⬆ TOP GRID', x + 6, y - 7);

    // Resize handle
    drawResizeHandle(ctx, x + w - 8, y + h - 8, '#6366f1');
  }

  // Draw bottom crop region
  if (smBottomCrop) {
    const x = smBottomCrop.x * scaleX;
    const y = smBottomCrop.y * scaleY;
    const w = smBottomCrop.w * scaleX;
    const h = smBottomCrop.h * scaleY;

    ctx.clearRect(x, y, w, h);

    ctx.strokeStyle = '#10b981';
    ctx.lineWidth = 2;
    ctx.setLineDash([6, 3]);
    ctx.strokeRect(x, y, w, h);
    ctx.setLineDash([]);

    // Label
    ctx.fillStyle = '#10b981';
    ctx.fillRect(x, y - 22, 108, 22);
    ctx.fillStyle = '#fff';
    ctx.font = 'bold 11px Inter, sans-serif';
    ctx.fillText('⬇ BOTTOM GRID', x + 6, y - 7);

    drawResizeHandle(ctx, x + w - 8, y + h - 8, '#10b981');
  }
}

function drawResizeHandle(ctx, x, y, color) {
  ctx.fillStyle = color;
  ctx.fillRect(x, y, 10, 10);
  ctx.fillStyle = '#fff';
  ctx.fillRect(x + 2, y + 2, 6, 6);
}


// ── Short Preview (Phone Mockup) ───────────────────────────────────────────

function drawShortPreview() {
  const canvas = $('smPreviewCanvas');
  const videoEl = $('smVideoPreview');
  if (!canvas || !videoEl || !smVideoInfo || !smTopCrop || !smBottomCrop) return;

  const previewW = 180;
  const previewH = 320;
  canvas.width = previewW;
  canvas.height = previewH;

  const ctx = canvas.getContext('2d');
  ctx.fillStyle = '#000';
  ctx.fillRect(0, 0, previewW, previewH);

  const gridH = previewH / 2;

  // Check if video has frames to draw
  if (videoEl.readyState >= 2) {
    // Top grid
    try {
      ctx.drawImage(
        videoEl,
        smTopCrop.x, smTopCrop.y, smTopCrop.w, smTopCrop.h,
        0, 0, previewW, gridH
      );
    } catch (e) { /* ignore */ }

    // Bottom grid
    try {
      ctx.drawImage(
        videoEl,
        smBottomCrop.x, smBottomCrop.y, smBottomCrop.w, smBottomCrop.h,
        0, gridH, previewW, gridH
      );
    } catch (e) { /* ignore */ }
  }

  // Label overlays
  ctx.fillStyle = 'rgba(99, 102, 241, 0.6)';
  ctx.fillRect(0, gridH - 18, previewW, 18);
  ctx.fillStyle = '#fff';
  ctx.font = 'bold 10px Inter, sans-serif';
  ctx.textAlign = 'center';
  ctx.fillText('GAMEPLAY', previewW / 2, gridH - 5);

  ctx.fillStyle = 'rgba(16, 185, 129, 0.6)';
  ctx.fillRect(0, previewH - 18, previewW, 18);
  ctx.fillStyle = '#fff';
  ctx.fillText('VTUBER / CAM', previewW / 2, previewH - 5);
  ctx.textAlign = 'start';

  // Divider line
  ctx.strokeStyle = 'rgba(255,255,255,0.4)';
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(0, gridH);
  ctx.lineTo(previewW, gridH);
  ctx.stroke();
}


// ── Canvas Mouse Interaction (Drag & Resize) ───────────────────────────────

function onCanvasMouseDown(e) {
  const canvas = $('smCanvas');
  if (!canvas || !smVideoInfo) return;

  const rect = canvas.getBoundingClientRect();
  const mx = e.clientX - rect.left;
  const my = e.clientY - rect.top;
  const scaleX = rect.width / smVideoInfo.width;
  const scaleY = rect.height / smVideoInfo.height;

  // Check resize handles first (bottom-right corner)
  for (const [region, crop] of [['bottom', smBottomCrop], ['top', smTopCrop]]) {
    if (!crop) continue;
    const hx = (crop.x + crop.w) * scaleX - 8;
    const hy = (crop.y + crop.h) * scaleY - 8;
    if (mx >= hx && mx <= hx + 14 && my >= hy && my <= hy + 14) {
      smDragging = { region, type: 'resize', startMx: mx, startMy: my, origCrop: { ...crop } };
      canvas.style.cursor = 'nwse-resize';
      return;
    }
  }

  // Check move (inside crop area)
  for (const [region, crop] of [['bottom', smBottomCrop], ['top', smTopCrop]]) {
    if (!crop) continue;
    const cx = crop.x * scaleX;
    const cy = crop.y * scaleY;
    const cw = crop.w * scaleX;
    const ch = crop.h * scaleY;
    if (mx >= cx && mx <= cx + cw && my >= cy && my <= cy + ch) {
      smDragging = { region, type: 'move', startMx: mx, startMy: my, origCrop: { ...crop } };
      canvas.style.cursor = 'grabbing';
      return;
    }
  }
}

function onCanvasMouseMove(e) {
  const canvas = $('smCanvas');
  if (!canvas || !smVideoInfo) return;

  const rect = canvas.getBoundingClientRect();
  const mx = e.clientX - rect.left;
  const my = e.clientY - rect.top;
  const scaleX = rect.width / smVideoInfo.width;
  const scaleY = rect.height / smVideoInfo.height;

  if (!smDragging) {
    // Update cursor based on hover
    let cursor = 'default';
    for (const [, crop] of [['bottom', smBottomCrop], ['top', smTopCrop]]) {
      if (!crop) continue;
      const hx = (crop.x + crop.w) * scaleX - 8;
      const hy = (crop.y + crop.h) * scaleY - 8;
      if (mx >= hx && mx <= hx + 14 && my >= hy && my <= hy + 14) { cursor = 'nwse-resize'; break; }
      const cx = crop.x * scaleX;
      const cy = crop.y * scaleY;
      if (mx >= cx && mx <= cx + crop.w * scaleX && my >= cy && my <= cy + crop.h * scaleY) { cursor = 'grab'; break; }
    }
    canvas.style.cursor = cursor;
    return;
  }

  const dx = (mx - smDragging.startMx) / scaleX;
  const dy = (my - smDragging.startMy) / scaleY;
  const crop = smDragging.region === 'top' ? smTopCrop : smBottomCrop;
  const orig = smDragging.origCrop;

  if (smDragging.type === 'move') {
    let newX = Math.round(orig.x + dx);
    let newY = Math.round(orig.y + dy);
    newX = Math.max(0, Math.min(newX, smVideoInfo.width - crop.w));
    newY = Math.max(0, Math.min(newY, smVideoInfo.height - crop.h));
    crop.x = newX;
    crop.y = newY;
  } else if (smDragging.type === 'resize') {
    let newW = Math.round(orig.w + dx);
    let newH = Math.round(orig.h + dy);
    newW = Math.max(50, Math.min(newW, smVideoInfo.width - crop.x));
    newH = Math.max(50, Math.min(newH, smVideoInfo.height - crop.y));
    crop.w = newW;
    crop.h = newH;
  }

  drawCropOverlay();
  drawShortPreview();
}

function onCanvasMouseUp() {
  if (smDragging) {
    smDragging = null;
    const canvas = $('smCanvas');
    if (canvas) canvas.style.cursor = 'default';
  }
}


// ── Process (Generate Short) ───────────────────────────────────────────────

async function startSmProcess() {
  if (!smJobId || !smTopCrop || !smBottomCrop) return;

  showSmPhase('processing');

  try {
    await fetch(`/api/short-maker/${smJobId}/process`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        top_crop: smTopCrop,
        bottom_crop: smBottomCrop,
        padding: 0,
      }),
    });

    // Start polling
    smPollTimer = setInterval(pollSmStatus, 1500);
  } catch (err) {
    toast.error('Error starting process: ' + err.message);
    showSmPhase('editor');
  }
}

async function pollSmStatus() {
  if (!smJobId) return;

  try {
    const data = await apiFetch(`/api/short-maker/${smJobId}/status`);

    $('smProcessStatus').textContent = data.status === 'processing'
      ? 'Processing video with FFmpeg...'
      : data.status === 'completed'
        ? 'Short video created!'
        : data.error || 'Processing...';

    const pct = data.progress || 0;
    const bar = $('smProcessBar');
    if (bar) bar.style.width = pct + '%';

    if (data.status === 'completed') {
      clearInterval(smPollTimer);
      smPollTimer = null;
      showSmPhase('done');
    } else if (data.status === 'failed') {
      clearInterval(smPollTimer);
      smPollTimer = null;
      toast.error('Processing failed: ' + (data.error || 'Unknown error'));
      showSmPhase('editor');
    }
  } catch (err) {
    // Ignore transient errors
  }
}


// ── Phase Switching ────────────────────────────────────────────────────────

function showSmPhase(phase) {
  const phases = ['upload', 'uploading', 'loading-info', 'editor', 'processing', 'done'];
  phases.forEach(p => {
    const el = $('smPhase-' + p);
    if (el) el.style.display = (p === phase) ? '' : 'none';
  });
}


// ── Reset ──────────────────────────────────────────────────────────────────

function resetShortMaker() {
  smJobId = null;
  smVideoInfo = null;
  smTopCrop = null;
  smBottomCrop = null;
  smDragging = null;
  if (smPollTimer) clearInterval(smPollTimer);
  smPollTimer = null;

  const fileInput = $('smFileInput');
  if (fileInput) fileInput.value = '';

  showSmPhase('upload');
}


// ── Helpers ────────────────────────────────────────────────────────────────

// formatBytes and fmtTimeShort (as formatDuration) imported from utils.js
const formatDuration = fmtTimeShort;
