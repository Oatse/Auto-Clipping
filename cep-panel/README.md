# Auto-Clip panel for Premiere Pro

The Auto-Clip workflow inside Premiere: paste a stream URL, pick a model and
quality bar, watch progress, then import the finished timeline into the open
project — without leaving the app.

## Install

```bash
python -m launcher --install-panel
```

Then **fully restart Premiere** and open **Window > Extensions > Auto-Clip**.

## Why the panel must be signed

Current Premiere refuses unsigned CEP extensions. `PlayerDebugMode` alone is
no longer enough: the extension is rejected with `Signature verification
failed` in `%TEMP%\CEP11-PPRO.log` and simply never appears — no error in the
UI, just a missing panel. (The premiere-pro-mcp connector ships a signed ZXP
for the same reason.)

So the installer self-signs the panel with Adobe's `ZXPSignCmd`, generating a
local development certificate on first use. That certificate is a private key
and lives outside the repo, in `%LOCALAPPDATA%\AutoClip`.

`ZXPSignCmd.exe` is a 5 MB Adobe binary, fetched rather than vendored. Put it
at `tools/ZXPSignCmd.exe` (or point `ZXPSIGNCMD` at it):

```bash
curl -sSL -o tools/ZXPSignCmd.exe https://raw.githubusercontent.com/Adobe-CEP/CEP-Resources/ab5e4e3e53a42fad08e1225a22a991bb1ffe73f6/ZXPSignCMD/4.1.103/win64/ZXPSignCmd.exe
```

Without it the panel installs unsigned and the installer says so plainly,
rather than leaving you to wonder why nothing shows up.

## How it connects

`panel-config.json` is written into the bundle at install time, because the
installer is the only party that knows where the project lives — the panel
itself ends up in the CEP extensions folder, far away from it. It records the
project root, the Python interpreter, and the app address.

If the Auto-Clip server is not running when the panel opens, the panel starts
it (detached, so it outlives the panel) and waits for it to answer.

## Editing the panel

`--enable-nodejs` and remote debugging are on. With Premiere running and the
panel open, visit <http://localhost:8090> to inspect it in Chrome DevTools.

**Never put a double hyphen inside an XML comment** in `CSXS/manifest.xml`.
It is illegal XML, and CEP responds by rejecting the entire extension with
only a line number in the log. The installer validates the manifest before
copying to catch exactly that.

Re-run `python -m launcher --install-panel` after any change, then restart
Premiere — the panel is signed at install time, so edits made directly in the
extensions folder would break the signature.
