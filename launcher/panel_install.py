"""
launcher/panel_install.py — Install the Auto-Clip panel into Premiere.

Copies ``cep-panel/`` into the user's CEP extensions folder so Premiere loads
it at startup. Kept separate from the premiere-pro-mcp connector on purpose:
that one ships as a signed ZXP and is replaced on every npm update, so editing
it in place would break its signature and be overwritten. The two panels sit
side by side.

The panel is unsigned, which is why ``PlayerDebugMode`` must be set — the same
flag the connector's installer sets. This module reports on that flag but does
not change it: weakening Adobe's extension signature checking is the user's
decision to make knowingly, not a side effect of installing a panel.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from pathlib import Path

PANEL_ID = "ClipAutomationPanel"
SOURCE_DIRNAME = "cep-panel"

# Password for the local self-signed development certificate. This protects
# nothing of value — the certificate exists only so Premiere will load a panel
# built on this machine, and Adobe requires one.
CERT_PASSWORD = "autoclip-dev"


@dataclass
class PanelStatus:
    installed: bool
    destination: Path
    debug_mode: bool
    message: str = ""

    def __str__(self) -> str:
        lines = [
            f"panel installed : {'yes' if self.installed else 'no'} ({self.destination})",
            f"debug mode      : {'enabled' if self.debug_mode else 'NOT enabled'}",
        ]
        if self.message:
            lines.append(self.message)
        return "\n".join(lines)


def extensions_dir() -> Path:
    """Per-user CEP extensions folder."""
    if sys.platform == "win32":
        base = os.getenv("APPDATA", "")
        return Path(base) / "Adobe" / "CEP" / "extensions"
    return Path.home() / "Library" / "Application Support" / "Adobe" / "CEP" / "extensions"


def source_dir(project_root: Path | None = None) -> Path:
    root = project_root or Path(__file__).resolve().parent.parent
    return root / SOURCE_DIRNAME


def debug_mode_enabled() -> bool:
    """True when Premiere will load unsigned extensions.

    Without this the panel is installed but silently never appears, which is
    the single most confusing failure mode here — so it is checked explicitly
    rather than left for the user to discover.
    """
    if sys.platform != "win32":
        return True         # macOS uses a defaults key; not checked here
    try:
        import winreg

        for version in ("11", "12", "10", "9"):
            try:
                with winreg.OpenKey(
                    winreg.HKEY_CURRENT_USER, rf"Software\Adobe\CSXS.{version}"
                ) as key:
                    value, _ = winreg.QueryValueEx(key, "PlayerDebugMode")
                    if str(value).strip() == "1":
                        return True
            except OSError:
                continue
    except ImportError:
        return False
    return False


def status(project_root: Path | None = None) -> PanelStatus:
    destination = extensions_dir() / PANEL_ID
    installed = (destination / "CSXS" / "manifest.xml").is_file()
    debug = debug_mode_enabled()
    message = ""
    if installed and not debug:
        message = (
            "The panel is installed but Premiere will not load it: unsigned "
            "extensions need PlayerDebugMode. Run "
            "`premiere-pro-mcp --install-cep` (which sets it) or set "
            r"HKCU\Software\Adobe\CSXS.11\PlayerDebugMode to the string 1."
        )
    return PanelStatus(installed, destination, debug, message)


def validate_manifest(manifest_path: Path) -> None:
    """Fail loudly on a manifest CEP would reject.

    CEP's failure mode is silent: an unparsable manifest means the panel
    simply never appears in Window > Extensions, with the reason buried in
    CEP11-PPRO.log. The double-hyphen rule is the trap worth naming — a
    comment mentioning a Chromium flag like ``- -enable-nodejs`` (written
    without the space) is illegal XML and takes the whole extension down.
    """
    try:
        ET.parse(manifest_path)
    except ET.ParseError as exc:
        hint = ""
        if "hyphen" in str(exc).lower() or "comment" in str(exc).lower():
            hint = (
                " Likely a double hyphen inside an XML comment — XML forbids "
                "it, and CEP rejects the whole manifest."
            )
        raise ValueError(f"{manifest_path} is not valid XML: {exc}.{hint}") from exc


def install(
    project_root: Path | None = None,
    *,
    force: bool = True,
    app_host: str = "http://127.0.0.1:7860",
    python: str | None = None,
) -> PanelStatus:
    """Sign the panel and install it into the extensions folder.

    Signing is not optional. ``PlayerDebugMode`` alone is no longer enough on
    current Premiere: an unsigned extension is rejected with
    ``Signature verification failed`` in CEP11-PPRO.log and simply never
    appears. (premiere-pro-mcp ships a signed ZXP for the same reason.) So the
    panel is self-signed at install time, the same way.

    A ``panel-config.json`` is written into the bundle first, because the
    installer is the only party that knows where the project lives — the panel
    ends up in the CEP extensions folder, far from it.
    """
    root = project_root or Path(__file__).resolve().parent.parent
    source = source_dir(root)
    manifest = source / "CSXS" / "manifest.xml"
    if not manifest.is_file():
        raise FileNotFoundError(f"panel source not found at {source}")
    validate_manifest(manifest)

    destination = extensions_dir() / PANEL_ID
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if not force:
            return status(root)
        shutil.rmtree(destination)

    with tempfile.TemporaryDirectory(prefix="autoclip-panel-") as tmp:
        staging = Path(tmp) / "panel"
        shutil.copytree(source, staging)
        (staging / "panel-config.json").write_text(
            json.dumps(
                {
                    "projectRoot": str(root),
                    "appHost": app_host,
                    "python": python or sys.executable or "python",
                },
                indent=2,
            ),
            encoding="utf-8",
        )

        signer = find_signing_tool()
        if signer is None:
            # Install unsigned rather than nothing, but be explicit that
            # Premiere will refuse to load it.
            shutil.copytree(staging, destination)
            result = status(root)
            result.message = (
                "Installed WITHOUT a signature: ZXPSignCmd was not found, and "
                "current Premiere rejects unsigned extensions. Put "
                "ZXPSignCmd.exe next to the project or set ZXPSIGNCMD, then "
                "reinstall."
            )
            return result

        package = Path(tmp) / f"{PANEL_ID}.zxp"
        _sign(signer, staging, package)
        _extract(package, destination)

    return status(root)


def find_signing_tool() -> Path | None:
    """Locate ZXPSignCmd: env var, project root, or PATH."""
    from_env = os.getenv("ZXPSIGNCMD")
    if from_env and Path(from_env).is_file():
        return Path(from_env)

    root = Path(__file__).resolve().parent.parent
    for candidate in (root / "tools" / "ZXPSignCmd.exe", root / "ZXPSignCmd.exe"):
        if candidate.is_file():
            return candidate

    found = shutil.which("ZXPSignCmd") or shutil.which("ZXPSignCmd.exe")
    return Path(found) if found else None


def _sign(signer: Path, staging: Path, package: Path) -> None:
    """Self-sign ``staging`` into ``package``, creating a cert if needed."""
    cert = _certificate_path()
    if not cert.is_file():
        cert.parent.mkdir(parents=True, exist_ok=True)
        _run(
            [
                str(signer), "-selfSignedCert", "ID", "Jakarta",
                "Auto-Clip", "Auto-Clip Panel", CERT_PASSWORD, str(cert),
            ],
            "could not create the signing certificate",
        )
    _run(
        [str(signer), "-sign", str(staging), str(package), str(cert), CERT_PASSWORD],
        "could not sign the panel",
    )


def _run(command: list[str], failure: str) -> None:
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        detail = (result.stdout or result.stderr or "").strip()[:400]
        raise RuntimeError(f"{failure}: {detail}")


def _extract(package: Path, destination: Path) -> None:
    """A ZXP is a zip; unpacking it keeps META-INF/signatures.xml in place."""
    with zipfile.ZipFile(package) as archive:
        archive.extractall(destination)


def _certificate_path() -> Path:
    """Keep the dev certificate out of the repo — it is a private key."""
    base = Path(os.getenv("LOCALAPPDATA") or Path.home()) / "AutoClip"
    return base / "autoclip-panel.p12"


def uninstall() -> bool:
    destination = extensions_dir() / PANEL_ID
    if not destination.exists():
        return False
    shutil.rmtree(destination)
    return True


__all__ = [
    "PANEL_ID",
    "PanelStatus",
    "extensions_dir",
    "source_dir",
    "debug_mode_enabled",
    "validate_manifest",
    "status",
    "install",
    "uninstall",
]
