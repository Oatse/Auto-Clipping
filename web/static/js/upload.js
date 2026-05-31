/**
 * upload.js — Upload screen: drag-drop, file selection, form submit, clip picker
 */

import { apiFetch, formatBytes, formatClipDuration, showScreen, escHtml, toast } from './utils.js';
import * as S from './state.js';
import { openPreviewScreen } from './preview.js';
import { loadJobs } from './jobs.js';

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
const targetLang      = document.getElementById('targetLang');
const translatorBackend = document.getElementById('translatorBackend');
const refreshBtn      = document.getElementById('refreshBtn');

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
const spicyFilterEnabled    = document.getElementById('spicyFilterEnabled');
const naturalCaptionEnabled = document.getElementById('naturalCaptionEnabled');

// Transcribing screen
const transcribingStatus = document.getElementById('transcribingStatus');
const transcribingFile   = document.getElementById('transcribingFile');
const transcribingLog    = document.getElementById('transcribingLog');

// ── Setup ──────────────────────────────────────────────────────────────────
export function setupUpload() {
  setupDragDrop();
  setupAdvancedOptions();
  setupForm();
  setupModelSwitchNote();
  setupTranslatorBackend();
  setupClipPicker();
}

// ── Translation engine selector ────────────────────────────────────────────
// Syncs the hero "Translate" pill with the dropdown so the user can see at
// a glance which translator the next job will use. The selector itself is
// the source of truth — no localStorage; users frequently swap backends
// per-job during a Gemini rate-limit storm.
function setupTranslatorBackend() {
  if (!translatorBackend) return;
  const heroLabel = document.getElementById('heroTranslateLabel');
  const hint = document.getElementById('translatorBackendHint');

  const labelMap = {
    gemini: 'Gemini',
    claude: 'Claude (9router)',
  };
  const hintMap = {
    gemini: 'Speech-to-text always runs on ElevenLabs Scribe. This switch only changes which model translates the transcript. Gemini 3.5 Flash is the default — fastest and cheapest.',
    claude: 'Speech-to-text still runs on ElevenLabs Scribe. Claude Opus 4.7 (via 9router) is the recommended fallback when Gemini 3.5 / 2.5 Flash returns 503 — slower per-batch but more reliable under load.',
  };

  const sync = () => {
    const v = (translatorBackend.value || 'gemini').toLowerCase();
    if (heroLabel) heroLabel.textContent = labelMap[v] || labelMap.gemini;
    if (hint) hint.textContent = hintMap[v] || hintMap.gemini;
  };

  translatorBackend.addEventListener('change', sync);
  sync();
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
    toast.warn('Please select a video file: MP4, MOV, MKV, or AVI');
    return;
  }
  S.setSelectedFile(file);
  fileName.textContent = file.name;
  fileSize.textContent = formatBytes(file.size);
  // The CSS keys empty/filled visibility off `.dropzone.has-file` on the
  // parent (forms.css). Toggling only `.hidden` on the inner panes used
  // to leave the filled pill stuck at `display:none` from its base rule,
  // which is why the upload card went blank after picking a file.
  dropZone.classList.add('has-file');
  dropZoneInner.classList.add('hidden');
  fileSelected.classList.remove('hidden');
  transcribeBtn.disabled = false;
}

export function clearFile() {
  S.setSelectedFile(null);
  fileInput.value = '';
  dropZone.classList.remove('has-file');
  dropZoneInner.classList.remove('hidden');
  fileSelected.classList.add('hidden');
  if (transcribeBtn) transcribeBtn.disabled = true;
}

// ── Advanced Options ────────────────────────────────────────────────────────
function setupAdvancedOptions() {
  const advWrap = document.getElementById('advancedSection');

  // Accordion open/close — CSS keys off `.adv.is-open` on the wrapper.
  // We also keep the `.hidden` toggle on the body for legacy guard rails.
  advancedToggle.addEventListener('click', () => {
    const willOpen = !advWrap.classList.contains('is-open');
    advWrap.classList.toggle('is-open', willOpen);
    advancedBody.classList.toggle('hidden', !willOpen);
    if (advancedArrow) advancedArrow.style.transform = willOpen ? 'rotate(180deg)' : '';
  });

  // Speaker detection ON/OFF — controls visibility of num-speakers row
  speakerDetectionEnabled.addEventListener('change', () => {
    const enabled = speakerDetectionEnabled.checked;
    numSpeakersRow.classList.toggle('advanced-row-disabled', !enabled);
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
  speakerCountDec.addEventListener('click', () => setSpeakerCount(S.numSpeakersCount - 1));
  speakerCountInc.addEventListener('click', () => setSpeakerCount(S.numSpeakersCount + 1));

  // Quick-select pills
  speakerCountPills.querySelectorAll('.tag').forEach(btn => {
    btn.addEventListener('click', () => setSpeakerCount(parseInt(btn.dataset.count, 10)));
  });
}

function setSpeakerCount(n) {
  S.setNumSpeakersCount(Math.min(6, Math.max(1, n)));
  speakerCountVal.textContent = S.numSpeakersCount;
  speakerCountPills.querySelectorAll('.tag').forEach(btn => {
    btn.classList.toggle('is-active', parseInt(btn.dataset.count, 10) === S.numSpeakersCount);
  });
  speakerCountDec.disabled = S.numSpeakersCount <= 1;
  speakerCountInc.disabled = S.numSpeakersCount >= 6;
}

// ── ElevenLabs model note & advanced options sync ───────────────────────────
function setupModelSwitchNote() {
  // The transcription-engine field used to live on the upload card but was
  // repurposed into the Translation engine selector (Gemini / Claude). The
  // ElevenLabs quota chip used to live there too — it duplicated the
  // floating nav pill so the per-key quota lives there now. This stub
  // exists only so the upload-screen description label stays in sync if
  // it's ever re-introduced.
  const descEl = document.querySelector('#speakerDetectionEnabled')
    ?.closest('.advanced-row')
    ?.querySelector('.advanced-row-desc');
  if (descEl) {
    descEl.textContent =
      'ElevenLabs built-in speaker diarization. Disable for single-speaker content';
  }
}

// ── Form Submit → Transcribe Phase ────────────────────────────────────────
function setupForm() {
  uploadForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    if (!S.selectedFile) return;

    // Resolve every form-touching node at submit time. Module-load-time
    // const refs cause a misleading "Cannot read properties of null
    // (reading 'value')" toast whenever a template rename ships ahead
    // of a JS deploy or a stale ESM lives in the browser cache (which
    // is exactly how this regression surfaced after the
    // whisperModel → translatorBackend rename). A late lookup with
    // explicit null guards turns the same condition into an actionable
    // error message instead of a 7-word mystery.
    const targetLangEl     = document.getElementById('targetLang');
    const speakerDetEl     = document.getElementById('speakerDetectionEnabled');
    const numSpeakersOnEl  = document.getElementById('numSpeakersEnabled');
    const backendEl        = document.getElementById('translatorBackend');
    const spicyEl          = document.getElementById('spicyFilterEnabled');
    const submitBtn        = document.getElementById('transcribeBtn') || transcribeBtn;
    const submitBtnText    = submitBtn && submitBtn.querySelector('.btn-text');

    if (submitBtn) {
      submitBtn.disabled = true;
      submitBtn.classList.add('loading');
      if (submitBtnText) submitBtnText.textContent = 'Uploading...';
    }

    if (!targetLangEl) {
      toast.error(
        'Upload form is out of date — please hard-refresh (Ctrl+Shift+R) and try again.'
      );
      if (submitBtn) {
        submitBtn.classList.remove('loading');
        submitBtn.disabled = !S.selectedFile;
        if (submitBtnText) submitBtnText.textContent = 'Transcribe & preview';
      }
      return;
    }

    try {
      const formData = new FormData();
      formData.append('video', S.selectedFile);
      formData.append('target_language', targetLangEl.value);
      formData.append('transcribe_only', true);
      // Speaker-detection switch — default ON if the toggle ever goes
      // missing, since that's the upstream pipeline's safer default
      // (multi-speaker transcripts can always be flattened later, but
      // a flat transcript can't be re-diarised without re-running STT).
      formData.append(
        'speaker_detection',
        speakerDetEl ? speakerDetEl.checked : true,
      );
      if (backendEl && backendEl.value) {
        formData.append('translator_backend', backendEl.value);
      }
      if (spicyEl) {
        formData.append('spicy_filter', spicyEl.checked);
      }
      if (naturalCaptionEnabled) {
        formData.append('natural_caption', naturalCaptionEnabled.checked);
      }

      const wantSpeakerCap = speakerDetEl?.checked && numSpeakersOnEl?.checked;
      if (wantSpeakerCap) {
        formData.append('num_speakers', S.numSpeakersCount);
      }

      const job = await apiFetch('/api/jobs', { method: 'POST', body: formData });
      S.setActiveJobId(job.id);

      // Hand off to /editor/{id} — the editor template owns the
      // transcribing → preview UX (its scoped DOM and CSS). Doing the
      // SSE/polling in-place on /auto-subtitle would call
      // openPreviewScreen() against editor-only IDs (#previewVideo,
      // #subtitleOverlay, #transcriptBody) and silently TypeError out
      // as a misleading "Failed to start transcription" toast even
      // after the job actually completed.
      window.location.href = `/editor/${job.id}`;
    } catch (err) {
      toast.error('Failed to start transcription: ' + err.message);
      if (submitBtn) {
        submitBtn.classList.remove('loading');
        if (submitBtnText) submitBtnText.textContent = 'Transcribe & preview';
        submitBtn.disabled = !S.selectedFile;
      }
    }
  });

  refreshBtn.addEventListener('click', () => {
    if (S.jobsPanelTab === 'clips') {
      loadClipJobsList();
      return;
    }
    loadJobs();
  });
}

export async function watchTranscription(jobId) {
  return new Promise((resolve, reject) => {
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

    const poll = setInterval(async () => {
      try {
        const job = await apiFetch(`/api/jobs/${jobId}`);
        if (job.phase_label) {
          transcribingStatus.textContent = job.phase_label;
        }

        if (job.status === 'completed' && job.current_phase === 1) {
          clearInterval(poll);
          sse.close();

          const transcript = await fetchTranscript(jobId);
          // Treat empty transcript the same as a missing one. Without
          // this guard the editor would open the preview against an
          // empty list and present a video with no transcript pane —
          // the exact symptom screenshot the user reported.
          if (transcript && transcript.length) {
            S.setTranscriptData(transcript);
            openPreviewScreen(jobId);
            resolve();
          } else {
            reject(new Error(
              'Transcription completed but produced no segments. '
              + 'The audio may be silent or in an unsupported language.'
            ));
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

export async function fetchTranscript(jobId) {
  try {
    const data = await apiFetch(`/api/jobs/${jobId}/transcript`);
    return data.segments || data;
  } catch {
    return generateMockTranscript();
  }
}

function generateMockTranscript() {
  return [
    { start: 0.5, end: 3.2, text: "Welcome to the video!" },
    { start: 3.5, end: 7.1, text: "Today we're going to show you something amazing." },
    { start: 7.8, end: 11.4, text: "This is the subtitle preview system." },
    { start: 12.0, end: 15.6, text: "You can customize the style right here." },
    { start: 16.2, end: 20.8, text: "Choose your font, color, and animation." },
    { start: 21.5, end: 25.0, text: "It will look just like this on the final video!" },
  ];
}

// ── Clip Picker ────────────────────────────────────────────────────────────
let clipPickerOpen = false;
let clipPickerLoaded = false;

function setupClipPicker() {
  const btn = document.getElementById('clipPickerBtn');
  const list = document.getElementById('clipPickerList');
  if (!btn || !list) return;

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
      const header = document.createElement('div');
      header.className = 'clip-picker-group-header';
      header.innerHTML = `
        <span class="clip-picker-group-title">${escHtml(job.video_title || job.url || job.job_id)}</span>
        <span class="clip-picker-group-count">${job.clip_count} clip${job.clip_count !== 1 ? 's' : ''}</span>
      `;
      list.appendChild(header);

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

export async function startJobFromClip(clipPath, clipFilename) {
  const transcribeBtnEl = document.getElementById('transcribeBtn');
  transcribeBtnEl.disabled = true;
  transcribeBtnEl.classList.add('loading');
  transcribeBtnEl.querySelector('.btn-text').textContent = 'Starting...';

  try {
    const formData = new FormData();
    formData.append('clip_path', clipPath);
    formData.append('target_language', targetLang.value);
    formData.append('speaker_detection', speakerDetectionEnabled.checked);
    if (translatorBackend && translatorBackend.value) {
      formData.append('translator_backend', translatorBackend.value);
    }
    if (spicyFilterEnabled) {
      formData.append('spicy_filter', spicyFilterEnabled.checked);
    }

    if (speakerDetectionEnabled.checked && numSpeakersEnabled.checked) {
      formData.append('num_speakers', S.numSpeakersCount);
    }

    const job = await apiFetch('/api/jobs/from-clip', { method: 'POST', body: formData });
    S.setActiveJobId(job.id);

    // Hand off to the editor — same reasoning as the upload form.
    // /auto-subtitle does not own the preview DOM, so watching the
    // transcription in-place ends in a misleading "Failed" toast.
    window.location.href = `/editor/${job.id}`;
  } catch (err) {
    toast.error('Failed to start transcription: ' + err.message);
    transcribeBtnEl.classList.remove('loading');
    transcribeBtnEl.querySelector('.btn-text').textContent = 'Transcribe & Preview';
    transcribeBtnEl.disabled = !S.selectedFile;
  }
}

// ── Clip Jobs List (in upload panel) ───────────────────────────────────────
export async function loadClipJobsList() {
  const clipJobsList = document.getElementById('clipJobsList');
  if (!clipJobsList) return;

  // Skeleton placeholders mirror the real .clip-job-card layout so the
  // panel doesn't snap from "empty" to "filled".
  clipJobsList.innerHTML = Array.from({ length: 3 }, () => `
    <div class="skeleton-job-card" style="display:grid;grid-template-columns:132px 1fr auto;gap:12px;">
      <div class="skeleton" style="aspect-ratio:16/9;width:132px;border-radius:9px;"></div>
      <div style="display:flex;flex-direction:column;gap:8px;justify-content:center;">
        <div class="skeleton skeleton-line title"></div>
        <div class="skeleton skeleton-line meta"></div>
      </div>
      <div class="skeleton skeleton-line badge" style="align-self:center;"></div>
    </div>
  `).join('');

  try {
    const data = await apiFetch('/api/clip-finder/available-clips');

    if (!data || data.length === 0) {
      clipJobsList.innerHTML = `
        <div class="empty-state">
          <div class="empty-icon">◻</div>
          <p class="empty-title">No clips downloaded</p>
          <p class="empty-sub">Use Clip Finder to detect highlights from a YouTube video, then download the clips you want to caption.</p>
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

// formatClipDuration imported from utils.js
