/**
 * clipfinder.js — Clip Finder feature: YouTube clip detection via yt-dlp + Gemini AI
 */

import { escHtml, toast } from './utils.js';

// ── State ──────────────────────────────────────────────────────────────────
let cfJobId = null;
let cfSSE   = null;
let cfMode  = 'single-shot';

// ── Setup ──────────────────────────────────────────────────────────────────
export function setupClipFinder() {
  const cfUrl          = document.getElementById('cfUrl');
  const cfInstructions = document.getElementById('cfInstructions');
  const cfLang         = document.getElementById('cfLang');
  const cfStartOffset  = document.getElementById('cfStartOffset');
  const cfFindBtn      = document.getElementById('cfFindBtn');
  const cfDownloadAllBtn = document.getElementById('cfDownloadAllBtn');
  const cfModeSingle   = document.getElementById('cfModeSingle');
  const cfModeMulti    = document.getElementById('cfModeMulti');
  const cfEnableAudio  = document.getElementById('cfEnableAudio');
  const cfEnableChat   = document.getElementById('cfEnableChat');

  function updateFindBtn() {
    cfFindBtn.disabled = !cfUrl.value.trim();
  }
  cfUrl.addEventListener('input', updateFindBtn);
  cfInstructions.addEventListener('input', updateFindBtn);
  updateFindBtn();

  // Mode toggle (single-shot ↔ multi-stage)
  function setMode(mode) {
    cfMode = mode;
    [cfModeSingle, cfModeMulti].forEach(btn => {
      const active = btn && btn.dataset.mode === mode;
      if (!btn) return;
      btn.classList.toggle('active', active);
      btn.setAttribute('aria-checked', active ? 'true' : 'false');
    });
  }
  if (cfModeSingle) cfModeSingle.addEventListener('click', () => setMode('single-shot'));
  if (cfModeMulti)  cfModeMulti.addEventListener('click',  () => setMode('multi-stage'));

  // Find Clips button
  cfFindBtn.addEventListener('click', async () => {
    const url = cfUrl.value.trim();
    const instructions = cfInstructions.value.trim();
    const lang = cfLang.value;

    let startOffset = 0;
    const offsetRaw = cfStartOffset.value.trim();
    if (offsetRaw) {
      startOffset = cfParseTimeInput(offsetRaw);
      if (isNaN(startOffset) || startOffset < 0) {
        toast.warn('Invalid start time format. Use M:SS (e.g. 5:00) or seconds (e.g. 300).');
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
        body: JSON.stringify({
          url,
          instructions,
          lang,
          start_offset: startOffset,
          mode: cfMode,
          enable_audio_signals: cfEnableAudio ? cfEnableAudio.checked : true,
          enable_chat_signals:  cfEnableChat  ? cfEnableChat.checked  : true,
        }),
      });

      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: 'Request failed' }));
        throw new Error(err.detail || 'Failed to start clip finder');
      }

      const job = await res.json();
      cfJobId = job.id;

      cfShowProgress();
      cfStartSSE(job.id);

    } catch (err) {
      toast.error(err.message || 'Unknown error');
      cfFindBtn.disabled = false;
      cfFindBtn.querySelector('.btn-text').textContent = 'Find Clips';
    }
  });

  // Download All Clips button
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

      cfStartSSE(cfJobId);

    } catch (err) {
      toast.error(err.message || 'Unknown error');
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

  for (let i = 1; i <= 4; i++) {
    const step = document.getElementById('cfStep' + i);
    if (step) step.className = 'cf-step';
  }

  document.getElementById('cfProgressBar').style.width = '0%';
  document.getElementById('cfLog').innerHTML = '';
}

function cfUpdateSteps(status) {
  // 4-step progression: transcript → signals → analysis → results
  const stepMap = {
    transcribing: 1,
    signals:      2,
    analyzing:    3,
    analyzed:     4,
    downloading:  4,
    completed:    4,
  };

  const activeStep = stepMap[status] || 0;

  for (let i = 1; i <= 4; i++) {
    const step = document.getElementById('cfStep' + i);
    if (!step) continue;
    step.classList.remove('active', 'done');
    if (i < activeStep) step.classList.add('done');
    else if (i === activeStep) {
      if (status === 'analyzed' || status === 'completed') {
        step.classList.add('done');
      } else {
        step.classList.add('active');
      }
    }
  }

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
    setTimeout(() => cfLoadResults(jobId), 1000);
  };

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

        const dlBtn = document.getElementById('cfDownloadAllBtn');
        if (job.status === 'completed') {
          dlBtn.classList.remove('loading');
          dlBtn.querySelector('.btn-text').textContent = 'All Clips Downloaded';
          dlBtn.disabled = true;
        }

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
      const downloadedCount = job.clip_files ? job.clip_files.filter(f => f).length : 0;
      const remainingCount = clipCount - downloadedCount;

      dlAll.classList.remove('hidden');
      dlBtn.classList.remove('loading');

      if (remainingCount > 0) {
        dlBtn.disabled = false;
        dlBtn.querySelector('.btn-text').textContent =
          `Download ${remainingCount === clipCount ? 'All ' : ''}${remainingCount} Clip${remainingCount !== 1 ? 's' : ''}`;
      } else {
        dlBtn.disabled = true;
        dlBtn.querySelector('.btn-text').textContent = 'All Clips Downloaded';
      }

      cfRenderClipsInfoOnly(job);
    } else if (job.status === 'completed') {
      dlAll.classList.remove('hidden');
      dlBtn.disabled = true;
      dlBtn.classList.remove('loading');
      dlBtn.querySelector('.btn-text').textContent = 'All Clips Downloaded';
      cfRenderClipsInfoOnly(job);
    } else if (job.status === 'downloading') {
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

function cfRenderClipsInfoOnly(job) {
  const grid = document.getElementById('cfClipsGrid');
  grid.innerHTML = '';

  // Job-wide signals summary at the top of the grid
  const summary = job.signals_summary || {};
  const summaryEntries = Object.entries(summary).filter(([, v]) => v > 0);
  if (summaryEntries.length > 0) {
    const wrap = document.createElement('div');
    wrap.className = 'cf-signals-summary';
    wrap.innerHTML = `<span class="cf-signals-summary-label">Signals</span>` +
      summaryEntries.map(([kind, count]) =>
        `<span class="cf-signal-pill cf-signal-pill--${escHtml(kind)}">${cfSignalLabel(kind)}: ${count}</span>`
      ).join('');
    grid.appendChild(wrap);
  }

  // Sort clips by score (descending) when score data is present.  Keep
  // the original index in `originalIdx` so the download URLs
  // (/api/clip-finder/clips/{job}/{idx}) and the file lookup in
  // `job.clip_files[idx]` keep referencing the right backend record.
  // Clips without a score sink to the bottom in their original order.
  const ordered = job.clips
    .map((clip, originalIdx) => ({ clip, originalIdx }))
    .sort((a, b) => {
      const sa = (a.clip.score && typeof a.clip.score.total === 'number')
        ? a.clip.score.total : -Infinity;
      const sb = (b.clip.score && typeof b.clip.score.total === 'number')
        ? b.clip.score.total : -Infinity;
      if (sa === sb) return a.originalIdx - b.originalIdx;
      return sb - sa;
    });

  // Results bar — surfaces clip count + the active sort.  Only shown
  // when at least one clip has a score; otherwise sorting is a no-op
  // and the strip would be misleading.
  const hasScores = ordered.some(({ clip }) =>
    clip.score && typeof clip.score.total === 'number'
  );
  if (ordered.length > 0) {
    const bar = document.createElement('div');
    bar.className = 'cf-results-bar';
    bar.innerHTML = `
      <div class="cf-results-bar-count">
        <strong>${ordered.length}</strong> clip${ordered.length === 1 ? '' : 's'}
        ${hasScores ? '· ranked by score' : ''}
      </div>
      ${hasScores ? `
        <div class="cf-results-bar-hint" title="Highest score first.  Clips without a score sink to the bottom.">
          sorted by score
        </div>
      ` : ''}
    `;
    grid.appendChild(bar);
  }

  ordered.forEach(({ clip, originalIdx }, displayIdx) => {
    const idx = originalIdx;  // backend index — used by all URL building
    const card = document.createElement('div');
    card.className = 'cf-clip-card';
    card.style.setProperty('--cf-card-i', String(displayIdx));

    const startFmt = cfFmtTime(clip.start);
    const endFmt   = cfFmtTime(clip.end);
    const duration = clip.end - clip.start;
    const durFmt   = duration >= 60
      ? Math.floor(duration / 60) + 'm ' + Math.floor(duration % 60) + 's'
      : Math.floor(duration) + 's';

    const isDownloaded = job.clip_files && job.clip_files[idx];
    const scoreHtml    = cfRenderScore(clip);
    const hunterHtml   = cfRenderHunter(clip);
    const highlightHtml= cfRenderHighlight(clip);
    const signalsHtml  = cfRenderClipSignals(clip);

    if (isDownloaded) {
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
          <div class="cf-clip-number">${hunterHtml}#${idx + 1}${highlightHtml}</div>
          <div class="cf-clip-title">${escHtml(clip.title || 'Clip ' + (idx + 1))}</div>
          <div class="cf-clip-time">${startFmt} - ${endFmt}</div>
          ${scoreHtml}
          ${clip.reason ? `<div class="cf-clip-reason">${escHtml(clip.reason)}</div>` : ''}
          ${signalsHtml}
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
      card.innerHTML = `
        <div class="cf-clip-placeholder">
          <svg width="40" height="40" viewBox="0 0 40 40" fill="none">
            <rect x="4" y="8" width="32" height="24" rx="3" stroke="currentColor" stroke-width="1.5" opacity="0.4"/>
            <path d="M16 15l10 5-10 5V15z" fill="currentColor" opacity="0.4"/>
          </svg>
        </div>
        <div class="cf-clip-info">
          <div class="cf-clip-number">${hunterHtml}#${idx + 1}${highlightHtml}</div>
          <div class="cf-clip-title">${escHtml(clip.title || 'Clip ' + (idx + 1))}</div>
          <div class="cf-clip-time">${startFmt} - ${endFmt} (${durFmt})</div>
          ${scoreHtml}
          ${clip.reason ? `<div class="cf-clip-reason">${escHtml(clip.reason)}</div>` : ''}
          ${signalsHtml}
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

  grid.querySelectorAll('.cf-clip-dl-single').forEach(btn => {
    btn.addEventListener('click', () => cfDownloadSingleClip(btn, job.id));
  });
}

// ── Helpers: render score, hunter chip, signals ────────────────────────────

function cfRenderScore(clip) {
  const s = clip.score;
  if (!s || typeof s.total !== 'number') return '';
  const dims = [
    ['Hook',    s.retention_hook],
    ['Emotion', s.emotional_intensity],
    ['Cycle',   s.completeness],
    ['Replay',  s.replayability],
  ];
  return `
    <div class="cf-clip-score">
      <div class="cf-clip-score-total">${(s.total || 0).toFixed(1)}<small>/10</small></div>
      <div class="cf-clip-score-bars">
        ${dims.map(([name, val]) => {
          const v = Math.max(0, Math.min(10, Number(val) || 0));
          return `
            <div class="cf-clip-score-bar">
              <span class="cf-clip-score-bar-name">${name}</span>
              <div class="cf-clip-score-bar-track">
                <div class="cf-clip-score-bar-fill" style="width:${v * 10}%"></div>
              </div>
              <span class="cf-clip-score-bar-val">${v.toFixed(1)}</span>
            </div>
          `;
        }).join('')}
      </div>
    </div>
  `;
}

function cfRenderHunter(clip) {
  const h = clip.hunter;
  if (!h || h === 'general') return '';
  return `<span class="cf-clip-hunter cf-clip-hunter--${escHtml(h)}">${escHtml(h)}</span>`;
}

function cfRenderHighlight(clip) {
  const ht = clip.highlight_type;
  if (!ht || ht === '' || ht === 'other') return '';
  const labels = {
    karma_arc: 'Karma',
    genuine_reaction: 'Reaction',
    clutch_play: 'Clutch',
    chaotic_plea: 'Chaos',
  };
  const label = labels[ht] || ht;
  return ` <span class="cf-clip-badge cf-clip-badge--${escHtml(ht)}">${escHtml(label)}</span>`;
}

function cfRenderClipSignals(clip) {
  const sigs = clip.signals || [];
  if (!sigs.length) return '';
  // Group by kind, count, take top 4
  const counts = sigs.reduce((acc, s) => {
    acc[s.kind] = (acc[s.kind] || 0) + 1;
    return acc;
  }, {});
  const entries = Object.entries(counts).slice(0, 4);
  return `
    <div class="cf-clip-signals">
      ${entries.map(([k, n]) =>
        `<span class="cf-signal-pill cf-signal-pill--${escHtml(k)}">${cfSignalLabel(k)} ×${n}</span>`
      ).join('')}
    </div>
  `;
}

function cfSignalLabel(kind) {
  const map = {
    audio_peak: 'audio peak',
    audio_silence: 'silence',
    chat_spike: 'chat spike',
    chat_emote: 'emote storm',
    chat_superchat: 'superchat',
    scene_cut: 'scene cut',
    generic: 'signal',
  };
  return map[kind] || kind;
}

async function cfDownloadSingleClip(btn, jobId) {
  const clipIdx = parseInt(btn.dataset.clipIdx, 10);
  if (isNaN(clipIdx)) return;

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
      await cfLoadResults(jobId);
      return;
    }

    for (let attempt = 0; attempt < 600; attempt++) {
      await new Promise(r => setTimeout(r, 2000));
      const pollRes = await fetch(`/api/clip-finder/jobs/${jobId}`);
      if (!pollRes.ok) continue;
      const job = await pollRes.json();

      if (job.clip_files && job.clip_files[clipIdx]) {
        await cfLoadResults(jobId);
        return;
      }

      if (job.status === 'failed') {
        throw new Error(job.error || 'Download failed');
      }
    }

    throw new Error('Download timed out (>20 menit)');
  } catch (err) {
    toast.error(err.message || 'Unknown error');
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
      <p class="cf-empty-sub">${escHtml(msg)}</p>
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
      <p class="cf-empty-sub">${escHtml(msg)}</p>
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

function cfParseTimeInput(str) {
  str = str.trim();
  const hms = str.match(/^(\d+):(\d{1,2}):(\d{1,2})$/);
  if (hms) return parseInt(hms[1]) * 3600 + parseInt(hms[2]) * 60 + parseInt(hms[3]);
  const ms = str.match(/^(\d+):(\d{1,2})$/);
  if (ms) return parseInt(ms[1]) * 60 + parseInt(ms[2]);
  const n = parseFloat(str);
  return isNaN(n) ? NaN : n;
}
