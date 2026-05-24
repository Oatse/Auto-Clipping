/* ============================================================================
 * nav.js — Floating pill nav controller. Mobile sheet, system-status fetch.
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

async function bootSystemStatus() {
  const el = $("systemStatus");
  if (!el) return;
  const dot = el.querySelector(".dot");
  const txt = el.querySelector(".status-text");

  try {
    const r = await fetch("/api/system");
    if (!r.ok) throw new Error("system " + r.status);
    const data = await r.json();
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
}

document.addEventListener("DOMContentLoaded", () => {
  bootMobileSheet();
  bootSystemStatus();
});
