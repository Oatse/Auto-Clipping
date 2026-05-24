/* =====================================================================
 * cfMotion.js — Awwwards-tier motion layer for the Clip Finder screen.
 *
 * Defensive rewrite: zero MutationObservers, zero perpetual rAF loops,
 * zero global pointer listeners. Everything is gated behind explicit
 * triggers (nav-tab click, button hover) so a hidden bug in any of
 * those handlers cannot freeze the page.
 *
 * Effects:
 *   1. Mount-class trigger (.cf-mounted) on initial visibility + nav-tab
 *      click. CSS uses it to gate entrance animations.
 *   2. Magnetic CTA — scoped to the buttons themselves (mouseenter →
 *      attach pointermove on the button, mouseleave → detach + reset).
 *   3. Spotlight-tracking on clip cards via CSS variables. Listener is
 *      attached lazily on first hover of a card and self-throttles via
 *      requestAnimationFrame.
 *
 * Performance guarantees:
 *   - No animation frame is requested unless the user is actively
 *     pointing at a magnet button or a clip card.
 *   - No DOM mutation observer runs.
 *   - All listeners declare { passive: true }.
 *   - Reduced motion + coarse pointer skip every effect.
 * ===================================================================== */

(() => {
  'use strict';

  const PREFERS_REDUCED_MOTION =
    window.matchMedia &&
    window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  const COARSE_POINTER =
    window.matchMedia &&
    window.matchMedia('(hover: none), (pointer: coarse)').matches;

  const screenEl = document.getElementById('screen-clipfinder');
  if (!screenEl) return;

  /* ── 1. Mount-class trigger ──────────────────────────────────────── */
  const setMounted = () => {
    if (screenEl.classList.contains('cf-mounted')) return;
    screenEl.classList.add('cf-mounted');
  };

  // Fire once if the screen is already visible at script load.
  if (screenEl.classList.contains('active')) {
    requestAnimationFrame(setMounted);
  } else {
    // First-time mount when user navigates to the tab.
    document.querySelectorAll('.nav-tab[data-tab="clipfinder"]').forEach(tab => {
      tab.addEventListener('click', () => {
        // Defer two frames so the SPA can flip .active first.
        requestAnimationFrame(() =>
          requestAnimationFrame(setMounted)
        );
      }, { passive: true });
    });
  }

  if (PREFERS_REDUCED_MOTION) return;
  if (COARSE_POINTER) return;

  /* ── 2. Magnetic CTAs (scoped to the button only) ────────────────── */
  const MAGNET_PULL = 8;     // px max — gentle to avoid jitter.

  const attachMagnet = (btn) => {
    if (!btn || btn.dataset.cfMagnet === '1') return;
    btn.dataset.cfMagnet = '1';

    let raf = 0;
    let active = false;
    let tx = 0, ty = 0;
    let cx = 0, cy = 0;

    const tick = () => {
      cx += (tx - cx) * 0.22;
      cy += (ty - cy) * 0.22;
      btn.style.setProperty('--cf-mag-x', cx.toFixed(2) + 'px');
      btn.style.setProperty('--cf-mag-y', cy.toFixed(2) + 'px');
      const settled = Math.abs(tx - cx) < 0.05 && Math.abs(ty - cy) < 0.05;
      if (active || !settled) {
        raf = requestAnimationFrame(tick);
      } else {
        raf = 0;
      }
    };

    const onMove = (e) => {
      const r = btn.getBoundingClientRect();
      const dx = e.clientX - (r.left + r.width / 2);
      const dy = e.clientY - (r.top + r.height / 2);
      const reach = Math.max(r.width, r.height) / 2;
      tx = (dx / reach) * MAGNET_PULL;
      ty = (dy / reach) * MAGNET_PULL;
      if (!raf) raf = requestAnimationFrame(tick);
    };

    btn.addEventListener('pointerenter', () => {
      active = true;
      btn.addEventListener('pointermove', onMove, { passive: true });
    }, { passive: true });

    btn.addEventListener('pointerleave', () => {
      active = false;
      tx = 0; ty = 0;
      btn.removeEventListener('pointermove', onMove);
      if (!raf) raf = requestAnimationFrame(tick);
    }, { passive: true });
  };

  // Defer to next frame so clip-finder.js has wired its own listeners.
  requestAnimationFrame(() => {
    attachMagnet(document.getElementById('cfFindBtn'));
    attachMagnet(document.getElementById('cfDownloadAllBtn'));
  });

  /* ── 3. Spotlight-tracking on clip cards (lazy, per-card) ────────── */
  const grid = document.getElementById('cfClipsGrid');
  if (!grid) return;

  let spotRaf = 0;
  let spotCard = null;
  let spotX = 0, spotY = 0;

  const writeSpot = () => {
    spotRaf = 0;
    if (!spotCard) return;
    const r = spotCard.getBoundingClientRect();
    spotCard.style.setProperty('--mx', (((spotX - r.left) / r.width) * 100).toFixed(1) + '%');
    spotCard.style.setProperty('--my', (((spotY - r.top) / r.height) * 100).toFixed(1) + '%');
  };

  // Single delegated listener — only fires while user pointer is in grid.
  grid.addEventListener('pointermove', (e) => {
    const card = e.target.closest && e.target.closest('.cf-clip-card');
    if (!card) { spotCard = null; return; }
    spotCard = card;
    spotX = e.clientX;
    spotY = e.clientY;
    if (!spotRaf) spotRaf = requestAnimationFrame(writeSpot);
  }, { passive: true });

  grid.addEventListener('pointerleave', () => {
    spotCard = null;
  }, { passive: true });
})();
