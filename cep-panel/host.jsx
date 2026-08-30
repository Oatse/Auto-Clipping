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
 * Import an FCP7 XML timeline into the current project.
 * app.openFCPXML is the documented entry point for this interchange format.
 */
function acImportFcpXml(path) {
  try {
    app.openFCPXML(path);
    return acJson({ ok: true, imported: true, path: path });
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
