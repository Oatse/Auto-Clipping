/**
 * multi_pov.js — Workspace 05 (Multi POV) frontend.
 *
 * Flow:
 *   1. User fills in 2-5 source URLs + label + optional start offset.
 *   2. POST /api/multi-pov/jobs  → job_id.
 *   3. SSE log stream updates the log panel in real-time.
 *   4. Poll GET /api/multi-pov/jobs/{id} for structured progress.
 *   5. When status==="analyzed" render POV groups + unmatched clips.
 *   6. User selects groups → Download selected (sequential, bot-safe).
 *
 * Fixes (2026-06-21):
 *   - Log panel was hidden with !important — now shows during job run.
 *   - Source pipeline cards now show expandable clip list per source.
 *   - Multi-POV groups show all found events with per-group download button.
 *   - "Download all groups" replaced by checkbox selection + Download selected.
 *   - Selected bulk download triggers groups SEQUENTIALLY to avoid yt-dlp bot detection.
 */

import { escHtml, toast } from './utils.js';

// ── State ─────────────────────────────────────────────────────────────────────
let mpovJobId        = null;
let mpovSSE          = null;
let mpovPollInterval = null;
let mpovSourceCount  = 2;
const MPOV_MAX_SOURCES = 5;

let mpovMode    = 'single-shot';  // managed by segmented pill
let mpovProfile = 'vtuber';       // managed by segmented pill

// Set of group_ids currently selected for download
const mpovSelectedGroups = new Set();

// ── Helpers ───────────────────────────────────────────────────────────────────

function parseOffset(raw) {
  const s = (raw || '').trim();
  if (!s || s === '0') return 0;
  const parts = s.split(':').map(Number);
  if (parts.length === 3) return parts[0] * 3600 + parts[1] * 60 + parts[2];
  if (parts.length === 2) return parts[0] * 60 + parts[1];
  return parseFloat(s) || 0;
}

function statusBadgeClass(status) {
  const map = {
    queued: 'badge--neutral',
    extracting: 'badge--info',
    matching: 'badge--info',
    analyzing: 'badge--info',
    analyzed: 'badge--success',
    downloading: 'badge--info',
    completed: 'badge--success',
    failed: 'badge--error',
  };
  return map[status] || 'badge--neutral';
}

function fmtTime(secs) {
  if (!secs && secs !== 0) return '—';
  const h = Math.floor(secs / 3600);
  const m = Math.floor((secs % 3600) / 60);
  const s = Math.floor(secs % 60);
  return h > 0
    ? `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`
    : `${m}:${String(s).padStart(2, '0')}`;
}

function confBadge(confidence, isMultiPov) {
  const pct = Math.round((confidence || 0) * 100);
  const cls = isMultiPov ? 'badge--success' : 'badge--neutral';
  return `<span class="badge ${cls}">${pct}% confidence</span>`;
}

// ── DOM references ────────────────────────────────────────────────────────────

const $ = id => document.getElementById(id);

const elSourceList       = $('mpovSourceList');
const elAddSource        = $('mpovAddSource');
const elRemoveSource     = $('mpovRemoveSource');
const elStartBtn         = $('mpovStartBtn');
const elInstructions     = $('mpovInstructions');
const elModel            = $('mpovModel');
const elAudio            = $('mpovAudio');
const elChat             = $('mpovChat');

// Segmented pill groups
const elModeGroup        = document.querySelectorAll('.segmented [data-mode]');
const elProfileGroup     = document.querySelectorAll('#mpovScoringProfile [data-profile]');

const elLogPanel         = $('mpovLogPanel');
const elLog              = $('mpovLog');
const elStatusBadge      = $('mpovStatusBadge');

const elSourceProgress   = $('mpovSourceProgress');
const elSourceCards      = $('mpovSourceCards');

const elGroupsPanel      = $('mpovGroupsPanel');
const elGroupsList       = $('mpovGroupsList');
const elC1Warning        = $('mpovGroupsC1Warning');

// New selection controls
const elSelectedBadge      = $('mpovSelectedBadge');
const elSelectAllBtn       = $('mpovSelectAllBtn');
const elDownloadSelectedBtn = $('mpovDownloadSelectedBtn');

const elUnmatchedPanel   = $('mpovUnmatchedPanel');
const elUnmatchedList    = $('mpovUnmatchedList');
const elUnmatchedBadge   = $('mpovUnmatchedBadge');

// ── Source row management ─────────────────────────────────────────────────────

function updateAddRemoveButtons() {
  elAddSource.disabled   = mpovSourceCount >= MPOV_MAX_SOURCES;
  elRemoveSource.style.display = mpovSourceCount > 2 ? 'inline-flex' : 'none';
}

function addSourceRow() {
  if (mpovSourceCount >= MPOV_MAX_SOURCES) return;
  const idx = mpovSourceCount;
  mpovSourceCount++;

  const div = document.createElement('div');
  div.className = 'mpov-source';
  div.dataset.idx = idx;
  div.innerHTML = `
    <div style="display:grid; gap: var(--s-3);">
      <div class="field">
        <label class="field__label" for="mpovUrl${idx}">Source ${idx + 1} · YouTube URL</label>
        <input class="input" id="mpovUrl${idx}" type="url" placeholder="https://www.youtube.com/watch?v=…" />
      </div>
      <div class="row" style="gap: var(--s-4);">
        <div class="field" style="flex:1;">
          <label class="field__label" for="mpovLabel${idx}">Label (optional)</label>
          <input class="input" id="mpovLabel${idx}" type="text" placeholder="e.g. Observer cam" maxlength="40" />
        </div>
        <div class="field" style="flex:0 0 140px;">
          <label class="field__label" for="mpovOffset${idx}">Start offset (mm:ss)</label>
          <input class="input input--mono" id="mpovOffset${idx}" type="text" value="0" placeholder="03:54" />
        </div>
      </div>
    </div>`;
  elSourceList.appendChild(div);
  updateAddRemoveButtons();
}

function removeSourceRow() {
  if (mpovSourceCount <= 2) return;
  const rows = elSourceList.querySelectorAll('.mpov-source');
  const last = rows[rows.length - 1];
  if (last && parseInt(last.dataset.idx) >= 2) {
    last.remove();
    mpovSourceCount--;
    updateAddRemoveButtons();
  }
}

elAddSource.addEventListener('click', addSourceRow);
elRemoveSource.addEventListener('click', removeSourceRow);
updateAddRemoveButtons();

// ── Segmented pill: Detection mode ─────────────────────────────────────────

elModeGroup.forEach(btn => {
  btn.addEventListener('click', () => {
    elModeGroup.forEach(b => b.classList.remove('is-active'));
    btn.classList.add('is-active');
    mpovMode = btn.dataset.mode;
  });
});

// ── Segmented pill: Scoring profile ──────────────────────────────────────

elProfileGroup.forEach(btn => {
  btn.addEventListener('click', () => {
    elProfileGroup.forEach(b => b.classList.remove('is-active'));
    btn.classList.add('is-active');
    mpovProfile = btn.dataset.profile;
  });
});

// ── Enable Add Source when both required URLs are filled ─────────────────

function checkEnableAdd() {
  const url0 = ($('mpovUrl0') || {}).value?.trim();
  const url1 = ($('mpovUrl1') || {}).value?.trim();
  elAddSource.disabled = !url0 || !url1 || mpovSourceCount >= MPOV_MAX_SOURCES;
}

document.addEventListener('input', e => {
  if (e.target.id === 'mpovUrl0' || e.target.id === 'mpovUrl1') checkEnableAdd();
});

// ── Collect form sources ─────────────────────────────────────────────────────

function collectSources() {
  const rows = elSourceList.querySelectorAll('.mpov-source');
  return Array.from(rows).map((row, i) => ({
    url:          (row.querySelector(`#mpovUrl${i}`) || {}).value?.trim() || '',
    label:        (row.querySelector(`#mpovLabel${i}`) || {}).value?.trim() || '',
    start_offset: parseOffset((row.querySelector(`#mpovOffset${i}`) || {}).value),
  }));
}

// ── Start job ─────────────────────────────────────────────────────────────────

elStartBtn.addEventListener('click', async () => {
  const sources = collectSources().filter(s => s.url);

  if (sources.length < 2) {
    toast('Please enter at least 2 YouTube URLs.', 'error');
    return;
  }

  if (mpovJobId) {
    // Reset previous job state
    if (mpovSSE) { mpovSSE.close(); mpovSSE = null; }
    if (mpovPollInterval) { clearInterval(mpovPollInterval); mpovPollInterval = null; }
    mpovJobId = null;
  }

  // Clear selection state
  mpovSelectedGroups.clear();
  updateSelectionUI();

  elStartBtn.disabled = true;
  elStartBtn.querySelector('.btn-text').textContent = 'Starting…';

  // Reset UI
  elLog.innerHTML = '';
  elSourceProgress.style.display = 'none';
  elGroupsPanel.style.display = 'none';
  elUnmatchedPanel.style.display = 'none';

  try {
    const resp = await fetch('/api/multi-pov/jobs', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        sources,
        instructions: elInstructions.value.trim(),
        mode: mpovMode,
        scoring_profile: mpovProfile,
        model: elModel.value,
        enable_audio_signals: elAudio.checked,
        enable_chat_signals: elChat.checked,
      }),
    });

    if (!resp.ok) {
      const err = await resp.json().catch(() => ({ detail: resp.statusText }));
      throw new Error(err.detail || 'Request failed');
    }

    const job = await resp.json();
    mpovJobId = job.id;

    elStartBtn.querySelector('.btn-text').textContent = 'Analyzing…';
    renderSourceProgressCards(job.source_results || []);
    elSourceProgress.style.display = '';

    startSSE(mpovJobId);
    startPoll(mpovJobId);

  } catch (err) {
    toast(`Error: ${err.message}`, 'error');
    elStartBtn.disabled = false;
    elStartBtn.querySelector('.btn-text').textContent = 'Find multi-POV moments';
  }
});

// ── SSE log stream ────────────────────────────────────────────────────────────

function startSSE(jobId) {
  if (mpovSSE) mpovSSE.close();
  mpovSSE = new EventSource(`/api/multi-pov/jobs/${jobId}/log`);

  mpovSSE.onmessage = e => {
    const data = JSON.parse(e.data);
    if (data.line) appendLog(data.line);
    if (data.done) {
      mpovSSE.close();
      mpovSSE = null;
    }
  };
  mpovSSE.onerror = () => { mpovSSE.close(); mpovSSE = null; };
}

function appendLog(line) {
  const el = document.createElement('div');
  el.className = 'log__line';
  el.textContent = line;
  elLog.appendChild(el);
  elLog.scrollTop = elLog.scrollHeight;
}

// ── Polling for structured state ──────────────────────────────────────────────

function startPoll(jobId) {
  if (mpovPollInterval) clearInterval(mpovPollInterval);
  mpovPollInterval = setInterval(() => pollJob(jobId), 1500);
}

async function pollJob(jobId) {
  try {
    const resp = await fetch(`/api/multi-pov/jobs/${jobId}`);
    if (!resp.ok) return;
    const job = await resp.json();
    applyJobState(job);

    const terminal = ['analyzed', 'completed', 'failed'];
    if (terminal.includes(job.status)) {
      clearInterval(mpovPollInterval);
      mpovPollInterval = null;
      elStartBtn.disabled = false;
      elStartBtn.querySelector('.btn-text').textContent = 'Find multi-POV moments';
    }
  } catch (_) {}
}

// ── Apply job state to UI ─────────────────────────────────────────────────────

function applyJobState(job) {
  // Status badge
  elStatusBadge.textContent = job.phase_label || job.status;
  elStatusBadge.className = `badge ${statusBadgeClass(job.status)}`;

  // Per-source progress cards
  if (job.source_results?.length) {
    renderSourceProgressCards(job.source_results);
    elSourceProgress.style.display = '';
  }

  // Results
  if (job.status === 'analyzed' || job.status === 'completed') {
    renderGroups(job);
    renderUnmatched(job);
  }
}

// ── Source progress cards ─────────────────────────────────────────────────────

function renderSourceProgressCards(sourceResults) {
  elSourceCards.innerHTML = '';
  sourceResults.forEach(r => {
    const card = document.createElement('div');
    card.className = 'surface';
    card.style.padding = 'var(--s-4)';

    const statusText = {
      pending: 'Pending…',
      extracting: 'Extracting transcript…',
      analyzing: 'AI analysis…',
      done: `Done · ${r.clips_found} clip(s)`,
      failed: `Failed: ${r.error || 'unknown error'}`,
    }[r.status] || r.sub_phase || r.status;

    const pct = Math.round(r.progress_pct || 0);
    const badgeCls = r.status === 'done' ? 'badge--success' : r.status === 'failed' ? 'badge--error' : 'badge--info';

    // Build clip list for this source (collapsible)
    const clips = r.clips || [];
    const clipsHtml = clips.length
      ? `<details style="margin-top:var(--s-3);">
          <summary style="cursor:pointer; font-size:0.85em; color:var(--c-muted); user-select:none;">
            Show ${clips.length} event(s) found
          </summary>
          <div style="display:grid; gap:var(--s-2); margin-top:var(--s-2); max-height:280px; overflow-y:auto;">
            ${clips.map((c, idx) => `
              <div style="display:flex; justify-content:space-between; align-items:flex-start; padding:var(--s-2) var(--s-3); background:var(--c-surface-2,rgba(0,0,0,.03)); border-radius:var(--r-1); gap:var(--s-3);">
                <div style="min-width:0; flex:1;">
                  <div style="font-size:0.85em; font-weight:600; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;" title="${escHtml(c.title || '')}">
                    ${escHtml(c.title || `Clip ${idx + 1}`)}
                  </div>
                  <div style="font-size:0.78em; color:var(--c-muted); margin-top:2px;">
                    ${fmtTime(c.start)} → ${fmtTime(c.end)}
                    (${Math.round((c.end || 0) - (c.start || 0))}s)
                    ${c.score != null ? `· score ${Math.round(c.score * 100)}` : ''}
                  </div>
                  ${c.reason ? `<div style="font-size:0.75em; color:var(--c-muted); margin-top:2px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;" title="${escHtml(c.reason)}">${escHtml(c.reason.slice(0, 90))}${c.reason.length > 90 ? '…' : ''}</div>` : ''}
                </div>
              </div>`).join('')}
          </div>
        </details>`
      : '';

    card.innerHTML = `
      <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom: var(--s-3);">
        <strong>${escHtml(r.label || r.url.slice(0, 48))}</strong>
        <span class="badge ${badgeCls}">${escHtml(statusText)}</span>
      </div>
      <div class="progress-bar" style="height:6px; background:var(--c-border); border-radius:99px; overflow:hidden;">
        <div style="height:100%; width:${pct}%; background:var(--c-lime); transition:width .4s ease;"></div>
      </div>
      ${clipsHtml}`;
    elSourceCards.appendChild(card);
  });
}

// ── Selection UI helpers ──────────────────────────────────────────────────────

function updateSelectionUI() {
  const count = mpovSelectedGroups.size;

  if (elSelectedBadge) {
    elSelectedBadge.textContent = `${count} selected`;
    elSelectedBadge.style.display = count > 0 ? '' : 'none';
  }

  if (elDownloadSelectedBtn) {
    elDownloadSelectedBtn.style.display = count > 0 ? '' : 'none';
    elDownloadSelectedBtn.textContent = count > 0
      ? `↓ Download ${count} group${count !== 1 ? 's' : ''}`
      : '↓ Download selected';
  }

  // Update all checkboxes to match state
  elGroupsList.querySelectorAll('.mpov-group-check').forEach(cb => {
    cb.checked = mpovSelectedGroups.has(cb.dataset.groupId);
  });
}

function getVisibleGroupIds() {
  return Array.from(
    elGroupsList.querySelectorAll('.mpov-group-check')
  ).map(cb => cb.dataset.groupId);
}

// ── POV Groups rendering ──────────────────────────────────────────────────────

function renderGroups(job) {
  const groups = job.pov_groups || [];
  const multiGroups = groups.filter(g => g.is_multi_pov);
  const singleGroups = groups.filter(g => !g.is_multi_pov);

  elGroupsPanel.style.display = '';
  elGroupsList.innerHTML = '';

  const allGroups = [...multiGroups, ...singleGroups];
  elC1Warning.style.display = multiGroups.length === 0 ? '' : 'none';

  // Show/hide select-all button
  if (elSelectAllBtn) {
    elSelectAllBtn.style.display = allGroups.length > 0 ? '' : 'none';
  }

  if (allGroups.length === 0) {
    elGroupsList.innerHTML = '<p class="empty-state">No moment groups found.</p>';
    updateSelectionUI();
    return;
  }

  allGroups.forEach((g, groupDisplayIdx) => {
    const groupId = g.group_id || '';
    const isSelected = mpovSelectedGroups.has(groupId);
    const perspectives = g.perspectives || [];
    const hasAnyFile = perspectives.some(p => p.file_path);

    // ── Group header surface ──────────────────────────────────────────────
    const groupWrap = document.createElement('div');
    groupWrap.className = 'surface';
    groupWrap.style.cssText = 'padding: var(--s-4) var(--s-5);';

    // Header row: checkbox + title + badges + download-group button
    const headerDiv = document.createElement('div');
    headerDiv.style.cssText = 'display:flex; align-items:flex-start; gap:var(--s-3); margin-bottom:var(--s-4);';
    headerDiv.innerHTML = `
      <label style="display:flex; align-items:center; cursor:pointer; padding-top:3px; flex-shrink:0;">
        <input type="checkbox" class="mpov-group-check" data-group-id="${escHtml(groupId)}"
          ${isSelected ? 'checked' : ''} style="width:16px;height:16px;cursor:pointer;" />
      </label>
      <div style="flex:1; min-width:0;">
        <div style="display:flex; gap:var(--s-2); align-items:center; flex-wrap:wrap; margin-bottom:var(--s-1);">
          <h3 style="margin:0; font-size:var(--fs-16);">${escHtml(g.title || '(no title)')}</h3>
          ${confBadge(g.confidence, g.is_multi_pov)}
          ${g.is_multi_pov
            ? `<span class="badge badge--lime">${perspectives.length} POV${perspectives.length !== 1 ? 's' : ''}</span>`
            : `<span class="badge badge--neutral">Single-source</span>`}
        </div>
        ${g.reason ? `<p style="margin:0; color:var(--c-muted); font-size:0.85em; line-height:1.4;">${escHtml(g.reason)}</p>` : ''}
      </div>
      <div style="flex-shrink:0;">
        ${hasAnyFile
          ? `<span class="badge badge--success">Downloaded</span>`
          : `<button class="btn btn--sm mpov-dl-group-btn"
               data-job-id="${escHtml(job.id)}"
               data-group-id="${escHtml(groupId)}">↓ Download group</button>`}
      </div>`;

    headerDiv.querySelector('.mpov-group-check').addEventListener('change', e => {
      if (e.target.checked) mpovSelectedGroups.add(groupId);
      else mpovSelectedGroups.delete(groupId);
      updateSelectionUI();
    });

    const dlGroupBtn = headerDiv.querySelector('.mpov-dl-group-btn');
    if (dlGroupBtn) {
      dlGroupBtn.addEventListener('click', () => downloadGroup(job.id, groupId, dlGroupBtn));
    }

    groupWrap.appendChild(headerDiv);

    // ── Per-perspective clip cards (cf-clip-card style) ───────────────────
    const clipsGrid = document.createElement('div');
    clipsGrid.className = 'cf-clips-grid';
    clipsGrid.style.cssText = 'padding:0; background:none; border:none; box-shadow:none;';

    perspectives.forEach((p, pIdx) => {
      const duration = (p.end || 0) - (p.start || 0);
      const durFmt = duration >= 60
        ? Math.floor(duration / 60) + 'm ' + Math.floor(duration % 60) + 's'
        : Math.floor(duration) + 's';

      const scoreHtml = renderPerspectiveScore(p.score);

      const card = document.createElement('div');
      card.className = 'cf-clip-card';
      card.style.setProperty('--cf-card-i', String(groupDisplayIdx * 10 + pIdx));

      if (p.file_path) {
        // Downloaded — show video player + download link
        card.innerHTML = `
          <div class="cf-clip-video-wrap">
            <video class="cf-clip-video" preload="metadata"
                   src="/api/multi-pov/jobs/${escHtml(job.id)}/groups/${escHtml(groupId)}/${p.source_idx}/stream"></video>
            <button class="cf-clip-play-btn">
              <svg width="24" height="24" viewBox="0 0 24 24" fill="white"><path d="M8 5v14l11-7z"/></svg>
            </button>
            <span class="cf-clip-duration">${durFmt}</span>
          </div>
          <div class="cf-clip-info">
            <div class="cf-clip-number">
              <span class="badge badge--neutral" style="font-size:0.7em; margin-right:4px;">Src ${p.source_idx}</span>
              ${escHtml(p.label || `Source ${p.source_idx}`)}
            </div>
            <div class="cf-clip-title">${escHtml(p.title || g.title || 'Clip')}</div>
            <div class="cf-clip-time">${fmtTime(p.start)} – ${fmtTime(p.end)} (${durFmt})</div>
            ${scoreHtml}
            ${p.reason ? `<div class="cf-clip-reason">${escHtml(p.reason)}</div>` : ''}
          </div>
          <div class="cf-clip-actions">
            <a class="cf-clip-download"
               href="/api/multi-pov/jobs/${escHtml(job.id)}/groups/${escHtml(groupId)}/${p.source_idx}"
               download>
              <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
                <path d="M7 1v9M3 7l4 4 4-4M1 13h12" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
              </svg>
              Download
            </a>
          </div>`;

        const video = card.querySelector('.cf-clip-video');
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
        // Not yet downloaded — show placeholder + download button
        card.innerHTML = `
          <div class="cf-clip-placeholder">
            <svg width="40" height="40" viewBox="0 0 40 40" fill="none">
              <rect x="4" y="8" width="32" height="24" rx="3" stroke="currentColor" stroke-width="1.5" opacity="0.4"/>
              <path d="M16 15l10 5-10 5V15z" fill="currentColor" opacity="0.4"/>
            </svg>
          </div>
          <div class="cf-clip-info">
            <div class="cf-clip-number">
              <span class="badge badge--neutral" style="font-size:0.7em; margin-right:4px;">Src ${p.source_idx}</span>
              ${escHtml(p.label || `Source ${p.source_idx}`)}
            </div>
            <div class="cf-clip-title">${escHtml(p.title || g.title || 'Clip')}</div>
            <div class="cf-clip-time">${fmtTime(p.start)} – ${fmtTime(p.end)} (${durFmt})</div>
            ${scoreHtml}
            ${p.reason ? `<div class="cf-clip-reason">${escHtml(p.reason)}</div>` : ''}
          </div>
          <div class="cf-clip-actions">
            <button class="cf-clip-download mpov-dl-perspective-btn"
              data-job-id="${escHtml(job.id)}"
              data-group-id="${escHtml(groupId)}"
              data-source-idx="${p.source_idx}">
              <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
                <path d="M7 1v9M3 7l4 4 4-4M1 13h12" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
              </svg>
              Download
            </button>
          </div>`;

        const dlPerspBtn = card.querySelector('.mpov-dl-perspective-btn');
        dlPerspBtn.addEventListener('click', () => downloadPerspective(job.id, groupId, p.source_idx, dlPerspBtn));
      }

      clipsGrid.appendChild(card);
    });

    groupWrap.appendChild(clipsGrid);
    elGroupsList.appendChild(groupWrap);
  });

  updateSelectionUI();
}

// ── Unmatched clips rendering (cf-clip-card style) ────────────────────────────

function renderUnmatched(job) {
  const unmatched = job.unmatched_clips || [];
  if (!unmatched.length) {
    elUnmatchedPanel.style.display = 'none';
    return;
  }

  elUnmatchedPanel.style.display = '';
  elUnmatchedBadge.textContent = unmatched.length;
  elUnmatchedList.innerHTML = '';

  // Render each unmatched clip as a cf-clip-card (no download — info-only)
  unmatched.forEach((clip, idx) => {
    const duration = (clip.end || 0) - (clip.start || 0);
    const durFmt = duration >= 60
      ? Math.floor(duration / 60) + 'm ' + Math.floor(duration % 60) + 's'
      : Math.floor(duration) + 's';

    const card = document.createElement('div');
    card.className = 'cf-clip-card';
    card.style.setProperty('--cf-card-i', String(idx));
    card.innerHTML = `
      <div class="cf-clip-placeholder">
        <svg width="40" height="40" viewBox="0 0 40 40" fill="none">
          <rect x="4" y="8" width="32" height="24" rx="3" stroke="currentColor" stroke-width="1.5" opacity="0.4"/>
          <path d="M16 15l10 5-10 5V15z" fill="currentColor" opacity="0.4"/>
        </svg>
      </div>
      <div class="cf-clip-info">
        <div class="cf-clip-number">
          <span class="badge badge--neutral" style="font-size:0.7em; margin-right:4px;">Src ${clip.source_idx}</span>
        </div>
        <div class="cf-clip-title">${escHtml(clip.title || 'Clip')}</div>
        <div class="cf-clip-time">${fmtTime(clip.start)} – ${fmtTime(clip.end)} (${durFmt})</div>
        ${clip.reason ? `<div class="cf-clip-reason">${escHtml(clip.reason.slice(0, 120))}</div>` : ''}
      </div>`;
    elUnmatchedList.appendChild(card);
  });
}

// ── Score renderer (mirrors cfRenderScore from clipfinder.js) ─────────────────

function renderPerspectiveScore(score) {
  if (!score || typeof score.total !== 'number') return '';
  const dims = [
    ['Hook',    score.retention_hook],
    ['Emotion', score.emotional_intensity],
    ['Cycle',   score.completeness],
    ['Replay',  score.replayability],
  ];
  return `
    <div class="cf-clip-score">
      <div class="cf-clip-score-total">${(score.total || 0).toFixed(1)}<small>/10</small></div>
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
            </div>`;
        }).join('')}
      </div>
    </div>`;
}

// ── Download: single group (fire-and-forget, polls for result) ────────────────

function downloadGroup(jobId, groupId, btn) {
  if (btn) { btn.disabled = true; btn.textContent = 'Downloading…'; }
  fetch(`/api/multi-pov/jobs/${jobId}/download-group/${groupId}`, { method: 'POST' })
    .then(async r => {
      if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
      toast('Group download started.', 'success');
      startPoll(jobId);
      if (btn) btn.textContent = '↓ Downloading…';
    })
    .catch(err => {
      toast(`Download error: ${err.message}`, 'error');
      if (btn) { btn.disabled = false; btn.textContent = '↓ Download group'; }
    });
}

// ── Download: single perspective clip ─────────────────────────────────────────

async function downloadPerspective(jobId, groupId, sourceIdx, btn) {
  if (btn) {
    btn.disabled = true;
    btn.innerHTML = `
      <svg class="cf-spin" width="14" height="14" viewBox="0 0 14 14" fill="none">
        <circle cx="7" cy="7" r="5.5" stroke="currentColor" stroke-width="1.5" stroke-dasharray="20 12" stroke-linecap="round"/>
      </svg>
      Downloading…`;
  }

  try {
    // Trigger the per-group download (which also downloads this perspective)
    const resp = await fetch(
      `/api/multi-pov/jobs/${jobId}/download-group/${groupId}?wait=true`,
      { method: 'POST' }
    );
    if (!resp.ok) throw new Error((await resp.json().catch(() => ({}))).detail || resp.statusText);

    toast('Clip downloaded. Reloading results…', 'success');
    // Re-poll to refresh file_path fields
    const jobResp = await fetch(`/api/multi-pov/jobs/${jobId}`);
    if (jobResp.ok) {
      const job = await jobResp.json();
      renderGroups(job);
      renderUnmatched(job);
    }
  } catch (err) {
    toast(`Download error: ${err.message}`, 'error');
    if (btn) {
      btn.disabled = false;
      btn.innerHTML = `
        <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
          <path d="M7 1v9M3 7l4 4 4-4M1 13h12" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
        </svg>
        Download`;
    }
  }
}

// ── Select-all button ─────────────────────────────────────────────────────────

if (elSelectAllBtn) {
  elSelectAllBtn.addEventListener('click', () => {
    const ids = getVisibleGroupIds();
    const allSelected = ids.every(id => mpovSelectedGroups.has(id));
    if (allSelected) {
      // Deselect all
      ids.forEach(id => mpovSelectedGroups.delete(id));
      elSelectAllBtn.textContent = 'Select all';
    } else {
      // Select all
      ids.forEach(id => mpovSelectedGroups.add(id));
      elSelectAllBtn.textContent = 'Deselect all';
    }
    updateSelectionUI();
  });
}

// ── Download selected groups (SEQUENTIAL to avoid yt-dlp bot detection) ───────

if (elDownloadSelectedBtn) {
  elDownloadSelectedBtn.addEventListener('click', async () => {
    if (!mpovJobId || mpovSelectedGroups.size === 0) return;

    const groupIds = Array.from(mpovSelectedGroups);
    elDownloadSelectedBtn.disabled = true;
    elDownloadSelectedBtn.textContent = `Downloading 0 / ${groupIds.length}…`;

    startPoll(mpovJobId);

    let successCount = 0;
    let failCount = 0;

    // Sequential download — avoids triggering yt-dlp bot detection
    for (let i = 0; i < groupIds.length; i++) {
      const groupId = groupIds[i];
      elDownloadSelectedBtn.textContent = `Downloading ${i + 1} / ${groupIds.length}…`;

      try {
        // Use ?wait=true so the server blocks until this group is fully downloaded.
        // This guarantees true sequential execution — no concurrent yt-dlp sessions.
        const resp = await fetch(
          `/api/multi-pov/jobs/${mpovJobId}/download-group/${groupId}?wait=true`,
          { method: 'POST' }
        );
        if (!resp.ok) {
          const err = await resp.json().catch(() => ({ detail: resp.statusText }));
          throw new Error(err.detail || resp.statusText);
        }
        successCount++;

        // Brief pause between groups to be polite to YouTube
        if (i < groupIds.length - 1) {
          await new Promise(r => setTimeout(r, 800));
        }
      } catch (err) {
        failCount++;
        appendLog(`[ERROR] Group "${groupId}" download failed: ${err.message}`);
      }
    }

    const summary = failCount === 0
      ? `All ${successCount} group(s) downloaded successfully.`
      : `${successCount} group(s) downloaded, ${failCount} failed.`;
    toast(summary, failCount === 0 ? 'success' : 'error');

    elDownloadSelectedBtn.disabled = false;
    elDownloadSelectedBtn.textContent = `↓ Download ${groupIds.length !== 1 ? 'selected' : ''}`;
    updateSelectionUI();
  });
}
