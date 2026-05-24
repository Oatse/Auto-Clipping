/* ============================================================================
 * nav.js — Floating pill nav controller. Mobile sheet, system-status fetch,
 *           and the clickable system-status popup (engines + ElevenLabs quota).
 * ============================================================================ */

const $ = (id) => document.getElementById(id);

function bootMobileSheet() {
  const btn   = $("navMenuBtn");
  const sheet = $("navSheet");
  if (!btn || !sheet) return;

  const setOpen = (open) => {
    btn.classList.toggle("is-open", open);
    sheet.classList.toggle("is-open", open);
    btn.setAttribute("aria-expanded", String(open));
    sheet.setAttribute("aria-hidden", String(!open));
    document.body.classList.toggle("no-scroll", open);
  };

  btn.addEventListener("click", () => setOpen(!sheet.classList.contains("is-open")));

  // Close on link click + Esc
  sheet.querySelectorAll("a").forEach((a) => a.addEventListener("click", () => setOpen(false)));
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && sheet.classList.contains("is-open")) setOpen(false);
  });
}

// ── System status: pill + clickable popup ─────────────────────────────────
let _systemPayload = null;       // last /api/system response, reused for popup
let _quotaPromise  = null;       // memoised quota fetch (1 per page load)

async function bootSystemStatus() {
  const el = $("systemStatus");
  if (!el) return;
  const dot = el.querySelector(".dot");
  const txt = el.querySelector(".status-text");

  try {
    const r = await fetch("/api/system");
    if (!r.ok) throw new Error("system " + r.status);
    const data = await r.json();
    _systemPayload = data;

    const eleven = !!data?.env?.elevenlabs_key_set;
    const gemini = !!data?.env?.gemini_keys_set;
    const ffmpeg = !!data?.packages?.ffmpeg;

    const okAll = eleven && gemini && ffmpeg;
    const partial = eleven || gemini || ffmpeg;

    if (okAll) {
      el.dataset.state = "ok";
      txt.textContent = "All systems online";
    } else if (partial) {
      el.dataset.state = "warn";
      txt.textContent = "Partial: check API keys";
    } else {
      el.dataset.state = "err";
      txt.textContent = "No engines configured";
    }
  } catch (err) {
    el.dataset.state = "err";
    if (txt) txt.textContent = "Offline";
  }

  // Popup is rendered lazily on the first open; re-render whenever
  // the user re-opens to surface fresh quota + engine state.
  bindStatusPopup();
}

function bindStatusPopup() {
  const trigger = $("systemStatus");
  const popup   = $("systemStatusPopup");
  if (!trigger || !popup) return;

  // Avoid double-binding if bootSystemStatus runs more than once.
  if (trigger.dataset.popupBound === "1") return;
  trigger.dataset.popupBound = "1";

  const setOpen = (open) => {
    popup.dataset.open = String(open);
    popup.hidden = !open;
    trigger.setAttribute("aria-expanded", String(open));
    if (open) renderStatusPopup();
  };

  trigger.addEventListener("click", (e) => {
    e.stopPropagation();
    setOpen(popup.dataset.open !== "true");
  });

  document.addEventListener("click", (e) => {
    if (popup.dataset.open !== "true") return;
    if (popup.contains(e.target) || trigger.contains(e.target)) return;
    setOpen(false);
  });

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && popup.dataset.open === "true") setOpen(false);
  });
}

function renderStatusPopup() {
  const popup = $("systemStatusPopup");
  const grid  = $("systemStatusPopupGrid");
  const state = $("systemStatusPopupState");
  if (!popup || !grid) return;

  const data = _systemPayload || {};
  const ffmpeg     = !!data?.packages?.ffmpeg;
  const elevenlabs = !!data?.packages?.elevenlabs;
  const elevenKey  = !!data?.env?.elevenlabs_key_set;
  const gemini     = !!data?.env?.gemini_keys_set;
  const deepl      = !!data?.env?.deepl_key_set;
  const pycaps     = !!data?.packages?.pycaps;
  const cuda       = !!data?.cuda_available;

  const okAll = ffmpeg && (elevenlabs || elevenKey) && gemini;
  const overall = okAll ? "ok" : ((ffmpeg || elevenKey || gemini) ? "warn" : "err");
  popup.dataset.state = overall;
  if (state) {
    state.textContent = overall === "ok"
      ? "All systems online"
      : overall === "warn" ? "Partial — check setup" : "No engines configured";
  }

  // Items: required engines first (ok/err), then optional (ok/warn).
  const items = [
    { label: "FFmpeg",     ok: ffmpeg,     val: ffmpeg ? "ready" : "missing" },
    { label: "ElevenLabs", ok: elevenKey,  val: elevenKey ? "ready" : "missing key" },
    { label: "Gemini",     ok: gemini,     val: gemini ? "ready" : "missing key" },
    { label: "DeepL",      ok: deepl, optional: true,
      val: deepl ? "ready" : "optional" },
    { label: "PyCaps",     ok: pycaps, optional: true,
      val: pycaps ? "ready" : "optional" },
    { label: "GPU",        ok: cuda, optional: true,
      val: cuda ? (data.gpu_name || "CUDA") : "CPU only" },
  ];

  grid.innerHTML = items.map(i => {
    const cls = i.ok ? "is-ok" : (i.optional ? "is-warn" : "is-err");
    return `<div class="nav__status-popup-row ${cls}"><span>${i.label}</span><span>${i.val}</span></div>`;
  }).join("");

  renderQuotaSection();
}

async function renderQuotaSection() {
  const body = $("systemStatusPopupQuotaBody");
  if (!body) return;

  if (!_quotaPromise) {
    _quotaPromise = fetch("/api/elevenlabs/quota")
      .then(r => r.ok ? r.json() : Promise.reject(new Error("quota " + r.status)))
      .catch(err => ({ _error: err.message || String(err) }));
  }

  body.innerHTML = `<div class="nav__status-popup-row is-loading"><span>Loading quota…</span><span>—</span></div>`;
  const data = await _quotaPromise;

  if (!data || data._error) {
    body.innerHTML = `<div class="nav__status-popup-row is-warn"><span>Could not load quota</span><span>retry</span></div>`;
    return;
  }

  const keys = Array.isArray(data.keys) ? data.keys : [];
  if (!keys.length) {
    body.innerHTML = `<div class="nav__status-popup-row is-warn"><span>No keys configured</span><span>—</span></div>`;
    return;
  }

  body.innerHTML = keys.map(k => renderQuotaRow(k)).join("");
}

function renderQuotaRow(k) {
  const label = escapeHtml(k.key_label || "key");
  if (k.error) {
    return `<div class="nav__status-popup-quota" data-level="err">
      <div class="nav__status-popup-quota-head"><strong>${label}</strong><span>error</span></div>
      <div class="nav__status-popup-quota-foot">${escapeHtml(k.error)}</div>
    </div>`;
  }

  const used  = Number(k.character_count) || 0;
  const limit = Number(k.character_limit) || 0;
  const pct   = limit > 0 ? Math.min(100, Math.round((used / limit) * 100)) : 0;
  const remaining = Math.max(0, limit - used);
  const tier = (k.tier || "free").replace(/_/g, " ");

  let resetStr = "";
  if (k.next_reset_unix) {
    const d = new Date(k.next_reset_unix * 1000);
    if (!isNaN(d.getTime())) resetStr = ` · resets ${d.toLocaleDateString()}`;
  }

  const level = pct > 80 ? "err" : pct > 50 ? "warn" : "ok";

  return `<div class="nav__status-popup-quota" data-level="${level}">
    <div class="nav__status-popup-quota-head">
      <span><strong>${label}</strong> · ${escapeHtml(tier)}</span>
      <span>${pct}% used</span>
    </div>
    <div class="nav__status-popup-quota-bar"><span style="width:${pct}%"></span></div>
    <div class="nav__status-popup-quota-foot">
      ${remaining.toLocaleString()} / ${limit.toLocaleString()} chars remaining${resetStr}
    </div>
  </div>`;
}

function escapeHtml(str) {
  return String(str ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

document.addEventListener("DOMContentLoaded", () => {
  bootMobileSheet();
  bootSystemStatus();
});
