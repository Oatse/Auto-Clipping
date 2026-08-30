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
    projectRoot: "",
    python: "python",
    jobId: null,
    polling: null,
    result: null,
    logLines: 0,
    starting: false
  };

  /* Node is available because the manifest enables it. Guarded so a CEP build
     without Node degrades to "server offline" instead of a blank panel. */
  var node = (function () {
    try {
      return {
        fs: require("fs"),
        path: require("path"),
        child: require("child_process")
      };
    } catch (e) {
      return null;
    }
  })();

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
    importBtn: $("import"), revealBtn: $("reveal"), subtitle: $("subtitle"),
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

  /**
   * Three distinct outcomes, because they need three different fixes:
   *   no answer  -> nothing is listening; we can start it ourselves.
   *   404        -> something IS listening but has no compilation route, i.e.
   *                 an older server still running. Starting another would not
   *                 help and would collide on the port; it must be restarted.
   *   200        -> ready.
   * Treating 404 as "connected" is what let the panel report success and then
   * fail the first job with a bare "Not Found".
   */
  function checkConnection(onOffline) {
    request("GET", "/api/compilation/jobs", null, function (err, body, status) {
      if (err) {
        setConn("bad", "server offline");
        els.start.disabled = true;
        if (onOffline) onOffline();
        return;
      }
      if (status === 404) {
        setConn("bad", "server outdated");
        els.start.disabled = true;
        state.starting = false;
        els.statusCard.hidden = false;
        els.statusPhase.textContent = "Server needs restarting";
        els.statusDetail.textContent =
          "The Auto-Clip server is running an older build without the " +
          "compilation endpoints. Stop it and start it again, then reopen " +
          "this panel.";
        return;
      }
      if (status >= 400) {
        setConn("bad", "server error " + status);
        els.start.disabled = true;
        return;
      }
      setConn("ok", "connected");
      els.start.disabled = false;
      state.starting = false;
    });
  }

  /**
   * Start the Auto-Clip server ourselves when it is not running.
   *
   * The panel only appears inside an open project, so the natural moment to
   * bring the server up is when the panel opens — otherwise the first thing a
   * user sees is an error telling them to go run something in a terminal.
   *
   * Detached and unref'd on purpose: the server must outlive the panel, so
   * closing the panel or Premiere does not kill a running job.
   */
  function startServer() {
    if (state.starting) return;
    if (!node || !state.projectRoot) {
      setConn("bad", "server offline");
      els.statusDetail.textContent =
        "Start it with start-workspace.bat in the Auto-Clip folder.";
      return;
    }

    state.starting = true;
    setConn("busy", "starting server…");

    try {
      var child = node.child.spawn(
        state.python,
        ["run_web.py", "--host", "127.0.0.1", "--port", String(portOf(state.appHost))],
        { cwd: state.projectRoot, detached: true, stdio: "ignore", windowsHide: true }
      );
      child.unref();
    } catch (e) {
      state.starting = false;
      setConn("bad", "could not start server");
      els.statusDetail.textContent = String(e);
      return;
    }

    // Poll rather than guess: uvicorn takes a moment, and a fixed delay would
    // either feel slow or report failure too early.
    var attempts = 0;
    var timer = setInterval(function () {
      attempts++;
      request("GET", "/api/compilation/jobs", null, function (err) {
        if (!err) {
          clearInterval(timer);
          state.starting = false;
          setConn("ok", "connected");
          els.start.disabled = false;
        } else if (attempts >= 30) {           // ~30s
          clearInterval(timer);
          state.starting = false;
          setConn("bad", "server did not start");
          els.statusDetail.textContent =
            "Check the Auto-Clip folder setting, or start it manually with " +
            "start-workspace.bat.";
        }
      });
    }, 1000);
  }

  function portOf(host) {
    var match = /:(\d+)/.exec(host);
    return match ? match[1] : "7860";
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

  /**
   * Subtitle whatever sequence is open, which is deliberately independent of
   * having just run a compilation: the point is to caption the finished edit,
   * whenever that happens.
   */
  function subtitleTimeline() {
    els.subtitle.disabled = true;
    els.statusCard.hidden = false;
    els.log.textContent = "";
    state.logLines = 0;
    els.bar.className = "fill indeterminate";
    els.statusPhase.textContent = "Subtitling…";
    els.statusDetail.textContent =
      "Exporting the timeline audio, then transcribing it. This can take a " +
      "few minutes on a long sequence.";

    var payload = {
      job_id: state.result ? state.result.id : null,
      translate_to: "en"
    };

    request("POST", "/api/compilation/subtitle", payload, function (err, body) {
      if (err || !body || !body.job_id) {
        els.subtitle.disabled = false;
        els.statusPhase.textContent = "Subtitling failed";
        els.statusDetail.textContent =
          (body && body.detail) || (err && err.message) || "no response";
        return;
      }
      pollSubtitle(body.job_id);
    });
  }

  /* Exporting audio and transcribing takes minutes, so the run is a job we
     poll rather than a request we hold open. */
  function pollSubtitle(subtitleJobId) {
    request("GET", "/api/compilation/subtitle/" + subtitleJobId, null,
      function (err, job) {
        if (err || !job) {
          els.subtitle.disabled = false;
          els.statusPhase.textContent = "Subtitling failed";
          els.statusDetail.textContent = (err && err.message) || "lost contact";
          return;
        }

        els.statusPhase.textContent = job.phase_label || job.status;
        appendLog(job.logs);

        if (job.status === "running") {
          setTimeout(function () { pollSubtitle(subtitleJobId); }, POLL_MS);
          return;
        }

        els.subtitle.disabled = false;
        els.bar.className = "fill";
        els.bar.style.width = "100%";

        if (job.status !== "completed") {
          els.statusPhase.textContent = "Subtitling failed";
          els.statusDetail.textContent =
            (job.errors || []).join("; ") || "the run produced no captions";
          return;
        }
        els.statusPhase.textContent = "Captions ready";
        els.statusDetail.textContent =
          job.segment_count + " caption(s)" +
          (job.imported
            ? " imported into the project — drag them onto a caption track."
            : " written to " + job.srt_path);
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

  /**
   * Config written by the installer, which is the only party that knows where
   * the Auto-Clip project lives — the panel itself is installed into the CEP
   * extensions folder, far away from it. A user override in localStorage wins.
   */
  function loadSettings() {
    if (node) {
      try {
        var configPath = node.path.join(__dirname, "panel-config.json");
        var config = JSON.parse(node.fs.readFileSync(configPath, "utf-8"));
        state.projectRoot = config.projectRoot || "";
        state.python = config.python || "python";
        if (config.appHost) state.appHost = config.appHost;
      } catch (e) {
        // No config: the panel still works against an already-running server.
      }
    }
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
  els.subtitle.addEventListener("click", subtitleTimeline);
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
  // Opening the panel is the moment to bring the server up, so the first
  // thing a user sees is a working panel rather than an error.
  checkConnection(startServer);
  setInterval(function () {
    if (!state.jobId && !state.starting) checkConnection();
  }, 10000);
})();
