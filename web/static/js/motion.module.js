/**
 * ClipAuto — Motion Module
 *
 * GSAP-coordinated micro-interactions. Pure visual layer.
 * Hooks via existing class names — does NOT touch app state, routing,
 * or any logic. Falls back gracefully if GSAP isn't loaded.
 *
 * Choreography:
 *   - App entry intro (header + hero stagger)
 *   - Top nav tab change crossfade
 *   - Inspector tab pane crossfade
 *   - Style-section accordion (height tween)
 *   - Active transcript cue auto-scroll
 *
 * Respects prefers-reduced-motion.
 */

(function () {
  "use strict";

  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

  // ──────────────────────────────────────────────────────────────────
  // App ready
  // ──────────────────────────────────────────────────────────────────
  function markAppReady() {
    document.body.classList.remove("motion-app-pre");
    document.body.classList.add("motion-app-ready");
  }

  // ──────────────────────────────────────────────────────────────────
  // Hero entry stagger (header + hero items)
  // ──────────────────────────────────────────────────────────────────
  function playEntryIntro() {
    if (reduced || !window.gsap) return;

    const header = $(".header-inner");
    const hero = $('[data-motion="hero"]') || $(".hero-top");
    const heroChildren = hero ? Array.from(hero.children) : [];

    if (header) {
      window.gsap.from(header, {
        y: -8,
        opacity: 0,
        duration: 0.32,
        ease: "power3.out",
      });
    }

    if (heroChildren.length) {
      window.gsap.from(heroChildren, {
        y: 8,
        opacity: 0,
        duration: 0.28,
        ease: "power3.out",
        stagger: 0.06,
        delay: 0.08,
        clearProps: "all",
      });
    }
  }

  // ──────────────────────────────────────────────────────────────────
  // Top nav tab change — crossfade panes (no logic interception)
  // Listens to clicks on .nav-tab and animates the *destination* screen.
  // The actual tab-switch logic stays in app.js.
  // ──────────────────────────────────────────────────────────────────
  function setupTabCrossfade() {
    const observer = new MutationObserver((mutations) => {
      for (const m of mutations) {
        if (m.type !== "attributes" || m.attributeName !== "class") continue;
        const el = m.target;
        if (!el.classList || !el.classList.contains("app-screen")) continue;
        if (!el.classList.contains("active")) continue;
        if (reduced || !window.gsap) continue;

        window.gsap.fromTo(
          el,
          { opacity: 0, y: 6 },
          {
            opacity: 1,
            y: 0,
            duration: 0.18,
            ease: "power2.out",
            clearProps: "all",
          }
        );
      }
    });

    $$(".app-screen").forEach((s) => observer.observe(s, { attributes: true }));
  }

  // ──────────────────────────────────────────────────────────────────
  // Topbar crumb — reflect active sidebar tab label
  // ──────────────────────────────────────────────────────────────────
  function setupTopbarCrumb() {
    const crumbEl = $("#topbarCrumbCurrent");
    if (!crumbEl) return;

    const labelMap = {
      subtitle: "Auto Subtitle",
      clipfinder: "Clip Finder",
      shortmaker: "Short Maker",
    };

    function update() {
      const active = $(".nav-tab.active");
      if (!active) return;
      const tab = active.dataset.tab;
      const label = labelMap[tab] || active.textContent.trim();
      if (crumbEl.textContent !== label) {
        if (!reduced && window.gsap) {
          window.gsap.fromTo(
            crumbEl,
            { opacity: 0.4, y: -2 },
            { opacity: 1, y: 0, duration: 0.18, ease: "power2.out" }
          );
        }
        crumbEl.textContent = label;
      }
    }

    update();
    const observer = new MutationObserver(update);
    $$(".nav-tab").forEach((t) =>
      observer.observe(t, { attributes: true, attributeFilter: ["class"] })
    );
  }

  // ──────────────────────────────────────────────────────────────────
  // Inspector tab pane crossfade
  // ──────────────────────────────────────────────────────────────────
  function setupInspectorTabFade() {
    const tabs = $(".style-panel-tabs");
    if (!tabs) return;

    tabs.addEventListener("click", (e) => {
      const tab = e.target.closest(".sp-tab");
      if (!tab) return;
      const targetId = tab.dataset.spTab;
      if (!targetId) return;

      // Wait one frame so app.js can swap .active first
      requestAnimationFrame(() => {
        const pane = $(`#spPane-${targetId}`);
        if (!pane || reduced || !window.gsap) return;

        window.gsap.fromTo(
          pane,
          { opacity: 0, y: 4 },
          {
            opacity: 1,
            y: 0,
            duration: 0.16,
            ease: "power2.out",
            clearProps: "all",
          }
        );
      });
    });
  }

  // ──────────────────────────────────────────────────────────────────
  // Active transcript cue — smooth auto-scroll into view
  // Watches DOM mutations on .transcript-body for .active class changes.
  // ──────────────────────────────────────────────────────────────────
  function setupTranscriptAutoscroll() {
    const body = $(".transcript-body");
    if (!body) return;

    let lastActiveId = null;

    const observer = new MutationObserver(() => {
      const active = body.querySelector(".transcript-seg.active");
      if (!active) return;
      const id = active.dataset.segId || active.id || active.textContent.slice(0, 32);
      if (id === lastActiveId) return;
      lastActiveId = id;

      // Use native scrollIntoView for smoothness; respects reduced motion at the
      // browser level via scroll-behavior media query in base.css.
      const rect = active.getBoundingClientRect();
      const parentRect = body.getBoundingClientRect();
      const isVisible =
        rect.top >= parentRect.top + 12 &&
        rect.bottom <= parentRect.bottom - 12;
      if (isVisible) return;

      active.scrollIntoView({
        behavior: reduced ? "auto" : "smooth",
        block: "center",
      });
    });

    observer.observe(body, {
      attributes: true,
      attributeFilter: ["class"],
      subtree: true,
      childList: true,
    });
  }

  // ──────────────────────────────────────────────────────────────────
  // Editor entry choreography — when body.preview-active flips on,
  // play a coordinated GSAP reveal of left/center/right docks.
  // CSS already has fallback @keyframes evFadeUp; GSAP overlays a
  // smoother stagger on top when present.
  // ──────────────────────────────────────────────────────────────────
  function setupEditorEntry() {
    let lastState = document.body.classList.contains("preview-active");

    const playEditorReveal = () => {
      if (reduced || !window.gsap) return;
      const left  = $(".preview-left");
      const view  = $(".preview-center .video-preview-wrap");
      const tl    = $(".preview-center .timeline-panel");
      const right = $(".preview-right");
      const targets = [left, view, tl, right].filter(Boolean);
      if (!targets.length) return;

      window.gsap.fromTo(
        targets,
        { opacity: 0, y: 12 },
        {
          opacity: 1,
          y: 0,
          duration: 0.42,
          ease: "power3.out",
          stagger: 0.07,
          clearProps: "transform",
        }
      );
    };

    const observer = new MutationObserver(() => {
      const isActive = document.body.classList.contains("preview-active");
      if (isActive && !lastState) {
        // Defer one frame so DOM/CSS is settled before tween reads layout.
        requestAnimationFrame(playEditorReveal);
      }
      lastState = isActive;
    });

    observer.observe(document.body, {
      attributes: true,
      attributeFilter: ["class"],
    });

    if (lastState) requestAnimationFrame(playEditorReveal);
  }

  // ──────────────────────────────────────────────────────────────────
  // Boot
  // ──────────────────────────────────────────────────────────────────
  function boot() {
    markAppReady();
    playEntryIntro();
    setupTabCrossfade();
    setupTopbarCrumb();
    setupInspectorTabFade();
    setupTranscriptAutoscroll();
    setupEditorEntry();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot, { once: true });
  } else {
    boot();
  }
})();
