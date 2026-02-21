/**
 * app.js — Application entry point
 * Imports all modules and initialises the application on DOMContentLoaded.
 */

import { setupNavTabs } from './js/utils.js';
import { setupUpload } from './js/upload.js';
import { setupPreview, applyPreset } from './js/preview.js';
import { setupTimeline } from './js/timeline.js';
import { setupJobs, loadJobs, loadSystemInfo } from './js/jobs.js';
import { setupClipFinder } from './js/clipfinder.js';
import { setupRender } from './js/render.js';

document.addEventListener('DOMContentLoaded', () => {
  // Navigation tabs (subtitle / clipfinder)
  setupNavTabs();

  // Upload screen: drag-drop, form, advanced options, clip picker
  setupUpload();

  // Preview screen: video, subtitles, style controls, save
  setupPreview();

  // Timeline: ruler, segments, split/merge, dragging
  setupTimeline();

  // Jobs: list, modal, SSE, system info
  setupJobs();

  // Clip Finder: YouTube clip detection
  setupClipFinder();

  // Render: options modal, AE export
  setupRender();

  // Apply default subtitle style preset
  applyPreset('vtuber-pop');

  // Initial data loads
  loadJobs();
  loadSystemInfo();

  // Periodic job list refresh
  setInterval(loadJobs, 5000);
});
