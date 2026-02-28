/**
 * app.js — Frontend application logic for CLIP-AUTOMATION Web UI
 * Flow: Upload → Transcribe (voice-to-text) → Preview & Design → Render
 */

const API = '';

// ── App State ──────────────────────────────────────────────────────────────
let selectedFile   = null;
let activeJobId    = null;
let sseSource      = null;
let pollInterval   = null;
let transcriptData = [];   // Array of {start, end, text, words?}
let originalTranscriptData = null;  // ElevenLabs original (before Gemini), null if unavailable
let showingOriginal = false;        // Toggle state for original transcript view
let subtitleTimer  = null; // requestAnimationFrame handle
let currentStyle   = {};   // Live subtitle style state
let currentAnim    = 'word-pop';
let currentPos     = 'bottom';
let editMode       = false;

// Timeline state
let timelineZoom     = 1.0;
let videoDuration    = 0;
let selectedSegIdx   = null;
let draggingPlayhead = false;
let draggingSegEdge  = null; // {segIdx, edge: 'start'|'end'}
let draggingSegBody  = null; // {segIdx, offsetTime, segDuration} for whole-segment drag
let subtitleDragState = null; // {segIdx, startX, startY, origPosX, origPosY}

// Merge / split state
let splitDialogIdx     = null;  // which segment is being split
let splitDialogWordIdx = 1;     // words[0..wordIdx-1] → part 1, rest → part 2

// Per-speaker custom styles: { "SPEAKER_00": { color: "#ffffff" }, ... }
let speakerStyles  = {};

// Auto-save state
let autoSaveTimer  = null;
let isSaving       = false;
const AUTOSAVE_DELAY = 2000; // 2 seconds debounce

// Speaker color palette — index maps to SPEAKER_00, SPEAKER_01, etc.
const SPEAKER_COLORS = [
  '#ffffff',  // SPEAKER_00 — white (default)
  '#FFE600',  // SPEAKER_01 — yellow
  '#00F5FF',  // SPEAKER_02 — cyan
  '#FF85C2',  // SPEAKER_03 — pink
  '#7FFF00',  // SPEAKER_04 — lime
  '#FF8C00',  // SPEAKER_05 — orange
];

// ── Save / Auto-Save ───────────────────────────────────────────────────────

function scheduleAutoSave() {
  if (autoSaveTimer) clearTimeout(autoSaveTimer);
  showAutoSaveIndicator('pending');
  autoSaveTimer = setTimeout(() => saveTranscript(true), AUTOSAVE_DELAY);
}

async function saveTranscript(isAutoSave = false) {
  if (!activeJobId || !transcriptData.length || isSaving) return;
  isSaving = true;
  showAutoSaveIndicator('saving');
  try {
    await apiFetch(`/api/jobs/${activeJobId}/transcript`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ segments: transcriptData }),
    });
    showAutoSaveIndicator('saved');
  } catch (err) {
    console.error('Save failed:', err);
    showAutoSaveIndicator('error');
  } finally {
    isSaving = false;
  }
}

function showAutoSaveIndicator(state) {
  const el = document.getElementById('autosaveIndicator');
  if (!el) return;
  el.className = 'autosave-indicator';
  switch (state) {
    case 'pending':  el.textContent = ''; break;
    case 'saving':   el.textContent = 'Saving…'; el.classList.add('saving'); break;
    case 'saved':    el.textContent = '✓ Saved'; el.classList.add('saved'); break;
    case 'error':    el.textContent = '✗ Save failed'; el.classList.add('error'); break;
  }
}

function getSpeakerColor(speakerId) {
  if (!speakerId) return speakerStyles['SPEAKER_00']?.color || SPEAKER_COLORS[0];
  // Check custom override first
  if (speakerStyles[speakerId]?.color) return speakerStyles[speakerId].color;
  const idx = parseInt((speakerId.match(/\d+$/) || ['0'])[0], 10);
  return SPEAKER_COLORS[idx % SPEAKER_COLORS.length];
}

function getSpeakerStrokeColor(speakerId) {
  if (!speakerId) return speakerStyles['SPEAKER_00']?.strokeColor || null;
  return speakerStyles[speakerId]?.strokeColor || null;
}

// Preset definitions
const PRESETS = {
  'vtuber-pop': {
    fontFamily: "'Bangers', cursive",
    fontSize: 54,
    fontColor: '#ffffff',
    strokeEnabled: true,
    strokeColor: '#ff006e',
    strokeWidth: 4,
    glowEnabled: true,
    glowColor: '#ff006e',
    glowBlur: 14,
    bgBoxEnabled: false,
    anim: 'word-pop',
    pos: 'bottom',
  },
  'vtuber-neon': {
    fontFamily: "'Orbitron', sans-serif",
    fontSize: 40,
    fontColor: '#00f5ff',
    strokeEnabled: false,
    strokeColor: '#000000',
    strokeWidth: 0,
    glowEnabled: true,
    glowColor: '#00f5ff',
    glowBlur: 20,
    bgBoxEnabled: false,
    anim: 'zoom-flash',
    pos: 'bottom',
  },
  'anime-bold': {
    fontFamily: "'Fredoka One', cursive",
    fontSize: 50,
    fontColor: '#ffe600',
    strokeEnabled: true,
    strokeColor: '#000000',
    strokeWidth: 4,
    glowEnabled: false,
    glowColor: '#ffe600',
    glowBlur: 0,
    bgBoxEnabled: false,
    anim: 'bounce-in',
    pos: 'bottom',
  },
  'clean-white': {
    fontFamily: "'Inter', sans-serif",
    fontSize: 38,
    fontColor: '#ffffff',
    strokeEnabled: false,
    strokeColor: '#000000',
    strokeWidth: 0,
    glowEnabled: false,
    glowColor: '#000000',
    glowBlur: 0,
    bgBoxEnabled: true,
    bgBoxColor: '#000000',
    bgOpacity: 60,
    anim: 'slide-up',
    pos: 'bottom',
  },
  'retro-game': {
    fontFamily: "'Press Start 2P', monospace",
    fontSize: 22,
    fontColor: '#00ff41',
    strokeEnabled: true,
    strokeColor: '#003300',
    strokeWidth: 2,
    glowEnabled: true,
    glowColor: '#00ff41',
    glowBlur: 10,
    bgBoxEnabled: true,
    bgBoxColor: '#000000',
    bgOpacity: 80,
    anim: 'typewriter',
    pos: 'bottom',
  },
  'idol-pink': {
    fontFamily: "'Righteous', cursive",
    fontSize: 46,
    fontColor: '#ff85c2',
    strokeEnabled: true,
    strokeColor: '#ffffff',
    strokeWidth: 2,
    glowEnabled: true,
    glowColor: '#ff85c2',
    glowBlur: 16,
    bgBoxEnabled: false,
    anim: 'word-pop',
    pos: 'bottom',
  },
};

// ── VTuber Highlights Preset Instructions ─────────────────────────────────
const VTUBER_HIGHLIGHTS_PRESET = `Find high-engagement VTuber highlights using these criteria:

PEAK MOMENTS (prioritize these):
• Karma Arc: Extreme overconfidence immediately followed by a disastrous fail
• Genuine Reactions: Non-scripted scares, wheezing laughter, or unhinged rants that show personality
• High-Intensity Gameplay: Clutch plays or epic fails where chat reactions are too fast to read
• Chaotic Pleas: Hilarious screaming, begging NPCs/enemies for mercy, or panic-induced noise

STRUCTURE (required for every clip):
• Setup (Bridge): Include 15–45 seconds of buildup — the calm before the storm
• Hook: If the VTuber sets a goal or tells a story, include that narrative so viewers feel invested
• Full Cycle (No Cliffhangers): Include the Aftermath — VTuber's reaction after the peak event (speechless, reading funny chat, making excuses). Only end the clip when the topic changes or the energy settles.

DURATION & PACING:
• Target clip length: 2–5 minutes per clip
• Flag any silence longer than 5 seconds inside the clip as a potential edit point

For each clip output highlight_type (karma_arc | genuine_reaction | clutch_play | chaotic_plea | other) and dead_air_timestamps (array of timestamps in seconds where silence > 5s occurs inside the clip).`;

// ── DOM Refs ───────────────────────────────────────────────────────────────
const dropZone        = document.getElementById('dropZone');
const dropZoneInner   = document.getElementById('dropZoneInner');
const fileSelected    = document.getElementById('fileSelected');
const fileInput       = document.getElementById('fileInput');
const fileName        = document.getElementById('fileName');
const fileSize        = document.getElementById('fileSize');
const fileRemove      = document.getElementById('fileRemove');
const uploadForm      = document.getElementById('uploadForm');
const transcribeBtn   = document.getElementById('transcribeBtn');

// Advanced options
const advancedToggle         = document.getElementById('advancedToggle');
const advancedArrow          = document.getElementById('advancedArrow');
const advancedBody           = document.getElementById('advancedBody');
const speakerDetectionEnabled = document.getElementById('speakerDetectionEnabled');
const numSpeakersRow         = document.getElementById('numSpeakersRow');
const numSpeakersEnabled     = document.getElementById('numSpeakersEnabled');
const numSpeakersInput       = document.getElementById('numSpeakersInput');
const speakerCountDec        = document.getElementById('speakerCountDec');
const speakerCountInc        = document.getElementById('speakerCountInc');
const speakerCountVal        = document.getElementById('speakerCountVal');
const speakerCountPills      = document.getElementById('speakerCountPills');
const targetLang      = document.getElementById('targetLang');
const whisperModel    = document.getElementById('whisperModel');
const jobsList        = document.getElementById('jobsList');
const clipJobsList    = document.getElementById('clipJobsList');
const jobsPane        = document.getElementById('jobsPane');
const clipsPane       = document.getElementById('clipsPane');
const jobsToggleBtns  = document.querySelectorAll('.jobs-toggle-btn');
const refreshBtn      = document.getElementById('refreshBtn');
const systemStatus    = document.getElementById('systemStatus');
const sysGrid         = document.getElementById('sysGrid');
const modalOverlay    = document.getElementById('modalOverlay');
const modalClose      = document.getElementById('modalClose');
const modalTitle      = document.getElementById('modalTitle');
const modalId         = document.getElementById('modalId');
const modalPhase      = document.getElementById('modalPhase');
const modalPct        = document.getElementById('modalPct');
const modalProgressBar = document.getElementById('modalProgressBar');
const phaseDots       = document.getElementById('phaseDots');
const logBody         = document.getElementById('logBody');
const logBadge        = document.getElementById('logBadge');
const modalActions    = document.getElementById('modalActions');

// Preview screen
const previewVideo     = document.getElementById('previewVideo');
const subtitleOverlay  = document.getElementById('subtitleOverlay');
const subtitleContainer = document.getElementById('subtitleContainer');
const transcriptBody   = document.getElementById('transcriptBody');
const previewPlayBtn   = document.getElementById('previewPlayBtn');
const previewRestartBtn = document.getElementById('previewRestartBtn');
const editTranscriptBtn = document.getElementById('editTranscriptBtn');
const backToUploadBtn  = document.getElementById('backToUploadBtn');
const startRenderBtn   = document.getElementById('startRenderBtn');
const fullscreenBtn    = document.getElementById('fullscreenBtn');
const videoWrap        = document.querySelector('.video-preview-wrap');

// Style controls
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

// Speaker style panel
const speakerStylesSection = document.getElementById('speakerStylesSection');
const speakerStylesPanel   = document.getElementById('speakerStylesPanel');

// Timeline elements
const timelinePanel    = document.getElementById('timelinePanel');
const timelineRuler    = document.getElementById('timelineRuler');
const timelineTrackArea = document.getElementById('timelineTrackArea');
const timelineTrack    = document.getElementById('timelineTrack');
const timelinePlayhead = document.getElementById('timelinePlayhead');
const timelineScrollArea = document.getElementById('timelineScrollArea');
const timelineLabelsCol  = document.getElementById('timelineLabelsCol');
const tlTimeDisplay    = document.getElementById('tlTimeDisplay');
const addSegmentBtn    = document.getElementById('addSegmentBtn');
const deleteSegmentBtn = document.getElementById('deleteSegmentBtn');

// AE Export
const exportAEBtn      = document.getElementById('exportAEBtn');

// Segment dialog
const segmentDialog    = document.getElementById('segmentDialog');
const newSegStart      = document.getElementById('newSegStart');
const newSegEnd        = document.getElementById('newSegEnd');
const newSegText       = document.getElementById('newSegText');
const newSegSpeaker    = document.getElementById('newSegSpeaker');
const cancelNewSeg     = document.getElementById('cancelNewSeg');
const confirmNewSeg    = document.getElementById('confirmNewSeg');

// Split dialog
const splitDialog      = document.getElementById('splitDialog');
const splitDialogWords = document.getElementById('splitDialogWords');
const splitPart1Time   = document.getElementById('splitPart1Time');
const splitPart1Text   = document.getElementById('splitPart1Text');
const splitPart2Time   = document.getElementById('splitPart2Time');
const splitPart2Text   = document.getElementById('splitPart2Text');
const cancelSplitSeg   = document.getElementById('cancelSplitSeg');
const confirmSplitSeg  = document.getElementById('confirmSplitSeg');

// Transcribing screen
const transcribingStatus = document.getElementById('transcribingStatus');
const transcribingFile   = document.getElementById('transcribingFile');
const transcribingLog    = document.getElementById('transcribingLog');

// Render screen
const renderStatus  = document.getElementById('renderStatus');
const renderFile    = document.getElementById('renderFile');
const renderPhases  = document.getElementById('renderPhases');
const renderBar     = document.getElementById('renderBar');
const renderLog     = document.getElementById('renderLog');

// Fullscreen scale factor: applied to font sizes so fullscreen preview
// matches the proportional appearance of the final render output.
let fsScale = 1;
let previewWidthBeforeFs = 0;
let jobsPanelTab = 'jobs';

function toggleFullscreen() {
  if (!document.fullscreenElement) {
    // Capture the normal preview width BEFORE entering fullscreen
    previewWidthBeforeFs = previewVideo.clientWidth;
    videoWrap.requestFullscreen().catch(() => {});
  } else {
    document.exitFullscreen();
  }
}

// ── Init ───────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  loadSystemInfo();
  loadJobs();
  setupJobsPanelToggle();
  setupDragDrop();
  setupAdvancedOptions();
  setupForm();
  setupModal();
  setupPreviewControls();
  setupStyleControls();
  setupTimeline();
  setupSplitDialog();
  setupAEExport();
  setupClipFinder();
  setupClipPicker();
  setupNavTabs();
  applyPreset('vtuber-pop');
  setupModelSwitchNote();

  setInterval(loadJobs, 5000);
});

// ── ElevenLabs model note & advanced options sync ───────────────────────────
function setupModelSwitchNote() {
  const note = document.getElementById('elevenlabsNote');
  const quotaBox = document.getElementById('elevenlabsQuota');
  const quotaContent = document.getElementById('elQuotaContent');
  if (!whisperModel || !note) return;

  let quotaLoaded = false;

  function updateNote() {
    const isEL = whisperModel.value === 'elevenlabs';
    note.style.display = isEL ? 'block' : 'none';
    if (quotaBox) quotaBox.style.display = isEL ? 'block' : 'none';

    // Fetch quota once when ElevenLabs is first selected
    if (isEL && !quotaLoaded) {
      quotaLoaded = true;
      fetchElevenLabsQuota();
    }

    // Update Speaker Detection description based on model
    const descEl = document.querySelector('#speakerDetectionEnabled')
      ?.closest('.advanced-row')
      ?.querySelector('.advanced-row-desc');
    if (descEl) {
      descEl.textContent = isEL
        ? 'ElevenLabs built-in speaker diarization. Disable for single-speaker content'
        : 'Detect multiple speakers via timing gaps. Disable for single-speaker to use Pycaps animations';
    }
  }

  async function fetchElevenLabsQuota() {
    if (!quotaContent) return;
    quotaContent.textContent = 'Loading quota...';
    try {
      const data = await apiFetch('/api/elevenlabs/quota');
      const keys = data.keys || [];

      if (keys.length === 0) {
        quotaContent.textContent = 'No keys configured';
        return;
      }

      let html = '';
      keys.forEach((k, i) => {
        const isLast = i === keys.length - 1;
        const sep = !isLast
          ? 'margin-bottom:10px;padding-bottom:10px;border-bottom:1px solid rgba(255,255,255,0.08);'
          : '';

        if (k.error) {
          html += `<div style="${sep}">` +
            `<span style="opacity:0.55;font-size:0.78rem;">${k.key_label}</span> ` +
            `<span style="color:#f44336;font-size:0.78rem;">⚠ ${k.error}</span></div>`;
          return;
        }

        const used = k.character_count || 0;
        const limit = k.character_limit || 0;
        const pct = limit > 0 ? Math.round((used / limit) * 100) : 0;
        const remaining = Math.max(0, limit - used);
        const tier = (k.tier || 'unknown').replace(/_/g, ' ');
        let resetStr = '';
        if (k.next_reset_unix) {
          const d = new Date(k.next_reset_unix * 1000);
          resetStr = ` · Reset ${d.toLocaleDateString()}`;
        }
        let barColor = '#4caf50';
        if (pct > 80) barColor = '#f44336';
        else if (pct > 50) barColor = '#ff9800';

        html +=
          `<div style="${sep}">` +
            `<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px;">` +
              `<span><span style="opacity:0.5;font-size:0.78rem;">${k.key_label} · </span><strong>${tier}</strong>${resetStr}</span>` +
              `<span style="color:${barColor};font-weight:600;">${pct}% used</span>` +
            `</div>` +
            `<div style="background:rgba(255,255,255,0.08);border-radius:4px;height:5px;overflow:hidden;">` +
              `<div style="width:${pct}%;height:100%;background:${barColor};border-radius:4px;transition:width .3s;"></div>` +
            `</div>` +
            `<div style="margin-top:4px;font-size:0.77rem;opacity:0.65;">${remaining.toLocaleString()} / ${limit.toLocaleString()} chars remaining</div>` +
          `</div>`;
      });
      quotaContent.innerHTML = html;
    } catch (e) {
      quotaContent.textContent = 'Could not load quota info';
      console.error('ElevenLabs quota fetch failed:', e);
    }
  }

  whisperModel.addEventListener('change', () => {
    quotaLoaded = false; // re-fetch on every switch to ElevenLabs
    updateNote();
  });
  updateNote(); // initial state
}

// ── Advanced Options ────────────────────────────────────────────────────────
let numSpeakersCount = 2; // current manual speaker count value

function setupAdvancedOptions() {
  // Accordion open/close
  advancedToggle.addEventListener('click', () => {
    const open = !advancedBody.classList.contains('hidden');
    advancedBody.classList.toggle('hidden', open);
    advancedArrow.style.transform = open ? '' : 'rotate(180deg)';
  });

  // Speaker detection ON/OFF — controls visibility of num-speakers row
  speakerDetectionEnabled.addEventListener('change', () => {
    const enabled = speakerDetectionEnabled.checked;
    numSpeakersRow.classList.toggle('advanced-row-disabled', !enabled);
    // If disabled, also uncheck num speakers override
    if (!enabled) {
      numSpeakersEnabled.checked = false;
      numSpeakersInput.classList.add('hidden');
    }
  });

  // Toggle num-speaker input visibility
  numSpeakersEnabled.addEventListener('change', () => {
    numSpeakersInput.classList.toggle('hidden', !numSpeakersEnabled.checked);
  });

  // +/- buttons
  speakerCountDec.addEventListener('click', () => setSpeakerCount(numSpeakersCount - 1));
  speakerCountInc.addEventListener('click', () => setSpeakerCount(numSpeakersCount + 1));

  // Quick-select pills
  speakerCountPills.querySelectorAll('.speaker-pill').forEach(btn => {
    btn.addEventListener('click', () => setSpeakerCount(parseInt(btn.dataset.count, 10)));
  });
}

function setSpeakerCount(n) {
  numSpeakersCount = Math.min(6, Math.max(1, n));
  speakerCountVal.textContent = numSpeakersCount;
  speakerCountPills.querySelectorAll('.speaker-pill').forEach(btn => {
    btn.classList.toggle('active', parseInt(btn.dataset.count, 10) === numSpeakersCount);
  });
  // Disable dec/inc at boundaries
  speakerCountDec.disabled = numSpeakersCount <= 1;
  speakerCountInc.disabled = numSpeakersCount >= 6;
}

// ── Screen Navigation ──────────────────────────────────────────────────────
let activeTab = 'subtitle'; // 'subtitle' or 'clipfinder'

function showScreen(name) {
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

function switchTab(tab) {
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

// ── System Info ────────────────────────────────────────────────────────────
async function loadSystemInfo() {
  try {
    const data = await apiFetch('/api/system');
    const dot  = systemStatus.querySelector('.status-dot');
    const text = systemStatus.querySelector('.status-text');

    const allOk = data.packages.ffmpeg && data.packages.whisperx;
    dot.className = 'status-dot ' + (allOk ? 'ok' : 'warn');
    text.textContent = allOk
      ? `Ready · ${data.cuda_available ? data.gpu_name || 'GPU' : 'CPU'} · Torch ${data.torch_version}`
      : 'Setup required — FFmpeg or WhisperX missing';

    const items = [
      { label: 'FFmpeg',    ok: data.packages.ffmpeg },
      { label: 'WhisperX', ok: data.packages.whisperx },
      { label: 'Pycaps',   ok: data.packages.pycaps },
      { label: 'CUDA',     ok: data.cuda_available, warn: !data.cuda_available },
    ];
    sysGrid.innerHTML = items.map(i => `
      <div class="sys-item ${i.ok ? 'ok' : i.warn ? 'warn' : 'err'}">
        <span class="dot"></span>${i.label}
      </div>
    `).join('');

    // Populate whisper model selector dynamically from server
    if (data.whisper_models && whisperModel) {
      const currentVal = whisperModel.value;
      whisperModel.innerHTML = '';
      const modelIcons = { 'whisperx': '🧠', 'faster-whisper': '🎌', 'elevenlabs': '🔊' };
      for (const [key, m] of Object.entries(data.whisper_models)) {
        const opt = document.createElement('option');
        opt.value = key;
        const icon = modelIcons[m.type] || '🧠';
        opt.textContent = `${icon} ${m.label} — ${m.description}`;
        whisperModel.appendChild(opt);
      }
      // Restore previous selection if still valid
      if ([...whisperModel.options].some(o => o.value === currentVal)) {
        whisperModel.value = currentVal;
      }
    }
  } catch (e) {
    console.error('System info failed:', e);
  }
}

// ── Drag & Drop ────────────────────────────────────────────────────────────
function setupDragDrop() {
  dropZone.addEventListener('click', (e) => {
    if (!e.target.closest('.file-remove')) fileInput.click();
  });
  dropZone.addEventListener('dragover', (e) => {
    e.preventDefault();
    dropZone.classList.add('drag-over');
  });
  dropZone.addEventListener('dragleave', () => dropZone.classList.remove('drag-over'));
  dropZone.addEventListener('drop', (e) => {
    e.preventDefault();
    dropZone.classList.remove('drag-over');
    const file = e.dataTransfer.files[0];
    if (file) setFile(file);
  });
  fileInput.addEventListener('change', () => {
    if (fileInput.files[0]) setFile(fileInput.files[0]);
  });
  fileRemove.addEventListener('click', (e) => {
    e.stopPropagation();
    clearFile();
  });
}

function setFile(file) {
  const validTypes = ['.mp4', '.mov', '.mkv', '.avi'];
  const ext = '.' + file.name.split('.').pop().toLowerCase();
  if (!validTypes.includes(ext)) {
    alert('Please select a video file: MP4, MOV, MKV, or AVI');
    return;
  }
  selectedFile = file;
  fileName.textContent = file.name;
  fileSize.textContent = formatBytes(file.size);
  dropZoneInner.classList.add('hidden');
  fileSelected.classList.remove('hidden');
  transcribeBtn.disabled = false;
}

function clearFile() {
  selectedFile = null;
  fileInput.value = '';
  dropZoneInner.classList.remove('hidden');
  fileSelected.classList.add('hidden');
  transcribeBtn.disabled = true;
}

// ── Form Submit → Transcribe Phase ────────────────────────────────────────
function setupForm() {
  uploadForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    if (!selectedFile) return;

    transcribeBtn.disabled = true;
    transcribeBtn.classList.add('loading');
    transcribeBtn.querySelector('.btn-text').textContent = 'Uploading...';

    try {
      // Upload and start transcription job
      const formData = new FormData();
      formData.append('video', selectedFile);
      formData.append('target_language', targetLang.value);
      formData.append('transcribe_only', true); // Signal to only run phase 1
      formData.append('speaker_detection', speakerDetectionEnabled.checked);
      formData.append('whisper_model', whisperModel.value);

      // Pass manual speaker count if user enabled it (only relevant when detection is ON)
      if (speakerDetectionEnabled.checked && numSpeakersEnabled.checked) {
        formData.append('num_speakers', numSpeakersCount);
      }

      const job = await apiFetch('/api/jobs', { method: 'POST', body: formData });
      activeJobId = job.id;

      // Show transcribing screen
      const modelLabel = whisperModel.options[whisperModel.selectedIndex].text;
      transcribingFile.textContent = selectedFile.name;
      transcribingStatus.textContent = `Running transcription with ${modelLabel}...`;
      transcribingLog.textContent = '';
      showScreen('transcribing');

      // Watch job until phase 1 completes
      await watchTranscription(job.id);

    } catch (err) {
      alert('Failed to start transcription: ' + err.message);
      showScreen('upload');
    } finally {
      transcribeBtn.classList.remove('loading');
      transcribeBtn.querySelector('.btn-text').textContent = 'Transcribe & Preview';
      transcribeBtn.disabled = !selectedFile;
    }
  });

  refreshBtn.addEventListener('click', () => {
    if (jobsPanelTab === 'clips') {
      loadClipJobsList();
      return;
    }
    loadJobs();
  });
}

async function watchTranscription(jobId) {
  return new Promise((resolve, reject) => {
    // SSE for live log
    const sse = new EventSource(`/api/jobs/${jobId}/log`);
    sse.onmessage = (e) => {
      const data = JSON.parse(e.data);
      if (data.line) {
        const line = document.createElement('div');
        line.textContent = data.line;
        transcribingLog.appendChild(line);
        transcribingLog.scrollTop = transcribingLog.scrollHeight;
      }
      if (data.done) {
        sse.close();
      }
    };
    sse.onerror = () => sse.close();

    // Poll for completion
    const poll = setInterval(async () => {
      try {
        const job = await apiFetch(`/api/jobs/${jobId}`);

        // Update status text
        if (job.phase_label) {
          transcribingStatus.textContent = job.phase_label;
        }

        if (job.status === 'completed' && job.current_phase === 1) {
          clearInterval(poll);
          sse.close();

          // Fetch transcript data
          const transcript = await fetchTranscript(jobId);
          if (transcript) {
            transcriptData = transcript;
            openPreviewScreen(jobId);
            resolve();
          } else {
            reject(new Error('Could not load transcript'));
          }
        } else if (job.status === 'failed') {
          clearInterval(poll);
          sse.close();
          reject(new Error(job.error || 'Transcription failed'));
        }
      } catch (err) {
        clearInterval(poll);
        sse.close();
        reject(err);
      }
    }, 2000);
  });
}

async function fetchTranscript(jobId) {
  try {
    // Try to get transcript from API
    const data = await apiFetch(`/api/jobs/${jobId}/transcript`);
    return data.segments || data;
  } catch {
    // Fallback: use mock transcript for UI demo when API not implemented
    return generateMockTranscript();
  }
}

function generateMockTranscript() {
  // Mock data to demonstrate UI when backend transcript endpoint isn't ready
  return [
    { start: 0.5, end: 3.2, text: "Welcome to the video!" },
    { start: 3.5, end: 7.1, text: "Today we're going to show you something amazing." },
    { start: 7.8, end: 11.4, text: "This is the subtitle preview system." },
    { start: 12.0, end: 15.6, text: "You can customize the style right here." },
    { start: 16.2, end: 20.8, text: "Choose your font, color, and animation." },
    { start: 21.5, end: 25.0, text: "It will look just like this on the final video!" },
  ];
}

// ── Preview Screen ─────────────────────────────────────────────────────────
function openPreviewScreen(jobId) {
  // Load video into player
  const videoUrl = `/api/jobs/${jobId}/video`;
  previewVideo.src = videoUrl;
  previewVideo.load();

  // Render transcript list
  renderTranscriptList();

  // Build per-speaker style panel
  buildSpeakerStylePanel();

  // Initialize original transcript toggle + check availability
  initOriginalTranscriptToggle();
  checkOriginalTranscriptAvailable(jobId);

  // Get video duration when metadata loads
  previewVideo.addEventListener('loadedmetadata', function onMeta() {
    videoDuration = previewVideo.duration;
    renderTimeline();
    previewVideo.removeEventListener('loadedmetadata', onMeta);
  });

  // Start subtitle sync loop
  startSubtitleSync();

  showScreen('preview');
  loadJobs();
}

/**
 * Build the per-speaker color picker rows.
 * Collects unique speakers from transcriptData, initialises speakerStyles
 * with palette defaults, then renders a color-picker row per speaker.
 * Shows the section only when two or more speakers are present.
 */
function buildSpeakerStylePanel() {
  // Collect unique speakers in order of appearance
  const seen = [];
  transcriptData.forEach(seg => {
    const sp = seg.speaker || 'SPEAKER_00';
    if (!seen.includes(sp)) seen.push(sp);
  });

  // Initialise speakerStyles (keep any existing overrides)
  seen.forEach(sp => {
    if (!speakerStyles[sp]) {
      const idx = parseInt((sp.match(/\d+$/) || ['0'])[0], 10);
      speakerStyles[sp] = { color: SPEAKER_COLORS[idx % SPEAKER_COLORS.length], strokeColor: null };
    } else if (!('strokeColor' in speakerStyles[sp])) {
      speakerStyles[sp].strokeColor = null;
    }
  });

  // Show section only for multi-speaker content
  // (single-speaker means detection was off or video has one person)
  if (seen.length <= 1) {
    speakerStylesSection.style.display = 'none';
    return;
  }
  speakerStylesSection.style.display = '';

  speakerStylesPanel.innerHTML = '';
  seen.forEach(sp => {
    const idx        = parseInt((sp.match(/\d+$/) || ['0'])[0], 10);
    const label      = `Speaker ${idx}`;
    const color      = speakerStyles[sp].color;
    const strokeOverride = speakerStyles[sp].strokeColor;
    const strokeVal  = strokeOverride || strokeColorEl.value || '#000000';
    const strokeActive = !!strokeOverride;

    const row = document.createElement('div');
    row.className = 'speaker-style-row';
    row.innerHTML = `
      <span class="speaker-style-badge" style="color:${color};border-color:${color}">S${idx}</span>
      <span class="speaker-style-name">${label}</span>
      <div class="speaker-color-group">
        <span class="speaker-color-label">Fill</span>
        <input type="color" class="color-input speaker-color-input" value="${color}" data-speaker="${sp}" title="Text color for ${sp}" />
      </div>
      <div class="speaker-color-group">
        <span class="speaker-color-label">Stroke</span>
        <input type="color" class="color-input speaker-stroke-input ${strokeActive ? 'active-override' : ''}" value="${strokeVal}" data-speaker="${sp}" title="Stroke color for ${sp} (click × to reset)" />
        <button class="speaker-stroke-clear ${strokeActive ? '' : 'hidden'}" data-speaker="${sp}" title="Reset to global stroke">×</button>
      </div>
    `;

    // Fill color change
    const fillInput = row.querySelector('.speaker-color-input');
    fillInput.addEventListener('input', () => {
      speakerStyles[sp] = { ...speakerStyles[sp], color: fillInput.value };
      row.querySelector('.speaker-style-badge').style.color = fillInput.value;
      row.querySelector('.speaker-style-badge').style.borderColor = fillInput.value;
      renderTranscriptList();
      onStyleChange();
    });

    // Stroke color change — activates override on first change
    const strokeInput = row.querySelector('.speaker-stroke-input');
    const strokeClear = row.querySelector('.speaker-stroke-clear');
    strokeInput.addEventListener('input', () => {
      speakerStyles[sp] = { ...speakerStyles[sp], strokeColor: strokeInput.value };
      strokeInput.classList.add('active-override');
      strokeClear.classList.remove('hidden');
      onStyleChange();
    });
    strokeClear.addEventListener('click', (e) => {
      e.stopPropagation();
      speakerStyles[sp] = { ...speakerStyles[sp], strokeColor: null };
      strokeInput.value = strokeColorEl.value || '#000000';
      strokeInput.classList.remove('active-override');
      strokeClear.classList.add('hidden');
      onStyleChange();
    });

    speakerStylesPanel.appendChild(row);
  });
}

function renderTranscriptList() {
  transcriptBody.innerHTML = '';
  transcriptData.forEach((seg, idx) => {
    const speakerColor = getSpeakerColor(seg.speaker);
    const speakerNum = seg.speaker
      ? (seg.speaker.match(/\d+$/) || ['0'])[0]
      : '0';
    const speakerLabel = `S${parseInt(speakerNum, 10)}`;
    const isLast = idx === transcriptData.length - 1;
    const div = document.createElement('div');
    div.className = 'transcript-seg';
    div.dataset.idx = idx;
    div.innerHTML = `
      <div class="seg-row-time">
        <span class="seg-time seg-time-btn" data-idx="${idx}" title="Click to edit timing">${fmtTime(seg.start)}<span class="seg-time-sep"> › </span>${fmtTime(seg.end)}<span class="seg-time-edit-icon">✎</span></span>
        <span class="seg-actions">
          <button class="seg-btn seg-split-btn" data-idx="${idx}" title="Split segment">✂</button>
          ${!isLast ? `<button class="seg-btn seg-merge-btn" data-idx="${idx}" title="Merge with next">⊕</button>` : ''}
        </span>
      </div>
      <div class="seg-row-content">
        <span class="seg-speaker seg-speaker-btn" style="color:${speakerColor};border-color:${speakerColor}" data-idx="${idx}" title="Click to change speaker">${speakerLabel}<span class="seg-speaker-edit-icon">✎</span></span>
        <span class="seg-text" ${editMode ? 'contenteditable="true"' : ''}>${escHtml(seg.text)}</span>
      </div>
    `;
    div.addEventListener('click', (e) => {
      // Don't seek video if clicking on action buttons, speaker button, or time button
      if (e.target.closest('.seg-actions')) return;
      if (e.target.closest('.seg-speaker-btn')) return;
      if (e.target.closest('.seg-time-btn')) return;
      previewVideo.currentTime = seg.start;
      previewVideo.play();
      setActiveSeg(idx);
    });
    // Time edit click
    const timeBtn = div.querySelector('.seg-time-btn');
    timeBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      openTimeEditor(timeBtn, idx);
    });
    // Speaker edit click
    const speakerBtn = div.querySelector('.seg-speaker-btn');
    speakerBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      openSpeakerPicker(speakerBtn, idx);
    });
    if (editMode) {
      const textEl = div.querySelector('.seg-text');
      textEl.addEventListener('input', () => {
        transcriptData[idx].text = textEl.textContent;
        scheduleAutoSave();
      });
    }
    // Split button
    div.querySelector('.seg-split-btn').addEventListener('click', (e) => {
      e.stopPropagation();
      openSplitDialog(idx);
    });
    // Merge button (only present if not last)
    const mergeBtn = div.querySelector('.seg-merge-btn');
    if (mergeBtn) {
      mergeBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        mergeSegmentWithNext(idx);
      });
    }
    transcriptBody.appendChild(div);
  });
}

// ── Original Transcript Toggle ────────────────────────────────────────────

function renderOriginalTranscriptList() {
  if (!originalTranscriptData) return;
  transcriptBody.innerHTML = '';
  originalTranscriptData.forEach((seg, idx) => {
    const speakerColor = getSpeakerColor(seg.speaker);
    const speakerNum = seg.speaker
      ? (seg.speaker.match(/\d+$/) || ['0'])[0]
      : '0';
    const speakerLabel = `S${parseInt(speakerNum, 10)}`;
    const div = document.createElement('div');
    div.className = 'transcript-seg transcript-seg-original';
    div.dataset.idx = idx;
    div.innerHTML = `
      <div class="seg-row-time">
        <span class="seg-time">${fmtTime(seg.start)}<span class="seg-time-sep"> › </span>${fmtTime(seg.end)}</span>
      </div>
      <div class="seg-row-content">
        <span class="seg-speaker" style="color:${speakerColor};border-color:${speakerColor}">${speakerLabel}</span>
        <span class="seg-text">${escHtml(seg.text)}</span>
      </div>
    `;
    div.addEventListener('click', () => {
      previewVideo.currentTime = seg.start;
      previewVideo.play();
    });
    transcriptBody.appendChild(div);
  });
}

async function fetchOriginalTranscript(jobId) {
  try {
    const data = await apiFetch(`/api/jobs/${jobId}/transcript/original`);
    return data.segments || null;
  } catch {
    return null;
  }
}

function initOriginalTranscriptToggle() {
  const toggleWrap = document.getElementById('transcriptSourceToggle');
  const toggleInput = document.getElementById('showOriginalTranscript');
  const label = document.getElementById('transcriptSourceLabel');
  const editBtn = document.getElementById('editTranscriptBtn');

  if (!toggleWrap || !toggleInput) return;

  toggleInput.addEventListener('change', () => {
    showingOriginal = toggleInput.checked;
    if (showingOriginal) {
      label.textContent = 'ElevenLabs';
      editBtn.style.display = 'none';
      renderOriginalTranscriptList();
    } else {
      label.textContent = 'Refined';
      editBtn.style.display = '';
      renderTranscriptList();
    }
  });
}

async function checkOriginalTranscriptAvailable(jobId) {
  const toggleWrap = document.getElementById('transcriptSourceToggle');
  const toggleInput = document.getElementById('showOriginalTranscript');
  if (!toggleWrap) return;

  originalTranscriptData = await fetchOriginalTranscript(jobId);
  if (originalTranscriptData && originalTranscriptData.length > 0) {
    toggleWrap.style.display = 'flex';
  } else {
    toggleWrap.style.display = 'none';
  }

  // Reset toggle state
  showingOriginal = false;
  if (toggleInput) toggleInput.checked = false;
  const label = document.getElementById('transcriptSourceLabel');
  if (label) label.textContent = 'Refined';
}

function setActiveSeg(idx) {
  transcriptBody.querySelectorAll('.transcript-seg').forEach((el, i) => {
    el.classList.toggle('active', i === idx);
  });
  // Scroll into view
  const active = transcriptBody.querySelector('.transcript-seg.active');
  if (active) active.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

// ── Time Editor Popup ──────────────────────────────────────────────────────
function parseTime(str) {
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

let activeTimeEditor = null;

function closeTimeEditor() {
  if (activeTimeEditor) { activeTimeEditor.remove(); activeTimeEditor = null; }
}

function openTimeEditor(anchorEl, segIdx) {
  // Close other popups
  closeTimeEditor();
  closeSpeakerPicker();

  const seg = transcriptData[segIdx];
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

  // Spin buttons: nudge ±0.1s
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
    transcriptData[segIdx].start = parseFloat(newStart.toFixed(1));
    transcriptData[segIdx].end   = parseFloat(newEnd.toFixed(1));
    closeTimeEditor();
    renderTranscriptList();
    renderTimeline();
    onStyleChange();
    scheduleAutoSave();
  }

  popup.querySelector('#teConfirmBtn').addEventListener('click', (e) => { e.stopPropagation(); applyChanges(); });
  popup.querySelector('#teCancelBtn').addEventListener('click', (e) => { e.stopPropagation(); closeTimeEditor(); });
  startInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') applyChanges(); if (e.key === 'Escape') closeTimeEditor(); });
  endInput.addEventListener('keydown',   (e) => { if (e.key === 'Enter') applyChanges(); if (e.key === 'Escape') closeTimeEditor(); });

  // Auto-select text on focus
  startInput.addEventListener('focus', () => startInput.select());
  endInput.addEventListener('focus',   () => endInput.select());
  startInput.focus();

  // Close on outside click
  setTimeout(() => {
    document.addEventListener('click', closeTimeEditor, { once: true });
  }, 0);
}

// Speaker picker popup for editing segment speaker
let activeSpeakerPicker = null;

function closeSpeakerPicker() {
  if (activeSpeakerPicker) {
    activeSpeakerPicker.remove();
    activeSpeakerPicker = null;
  }
}

function openSpeakerPicker(anchorEl, segIdx) {
  closeSpeakerPicker();
  closeTimeEditor();

  // Collect unique speakers from transcriptData
  const seen = [];
  transcriptData.forEach(s => {
    const sp = s.speaker || 'SPEAKER_00';
    if (!seen.includes(sp)) seen.push(sp);
  });

  const currentSpeaker = transcriptData[segIdx].speaker || 'SPEAKER_00';

  const picker = document.createElement('div');
  picker.className = 'speaker-picker-popup';
  picker.innerHTML = `
    <div class="speaker-picker-header">Change Speaker</div>
    <div class="speaker-picker-list">
      ${seen.map(sp => {
        const idx = parseInt((sp.match(/\d+$/) || ['0'])[0], 10);
        const color = getSpeakerColor(sp);
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

  // Position below the anchor (fixed positioning = viewport coords)
  document.body.appendChild(picker);
  const rect = anchorEl.getBoundingClientRect();
  const pickerW = 180;
  let left = rect.left;
  let top = rect.bottom + 4;
  // Keep inside viewport
  if (left + pickerW > window.innerWidth - 8) left = window.innerWidth - pickerW - 8;
  if (top < 0) top = 0;
  picker.style.left = `${left}px`;
  picker.style.top = `${top}px`;

  activeSpeakerPicker = picker;

  picker.querySelectorAll('.speaker-picker-item').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      const sp = btn.dataset.speaker;
      if (sp === '__new__') {
        // Ask for new speaker name
        const input = prompt('Enter new speaker ID (e.g. SPEAKER_02):', `SPEAKER_0${seen.length}`);
        if (!input || !input.trim()) { closeSpeakerPicker(); return; }
        const newSp = input.trim().toUpperCase().replace(/\s+/g, '_');
        // Initialise color for new speaker
        if (!speakerStyles[newSp]) {
          const nIdx = parseInt((newSp.match(/\d+$/) || ['0'])[0], 10);
          speakerStyles[newSp] = { color: SPEAKER_COLORS[nIdx % SPEAKER_COLORS.length] };
        }
        transcriptData[segIdx].speaker = newSp;
      } else {
        transcriptData[segIdx].speaker = sp;
      }
      closeSpeakerPicker();
      renderTranscriptList();
      buildSpeakerStylePanel();
      onStyleChange();
      scheduleAutoSave();
    });
  });

  // Close on outside click
  setTimeout(() => {
    document.addEventListener('click', closeSpeakerPicker, { once: true });
  }, 0);
}

// Subtitle sync loop using requestAnimationFrame
function startSubtitleSync() {
  if (subtitleTimer) cancelAnimationFrame(subtitleTimer);

  let lastActiveKey = '';

  function tick() {
    const t = previewVideo.currentTime;

    // Update timeline playhead and time display
    updatePlayhead(t);
    updateTimeDisplay(t);

    // Find ALL segments active at this moment (multi-speaker support)
    const activeSegs = transcriptData
      .map((s, i) => ({ ...s, _idx: i }))
      .filter(s => t >= s.start && t <= s.end);

    // Build a stable key to detect changes
    const activeKey = activeSegs.map(s => s._idx).join(',');

    if (activeKey !== lastActiveKey) {
      lastActiveKey = activeKey;
      // Highlight first active segment in transcript list
      setActiveSeg(activeSegs.length > 0 ? activeSegs[0]._idx : -1);
      renderActiveSubtitles(activeSegs, t);
    } else if (activeSegs.length > 0 && (currentAnim === 'karaoke' || currentAnim === 'narration-pop')) {
      // Update karaoke highlights for all active segments
      activeSegs.forEach(seg => updateKaraokeHighlight(seg, t, seg._idx));
    }

    subtitleTimer = requestAnimationFrame(tick);
  }

  subtitleTimer = requestAnimationFrame(tick);
}

function renderActiveSubtitles(activeSegs, currentTime) {
  subtitleContainer.innerHTML = '';

  // Reset any inline flex overrides so CSS classes (pos-bottom, pos-top, etc.) take effect
  subtitleOverlay.style.alignItems = '';
  subtitleOverlay.style.justifyContent = '';

  if (activeSegs.length === 0) return;

  const style = collectStyle();

  // Single-speaker: use fontColor (matches Pycaps render which uses fontColor)
  // Multi-speaker: use per-speaker color (matches ASS render which uses speakerStyles)
  const isMultiSpeaker = new Set(transcriptData.map(s => s.speaker || 'SPEAKER_00')).size > 1;

  activeSegs.forEach((seg, layerIdx) => {
    const speakerColor       = isMultiSpeaker ? getSpeakerColor(seg.speaker) : null;
    const speakerStrokeColor = isMultiSpeaker ? getSpeakerStrokeColor(seg.speaker) : null;
    const words = seg.text.split(' ');

    const wordsHtml = words.map((w) => {
      return `<span class="sub-word" style="${buildWordStyle(style, speakerColor, speakerStrokeColor)}">${escHtml(w)}</span>`;
    }).join('');

    const lineDiv = document.createElement('div');
    lineDiv.className = `subtitle-line anim-${currentAnim} speaker-layer-${layerIdx} draggable`;
    lineDiv.dataset.segIdx = seg._idx;
    lineDiv.dataset.speaker = seg.speaker || 'SPEAKER_00';
    lineDiv.style.cssText = buildLineStyle(style, speakerColor, layerIdx);

    // Per-segment position override
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
      // Calculate the actual visual center of the subtitle relative to the overlay
      const lineRect = lineDiv.getBoundingClientRect();
      const actualPosX = ((lineRect.left + lineRect.width / 2) - overlayRect.left) / overlayRect.width * 100;
      const actualPosY = ((lineRect.top + lineRect.height / 2) - overlayRect.top) / overlayRect.height * 100;

      // Immediately set posOverride with exact visual position on the segment data
      const seg = transcriptData[segIdx];
      seg.posX = actualPosX;
      seg.pos_x = actualPosX;
      seg.posY = actualPosY;
      seg.pos_y = actualPosY;
      seg.posOverride = true;
      seg.pos_override = true;

      // Apply absolute positioning directly on this element to prevent
      // a visual jump (no re-render, keeps the element in-place)
      lineDiv.style.position = 'absolute';
      lineDiv.style.left = actualPosX + '%';
      lineDiv.style.top = actualPosY + '%';
      lineDiv.style.transform = 'translate(-50%, -50%)';

      subtitleDragState = {
        segIdx,
        startX: e.clientX,
        startY: e.clientY,
        overlayRect,
        origPosX: actualPosX,
        origPosY: actualPosY,
      };
      lineDiv.classList.add('dragging');
    });

    subtitleContainer.appendChild(lineDiv);

    // Karaoke / Narration Pop highlights
    if ((currentAnim === 'karaoke' || currentAnim === 'narration-pop') && seg.words) {
      updateKaraokeHighlight(seg, currentTime, seg._idx);
    }
  });
}

// Legacy single-segment wrapper (still needed by onStyleChange)
function renderSubtitle(seg, currentTime) {
  renderActiveSubtitles([{ ...seg, _idx: transcriptData.indexOf(seg) }], currentTime);
}

function updateKaraokeHighlight(seg, currentTime, segIdx) {
  if (!seg.words || !seg.words.length) return;
  // Find the subtitle line for this specific segment
  const lineDiv = subtitleContainer.querySelector(`[data-seg-idx="${segIdx}"]`);
  if (!lineDiv) return;
  const wordEls = lineDiv.querySelectorAll('.sub-word');
  if (!wordEls.length) return;

  seg.words.forEach((w, i) => {
    if (!wordEls[i]) return;
    const isActive = currentTime >= w.start && currentTime <= w.end;
    wordEls[i].classList.toggle('karaoke-active', isActive);
    if (isActive) {
      wordEls[i].style.color = document.getElementById('fontColor').value;
    } else {
      wordEls[i].style.color = '';
    }
  });
}

// ── Style Computation ──────────────────────────────────────────────────────
function collectStyle() {
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
    // Per-speaker style overrides (keyed by SPEAKER_XX)
    speakerStyles: Object.fromEntries(
      Object.entries(speakerStyles).map(([k, v]) => [k, { color: v.color, strokeColor: v.strokeColor || null }])
    ),
  };
}

function buildWordStyle(style, speakerColor, speakerStrokeColor) {
  let parts = [];
  let shadows = [];

  // Stroke via multi-shadow outline (matches Pycaps render engine)
  if (style.strokeEnabled && style.strokeWidth > 0) {
    const w = Math.round(style.strokeWidth * fsScale);
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

  // Glow — always use configured glowColor (matches render engine)
  if (style.glowEnabled && style.glowBlur > 0) {
    const gb = Math.round(style.glowBlur * fsScale);
    const gc = style.glowColor;
    shadows.push(`0 0 ${gb}px ${gc}`, `0 0 ${gb * 2}px ${gc}`);
  }

  if (shadows.length > 0) {
    parts.push(`text-shadow: ${shadows.join(', ')}`);
  }

  // Background box on word level (matches Pycaps render engine)
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

function buildLineStyle(style, speakerColor, layerIdx) {
  const color = speakerColor || style.fontColor;
  // Stack multiple speakers: each additional layer shifts up by ~2em
  const marginBottom = layerIdx > 0 ? `${layerIdx * 2.8}em` : '0';

  let parts = [
    `font-family: ${style.fontFamily}`,
    `font-size: ${Math.round(style.fontSize * fsScale)}px`,
    `color: ${color}`,
    `font-weight: 800`,
    `line-height: 1.2`,
    `margin-bottom: ${marginBottom}`,
  ];

  return parts.join('; ');
}

// Trigger re-render when style changes
function onStyleChange() {
  if (subtitleContainer.children.length > 0) {
    const t = previewVideo.currentTime;
    const activeSegs = transcriptData
      .map((s, i) => ({ ...s, _idx: i }))
      .filter(s => t >= s.start && t <= s.end);
    if (activeSegs.length > 0) renderActiveSubtitles(activeSegs, t);
  }
}

// ── Style Controls Setup ──────────────────────────────────────────────────
function setupStyleControls() {
  // Range sliders with live value display
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

  // Color pickers
  fontColorEl.addEventListener('input', onStyleChange);
  strokeColorEl.addEventListener('input', onStyleChange);
  glowColorEl.addEventListener('input', onStyleChange);
  bgBoxColorEl.addEventListener('input', onStyleChange);
  fontFamilyEl.addEventListener('change', onStyleChange);

  // Toggle controls
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

  // Color swatches
  colorSwatches.querySelectorAll('.swatch').forEach(btn => {
    btn.addEventListener('click', () => {
      const color = btn.dataset.color;
      fontColorEl.value = color;
      colorSwatches.querySelectorAll('.swatch').forEach(s => s.classList.remove('active'));
      btn.classList.add('active');
      onStyleChange();
    });
  });

  // Animation buttons
  animGrid.querySelectorAll('.anim-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      animGrid.querySelectorAll('.anim-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      currentAnim = btn.dataset.anim;
      onStyleChange();
    });
  });

  // Position buttons
  positionGrid.querySelectorAll('.pos-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      positionGrid.querySelectorAll('.pos-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      currentPos = btn.dataset.pos;
      subtitleOverlay.className = 'subtitle-overlay pos-' + currentPos;
    });
  });

  // Preset buttons
  presetGrid.querySelectorAll('.preset-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      presetGrid.querySelectorAll('.preset-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      applyPreset(btn.dataset.preset);
    });
  });
}

function applyPreset(name) {
  const p = PRESETS[name];
  if (!p) return;

  // Set font family option
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

  // Anim
  currentAnim = p.anim;
  animGrid.querySelectorAll('.anim-btn').forEach(b => {
    b.classList.toggle('active', b.dataset.anim === p.anim);
  });

  // Position
  currentPos = p.pos;
  positionGrid.querySelectorAll('.pos-btn').forEach(b => {
    b.classList.toggle('active', b.dataset.pos === p.pos);
  });
  subtitleOverlay.className = 'subtitle-overlay pos-' + currentPos;

  onStyleChange();
}

// ── Preview Controls ───────────────────────────────────────────────────────
function setupPreviewControls() {
  // Save button
  const saveBtn = document.getElementById('saveTranscriptBtn');
  if (saveBtn) {
    saveBtn.addEventListener('click', () => saveTranscript(false));
  }

  // Ctrl+S shortcut for save
  document.addEventListener('keydown', (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === 's') {
      const previewScreen = document.getElementById('screen-preview');
      if (previewScreen && previewScreen.classList.contains('active')) {
        e.preventDefault();
        saveTranscript(false);
      }
    }
  });
  previewPlayBtn.addEventListener('click', () => {
    if (previewVideo.paused) {
      previewVideo.play();
      previewPlayBtn.textContent = '⏸ Pause';
    } else {
      previewVideo.pause();
      previewPlayBtn.textContent = '▶ Play';
    }
  });

  previewRestartBtn.addEventListener('click', () => {
    previewVideo.currentTime = 0;
    previewVideo.play();
    previewPlayBtn.textContent = '⏸ Pause';
  });

  previewVideo.addEventListener('ended', () => {
    previewPlayBtn.textContent = '▶ Play';
    subtitleContainer.innerHTML = '';
  });

  previewVideo.addEventListener('pause', () => {
    previewPlayBtn.textContent = '▶ Play';
  });
  previewVideo.addEventListener('play', () => {
    previewPlayBtn.textContent = '⏸ Pause';
  });

  if (backToUploadBtn) {
    backToUploadBtn.addEventListener('click', () => {
      previewVideo.pause();
      if (subtitleTimer) cancelAnimationFrame(subtitleTimer);
      selectedSegIdx = null;
      videoDuration = 0;
      timelineZoom = 1.0;
      showScreen('upload');
      clearFile();
    });
  }

  // Navbar back button — same behaviour as the in-panel back button
  const navBackBtn = document.getElementById('navBackBtn');
  if (navBackBtn) {
    navBackBtn.addEventListener('click', () => {
      const activeScreen = document.querySelector('.app-screen.active');
      const screenId = activeScreen ? activeScreen.id : '';
      if (screenId === 'screen-preview') {
        previewVideo.pause();
        if (subtitleTimer) cancelAnimationFrame(subtitleTimer);
        selectedSegIdx = null;
        videoDuration = 0;
        timelineZoom = 1.0;
        clearFile();
      }
      showScreen('upload');
      switchTab('subtitle');
    });
  }

  editTranscriptBtn.addEventListener('click', () => {
    editMode = !editMode;
    editTranscriptBtn.textContent = editMode ? '✅ Done' : '✏️ Edit';
    renderTranscriptList();
  });

  startRenderBtn.addEventListener('click', openRenderOptionsModal);

  // Fullscreen toggle
  fullscreenBtn.addEventListener('click', toggleFullscreen);
  document.addEventListener('fullscreenchange', () => {
    const isFs = !!document.fullscreenElement;
    fullscreenBtn.textContent = isFs ? '✕' : '⛶';
    fullscreenBtn.title = isFs ? 'Exit Fullscreen (F or Esc)' : 'Fullscreen (F)';

    if (isFs) {
      // Wait one frame so browser has applied fullscreen dimensions
      requestAnimationFrame(() => {
        const cW = videoWrap.offsetWidth;
        const cH = videoWrap.offsetHeight;
        // Native video aspect ratio (fallback 16:9 if video not loaded yet)
        const vAspect = previewVideo.videoWidth > 0
          ? previewVideo.videoWidth / previewVideo.videoHeight
          : 16 / 9;
        const cAspect = cW / cH;

        // Compute actual displayed video rect (object-fit: contain letterboxing)
        let vW, vH, vX, vY;
        if (cAspect >= vAspect) {
          // Pillarboxed (black bars on sides)
          vH = cH;  vW = cH * vAspect;
          vX = (cW - vW) / 2;  vY = 0;
        } else {
          // Letterboxed (black bars top/bottom)
          vW = cW;  vH = cW / vAspect;
          vX = 0;   vY = (cH - vH) / 2;
        }

        // Reposition subtitle overlay to cover only the actual video area,
        // so subtitles never appear in the letterbox/pillarbox black bars.
        subtitleOverlay.style.left   = vX + 'px';
        subtitleOverlay.style.top    = vY + 'px';
        subtitleOverlay.style.right  = 'auto';
        subtitleOverlay.style.bottom = 'auto';
        subtitleOverlay.style.width  = vW + 'px';
        subtitleOverlay.style.height = vH + 'px';

        // Scale fonts so fullscreen looks proportionally identical to render output.
        // Formula: fsScale = fs_displayed_width / normal_preview_width
        fsScale = previewWidthBeforeFs > 0 ? vW / previewWidthBeforeFs : 1;
        onStyleChange();
      });
    } else {
      // Restore overlay to full-container coverage (CSS inset:0 takes over)
      subtitleOverlay.style.left   = '';
      subtitleOverlay.style.top    = '';
      subtitleOverlay.style.right  = '';
      subtitleOverlay.style.bottom = '';
      subtitleOverlay.style.width  = '';
      subtitleOverlay.style.height = '';
      fsScale = 1;
      onStyleChange();
    }
  });
}

// ── Render Options Modal ───────────────────────────────────────────────────
function openRenderOptionsModal() {
  if (!activeJobId) return;

  const overlay  = document.getElementById('renderOptionsOverlay');
  const closeBtn = document.getElementById('renderOptionsClose');
  const cancelBtn = document.getElementById('renderOptionsCancel');
  const confirmBtn = document.getElementById('renderOptionsConfirm');
  const optRefined  = document.getElementById('renderOptRefined');
  const optOriginal = document.getElementById('renderOptOriginal');

  // Hide "original" option if original transcript is not available
  if (!originalTranscriptData || originalTranscriptData.length === 0) {
    optOriginal.style.display = 'none';
  } else {
    optOriginal.style.display = 'flex';
  }

  // Reset selection to refined
  optRefined.querySelector('input').checked = true;
  optRefined.classList.add('selected');
  optOriginal.classList.remove('selected');

  // Radio change → update card styling
  const radios = overlay.querySelectorAll('input[name="renderTranscriptSource"]');
  radios.forEach(r => {
    r.onchange = () => {
      optRefined.classList.toggle('selected', optRefined.querySelector('input').checked);
      optOriginal.classList.toggle('selected', optOriginal.querySelector('input').checked);
    };
  });

  overlay.classList.remove('hidden');

  const close = () => overlay.classList.add('hidden');
  closeBtn.onclick = close;
  cancelBtn.onclick = close;
  overlay.onclick = (e) => { if (e.target === overlay) close(); };

  confirmBtn.onclick = () => {
    const selected = overlay.querySelector('input[name="renderTranscriptSource"]:checked').value;
    close();
    startRender(selected);   // 'refined' or 'original'
  };
}

// ── Start Render ───────────────────────────────────────────────────────────
async function startRender(transcriptSource = 'refined') {
  if (!activeJobId) return;

  startRenderBtn.disabled = true;

  // Choose transcript based on user selection in render options modal
  const chosenTranscript = (transcriptSource === 'original' && originalTranscriptData && originalTranscriptData.length > 0)
    ? originalTranscriptData
    : transcriptData;

  // Collect style config to pass to render
  const styleConfig = collectStyle();
  styleConfig.animStyle = currentAnim;
  styleConfig.position  = currentPos;
  styleConfig.transcript = chosenTranscript; // chosen transcript source
  styleConfig.transcriptSource = transcriptSource; // inform backend which source

  // Scale font-related sizes from browser-display pixels to actual video pixels.
  // Use the actual displayed video area (not just clientWidth) to account for
  // letterboxing / pillarboxing when the video aspect ratio differs from the
  // container aspect ratio (object-fit: contain).
  const nativeWidth  = previewVideo.videoWidth  || 0;
  const nativeHeight = previewVideo.videoHeight || 0;
  let displayedWidth = previewVideo.clientWidth;  // fallback

  if (nativeWidth > 0 && nativeHeight > 0 && previewVideo.clientHeight > 0) {
    const vAspect = nativeWidth / nativeHeight;
    const cAspect = previewVideo.clientWidth / previewVideo.clientHeight;
    if (cAspect >= vAspect) {
      // Container is wider than video → black bars on sides, video fills full height
      displayedWidth = previewVideo.clientHeight * vAspect;
    } else {
      // Container aspect ≤ video aspect → video fills full width (no side bars)
      displayedWidth = previewVideo.clientWidth;
    }
  }

  if (displayedWidth > 0 && nativeWidth > 0) {
    const scale = nativeWidth / displayedWidth;
    styleConfig.fontSize    = Math.round(styleConfig.fontSize * scale);
    styleConfig.strokeWidth = Math.round((styleConfig.strokeWidth || 0) * scale);
    styleConfig.glowBlur    = Math.round((styleConfig.glowBlur    || 0) * scale);
  }

  try {
    // Build render phases display
    const phases = ['Translation', 'Subtitles', 'Mux'];
    renderPhases.innerHTML = phases.map((p, i) =>
      `<div class="render-phase-item" id="rphase-${i}">${p}</div>`
    ).join('');

    renderFile.textContent = selectedFile ? selectedFile.name : activeJobId;
    renderStatus.textContent = 'Starting render pipeline...';
    renderLog.textContent = '';

    showScreen('rendering');

    // Start render job via API
    const job = await apiFetch(`/api/jobs/${activeJobId}/render`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ style_config: styleConfig }),
    });

    // Watch render progress
    await watchRender(activeJobId);

  } catch (err) {
    // Fallback: if render API not implemented yet, show error but keep on screen
    renderStatus.textContent = 'Error: ' + err.message;
    alert('Render failed: ' + err.message);
    startRenderBtn.disabled = false;
    showScreen('preview');
  }
}

async function watchRender(jobId) {
  return new Promise((resolve, reject) => {
    const sse = new EventSource(`/api/jobs/${jobId}/log`);
    sse.onmessage = (e) => {
      const data = JSON.parse(e.data);
      if (data.line) {
        const line = document.createElement('div');
        line.textContent = data.line;
        renderLog.appendChild(line);
        renderLog.scrollTop = renderLog.scrollHeight;
      }
      if (data.done) sse.close();
    };
    sse.onerror = () => sse.close();

    const PHASE_LABELS = ['', 'Transcription', 'Translation', 'Subtitles', 'Mux'];

    const poll = setInterval(async () => {
      try {
        const job = await apiFetch(`/api/jobs/${jobId}`);

        if (job.phase_label) renderStatus.textContent = job.phase_label;

        // Update phase indicators (offset by 1 since phase 1 already done)
        const phaseIdx = (job.current_phase || 1) - 2; // 0-based for phases 2-4
        document.querySelectorAll('.render-phase-item').forEach((el, i) => {
          el.classList.remove('active', 'done');
          if (i < phaseIdx) el.classList.add('done');
          else if (i === phaseIdx) el.classList.add('active');
        });

        // Update render bar
        renderBar.style.animation = 'none';
        renderBar.style.width = job.progress_pct + '%';
        renderBar.style.marginLeft = '0';

        await loadJobs();

        if (job.status === 'completed') {
          clearInterval(poll);
          sse.close();
          renderStatus.textContent = 'Render complete!';
          renderBar.style.width = '100%';
          renderBar.style.background = 'var(--green)';

          // Add download button
          const dlBtn = document.createElement('a');
          dlBtn.href = `/api/download/${jobId}`;
          dlBtn.className = 'btn-success';
          dlBtn.style.cssText = 'margin-top: 20px; display: inline-flex; align-items: center; gap: 8px;';
          dlBtn.textContent = '⬇ Download Output';
          dlBtn.download = true;
          document.querySelector('#screen-rendering .transcribing-card').appendChild(dlBtn);

          resolve();
        } else if (job.status === 'failed') {
          clearInterval(poll);
          sse.close();
          renderStatus.textContent = 'Render failed: ' + (job.error || 'Unknown error');
          renderBar.style.background = 'var(--red)';
          reject(new Error(job.error));
        }
      } catch (err) {
        clearInterval(poll);
        sse.close();
        reject(err);
      }
    }, 2000);
  });
}

// ── Jobs List ──────────────────────────────────────────────────────────────
async function loadJobs() {
  try {
    const jobs = await apiFetch('/api/jobs');
    renderJobs(jobs);
  } catch (e) {
    console.error('Load jobs failed:', e);
  }
}

function setupJobsPanelToggle() {
  if (!jobsToggleBtns || jobsToggleBtns.length === 0) return;

  jobsToggleBtns.forEach(btn => {
    btn.addEventListener('click', () => {
      const tab = btn.dataset.jobsTab;
      if (!tab || tab === jobsPanelTab) return;
      switchJobsPanelTab(tab);
    });
  });
}

function switchJobsPanelTab(tab) {
  jobsPanelTab = tab;
  jobsToggleBtns.forEach(btn => btn.classList.toggle('active', btn.dataset.jobsTab === tab));

  if (jobsPane) jobsPane.classList.toggle('hidden', tab !== 'jobs');
  if (clipsPane) clipsPane.classList.toggle('hidden', tab !== 'clips');

  if (tab === 'clips') {
    loadClipJobsList();
  } else {
    loadJobs();
  }
}

function renderJobs(jobs) {
  if (!jobs.length) {
    jobsList.innerHTML = `
      <div class="empty-state">
        <div class="empty-icon">📋</div>
        <p>No jobs yet. Upload a video to get started.</p>
      </div>`;
    return;
  }

  jobsList.innerHTML = jobs.map(job => {
    const barClass = job.status === 'completed' ? 'done' : job.status === 'failed' ? 'failed' : '';
    const elapsed = job.completed_at && job.started_at
      ? ` · ${Math.round(job.completed_at - job.started_at)}s`
      : '';
    const hasTranscript = job.has_transcript === true;
    const actionsBtnHtml = hasTranscript ? `
      <div class="job-actions">
        <button class="btn-resume-job" onclick="event.stopPropagation(); resumeJob('${job.id}')">
          ▶ Continue Editing
        </button>
        <button class="btn-view-job" onclick="event.stopPropagation(); openModal('${job.id}')">
          Details
        </button>
      </div>` : '';
    return `
      <div class="job-card status-${job.status}" onclick="openModal('${job.id}')">
        <div class="job-top">
          <div class="job-name" title="${job.filename}">${job.filename}</div>
          <span class="job-badge badge-${job.status}">${job.status}</span>
        </div>
        <div class="job-meta">
          <span>🌐 ${job.target_language.toUpperCase()}</span>
          <span>📁 ${job.id.slice(0,8)}</span>
          <span>${timeAgo(job.created_at)}${elapsed}</span>
        </div>
        <div class="job-progress-wrap">
          <div class="job-progress-bar-bg">
            <div class="job-progress-bar ${barClass}" style="width:${job.progress_pct}%"></div>
          </div>
          <span class="job-progress-pct">${Math.round(job.progress_pct)}%</span>
        </div>
        ${actionsBtnHtml}
      </div>`;
  }).join('');
}

// ── Resume Job (Continue Editing from Recent Jobs) ────────────────────────
async function resumeJob(jobId) {
  try {
    const transcript = await fetchTranscript(jobId);
    if (!transcript || !transcript.length) {
      alert('Transcript belum tersedia untuk job ini.');
      return;
    }
    activeJobId = jobId;
    transcriptData = transcript;
    openPreviewScreen(jobId);
  } catch (e) {
    console.error('Resume job failed:', e);
    alert('Gagal membuka job untuk editing: ' + (e.message || e));
  }
}

async function loadClipJobsList() {
  if (!clipJobsList) return;

  clipJobsList.innerHTML = '<div class="clip-picker-loading">Loading clips...</div>';
  try {
    const data = await apiFetch('/api/clip-finder/available-clips');

    if (!data || data.length === 0) {
      clipJobsList.innerHTML = `
        <div class="empty-state">
          <div class="empty-icon">◻</div>
          <p>No clips available. Use Clip Finder to download clips first.</p>
        </div>`;
      return;
    }

    const clipCards = [];
    data.forEach(job => {
      (job.clips || []).forEach(clip => {
        const start = typeof clip.start === 'number' ? clip.start : 0;
        const end = typeof clip.end === 'number' ? clip.end : 0;
        const duration = Math.max(0, end - start);
        const durationLabel = duration > 0 ? formatClipDuration(duration) : '--';

        const title = clip.title || clip.filename || `Clip ${clip.index + 1}`;
        const sourceTitle = job.video_title || job.url || job.job_id;

        clipCards.push(`
          <div class="clip-job-card">
            <div class="clip-job-video-wrap">
              <video class="clip-job-video" preload="metadata" muted playsinline
                src="/api/clip-finder/clips/${encodeURIComponent(job.job_id)}/${clip.index}/stream"></video>
              <span class="clip-job-duration">${durationLabel}</span>
            </div>
            <div class="clip-job-info">
              <div class="clip-job-title">${escHtml(title)}</div>
              <div class="clip-job-meta">${escHtml(sourceTitle)}</div>
              <div class="clip-job-meta">${escHtml(clip.filename || '')}</div>
            </div>
            <button class="clip-job-use" data-path="${escHtml(clip.path)}" data-file="${escHtml(clip.filename || title)}">Use clip</button>
          </div>
        `);
      });
    });

    if (clipCards.length === 0) {
      clipJobsList.innerHTML = `
        <div class="empty-state">
          <div class="empty-icon">◻</div>
          <p>No clips available. Use Clip Finder to download clips first.</p>
        </div>`;
      return;
    }

    clipJobsList.innerHTML = clipCards.join('');

    clipJobsList.querySelectorAll('.clip-job-card').forEach(card => {
      const video = card.querySelector('.clip-job-video');
      const durationEl = card.querySelector('.clip-job-duration');
      if (video) {
        video.addEventListener('loadedmetadata', () => {
          if (!durationEl) return;
          const seconds = Number(video.duration);
          if (Number.isFinite(seconds) && seconds > 0) {
            durationEl.textContent = formatClipDuration(seconds);
          }
        });

        video.addEventListener('mouseenter', () => {
          video.play().catch(() => {});
        });
        video.addEventListener('mouseleave', () => {
          video.pause();
          video.currentTime = 0;
        });
      }
    });

    clipJobsList.querySelectorAll('.clip-job-use').forEach(btn => {
      btn.addEventListener('click', () => {
        startJobFromClip(btn.dataset.path, btn.dataset.file || 'clip.mp4');
      });
    });

  } catch (err) {
    clipJobsList.innerHTML = `<div class="clip-picker-empty">Failed to load clips: ${escHtml(err.message)}</div>`;
  }
}

function formatClipDuration(seconds) {
  const total = Math.max(0, Math.floor(seconds));
  const mins = Math.floor(total / 60);
  const secs = total % 60;
  if (mins > 0) return `${mins}m ${secs}s`;
  return `${secs}s`;
}

// ── Modal ──────────────────────────────────────────────────────────────────
function setupModal() {
  modalClose.addEventListener('click', closeModal);
  modalOverlay.addEventListener('click', (e) => {
    if (e.target === modalOverlay) closeModal();
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') closeModal();
  });
}

async function openModal(jobId) {
  activeJobId = jobId;
  modalOverlay.classList.remove('hidden');
  logBody.innerHTML = '';

  phaseDots.innerHTML = Array.from({length: 4}, (_, i) =>
    `<div class="phase-dot" id="dot-${i+1}"></div>`
  ).join('');

  await refreshModal(jobId);
  startSSE(jobId);
  pollInterval = setInterval(() => refreshModal(jobId), 2000);
}

function closeModal() {
  modalOverlay.classList.add('hidden');
  activeJobId = null;
  if (sseSource)    { sseSource.close(); sseSource = null; }
  if (pollInterval) { clearInterval(pollInterval); pollInterval = null; }
}

async function refreshModal(jobId) {
  try {
    const job = await apiFetch(`/api/jobs/${jobId}`);
    updateModalUI(job);
    await loadJobs();
  } catch (e) {
    console.error('Modal refresh failed:', e);
  }
}

function updateModalUI(job) {
  modalTitle.textContent = job.filename;
  modalId.textContent = `Job ID: ${job.id}`;
  modalPhase.textContent = job.phase_label;
  modalPct.textContent = `${Math.round(job.progress_pct)}%`;

  modalProgressBar.style.width = `${job.progress_pct}%`;
  modalProgressBar.className = 'progress-bar' +
    (job.status === 'completed' ? ' done' : job.status === 'failed' ? ' failed' : '');

  for (let i = 1; i <= 7; i++) {
    const dot = document.getElementById(`dot-${i}`);
    if (!dot) continue;
    dot.className = 'phase-dot';
    if (i < job.current_phase) dot.classList.add('done');
    else if (i === job.current_phase) dot.classList.add('active');
  }

  logBadge.className = 'log-badge';
  if (job.status === 'completed') { logBadge.textContent = 'DONE'; logBadge.classList.add('done'); }
  else if (job.status === 'failed') { logBadge.textContent = 'FAILED'; logBadge.classList.add('failed'); }
  else { logBadge.textContent = 'LIVE'; }

  renderModalActions(job);
}

function renderModalActions(job) {
  let html = '';
  if (job.status === 'completed' && job.output_file) {
    html += `<a href="/api/download/${job.id}" class="btn-success" download>⬇ Download Output</a>`;
  }
  if (job.status === 'running' || job.status === 'queued') {
    html += `<button class="btn-danger" onclick="cancelJob('${job.id}')">✕ Cancel</button>`;
  }
  html += `<button class="btn-secondary" onclick="deleteJob('${job.id}')">🗑 Remove</button>`;
  modalActions.innerHTML = html;
}

// ── SSE Log Streaming ──────────────────────────────────────────────────────
function startSSE(jobId) {
  if (sseSource) sseSource.close();
  sseSource = new EventSource(`/api/jobs/${jobId}/log`);

  sseSource.onmessage = (e) => {
    const data = JSON.parse(e.data);
    if (data.line) appendLog(data.line);
    if (data.done) { sseSource.close(); sseSource = null; }
  };
  sseSource.onerror = () => { sseSource.close(); sseSource = null; };
}

function appendLog(line) {
  const div = document.createElement('div');
  div.className = 'log-line';
  if (line.includes('✗') || line.includes('Error') || line.includes('failed')) div.classList.add('error');
  else if (line.includes('✓') || line.includes('complete')) div.classList.add('success');
  else if (line.includes('WARNING') || line.includes('warn')) div.classList.add('warn');
  div.textContent = line;
  logBody.appendChild(div);
  logBody.scrollTop = logBody.scrollHeight;
}

// ── Job Actions ────────────────────────────────────────────────────────────
async function cancelJob(jobId) {
  if (!confirm('Cancel this job?')) return;
  await apiFetch(`/api/jobs/${jobId}`, { method: 'DELETE' });
  closeModal();
  loadJobs();
}

async function deleteJob(jobId) {
  if (!confirm('Remove this job from the list?')) return;
  await apiFetch(`/api/jobs/${jobId}`, { method: 'DELETE' });
  closeModal();
  loadJobs();
}

// ── Helpers ────────────────────────────────────────────────────────────────
async function apiFetch(url, options = {}) {
  const res = await fetch(API + url, options);
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail || res.statusText);
  }
  return res.json();
}

function formatBytes(bytes) {
  if (bytes < 1024) return bytes + ' B';
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
  return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
}

function timeAgo(ts) {
  const diff = Math.floor(Date.now() / 1000 - ts);
  if (diff < 60) return `${diff}s ago`;
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  return `${Math.floor(diff / 3600)}h ago`;
}

function fmtTime(secs) {
  const m = Math.floor(secs / 60);
  const s = Math.floor(secs % 60);
  const ms = Math.round((secs % 1) * 10);
  return `${m}:${String(s).padStart(2,'0')}.${ms}`;
}

function escHtml(text) {
  const d = document.createElement('div');
  d.textContent = text;
  return d.innerHTML;
}

// ═══════════════════════════════════════════════════════════════════════════
// TIMELINE COMPONENT
// ═══════════════════════════════════════════════════════════════════════════

function setupTimeline() {
  // Zoom controls removed — use scroll-to-zoom on the track area

  // Add segment button
  addSegmentBtn.addEventListener('click', () => openSegmentDialog(previewVideo.currentTime));

  // Delete segment button
  deleteSegmentBtn.addEventListener('click', () => {
    if (selectedSegIdx !== null && selectedSegIdx < transcriptData.length) {
      if (confirm('Delete this subtitle segment?')) {
        transcriptData.splice(selectedSegIdx, 1);
        selectedSegIdx = null;
        deleteSegmentBtn.disabled = true;
        renderTimeline();
        renderTranscriptList();
        onStyleChange();
        scheduleAutoSave();
      }
    }
  });

  // Segment dialog
  cancelNewSeg.addEventListener('click', closeSegmentDialog);
  confirmNewSeg.addEventListener('click', confirmAddSegment);

  // Playhead drag on track area
  timelineTrackArea.addEventListener('mousedown', (e) => {
    // Check if clicking the playhead handle
    if (e.target.classList.contains('playhead-handle')) {
      draggingPlayhead = true;
      e.preventDefault();
      return;
    }
    // Check if clicking a segment handle
    if (e.target.classList.contains('seg-handle')) {
      const segEl = e.target.parentElement;
      const segIdx = parseInt(segEl.dataset.idx);
      const edge = e.target.classList.contains('seg-handle-left') ? 'start' : 'end';
      draggingSegEdge = { segIdx, edge };
      e.preventDefault();
      return;
    }
    // Click on segment to select + initiate body drag
    if (e.target.classList.contains('timeline-segment') || e.target.closest('.timeline-segment')) {
      const segEl = e.target.classList.contains('timeline-segment') ? e.target : e.target.closest('.timeline-segment');
      const segIdx = parseInt(segEl.dataset.idx);
      selectSegment(segIdx);
      previewVideo.currentTime = transcriptData[segIdx].start;
      // Initiate body drag to move segment in time
      const trackRect = timelineTrack.getBoundingClientRect();
      const x = e.clientX - trackRect.left;
      const trackWidth = getTrackWidth();
      const clickTime = (x / trackWidth) * videoDuration;
      const seg = transcriptData[segIdx];
      draggingSegBody = {
        segIdx,
        offsetTime: clickTime - seg.start,
        segDuration: seg.end - seg.start,
      };
      segEl.classList.add('dragging-body');
      e.preventDefault();
      return;
    }
    // Click on empty area — seek playhead
    const trackRect = timelineTrack.getBoundingClientRect();
    const x = e.clientX - trackRect.left;
    const trackWidth = getTrackWidth();
    const t = (x / trackWidth) * videoDuration;
    if (t >= 0 && t <= videoDuration) {
      previewVideo.currentTime = t;
    }
  });

  // Mouse move for playhead and segment edge dragging
  document.addEventListener('mousemove', (e) => {
    if (draggingPlayhead) {
      const trackRect = timelineTrack.getBoundingClientRect();
      const x = e.clientX - trackRect.left;
      const trackWidth = getTrackWidth();
      const t = Math.max(0, Math.min(videoDuration, (x / trackWidth) * videoDuration));
      previewVideo.currentTime = t;
    }
    if (draggingSegEdge) {
      const trackRect = timelineTrack.getBoundingClientRect();
      const x = e.clientX - trackRect.left;
      const trackWidth = getTrackWidth();
      const t = Math.max(0, Math.min(videoDuration, (x / trackWidth) * videoDuration));
      const seg = transcriptData[draggingSegEdge.segIdx];
      if (draggingSegEdge.edge === 'start') {
        seg.start = Math.min(t, seg.end - 0.1);
      } else {
        seg.end = Math.max(t, seg.start + 0.1);
      }
      renderTimeline();
    }
    // Segment body drag — move entire segment along timeline
    if (draggingSegBody) {
      const trackRect = timelineTrack.getBoundingClientRect();
      const x = e.clientX - trackRect.left;
      const trackWidth = getTrackWidth();
      const mouseTime = (x / trackWidth) * videoDuration;
      const newStart = Math.max(0, Math.min(videoDuration - draggingSegBody.segDuration, mouseTime - draggingSegBody.offsetTime));
      const seg = transcriptData[draggingSegBody.segIdx];
      seg.start = newStart;
      seg.end = newStart + draggingSegBody.segDuration;
      renderTimeline();
    }
    // Subtitle drag on video preview
    if (subtitleDragState) {
      const ds = subtitleDragState;
      const dx = e.clientX - ds.startX;
      const dy = e.clientY - ds.startY;
      const newPosX = Math.max(5, Math.min(95, ds.origPosX + (dx / ds.overlayRect.width) * 100));
      const newPosY = Math.max(5, Math.min(95, ds.origPosY + (dy / ds.overlayRect.height) * 100));
      const seg = transcriptData[ds.segIdx];
      seg.posX = newPosX;
      seg.pos_x = newPosX;
      seg.posY = newPosY;
      seg.pos_y = newPosY;
      seg.posOverride = true;
      seg.pos_override = true;
      // Directly update element position without full re-render to avoid visual jump
      const lineEl = subtitleContainer.querySelector(`[data-seg-idx="${ds.segIdx}"]`);
      if (lineEl) {
        lineEl.style.left = newPosX + '%';
        lineEl.style.top = newPosY + '%';
      }
    }
  });

  // Mouse up — stop all dragging
  document.addEventListener('mouseup', () => {
    if (draggingPlayhead) draggingPlayhead = false;
    if (draggingSegEdge) {
      draggingSegEdge = null;
      renderTranscriptList();
      scheduleAutoSave();
    }
    if (draggingSegBody) {
      // Remove dragging-body class from all segments
      timelineTrack.querySelectorAll('.timeline-segment.dragging-body').forEach(el => el.classList.remove('dragging-body'));
      draggingSegBody = null;
      renderTranscriptList();
      onStyleChange();
      scheduleAutoSave();
    }
    if (subtitleDragState) {
      // Remove dragging class from subtitle lines
      document.querySelectorAll('.subtitle-line.dragging').forEach(el => el.classList.remove('dragging'));
      subtitleDragState = null;
      onStyleChange(); // Final clean re-render with the new position
    }
  });

  // Keyboard shortcuts
  document.addEventListener('keydown', (e) => {
    // Only handle keys when preview screen is visible and not typing in input
    const previewScreen = document.getElementById('screen-preview');
    if (!previewScreen.classList.contains('active')) return;
    if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA' || e.target.isContentEditable) return;

    if (e.key === ' ') {
      e.preventDefault();
      if (previewVideo.paused) previewVideo.play();
      else previewVideo.pause();
    } else if (e.key === 'ArrowLeft') {
      e.preventDefault();
      previewVideo.currentTime = Math.max(0, previewVideo.currentTime - (e.shiftKey ? 5 : 1));
    } else if (e.key === 'ArrowRight') {
      e.preventDefault();
      previewVideo.currentTime = Math.min(videoDuration, previewVideo.currentTime + (e.shiftKey ? 5 : 1));
    } else if (e.key === 'Delete' || e.key === 'Backspace') {
      if (selectedSegIdx !== null && selectedSegIdx < transcriptData.length) {
        e.preventDefault();
        transcriptData.splice(selectedSegIdx, 1);
        selectedSegIdx = null;
        deleteSegmentBtn.disabled = true;
        renderTimeline();
        renderTranscriptList();
        onStyleChange();
        scheduleAutoSave();
      }
    } else if (e.key === 'f' || e.key === 'F') {
      e.preventDefault();
      toggleFullscreen();
    }
  });
}

function getTrackWidth() {
  return timelineScrollArea.clientWidth * timelineZoom;
}

function selectSegment(idx) {
  selectedSegIdx = idx;
  deleteSegmentBtn.disabled = false;
  // Highlight in timeline
  timelineTrack.querySelectorAll('.timeline-segment').forEach(el => {
    el.classList.toggle('selected', parseInt(el.dataset.idx) === idx);
  });
  // Also highlight in transcript list
  setActiveSeg(idx);
}

function renderTimeline() {
  if (!videoDuration || videoDuration <= 0) return;

  const ROW_HEIGHT   = 36;
  const ROW_PADDING  = 5;

  const trackWidth = getTrackWidth();
  timelineTrack.style.width    = trackWidth + 'px';
  timelineRuler.style.width    = trackWidth + 'px';
  timelineTrackArea.style.width = trackWidth + 'px';

  // ── Collect unique speakers in order of appearance ──
  const speakers = [];
  transcriptData.forEach(seg => {
    const sp = seg.speaker || 'SPEAKER_00';
    if (!speakers.includes(sp)) speakers.push(sp);
  });
  if (speakers.length === 0) speakers.push('SPEAKER_00');
  speakers.sort();

  const rowCount    = speakers.length;
  const trackHeight = rowCount * ROW_HEIGHT;

  // ── Update track area height ──
  timelineTrackArea.style.height = trackHeight + 'px';
  timelineTrack.style.height     = trackHeight + 'px';

  // ── Update labels column ──
  if (timelineLabelsCol) {
    timelineLabelsCol.innerHTML = '<div class="timeline-ruler-spacer"></div>';
    speakers.forEach(sp => {
      const spIdx = parseInt((sp.match(/\d+$/) || ['0'])[0], 10);
      const color = getSpeakerColor(sp);
      const labelRow = document.createElement('div');
      labelRow.className = 'timeline-label-row';
      labelRow.innerHTML = `<span style="color:${color}">S${spIdx}</span>`;
      timelineLabelsCol.appendChild(labelRow);
    });
  }

  // ── Clear old content ──
  timelineTrack.innerHTML = '';
  timelineRuler.innerHTML = '';

  // ── Draw row separator lines ──
  speakers.forEach((_sp, rowIdx) => {
    if (rowIdx > 0) {
      const sep = document.createElement('div');
      sep.className = 'timeline-row-line';
      sep.style.top = (rowIdx * ROW_HEIGHT) + 'px';
      timelineTrack.appendChild(sep);
    }
  });

  // ── Draw ruler ticks ──
  const pxPerSec = trackWidth / videoDuration;
  let tickInterval;
  if (pxPerSec > 50)      tickInterval = 1;
  else if (pxPerSec > 20) tickInterval = 2;
  else if (pxPerSec > 10) tickInterval = 5;
  else if (pxPerSec > 4)  tickInterval = 10;
  else                    tickInterval = 30;

  const majorEvery = tickInterval >= 10 ? 1 : (tickInterval >= 5 ? 2 : 5);

  for (let t = 0; t <= videoDuration; t += tickInterval) {
    const x = (t / videoDuration) * trackWidth;
    const tickCount = Math.round(t / tickInterval);
    const isMajor = tickCount % majorEvery === 0;

    const tick = document.createElement('div');
    tick.className = 'ruler-tick' + (isMajor ? ' major' : '');
    tick.style.left = x + 'px';
    timelineRuler.appendChild(tick);

    if (isMajor) {
      const label = document.createElement('span');
      label.className = 'ruler-label';
      label.style.left = x + 'px';
      label.textContent = fmtTimeShort(t);
      timelineRuler.appendChild(label);
    }
  }

  // ── Draw segment blocks ──
  transcriptData.forEach((seg, idx) => {
    const sp     = seg.speaker || 'SPEAKER_00';
    const rowIdx = speakers.indexOf(sp);
    const top    = rowIdx * ROW_HEIGHT + ROW_PADDING;
    const height = ROW_HEIGHT - ROW_PADDING * 2;

    const left  = (seg.start / videoDuration) * trackWidth;
    const width = Math.max(4, ((seg.end - seg.start) / videoDuration) * trackWidth);
    const color = getSpeakerColor(sp);

    const block = document.createElement('div');
    block.className = 'timeline-segment' + (selectedSegIdx === idx ? ' selected' : '');
    block.dataset.idx = idx;
    block.style.left   = left + 'px';
    block.style.width  = width + 'px';
    block.style.top    = top + 'px';
    block.style.height = height + 'px';
    block.style.background = color;
    block.title = `[${fmtTime(seg.start)} - ${fmtTime(seg.end)}] ${seg.text}`;

    // Text label (only if wide enough)
    if (width > 30) {
      block.textContent = seg.text.length > Math.floor(width / 6)
        ? seg.text.slice(0, Math.floor(width / 6)) + '…'
        : seg.text;
    }

    // Resize handles
    const handleL = document.createElement('div');
    handleL.className = 'seg-handle seg-handle-left';
    block.appendChild(handleL);

    const handleR = document.createElement('div');
    handleR.className = 'seg-handle seg-handle-right';
    block.appendChild(handleR);

    timelineTrack.appendChild(block);
  });

  // Re-add playhead element (it gets removed by innerHTML = '')
  const ph = document.createElement('div');
  ph.className = 'timeline-playhead';
  ph.id = 'timelinePlayhead';
  ph.innerHTML = '<div class="playhead-handle"></div><div class="playhead-line"></div>';
  timelineTrack.appendChild(ph);

  // Update playhead position
  updatePlayhead(previewVideo.currentTime);
}

function updatePlayhead(t) {
  const ph = document.getElementById('timelinePlayhead');
  if (!ph || !videoDuration) return;
  const trackWidth = getTrackWidth();
  const x = (t / videoDuration) * trackWidth;
  ph.style.left = x + 'px';
}

function updateTimeDisplay(t) {
  if (tlTimeDisplay) {
    tlTimeDisplay.textContent = `${fmtTimeShort(t)} / ${fmtTimeShort(videoDuration)}`;
  }
}

function fmtTimeShort(secs) {
  if (!secs || isNaN(secs)) return '0:00';
  const m = Math.floor(secs / 60);
  const s = Math.floor(secs % 60);
  return `${m}:${String(s).padStart(2, '0')}`;
}

// ── Segment Dialog ─────────────────────────────────────────────────────────

function openSegmentDialog(startTime) {
  const endTime = Math.min(startTime + 2, videoDuration);
  newSegStart.value = fmtTime(startTime);
  newSegEnd.value = fmtTime(endTime);
  newSegText.value = '';
  newSegSpeaker.value = 'SPEAKER_00';
  segmentDialog.classList.remove('hidden');
  newSegText.focus();
}

function closeSegmentDialog() {
  segmentDialog.classList.add('hidden');
}

function confirmAddSegment() {
  const start = parseTime(newSegStart.value);
  const end = parseTime(newSegEnd.value);
  const text = newSegText.value.trim();
  const speaker = newSegSpeaker.value;

  if (isNaN(start) || isNaN(end) || start >= end) {
    alert('Invalid time range.');
    return;
  }
  if (!text) {
    alert('Please enter subtitle text.');
    return;
  }

  const newSeg = { start, end, text, speaker, words: [] };

  // Insert sorted by start time
  let insertIdx = transcriptData.findIndex(s => s.start > start);
  if (insertIdx === -1) insertIdx = transcriptData.length;
  transcriptData.splice(insertIdx, 0, newSeg);

  closeSegmentDialog();
  renderTimeline();
  renderTranscriptList();
  selectSegment(insertIdx);
  scheduleAutoSave();
}

function parseTime(str) {
  // Parse "M:SS.s" or "M:SS" or just seconds
  str = str.trim();
  const parts = str.split(':');
  if (parts.length === 2) {
    const m = parseInt(parts[0]) || 0;
    const s = parseFloat(parts[1]) || 0;
    return m * 60 + s;
  }
  return parseFloat(str) || 0;
}

// ═══════════════════════════════════════════════════════════════════════════
// MERGE & SPLIT
// ═══════════════════════════════════════════════════════════════════════════

/**
 * Merge segment at `idx` with the segment immediately after it.
 * The merged segment inherits the speaker of the first segment.
 * Word timestamps (if present) are combined and sorted.
 */
function mergeSegmentWithNext(idx) {
  if (idx >= transcriptData.length - 1) return;
  const seg  = transcriptData[idx];
  const next = transcriptData[idx + 1];

  const merged = {
    start:   seg.start,
    end:     next.end,
    text:    seg.text.trim() + ' ' + next.text.trim(),
    speaker: seg.speaker,
    words:   [...(seg.words || []), ...(next.words || [])],
  };

  transcriptData.splice(idx, 2, merged);
  renderTimeline();
  renderTranscriptList();
  selectSegment(idx);
  onStyleChange();
  scheduleAutoSave();
}

/**
 * Open the split dialog for segment at `idx`.
 * Defaults to splitting at the middle word.
 */
function openSplitDialog(idx) {
  const seg   = transcriptData[idx];
  const words = seg.text.trim().split(/\s+/).filter(Boolean);
  if (words.length < 2) {
    alert('Cannot split a segment that contains only one word.');
    return;
  }
  splitDialogIdx     = idx;
  splitDialogWordIdx = Math.ceil(words.length / 2);
  renderSplitDialogContent();
  splitDialog.classList.remove('hidden');
}

function closeSplitDialog() {
  splitDialog.classList.add('hidden');
  splitDialogIdx     = null;
  splitDialogWordIdx = 1;
}

/**
 * Render the word tokens and preview rows inside the split dialog.
 * Called whenever the split point changes.
 */
function renderSplitDialogContent() {
  if (splitDialogIdx === null) return;
  const seg   = transcriptData[splitDialogIdx];
  const words = seg.text.trim().split(/\s+/).filter(Boolean);

  // Build word token HTML
  splitDialogWords.innerHTML = '';
  words.forEach((w, i) => {
    const span = document.createElement('span');
    span.className = 'split-word ' + (i < splitDialogWordIdx ? 'split-first' : 'split-second');
    span.textContent = w;
    span.title = 'Click to split before this word';
    // Clicking a word in second part: move split point BEFORE that word (i)
    // Clicking a word in first part: move split point AFTER that word (i+1)
    span.addEventListener('click', () => {
      const newIdx = i < splitDialogWordIdx ? i + 1 : i;
      if (newIdx <= 0 || newIdx >= words.length) return; // guard empty parts
      splitDialogWordIdx = newIdx;
      renderSplitDialogContent();
    });
    splitDialogWords.appendChild(span);

    // Visible divider at the split seam
    if (i === splitDialogWordIdx - 1) {
      const sep = document.createElement('span');
      sep.className = 'split-seam';
      sep.textContent = '|';
      splitDialogWords.appendChild(sep);
    }
  });

  // Update preview
  const splitTime = computeSplitTime(seg, splitDialogWordIdx);
  splitPart1Time.textContent = `${fmtTime(seg.start)} – ${fmtTime(splitTime)}`;
  splitPart1Text.textContent = words.slice(0, splitDialogWordIdx).join(' ');
  splitPart2Time.textContent = `${fmtTime(splitTime)} – ${fmtTime(seg.end)}`;
  splitPart2Text.textContent = words.slice(splitDialogWordIdx).join(' ');
}

/**
 * Compute the timestamp at which to split the segment.
 * Uses word-level timestamps when available; falls back to linear interpolation.
 */
function computeSplitTime(seg, wordIdx) {
  const words          = seg.text.trim().split(/\s+/).filter(Boolean);
  const wordTimestamps = seg.words || [];

  // Prefer start-time of the word that begins the second part
  if (wordTimestamps.length >= wordIdx && wordTimestamps[wordIdx]) {
    const t = wordTimestamps[wordIdx].start;
    if (t !== undefined && t > seg.start) return t;
  }
  // Also try end-time of the last word in the first part
  if (wordTimestamps.length >= wordIdx && wordTimestamps[wordIdx - 1]) {
    const t = wordTimestamps[wordIdx - 1].end;
    if (t !== undefined && t > seg.start) return t;
  }
  // Fallback: linear interpolation by word-count ratio
  const ratio = wordIdx / Math.max(words.length, 1);
  return seg.start + (seg.end - seg.start) * ratio;
}

/**
 * Perform the actual split, replacing the original segment with two new ones.
 */
function confirmSplit() {
  if (splitDialogIdx === null) return;
  const seg   = transcriptData[splitDialogIdx];
  const words = seg.text.trim().split(/\s+/).filter(Boolean);

  const firstText  = words.slice(0, splitDialogWordIdx).join(' ');
  const secondText = words.slice(splitDialogWordIdx).join(' ');
  if (!firstText || !secondText) {
    alert('Cannot create an empty segment. Adjust the split point.');
    return;
  }

  const splitTime      = computeSplitTime(seg, splitDialogWordIdx);
  const wordTimestamps = seg.words || [];

  const seg1 = {
    start:   seg.start,
    end:     splitTime,
    text:    firstText,
    speaker: seg.speaker,
    words:   wordTimestamps.slice(0, splitDialogWordIdx),
  };
  const seg2 = {
    start:   splitTime,
    end:     seg.end,
    text:    secondText,
    speaker: seg.speaker,
    words:   wordTimestamps.slice(splitDialogWordIdx),
  };

  transcriptData.splice(splitDialogIdx, 1, seg1, seg2);
  closeSplitDialog();
  renderTimeline();
  renderTranscriptList();
  selectSegment(splitDialogIdx);
  onStyleChange();
  scheduleAutoSave();
}

function setupSplitDialog() {
  cancelSplitSeg.addEventListener('click', closeSplitDialog);
  confirmSplitSeg.addEventListener('click', confirmSplit);
}

// ═══════════════════════════════════════════════════════════════════════════
// AE EXPORT
// ═══════════════════════════════════════════════════════════════════════════

function setupAEExport() {
  exportAEBtn.addEventListener('click', async () => {
    if (!activeJobId) {
      alert('No active job to export.');
      return;
    }

    const styleConfig = collectStyle();
    styleConfig.animStyle = currentAnim;
    styleConfig.position = currentPos;
    styleConfig.transcript = transcriptData;
    styleConfig.videoDuration = videoDuration || 60;
    styleConfig.videoWidth = previewVideo.videoWidth || 1920;
    styleConfig.videoHeight = previewVideo.videoHeight || 1080;
    styleConfig.fps = 30;

    // Scale font sizes from display pixels to native video pixels so that
    // the JSX composition matches the proportional size seen in preview.
    // Mirrors the same calculation used in startRender().
    const nativeWidth  = previewVideo.videoWidth  || 0;
    const nativeHeight = previewVideo.videoHeight || 0;
    let displayedWidth = previewVideo.clientWidth;

    if (nativeWidth > 0 && nativeHeight > 0 && previewVideo.clientHeight > 0) {
      const vAspect = nativeWidth / nativeHeight;
      const cAspect = previewVideo.clientWidth / previewVideo.clientHeight;
      if (cAspect >= vAspect) {
        displayedWidth = previewVideo.clientHeight * vAspect;
      } else {
        displayedWidth = previewVideo.clientWidth;
      }
    }

    if (displayedWidth > 0 && nativeWidth > 0) {
      const scale = nativeWidth / displayedWidth;
      styleConfig.fontSize    = Math.round(styleConfig.fontSize * scale);
      styleConfig.strokeWidth = Math.round((styleConfig.strokeWidth || 0) * scale);
      styleConfig.glowBlur    = Math.round((styleConfig.glowBlur    || 0) * scale);
    }

    exportAEBtn.disabled = true;
    exportAEBtn.querySelector('.btn-text').textContent = 'Exporting...';

    try {
      const res = await fetch(`/api/jobs/${activeJobId}/export-ae`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ style_config: styleConfig }),
      });

      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: 'Export failed' }));
        throw new Error(err.detail || 'Export failed');
      }

      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `subtitles_${activeJobId}.jsx`;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);

    } catch (err) {
      alert('AE Export failed: ' + err.message);
    } finally {
      exportAEBtn.disabled = false;
      exportAEBtn.querySelector('.btn-text').textContent = 'Export .jsx';
    }
  });
}


// ═══════════════════════════════════════════════════════════════════════════
// CLIP PICKER — Use existing Clip Finder videos for Auto Subtitle
// ═══════════════════════════════════════════════════════════════════════════

let clipPickerOpen = false;
let clipPickerLoaded = false;

function setupClipPicker() {
  const btn = document.getElementById('clipPickerBtn');
  const list = document.getElementById('clipPickerList');

  btn.addEventListener('click', async () => {
    clipPickerOpen = !clipPickerOpen;
    list.classList.toggle('hidden', !clipPickerOpen);

    if (clipPickerOpen) {
      clipPickerLoaded = false;
      await loadClipPickerList();
    }
  });
}

async function loadClipPickerList() {
  const list = document.getElementById('clipPickerList');
  list.innerHTML = '<div class="clip-picker-loading">Loading clips...</div>';

  try {
    const data = await apiFetch('/api/clip-finder/available-clips');

    if (!data || data.length === 0) {
      list.innerHTML = '<div class="clip-picker-empty">No clips available. Use Clip Finder to download clips first.</div>';
      return;
    }

    clipPickerLoaded = true;
    list.innerHTML = '';

    data.forEach(job => {
      // Job group header
      const header = document.createElement('div');
      header.className = 'clip-picker-group-header';
      header.innerHTML = `
        <span class="clip-picker-group-title">${escHtml(job.video_title || job.url || job.job_id)}</span>
        <span class="clip-picker-group-count">${job.clip_count} clip${job.clip_count !== 1 ? 's' : ''}</span>
      `;
      list.appendChild(header);

      // Individual clips
      job.clips.forEach(clip => {
        const item = document.createElement('div');
        item.className = 'clip-picker-item';

        const sizeMB = (clip.size / (1024 * 1024)).toFixed(1);
        const title = clip.title || clip.filename;
        let timeInfo = '';
        if (clip.start !== undefined && clip.end !== undefined) {
          const duration = clip.end - clip.start;
          const durFmt = duration >= 60
            ? Math.floor(duration / 60) + 'm ' + Math.floor(duration % 60) + 's'
            : Math.floor(duration) + 's';
          timeInfo = `<span class="clip-picker-time">${durFmt}</span>`;
        }

        item.innerHTML = `
          <div class="clip-picker-item-icon">
            <svg width="20" height="20" viewBox="0 0 20 20" fill="none">
              <rect x="2" y="4" width="16" height="12" rx="2" stroke="currentColor" stroke-width="1.5"/>
              <path d="M8 7.5l5 2.5-5 2.5v-5z" fill="currentColor"/>
            </svg>
          </div>
          <div class="clip-picker-item-info">
            <div class="clip-picker-item-title">${escHtml(title)}</div>
            <div class="clip-picker-item-meta">${clip.filename} · ${sizeMB} MB ${timeInfo}</div>
          </div>
          <button class="clip-picker-use-btn" data-path="${escHtml(clip.path)}">Use</button>
        `;

        item.querySelector('.clip-picker-use-btn').addEventListener('click', (e) => {
          e.stopPropagation();
          startJobFromClip(clip.path, clip.filename);
        });

        list.appendChild(item);
      });
    });

  } catch (err) {
    list.innerHTML = `<div class="clip-picker-empty">Failed to load clips: ${escHtml(err.message)}</div>`;
  }
}

async function startJobFromClip(clipPath, clipFilename) {
  const transcribeBtnEl = document.getElementById('transcribeBtn');
  transcribeBtnEl.disabled = true;
  transcribeBtnEl.classList.add('loading');
  transcribeBtnEl.querySelector('.btn-text').textContent = 'Starting...';

  try {
    const formData = new FormData();
    formData.append('clip_path', clipPath);
    formData.append('target_language', targetLang.value);
    formData.append('speaker_detection', speakerDetectionEnabled.checked);
    formData.append('whisper_model', whisperModel.value);

    if (speakerDetectionEnabled.checked && numSpeakersEnabled.checked) {
      formData.append('num_speakers', numSpeakersCount);
    }

    const job = await apiFetch('/api/jobs/from-clip', { method: 'POST', body: formData });
    activeJobId = job.id;

    // Show transcribing screen
    const modelLabel = whisperModel.options[whisperModel.selectedIndex].text;
    transcribingFile.textContent = clipFilename;
    transcribingStatus.textContent = `Running transcription with ${modelLabel}...`;
    transcribingLog.textContent = '';
    showScreen('transcribing');

    // Watch job until phase 1 completes
    await watchTranscription(job.id);

  } catch (err) {
    alert('Failed to start transcription: ' + err.message);
    showScreen('upload');
  } finally {
    transcribeBtnEl.classList.remove('loading');
    transcribeBtnEl.querySelector('.btn-text').textContent = 'Transcribe & Preview';
    transcribeBtnEl.disabled = !selectedFile;
  }
}


// ═══════════════════════════════════════════════════════════════════════════════
// CLIP FINDER — YouTube clip detection via yt-dlp + Gemini AI (2-phase flow)
// Phase 1: Extract transcript + Gemini analysis → show results (no video download)
// Phase 2: Download only the relevant clip sections on demand
// ═══════════════════════════════════════════════════════════════════════════════

let cfJobId    = null;
let cfSSE      = null;

function setupNavTabs() {
  document.querySelectorAll('.nav-tab').forEach(btn => {
    btn.addEventListener('click', () => switchTab(btn.dataset.tab));
  });
}

function setupClipFinder() {
  const cfUrl          = document.getElementById('cfUrl');
  const cfInstructions = document.getElementById('cfInstructions');
  const cfLang         = document.getElementById('cfLang');
  const cfStartOffset  = document.getElementById('cfStartOffset');
  const cfFindBtn      = document.getElementById('cfFindBtn');
  const cfDownloadAllBtn = document.getElementById('cfDownloadAllBtn');
  const cfPresetVtuber = document.getElementById('cfPresetVtuber');
  const cfPresetClear  = document.getElementById('cfPresetClear');

  // Enable/disable find button
  function updateFindBtn() {
    cfFindBtn.disabled = !cfUrl.value.trim();
  }
  cfUrl.addEventListener('input', updateFindBtn);
  cfInstructions.addEventListener('input', () => {
    updateFindBtn();
    // Show Clear button when textarea has content
    const hasContent = cfInstructions.value.trim().length > 0;
    cfPresetClear.classList.toggle('hidden', !hasContent);
    // Deactivate preset chip if user manually edits
    if (cfInstructions.value !== VTUBER_HIGHLIGHTS_PRESET) {
      cfPresetVtuber.classList.remove('active');
    }
  });
  updateFindBtn();

  // VTuber Highlights preset chip
  cfPresetVtuber.addEventListener('click', () => {
    const isActive = cfPresetVtuber.classList.contains('active');
    if (isActive) {
      // Toggle off — clear the field
      cfInstructions.value = '';
      cfPresetVtuber.classList.remove('active');
      cfPresetClear.classList.add('hidden');
    } else {
      cfInstructions.value = VTUBER_HIGHLIGHTS_PRESET;
      cfPresetVtuber.classList.add('active');
      cfPresetClear.classList.remove('hidden');
    }
    cfInstructions.dispatchEvent(new Event('input'));
  });

  // Clear preset chip
  cfPresetClear.addEventListener('click', () => {
    cfInstructions.value = '';
    cfPresetVtuber.classList.remove('active');
    cfPresetClear.classList.add('hidden');
    cfInstructions.dispatchEvent(new Event('input'));
  });

  // Find Clips button — triggers Phase 1 (transcript + AI analysis)
  cfFindBtn.addEventListener('click', async () => {
    const url = cfUrl.value.trim();
    const instructions = cfInstructions.value.trim();
    const lang = cfLang.value;

    // Parse start offset (supports "M:SS", "H:MM:SS", or plain seconds)
    let startOffset = 0;
    const offsetRaw = cfStartOffset.value.trim();
    if (offsetRaw) {
      startOffset = cfParseTimeInput(offsetRaw);
      if (isNaN(startOffset) || startOffset < 0) {
        alert('Invalid start time format. Use M:SS (e.g. 5:00) or seconds (e.g. 300).');
        return;
      }
    }

    if (!url) return;

    cfFindBtn.disabled = true;
    cfFindBtn.querySelector('.btn-text').textContent = 'Starting...';

    try {
      const res = await fetch('/api/clip-finder/jobs', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url, instructions, lang, start_offset: startOffset }),
      });

      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: 'Request failed' }));
        throw new Error(err.detail || 'Failed to start clip finder');
      }

      const job = await res.json();
      cfJobId = job.id;

      // Show progress
      cfShowProgress();
      cfStartSSE(job.id);

    } catch (err) {
      alert('Error: ' + err.message);
      cfFindBtn.disabled = false;
      cfFindBtn.querySelector('.btn-text').textContent = 'Find Clips';
    }
  });

  // Download All Clips button — triggers Phase 2 (selective download)
  cfDownloadAllBtn.addEventListener('click', async () => {
    if (!cfJobId) return;

    cfDownloadAllBtn.disabled = true;
    cfDownloadAllBtn.classList.add('loading');
    cfDownloadAllBtn.querySelector('.btn-text').textContent = 'Downloading...';

    try {
      const res = await fetch(`/api/clip-finder/jobs/${cfJobId}/download-clips`, {
        method: 'POST',
      });

      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: 'Download failed' }));
        throw new Error(err.detail || 'Failed to start download');
      }

      // Re-connect SSE for download progress
      cfStartSSE(cfJobId);

    } catch (err) {
      alert('Error: ' + err.message);
      cfDownloadAllBtn.disabled = false;
      cfDownloadAllBtn.classList.remove('loading');
      cfDownloadAllBtn.querySelector('.btn-text').textContent = 'Download All Clips';
    }
  });
}

function cfShowProgress() {
  const progress = document.getElementById('cfProgress');
  const grid     = document.getElementById('cfClipsGrid');
  const dlAll    = document.getElementById('cfDownloadAll');

  progress.classList.remove('hidden');
  dlAll.classList.add('hidden');
  grid.innerHTML = '';

  // Reset steps (now 3 steps)
  for (let i = 1; i <= 3; i++) {
    const step = document.getElementById('cfStep' + i);
    if (step) step.className = 'cf-step';
  }

  document.getElementById('cfProgressBar').style.width = '0%';
  document.getElementById('cfLog').innerHTML = '';
}

function cfUpdateSteps(status) {
  // New 3-step mapping for Phase 1
  const stepMap = {
    transcribing: 1,
    analyzing:    2,
    analyzed:     3,
    downloading:  3,  // Phase 2 — keep step 3 active
    completed:    3,
  };

  const activeStep = stepMap[status] || 0;

  for (let i = 1; i <= 3; i++) {
    const step = document.getElementById('cfStep' + i);
    if (!step) continue;
    step.classList.remove('active', 'done');
    if (i < activeStep) step.classList.add('done');
    else if (i === activeStep) {
      // Mark as done if analyzed/completed, active if still processing
      if (status === 'analyzed' || status === 'completed') {
        step.classList.add('done');
      } else {
        step.classList.add('active');
      }
    }
  }

  // Update step lines (now 2 lines for 3 steps)
  document.querySelectorAll('.cf-step-line').forEach((line, idx) => {
    line.classList.toggle('done', (idx + 1) < activeStep);
  });
}

function cfStartSSE(jobId) {
  if (cfSSE) cfSSE.close();

  cfSSE = new EventSource(`/api/clip-finder/jobs/${jobId}/log`);
  const logEl = document.getElementById('cfLog');

  cfSSE.onmessage = (ev) => {
    try {
      const data = JSON.parse(ev.data);
      if (data.line) {
        const div = document.createElement('div');
        div.className = 'cf-log-line';
        div.textContent = data.line;
        logEl.appendChild(div);
        logEl.scrollTop = logEl.scrollHeight;
      }
      if (data.done) {
        cfSSE.close();
        cfSSE = null;
        cfLoadResults(jobId);
      }
    } catch (_) {}
  };

  cfSSE.onerror = () => {
    cfSSE.close();
    cfSSE = null;
    // Poll for results in case SSE disconnected
    setTimeout(() => cfLoadResults(jobId), 1000);
  };

  // Poll job status for progress updates
  cfPollStatus(jobId);
}

async function cfPollStatus(jobId) {
  const bar     = document.getElementById('cfProgressBar');
  const findBtn = document.getElementById('cfFindBtn');

  while (true) {
    try {
      const res = await fetch(`/api/clip-finder/jobs/${jobId}`);
      if (!res.ok) break;
      const job = await res.json();

      bar.style.width = job.progress_pct + '%';
      cfUpdateSteps(job.status);

      if (job.status === 'analyzed' || job.status === 'completed' || job.status === 'failed') {
        findBtn.disabled = false;
        findBtn.querySelector('.btn-text').textContent = 'Find Clips';

        // Update download button state
        const dlBtn = document.getElementById('cfDownloadAllBtn');
        if (job.status === 'completed') {
          dlBtn.classList.remove('loading');
          dlBtn.querySelector('.btn-text').textContent = 'All Clips Downloaded';
          dlBtn.disabled = true;
        }

        // Render clip results in the UI
        await cfLoadResults(jobId);
        break;
      }
    } catch (_) {
      break;
    }
    await new Promise(r => setTimeout(r, 1500));
  }
}

async function cfLoadResults(jobId) {
  try {
    const res = await fetch(`/api/clip-finder/jobs/${jobId}`);
    if (!res.ok) return;
    const job = await res.json();

    const bar      = document.getElementById('cfProgressBar');
    const countBdg = document.getElementById('cfResultCount');
    const dlAll    = document.getElementById('cfDownloadAll');
    const dlBtn    = document.getElementById('cfDownloadAllBtn');

    bar.style.width = '100%';
    cfUpdateSteps(job.status);

    if (job.status === 'failed') {
      countBdg.textContent = 'Failed';
      dlAll.classList.add('hidden');
      cfRenderError(job.error || 'An unknown error occurred');
      return;
    }

    const clipCount = job.clips ? job.clips.length : 0;
    countBdg.textContent = clipCount + ' clip' + (clipCount !== 1 ? 's' : '');

    if (clipCount === 0) {
      dlAll.classList.add('hidden');
      cfRenderEmpty('No matching clips found for your instructions. Try different instructions.');
      return;
    }

    if (job.status === 'analyzed') {
      // Phase 1 complete — show cards with per-clip download buttons
      const downloadedCount = job.clip_files ? job.clip_files.filter(f => f).length : 0;
      const remainingCount = clipCount - downloadedCount;

      dlAll.classList.remove('hidden');
      dlBtn.classList.remove('loading');

      if (remainingCount > 0) {
        dlBtn.disabled = false;
        dlBtn.querySelector('.btn-text').textContent =
          `Download ${remainingCount === clipCount ? 'All ' : ''}${remainingCount} Clip${remainingCount !== 1 ? 's' : ''}`;
      } else {
        // All individually downloaded
        dlBtn.disabled = true;
        dlBtn.querySelector('.btn-text').textContent = 'All Clips Downloaded';
      }

      cfRenderClipsInfoOnly(job);
    } else if (job.status === 'completed') {
      // Phase 2 complete — show full cards with video preview
      dlAll.classList.remove('hidden');
      dlBtn.disabled = false;
      dlBtn.classList.remove('loading');
      dlBtn.querySelector('.btn-text').textContent = 'All Clips Downloaded';
      dlBtn.disabled = true;
      cfRenderClipsInfoOnly(job);
    } else if (job.status === 'downloading') {
      // Still downloading
      dlAll.classList.remove('hidden');
      dlBtn.disabled = true;
      dlBtn.classList.add('loading');
      dlBtn.querySelector('.btn-text').textContent = 'Downloading...';
      cfRenderClipsInfoOnly(job);
    }

  } catch (err) {
    cfRenderError('Failed to load results: ' + err.message);
  }
}

/**
 * Render clip cards with info only (no video preview).
 * Shown after Phase 1 (analysis) before user clicks Download.
 * Each card has an individual download button.
 */
/**
 * Return a styled HTML badge string for a VTuber highlight type.
 * Returns empty string when type is absent or 'other'.
 */
function cfHighlightBadge(type) {
  const labels = {
    karma_arc:        '🎭 Karma Arc',
    genuine_reaction: '😂 Genuine Reaction',
    clutch_play:      '🎮 Clutch Play',
    chaotic_plea:     '😱 Chaotic Plea',
  };
  const label = labels[type];
  if (!label) return '';
  return `<span class="cf-clip-badge cf-clip-badge--${escapeHtml(type)}">${label}</span>`;
}

/**
 * Return a dead-air note HTML string if the clip has flagged silence timestamps.
 */
function cfDeadAirNote(clip) {
  const ts = clip.dead_air_timestamps;
  if (!Array.isArray(ts) || ts.length === 0) return '';
  const times = ts.map(t => cfFmtTime(t)).join(', ');
  return `<div class="cf-dead-air-note">✂ Edit points (silence &gt;5s): ${escapeHtml(times)}</div>`;
}

function cfRenderClipsInfoOnly(job) {
  const grid = document.getElementById('cfClipsGrid');
  grid.innerHTML = '';

  job.clips.forEach((clip, idx) => {
    const card = document.createElement('div');
    card.className = 'cf-clip-card';

    const startFmt = cfFmtTime(clip.start);
    const endFmt   = cfFmtTime(clip.end);
    const duration = clip.end - clip.start;
    const durFmt   = duration >= 60
      ? Math.floor(duration / 60) + 'm ' + Math.floor(duration % 60) + 's'
      : Math.floor(duration) + 's';

    const badge    = cfHighlightBadge(clip.highlight_type);
    const deadAir  = cfDeadAirNote(clip);

    // Check if this clip is already downloaded
    const isDownloaded = job.clip_files && job.clip_files[idx];

    if (isDownloaded) {
      // Render with video preview + file download link
      card.innerHTML = `
        <div class="cf-clip-video-wrap">
          <video class="cf-clip-video" preload="metadata"
                 src="/api/clip-finder/clips/${job.id}/${idx}/stream">
          </video>
          <button class="cf-clip-play-btn" data-idx="${idx}">
            <svg width="24" height="24" viewBox="0 0 24 24" fill="white">
              <path d="M8 5v14l11-7z"/>
            </svg>
          </button>
          <span class="cf-clip-duration">${durFmt}</span>
        </div>
        <div class="cf-clip-info">
          <div class="cf-clip-number">#${idx + 1}</div>
          ${badge}
          <div class="cf-clip-title">${escapeHtml(clip.title || 'Clip ' + (idx + 1))}</div>
          <div class="cf-clip-time">${startFmt} - ${endFmt}</div>
          ${clip.reason ? `<div class="cf-clip-reason">${escapeHtml(clip.reason)}</div>` : ''}
          ${deadAir}
        </div>
        <div class="cf-clip-actions">
          <a class="cf-clip-download" href="/api/clip-finder/clips/${job.id}/${idx}" download>
            <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
              <path d="M7 1v9M3 7l4 4 4-4M1 13h12" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
            </svg>
            Download
          </a>
        </div>
      `;

      // Play/pause toggle for downloaded clips
      const video   = card.querySelector('.cf-clip-video');
      const playBtn = card.querySelector('.cf-clip-play-btn');
      playBtn.addEventListener('click', () => {
        if (video.paused) {
          document.querySelectorAll('.cf-clip-video').forEach(v => { if (v !== video) v.pause(); });
          video.play();
          playBtn.classList.add('playing');
        } else {
          video.pause();
          playBtn.classList.remove('playing');
        }
      });
      video.addEventListener('ended', () => playBtn.classList.remove('playing'));
    } else {
      // Render info-only card with individual download button
      card.innerHTML = `
        <div class="cf-clip-placeholder">
          <svg width="40" height="40" viewBox="0 0 40 40" fill="none">
            <rect x="4" y="8" width="32" height="24" rx="3" stroke="currentColor" stroke-width="1.5" opacity="0.4"/>
            <path d="M16 15l10 5-10 5V15z" fill="currentColor" opacity="0.4"/>
          </svg>
        </div>
        <div class="cf-clip-info">
          <div class="cf-clip-number">#${idx + 1}</div>
          ${badge}
          <div class="cf-clip-title">${escapeHtml(clip.title || 'Clip ' + (idx + 1))}</div>
          <div class="cf-clip-time">${startFmt} - ${endFmt} (${durFmt})</div>
          ${clip.reason ? `<div class="cf-clip-reason">${escapeHtml(clip.reason)}</div>` : ''}
          ${deadAir}
        </div>
        <div class="cf-clip-actions">
          <button class="cf-clip-download cf-clip-dl-single" data-clip-idx="${idx}">
            <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
              <path d="M7 1v9M3 7l4 4 4-4M1 13h12" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
            </svg>
            Download
          </button>
        </div>
      `;
    }

    grid.appendChild(card);
  });

  // Attach click handlers for individual download buttons
  grid.querySelectorAll('.cf-clip-dl-single').forEach(btn => {
    btn.addEventListener('click', () => cfDownloadSingleClip(btn, job.id));
  });
}

/**
 * Render clip cards with video preview and download links.
 * Shown after Phase 2 (download) completes.
 */
function cfRenderClips(job) {
  const grid = document.getElementById('cfClipsGrid');
  grid.innerHTML = '';

  job.clips.forEach((clip, idx) => {
    if (idx >= job.clip_files.length) return;

    const card = document.createElement('div');
    card.className = 'cf-clip-card';

    const startFmt = cfFmtTime(clip.start);
    const endFmt   = cfFmtTime(clip.end);
    const duration = clip.end - clip.start;
    const durFmt   = duration >= 60
      ? Math.floor(duration / 60) + 'm ' + Math.floor(duration % 60) + 's'
      : Math.floor(duration) + 's';

    const badge   = cfHighlightBadge(clip.highlight_type);
    const deadAir = cfDeadAirNote(clip);

    card.innerHTML = `
      <div class="cf-clip-video-wrap">
        <video class="cf-clip-video" preload="metadata"
               src="/api/clip-finder/clips/${job.id}/${idx}/stream">
        </video>
        <button class="cf-clip-play-btn" data-idx="${idx}">
          <svg width="24" height="24" viewBox="0 0 24 24" fill="white">
            <path d="M8 5v14l11-7z"/>
          </svg>
        </button>
        <span class="cf-clip-duration">${durFmt}</span>
      </div>
      <div class="cf-clip-info">
        <div class="cf-clip-number">#${idx + 1}</div>
        ${badge}
        <div class="cf-clip-title">${escapeHtml(clip.title || 'Clip ' + (idx + 1))}</div>
        <div class="cf-clip-time">${startFmt} - ${endFmt}</div>
        ${clip.reason ? `<div class="cf-clip-reason">${escapeHtml(clip.reason)}</div>` : ''}
        ${deadAir}
      </div>
      <div class="cf-clip-actions">
        <a class="cf-clip-download" href="/api/clip-finder/clips/${job.id}/${idx}" download>
          <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
            <path d="M7 1v9M3 7l4 4 4-4M1 13h12" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
          </svg>
          Download
        </a>
      </div>
    `;

    // Play/pause toggle
    const video   = card.querySelector('.cf-clip-video');
    const playBtn = card.querySelector('.cf-clip-play-btn');

    playBtn.addEventListener('click', () => {
      if (video.paused) {
        // Pause all other videos
        document.querySelectorAll('.cf-clip-video').forEach(v => {
          if (v !== video) v.pause();
        });
        video.play();
        playBtn.classList.add('playing');
      } else {
        video.pause();
        playBtn.classList.remove('playing');
      }
    });

    video.addEventListener('ended', () => {
      playBtn.classList.remove('playing');
    });

    video.addEventListener('pause', () => {
      playBtn.classList.remove('playing');
    });

    grid.appendChild(card);
  });
}

/**
 * Download a single clip by index.
 * Updates the button to show loading state, polls until done, then re-renders.
 */
async function cfDownloadSingleClip(btn, jobId) {
  const clipIdx = parseInt(btn.dataset.clipIdx, 10);
  if (isNaN(clipIdx)) return;

  // Disable button and show loading state
  btn.disabled = true;
  btn.innerHTML = `
    <svg class="cf-spin" width="14" height="14" viewBox="0 0 14 14" fill="none">
      <circle cx="7" cy="7" r="5.5" stroke="currentColor" stroke-width="1.5" stroke-dasharray="20 12" stroke-linecap="round"/>
    </svg>
    Downloading...
  `;

  try {
    const res = await fetch(`/api/clip-finder/jobs/${jobId}/download-clip/${clipIdx}`, {
      method: 'POST',
    });

    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: 'Download failed' }));
      throw new Error(err.detail || 'Failed to start download');
    }

    const data = await res.json();
    if (data.status === 'already_downloaded') {
      // Already done — just reload
      await cfLoadResults(jobId);
      return;
    }

    // Poll until this clip is downloaded
    for (let attempt = 0; attempt < 600; attempt++) {
      await new Promise(r => setTimeout(r, 2000));
      const pollRes = await fetch(`/api/clip-finder/jobs/${jobId}`);
      if (!pollRes.ok) continue;
      const job = await pollRes.json();

      if (job.clip_files && job.clip_files[clipIdx]) {
        // Clip is ready — re-render all cards
        await cfLoadResults(jobId);
        return;
      }

      if (job.status === 'failed') {
        throw new Error(job.error || 'Download failed');
      }
    }

    throw new Error('Download timed out (>20 menit)');
  } catch (err) {
    alert('Error: ' + err.message);
    btn.disabled = false;
    btn.innerHTML = `
      <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
        <path d="M7 1v9M3 7l4 4 4-4M1 13h12" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
      </svg>
      Download
    `;
  }
}

function cfRenderError(msg) {
  const grid = document.getElementById('cfClipsGrid');
  grid.innerHTML = `
    <div class="cf-empty-state cf-error-state">
      <div class="cf-empty-icon" style="color: var(--red);">
        <svg width="48" height="48" viewBox="0 0 48 48" fill="none">
          <circle cx="24" cy="24" r="20" stroke="currentColor" stroke-width="2"/>
          <path d="M16 16l16 16M32 16L16 32" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>
        </svg>
      </div>
      <p class="cf-empty-title">Error</p>
      <p class="cf-empty-sub">${escapeHtml(msg)}</p>
    </div>
  `;
}

function cfRenderEmpty(msg) {
  const grid = document.getElementById('cfClipsGrid');
  grid.innerHTML = `
    <div class="cf-empty-state">
      <div class="cf-empty-icon">
        <svg width="48" height="48" viewBox="0 0 48 48" fill="none">
          <rect x="6" y="10" width="36" height="28" rx="4" stroke="currentColor" stroke-width="2" opacity="0.3"/>
          <path d="M20 19l10 5-10 5V19z" fill="currentColor" opacity="0.3"/>
        </svg>
      </div>
      <p class="cf-empty-title">No clips found</p>
      <p class="cf-empty-sub">${escapeHtml(msg)}</p>
    </div>
  `;
}

function cfFmtTime(secs) {
  const h = Math.floor(secs / 3600);
  const m = Math.floor((secs % 3600) / 60);
  const s = Math.floor(secs % 60);
  if (h > 0) return h + ':' + String(m).padStart(2, '0') + ':' + String(s).padStart(2, '0');
  return m + ':' + String(s).padStart(2, '0');
}

/**
 * Parse a time input string into total seconds.
 * Supports: "M:SS", "H:MM:SS", or plain seconds (e.g. "300").
 */
function cfParseTimeInput(str) {
  str = str.trim();
  // H:MM:SS
  const hms = str.match(/^(\d+):(\d{1,2}):(\d{1,2})$/);
  if (hms) return parseInt(hms[1]) * 3600 + parseInt(hms[2]) * 60 + parseInt(hms[3]);
  // M:SS
  const ms = str.match(/^(\d+):(\d{1,2})$/);
  if (ms) return parseInt(ms[1]) * 60 + parseInt(ms[2]);
  // Plain seconds
  const n = parseFloat(str);
  return isNaN(n) ? NaN : n;
}

function escapeHtml(str) {
  const div = document.createElement('div');
  div.textContent = str;
  return div.innerHTML;
}
