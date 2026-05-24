/**
 * app.module.js — Per-page entry dispatcher.
 *
 * Each ClipAuto page is its own URL with a distinct <body class="p-*"> tag.
 * We dispatch the relevant setup* functions only for the active page so we
 * don't fire querySelectors against DOM that doesn't exist on that page.
 */

import { setupUpload, fetchTranscript } from './js/upload.js';
import { setupPreview, openPreviewScreen } from './js/preview.js';
import { applyPreset } from './js/styleControls.js';
import { setupTimeline } from './js/timeline.js';
import { setupJobs, loadJobs, loadSystemInfo } from './js/jobs.js';
import { setupClipFinder } from './js/clipfinder.js';
import { setupRender } from './js/render.js';
import { setupShortMaker } from './js/shortmaker.js';
import { setupEffects } from './js/effects.js';
import * as S from './js/state.js';
import { fetchTranscript } from './js/utils.js';

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
        const transcript = await fetchTranscript(jobId);
        if (!transcript || !transcript.length) return;
        S.setActiveJobId(jobId);
        S.setTranscriptData(transcript);
        openPreviewScreen(jobId);
        // Update header job name
        const nameEl = document.getElementById('editorJobName');
        if (nameEl) nameEl.textContent = jobId;
      } catch (err) {
        console.warn('[editor] auto-load failed:', err);
      }
    });
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
