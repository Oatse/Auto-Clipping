/* ============================================================================
 * motion.js — Page motion: scroll reveals, counters, magnetic CTAs.
 * No external deps. Respects prefers-reduced-motion.
 * ============================================================================ */

const PRM = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

/* ── Reveal on scroll ────────────────────────────────────────────────── */
function bootReveals() {
  if (PRM) return;
  document.documentElement.classList.add("js-reveal");

  const items = document.querySelectorAll("[data-reveal], [data-reveal-item]");
  if (!items.length) return;

  // Mark direct children of [data-reveal] that haven't already been tagged
  document.querySelectorAll("[data-reveal]").forEach((parent) => {
    parent.querySelectorAll(":scope > *").forEach((child) => {
      if (!child.hasAttribute("data-reveal-item") && !child.hasAttribute("data-reveal")) {
        child.setAttribute("data-reveal-item", "");
      }
    });
  });

  const io = new IntersectionObserver(
    (entries) => {
      entries.forEach((e) => {
        if (e.isIntersecting) {
          e.target.classList.add("is-in");
          io.unobserve(e.target);
        }
      });
    },
    { threshold: 0.12, rootMargin: "0px 0px -40px 0px" },
  );

  document.querySelectorAll("[data-reveal-item]").forEach((el) => io.observe(el));
}

/* ── Counter (fade up + animate number) ──────────────────────────────── */
function bootCounters() {
  const els = document.querySelectorAll("[data-counter][data-target]");
  if (!els.length) return;

  const ease = (t) => 1 - Math.pow(1 - t, 3);

  const animate = (el) => {
    const target = parseFloat(el.dataset.target || "0");
    const decimals = (el.dataset.target.split(".")[1] || "").length;
    const duration = 1200;
    const start = performance.now();

    const step = (now) => {
      const t = Math.min(1, (now - start) / duration);
      const v = ease(t) * target;
      el.textContent = decimals ? v.toFixed(decimals) : Math.round(v).toString();
      if (t < 1) requestAnimationFrame(step);
    };

    requestAnimationFrame(step);
  };

  if (PRM) {
    els.forEach((el) => {
      const tgt = el.dataset.target;
      el.textContent = tgt;
    });
    return;
  }

  const io = new IntersectionObserver(
    (entries) => {
      entries.forEach((e) => {
        if (e.isIntersecting) {
          animate(e.target);
          io.unobserve(e.target);
        }
      });
    },
    { threshold: 0.4 },
  );
  els.forEach((el) => io.observe(el));
}

/* ── Magnetic hover for primary CTAs ─────────────────────────────────── */
function bootMagnetic() {
  if (PRM) return;
  const targets = document.querySelectorAll(".btn--lg, .nav__brand");
  targets.forEach((el) => {
    el.addEventListener("pointermove", (e) => {
      const r = el.getBoundingClientRect();
      const x = e.clientX - r.left - r.width / 2;
      const y = e.clientY - r.top - r.height / 2;
      el.style.transform = `translate(${x * 0.06}px, ${y * 0.06}px)`;
    });
    el.addEventListener("pointerleave", () => {
      el.style.transform = "";
    });
  });
}

/* ── Init ────────────────────────────────────────────────────────────── */
document.addEventListener("DOMContentLoaded", () => {
  bootReveals();
  bootCounters();
  bootMagnetic();
});
