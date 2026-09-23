"""Canonical Claude Code config and transcript paths.

Provider connectivity belongs in Claude settings. ``CLAUDE_CONFIG_DIR`` is a
native storage-root selector, not a provider selector: changing it selects a
different session catalog. Keep every direct filesystem reader aligned with
the Claude SDK so a settings-only provider switch never changes the catalog.
"""
from __future__ import annotations

import os
import unicodedata
from pathlib import Path


def claude_config_dir() -> Path:
    """Return the active native Claude Code config root."""
    configured = os.environ.get("CLAUDE_CONFIG_DIR")
    if configured:
        # Match claude-agent-sdk 0.2.157 exactly: the environment value is a
        # filesystem path, not a shell expression, so do not expand "~" or
        # resolve symlinks behind the SDK's back.
        return Path(unicodedata.normalize("NFC", configured))
    return Path(unicodedata.normalize(
        "NFC", str(Path.home() / ".claude")))


def claude_projects_dir() -> Path:
    """Return the active native Claude Code transcript root."""
    return claude_config_dir() / "projects"
