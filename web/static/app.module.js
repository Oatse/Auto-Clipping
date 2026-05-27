/**
 * app.module.js — Per-page entry dispatcher.
 *
 * Each ClipAuto page is its own URL with a distinct <body class="p-*"> tag.
 * We dispatch the relevant setup* functions only for the active page so we
 * don't fire querySelectors against DOM that doesn't exist on that page.
 */

import { setupUpload, fetchTranscript, watchTranscription } from './js/upload.js';
import { setupPreview, openPreviewScreen } from './js/preview.js';
import { applyPreset } from './js/styleControls.js';
import { setupTimeline } from './js/timeline.js';
import { setupJobs, loadJobs, loadSystemInfo } from './js/jobs.js';
import { setupClipFinder } from './js/clipfinder.js';
import { setupRender } from './js/render.js';
import { setupShortMaker } from './js/shortmaker.js';
import { setupEffects } from './js/effects.js';
import { apiFetch, showScreen, toast } from './js/utils.js';
import * as S from './js/state.js';

function safeRun(label, fn) {
  try { fn(); }
  catch (err) {
    // Non-fatal — log so other setups can still run.
    console.warn(`[app] ${label} failed:`, err);
  }
}

function bootSubtitlePage() {
  // Subtitle landing page only has: dropzone, settings form, jobs sidebar.
  // Preview / timeline / render / effects belong to the editor route.
  safeRun('upload',     () => setupUpload());
  safeRun('jobs',       () => setupJobs());
  safeRun('jobs-load',  () => loadJobs());
  safeRun('sys-info',   () => loadSystemInfo());
  setInterval(() => safeRun('jobs-tick', () => loadJobs()), 5000);
}

function bootClipFinderPage() {
  safeRun('clipfinder', () => setupClipFinder());
}

function bootShortMakerPage() {
  safeRun('shortmaker', () => setupShortMaker());
}

function bootEditorPage() {
  // Editor reuses preview / timeline / render / effects.
  // upload / jobs are kept lightweight (job-modal still uses jobs.js).
  safeRun('preview',  () => setupPreview());
  safeRun('timeline', () => setupTimeline());
  safeRun('render',   () => setupRender());
  safeRun('effects',  () => setupEffects());
  safeRun('jobs',     () => setupJobs());
  safeRun('preset',   () => applyPreset('vtuber-pop'));

  // Auto-open the job specified in the URL (/editor/{job_id})
  const editorRoot = document.getElementById('editorRoot');
  const jobId = editorRoot && editorRoot.dataset.jobId;
  if (jobId) {
    safeRun('autoload-job', async () => {
      try {
        const job = await apiFetch(`/api/jobs/${jobId}`);
        const nameEl = document.getElementById('editorJobName');
        if (nameEl) nameEl.textContent = job.filename || jobId;

        // Job is still transcribing — show the transcribing screen and
        // wait for completion before loading the preview. Without this
        // the editor would call fetchTranscript() too early, fall back
        // to the mock transcript, and load openPreviewScreen against a
        // job whose video is still uploading.
        const isTranscribing = (
          job.status === 'queued' || job.status === 'running'
        );
        if (isTranscribing) {
          await showTranscribingThenOpen(jobId, job);
          return;
        }

        if (job.status === 'failed') {
          toast.error('Transcription failed: ' + (job.error || 'unknown error'));
          showScreen('upload');
          return;
        }

        // status === 'completed' — load the saved transcript and open
        // the preview screen.
        const transcript = await fetchTranscript(jobId);
        if (!transcript || !transcript.length) {
          toast.warn('No transcript available for this job yet.');
          showScreen('upload');
          return;
        }
        S.setActiveJobId(jobId);
        S.setTranscriptData(transcript);
        openPreviewScreen(jobId);
      } catch (err) {
        console.warn('[editor] auto-load failed:', err);
        toast.error('Could not load job: ' + (err.message || err));
        showScreen('upload');
      }
    });
  }
}

/**
 * Show the transcribing screen and watch the job until phase 1 finishes,
 * then load the transcript and open the preview screen. The editor
 * template ships #screen-transcribing, #transcribingFile, and
 * #transcribingStatus, so the same SSE+polling loop watchTranscription
 * uses on /auto-subtitle works here too.
 */
async function showTranscribingThenOpen(jobId, job) {
  const fileEl   = document.getElementById('transcribingFile');
  const statusEl = document.getElementById('transcribingStatus');
  const logEl    = document.getElementById('transcribingLog');
  if (fileEl)   fileEl.textContent   = job.filename || jobId;
  if (statusEl) statusEl.textContent = job.phase_label || 'Running transcription...';
  if (logEl)    logEl.textContent    = '';

  showScreen('transcribing');
  S.setActiveJobId(jobId);

  try {
    await watchTranscription(jobId);
  } catch (err) {
    toast.error('Transcription failed: ' + (err.message || err));
    showScreen('upload');
  }
}

document.addEventListener('DOMContentLoaded', () => {
  const cl = document.body?.classList;
  if (!cl) return;

  if (cl.contains('p-subtitle'))      bootSubtitlePage();
  else if (cl.contains('p-clipfinder')) bootClipFinderPage();
  else if (cl.contains('p-shortmaker')) bootShortMakerPage();
  else if (cl.contains('p-editor'))     bootEditorPage();
  // Home/landing pages do nothing — only nav.js + motion.js run.
});
