/**
 * state.js — Shared application state, constants, presets, and speaker styles
 */

// ── App State ──────────────────────────────────────────────────────────────
export let selectedFile   = null;
export let activeJobId    = null;
export let sseSource      = null;
export let pollInterval   = null;
export let transcriptData = [];   // Array of {start, end, text, words?}
export let originalTranscriptData = null;  // ElevenLabs original (before Gemini), null if unavailable
export let showingOriginal = false;        // Toggle state for original transcript view
export let subtitleTimer  = null; // requestAnimationFrame handle
export let currentStyle   = {};   // Live subtitle style state
export let currentAnim    = 'word-pop';
export let currentPos     = 'bottom';
export let editMode       = false;

// Timeline state
export let timelineZoom     = 1.0;
export let videoDuration    = 0;
export let selectedSegIdx   = null;
export let draggingPlayhead = false;
export let draggingSegEdge  = null; // {segIdx, edge: 'start'|'end'}
export let draggingSegBody  = null; // {segIdx, offsetTime, segDuration} for whole-segment drag
export let subtitleDragState = null; // {segIdx, startX, startY, origPosX, origPosY}

// Cached speaker row layout from last renderTimeline() — used for cross-speaker drag
export let timelineSpeakers = []; // sorted array of speaker IDs as rendered in timeline rows

// Merge / split state
export let splitDialogIdx     = null;  // which segment is being split
export let splitDialogWordIdx = 1;     // words[0..wordIdx-1] → part 1, rest → part 2

// Per-speaker custom styles: { "SPEAKER_00": { color: "#ffffff" }, ... }
export let speakerStyles  = {};

// ── Effects / Filters state ─────────────────────────────────────────────
// Each FX: { id, type, start, end, label, params: {...per-type} }
//   zoom-vtuber / zoom-in-center: { zoomLevel:1-200, transition:'smooth'|'instant', cropX:0-1, cropY:0-1 }
//   red-flash / flash-white: { intensity:10-100 }
//   shake: { intensity:10-100 }
//   vignette: { intensity:10-100 }
//   volume-boost: { gain:1-5 }
//   bass-boost: { gain:1-12 }
export let effectsData = [];
export let selectedFxId = null;
export let draggingFx = null;     // { id, offsetTime } for whole-block drag
export let draggingFxEdge = null; // { id, edge: 'start'|'end' }
export let fxLayerCount = 1;       // number of FX track layers (1+)
// Global filter (applied to whole video)
export let activeFilter = 'none';
export let filterBrightness = 0;
export let filterContrast   = 0;
export let filterSaturation = 0;

let _fxIdCounter = 0;
export function nextFxId() { return ++_fxIdCounter; }

// Undo / Redo stacks — store deep copies of transcriptData around each mutation
export let undoStack = [];
export let redoStack = [];
const UNDO_MAX = 50;  // keep at most 50 undo snapshots

/** Call BEFORE mutating transcriptData to save a restorable snapshot.
 *  Also clears the redo stack — new edits invalidate any redoable history. */
export function pushUndoSnapshot() {
  undoStack.push(JSON.parse(JSON.stringify(transcriptData)));
  if (undoStack.length > UNDO_MAX) undoStack.shift();
  // New mutation → forward history is no longer reachable
  redoStack = [];
}

/** Pop the most recent snapshot. Returns the array or null if empty. */
export function popUndoSnapshot() {
  if (undoStack.length === 0) return null;
  return undoStack.pop();
}

/** Push current transcript onto the redo stack (used by undo() before restoring). */
export function pushRedoSnapshot() {
  redoStack.push(JSON.parse(JSON.stringify(transcriptData)));
  if (redoStack.length > UNDO_MAX) redoStack.shift();
}

/** Pop the most recent redo snapshot. Returns the array or null if empty. */
export function popRedoSnapshot() {
  if (redoStack.length === 0) return null;
  return redoStack.pop();
}

export function clearUndoStack() {
  undoStack = [];
  redoStack = [];
}

// Auto-save state
export let autoSaveTimer  = null;
export let isSaving       = false;
export const AUTOSAVE_DELAY = 2000; // 2 seconds debounce

// Fullscreen scale
export let fsScale = 1;
export let previewWidthBeforeFs = 0;
export let jobsPanelTab = 'jobs';

// Advanced options
export let numSpeakersCount = 2;

// State setters (needed because ES module exports are live bindings but only writable from the defining module)
export function setSelectedFile(f) { selectedFile = f; }
export function setActiveJobId(id) { activeJobId = id; }
export function setSseSource(s) { sseSource = s; }
export function setPollInterval(p) { pollInterval = p; }
export function setTranscriptData(d) { transcriptData = d; }
export function setOriginalTranscriptData(d) { originalTranscriptData = d; }
export function setShowingOriginal(v) { showingOriginal = v; }
export function setSubtitleTimer(t) { subtitleTimer = t; }
export function setCurrentAnim(a) { currentAnim = a; }
export function setCurrentPos(p) { currentPos = p; }
export function setEditMode(m) { editMode = m; }
export function setTimelineZoom(z) { timelineZoom = z; }
export function setVideoDuration(d) { videoDuration = d; }
export function setSelectedSegIdx(i) { selectedSegIdx = i; }
export function setDraggingPlayhead(d) { draggingPlayhead = d; }
export function setDraggingSegEdge(d) { draggingSegEdge = d; }
export function setDraggingSegBody(d) { draggingSegBody = d; }
export function setSubtitleDragState(d) { subtitleDragState = d; }
export function setTimelineSpeakers(s) { timelineSpeakers = s; }
export function setSplitDialogIdx(i) { splitDialogIdx = i; }
export function setSplitDialogWordIdx(i) { splitDialogWordIdx = i; }
export function setSpeakerStyles(s) { speakerStyles = s; }
export function setAutoSaveTimer(t) { autoSaveTimer = t; }
export function setIsSaving(s) { isSaving = s; }
export function setFsScale(s) { fsScale = s; }
export function setPreviewWidthBeforeFs(w) { previewWidthBeforeFs = w; }
export function setJobsPanelTab(t) { jobsPanelTab = t; }
export function setNumSpeakersCount(n) { numSpeakersCount = n; }

// Effects / Filters setters
export function setEffectsData(d) { effectsData = d; }
export function setSelectedFxId(id) { selectedFxId = id; }
export function setDraggingFx(d) { draggingFx = d; }
export function setDraggingFxEdge(d) { draggingFxEdge = d; }
export function setFxLayerCount(n) { fxLayerCount = Math.max(1, Math.min(8, n)); }
export function setActiveFilter(f) { activeFilter = f; }
export function setFilterBrightness(v) { filterBrightness = v; }
export function setFilterContrast(v) { filterContrast = v; }
export function setFilterSaturation(v) { filterSaturation = v; }

// Speaker color palette — index maps to SPEAKER_00, SPEAKER_01, etc.
export const SPEAKER_COLORS = [
  '#ffffff',  // SPEAKER_00 — white (default)
  '#FFE600',  // SPEAKER_01 — yellow
  '#00F5FF',  // SPEAKER_02 — cyan
  '#FF85C2',  // SPEAKER_03 — pink
  '#7FFF00',  // SPEAKER_04 — lime
  '#FF8C00',  // SPEAKER_05 — orange
];

// Preset definitions
export const PRESETS = {
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

// ── Speaker Color Helpers ──────────────────────────────────────────────────
export function getSpeakerColor(speakerId) {
  if (!speakerId) return speakerStyles['SPEAKER_00']?.color || SPEAKER_COLORS[0];
  // Check custom override first
  if (speakerStyles[speakerId]?.color) return speakerStyles[speakerId].color;
  const idx = parseInt((speakerId.match(/\d+$/) || ['0'])[0], 10);
  return SPEAKER_COLORS[idx % SPEAKER_COLORS.length];
}

export function getSpeakerStrokeColor(speakerId) {
  if (!speakerId) return speakerStyles['SPEAKER_00']?.strokeColor || null;
  return speakerStyles[speakerId]?.strokeColor || null;
}
