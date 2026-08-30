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

import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

PANEL_ID = "ClipAutomationPanel"
SOURCE_DIRNAME = "cep-panel"


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


def install(project_root: Path | None = None, *, force: bool = True) -> PanelStatus:
    """Copy the panel into the extensions folder, replacing any previous copy."""
    source = source_dir(project_root)
    if not (source / "CSXS" / "manifest.xml").is_file():
        raise FileNotFoundError(f"panel source not found at {source}")

    destination = extensions_dir() / PANEL_ID
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if not force:
            return status(project_root)
        shutil.rmtree(destination)

    # copy2 keeps timestamps; dotfiles like .debug are included by copytree.
    shutil.copytree(source, destination)
    return status(project_root)


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
    "status",
    "install",
    "uninstall",
]
