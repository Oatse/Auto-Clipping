/*
 * host.jsx — ExtendScript run inside Premiere Pro by the Auto-Clip panel.
 *
 * Every function returns a JSON *string*, because CSInterface.evalScript hands
 * back a string and anything else would need parsing on both sides. Errors are
 * returned as data rather than thrown, so the panel can show them instead of
 * silently receiving "undefined".
 */

function acJson(obj) {
  // ExtendScript is ES3 and has no JSON.stringify, so serialise by hand.
  // Only the shapes this panel actually returns are supported.
  var parts = [];
  for (var key in obj) {
    if (!obj.hasOwnProperty(key)) continue;
    var value = obj[key];
    var encoded;
    if (typeof value === "boolean") {
      encoded = value ? "true" : "false";
    } else if (typeof value === "number") {
      encoded = String(value);
    } else if (value === null || value === undefined) {
      encoded = "null";
    } else {
      encoded = '"' + String(value)
        .replace(/\\/g, "/")
        .replace(/"/g, "'")
        .replace(/[\r\n]+/g, " ") + '"';
    }
    parts.push('"' + key + '":' + encoded);
  }
  return "{" + parts.join(",") + "}";
}

function acOk(data) { return acJson(data); }

function acError(message) {
  return acJson({ ok: false, error: String(message) });
}

/** Premiere version + whether a project is open. */
function acPing() {
  try {
    var project = app.project;
    return acJson({
      ok: true,
      version: app.version,
      projectName: project ? project.name : "",
      projectPath: project ? String(project.path) : ""
    });
  } catch (e) {
    return acError(e);
  }
}

/** Create a new project file at the given path. */
function acNewProject(path) {
  try {
    var created = app.newProject(path);
    return acJson({ ok: true, created: created ? true : false, path: path });
  } catch (e) {
    return acError(e);
  }
}

/**
 * Import an FCP7 XML timeline into the project that is already open.
 *
 * importFiles is used rather than app.openFCPXML deliberately. openFCPXML
 * takes (xmlPath, destinationProjectPath) — calling it with one argument
 * fails with "Not Enough Parameters", and calling it properly creates a
 * SEPARATE project, which is not what someone with their project already
 * open wants. importFiles brings the sequence into the current one.
 *
 * The path must be absolute: Premiere's working directory is its own, so a
 * relative path silently resolves to nothing.
 */
function acImportFcpXml(path) {
  try {
    var file = new File(path);
    if (!file.exists) {
      return acError("Timeline file not found: " + path);
    }
    var project = app.project;
    if (!project) {
      return acError("Open a project first, then import.");
    }

    var before = project.sequences.numSequences;
    var imported = project.importFiles(
      [path],
      true,                    // suppressUI: no modal dialog to block the bridge
      project.rootItem,
      false                    // not an image sequence
    );
    var after = project.sequences.numSequences;

    if (!imported) {
      return acError("Premiere refused the timeline import.");
    }
    return acJson({
      ok: true,
      imported: true,
      sequencesAdded: after - before,
      path: path
    });
  } catch (e) {
    return acError(e);
  }
}

/** Reveal the generated folder without leaving Premiere. */
function acRevealFolder(path) {
  try {
    var folder = new Folder(path);
    if (!folder.exists) return acError("Folder not found: " + path);
    folder.execute();
    return acJson({ ok: true, revealed: true });
  } catch (e) {
    return acError(e);
  }
}
