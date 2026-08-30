/*
 * main.js — Auto-Clip panel logic.
 *
 * The panel is the whole workflow in one place: it posts a job to the local
 * Auto-Clip server, follows it to completion, then imports the resulting
 * timeline into the open project via ExtendScript. No browser, no file
 * shuffling, no second app.
 *
 * Deliberately dependency-free ES5-ish JS: CEP's embedded Chromium is modern
 * enough for more, but a panel that cannot be debugged in place is worse than
 * one that is plain.
 */

(function () {
  "use strict";

  var cs = new CSInterface();
  var POLL_MS = 1500;
  var STORAGE_KEY = "autoclip.appHost";

  var state = {
    appHost: "http://127.0.0.1:7860",
    jobId: null,
    polling: null,
    result: null,
    logLines: 0
  };

  // ── element lookup ──────────────────────────────────────────────────
  function $(id) { return document.getElementById(id); }

  var els = {
    connDot: $("connDot"), connText: $("connText"),
    url: $("url"), instructions: $("instructions"), lang: $("lang"),
    offset: $("offset"), model: $("model"),
    threshold: $("threshold"), thresholdValue: $("thresholdValue"),
    projectName: $("projectName"), start: $("start"),
    statusCard: $("statusCard"), statusPhase: $("statusPhase"),
    statusDetail: $("statusDetail"), bar: $("bar"), log: $("log"),
    cancel: $("cancel"),
    resultCard: $("resultCard"), resultTitle: $("resultTitle"),
    resultMeta: $("resultMeta"), moments: $("moments"),
    importBtn: $("import"), revealBtn: $("reveal"),
    appUrl: $("appUrl"), settings: $("settings"),
    settingsSheet: $("settingsSheet"), appHost: $("appHost"),
    saveSettings: $("saveSettings")
  };

  // ── helpers ─────────────────────────────────────────────────────────

  function api(path) { return state.appHost.replace(/\/+$/, "") + path; }

  function request(method, path, body, done) {
    var xhr = new XMLHttpRequest();
    xhr.open(method, api(path), true);
    xhr.setRequestHeader("Content-Type", "application/json");
    xhr.timeout = 20000;
    xhr.onload = function () {
      var parsed = null;
      try { parsed = JSON.parse(xhr.responseText); } catch (e) {}
      done(null, parsed, xhr.status);
    };
    xhr.onerror = function () { done(new Error("cannot reach the Auto-Clip server")); };
    xhr.ontimeout = function () { done(new Error("the Auto-Clip server timed out")); };
    xhr.send(body ? JSON.stringify(body) : null);
  }

  /** Run a host function and hand back its parsed JSON. */
  function host(call, done) {
    cs.evalScript(call, function (raw) {
      if (!raw || raw === "undefined" || raw === "EvalScript error.") {
        done(new Error("Premiere did not respond. Try reopening this panel."));
        return;
      }
      var parsed;
      try { parsed = JSON.parse(raw); }
      catch (e) { done(new Error("unexpected reply from Premiere: " + raw)); return; }
      if (parsed.ok === false) { done(new Error(parsed.error || "Premiere reported an error")); return; }
      done(null, parsed);
    });
  }

  /** ExtendScript string literal: backslashes and quotes must survive. */
  function esc(value) {
    return String(value).replace(/\\/g, "\\\\").replace(/"/g, '\\"');
  }

  /** "1:23" or "83" -> 83 seconds. */
  function parseOffset(text) {
    var trimmed = String(text || "").trim();
    if (!trimmed) return 0;
    var parts = trimmed.split(":").map(Number);
    if (parts.some(isNaN)) return 0;
    return parts.length === 2 ? parts[0] * 60 + parts[1] : parts[0];
  }

  function mmss(seconds) {
    var s = Math.max(0, Math.round(seconds));
    return Math.floor(s / 60) + ":" + ("0" + (s % 60)).slice(-2);
  }

  function setConn(kind, text) {
    els.connDot.className = "dot " + kind;
    els.connText.textContent = text;
  }

  function appendLog(lines) {
    if (!lines || lines.length <= state.logLines) return;
    for (var i = state.logLines; i < lines.length; i++) {
      els.log.textContent += (els.log.textContent ? "\n" : "") + lines[i];
    }
    state.logLines = lines.length;
    els.log.scrollTop = els.log.scrollHeight;
  }

  // ── connection ──────────────────────────────────────────────────────

  function checkConnection() {
    request("GET", "/api/compilation/jobs", null, function (err) {
      if (err) {
        setConn("bad", "server offline");
        els.start.disabled = true;
        els.statusDetail.textContent = "";
        return;
      }
      setConn("ok", "connected");
      els.start.disabled = false;
    });
  }

  // ── running a job ───────────────────────────────────────────────────

  function startJob() {
    var url = els.url.value.trim();
    if (!url) { els.url.focus(); return; }

    els.start.disabled = true;
    els.resultCard.hidden = true;
    els.statusCard.hidden = false;
    els.log.textContent = "";
    state.logLines = 0;
    state.result = null;
    els.bar.className = "fill indeterminate";
    els.statusPhase.textContent = "Starting…";
    els.statusDetail.textContent = "Analysing the stream and downloading the master in parallel.";

    var payload = {
      url: url,
      instructions: els.instructions.value.trim(),
      lang: els.lang.value,
      start_offset: parseOffset(els.offset.value),
      model: els.model.value,
      threshold: parseFloat(els.threshold.value),
      project_name: els.projectName.value.trim()
    };

    request("POST", "/api/compilation/jobs", payload, function (err, body, status) {
      if (err || !body || status >= 400) {
        fail((body && body.detail) || (err && err.message) || "could not start the job");
        return;
      }
      state.jobId = body.job_id;
      setConn("busy", "working");
      poll();
    });
  }

  function poll() {
    clearTimeout(state.polling);
    request("GET", "/api/compilation/jobs/" + state.jobId, null, function (err, job) {
      if (err || !job) {
        fail((err && err.message) || "lost contact with the job");
        return;
      }

      els.statusPhase.textContent = job.phase_label || job.status;
      appendLog(job.logs);

      if (job.status === "completed") { succeed(job); return; }
      if (job.status === "failed") { fail(job.error || "the run failed"); return; }

      state.polling = setTimeout(poll, POLL_MS);
    });
  }

  function succeed(job) {
    state.result = job;
    setConn("ok", "connected");
    els.bar.className = "fill";
    els.bar.style.width = "100%";
    els.statusPhase.textContent = "Done";
    els.statusDetail.textContent = "";
    els.start.disabled = false;

    els.resultCard.hidden = false;
    els.resultTitle.textContent = job.moment_count + " moments found";
    els.resultMeta.textContent = (job.total_seconds / 60).toFixed(1) + " min of material";

    els.moments.innerHTML = "";
    (job.moments || []).forEach(function (moment) {
      var total = (moment.score && moment.score.total) || 0;
      var li = document.createElement("li");
      li.className = "moment";

      var time = document.createElement("span");
      time.className = "time";
      time.textContent = mmss(moment.start);

      var title = document.createElement("span");
      title.className = "title";
      title.textContent = moment.title || "Moment";
      title.title = moment.reason || "";

      var score = document.createElement("span");
      score.className = "score" + (total < 6.5 ? " mid" : "");
      score.textContent = total.toFixed(1);

      li.appendChild(time);
      li.appendChild(title);
      li.appendChild(score);
      els.moments.appendChild(li);
    });
  }

  function fail(message) {
    clearTimeout(state.polling);
    setConn("bad", "error");
    els.bar.className = "fill";
    els.bar.style.width = "100%";
    els.statusPhase.textContent = "Failed";
    els.statusDetail.textContent = message;
    els.start.disabled = false;
  }

  // ── importing into Premiere ─────────────────────────────────────────

  function importTimeline() {
    if (!state.result || !state.result.fcpxml_path) return;
    els.importBtn.disabled = true;
    els.statusCard.hidden = false;
    els.statusPhase.textContent = "Importing…";
    els.statusDetail.textContent = "Building the sequence in Premiere.";

    host('acImportFcpXml("' + esc(state.result.fcpxml_path) + '")', function (err) {
      els.importBtn.disabled = false;
      if (err) {
        els.statusPhase.textContent = "Import failed";
        els.statusDetail.textContent = err.message;
        return;
      }
      els.statusPhase.textContent = "Imported";
      els.statusDetail.textContent =
        "The sequence is in your project. The master stays whole, so every " +
        "clip can still be re-trimmed.";
    });
  }

  function revealFiles() {
    if (!state.result || !state.result.fcpxml_path) return;
    var folder = state.result.fcpxml_path.replace(/[\\/][^\\/]+$/, "");
    host('acRevealFolder("' + esc(folder) + '")', function (err) {
      if (err) els.statusDetail.textContent = err.message;
    });
  }

  // ── settings ────────────────────────────────────────────────────────

  function loadSettings() {
    try {
      var saved = window.localStorage.getItem(STORAGE_KEY);
      if (saved) state.appHost = saved;
    } catch (e) {}
    els.appHost.value = state.appHost;
    els.appUrl.textContent = state.appHost;
  }

  function saveSettings() {
    state.appHost = els.appHost.value.trim() || state.appHost;
    try { window.localStorage.setItem(STORAGE_KEY, state.appHost); } catch (e) {}
    els.appUrl.textContent = state.appHost;
    els.settingsSheet.hidden = true;
    checkConnection();
  }

  // ── wiring ──────────────────────────────────────────────────────────

  els.start.addEventListener("click", startJob);
  els.importBtn.addEventListener("click", importTimeline);
  els.revealBtn.addEventListener("click", revealFiles);
  els.cancel.addEventListener("click", function () { els.statusCard.hidden = true; });
  els.settings.addEventListener("click", function () {
    els.settingsSheet.hidden = !els.settingsSheet.hidden;
  });
  els.saveSettings.addEventListener("click", saveSettings);
  els.threshold.addEventListener("input", function () {
    els.thresholdValue.textContent = parseFloat(els.threshold.value).toFixed(1);
  });
  els.url.addEventListener("keydown", function (event) {
    if (event.key === "Enter") startJob();
  });

  loadSettings();
  checkConnection();
  setInterval(function () { if (!state.jobId) checkConnection(); }, 10000);
})();
