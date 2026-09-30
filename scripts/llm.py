"""Shared `claude -p` invocation for the LLM steps (classification, buffer
prep, community naming, briefings).

cron runs with a minimal PATH that lacks ~/.local/bin, so the binary is
resolved explicitly instead of relying on PATH lookup.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

_FALLBACK_BIN = Path.home() / ".local" / "bin" / "claude"

# Always pass a full model ID. Under cron (verified 2026-09-19), both the CLI's
# own default and the "opus" alias resolved to a retired ID
# (404 not_found: claude-opus-4-20250514), while a full ID worked. When this
# ID is retired, the pipeline's health check logs the 404; bump it here or set
# NOTE_TOPIX_CLAUDE_MODEL.
DEFAULT_MODEL = os.environ.get("NOTE_TOPIX_CLAUDE_MODEL", "claude-opus-5")


def claude_bin() -> str:
    found = shutil.which("claude")
    if found:
        return found
    if _FALLBACK_BIN.exists():
        return str(_FALLBACK_BIN)
    raise FileNotFoundError("`claude` CLI not found on PATH or at ~/.local/bin/claude")


def call_claude(prompt: str, model: str | None = None, timeout: int = 300) -> str:
    """Run a one-shot, tool-less completion and return stdout text."""
    cmd = [claude_bin(), "-p", "--output-format", "text", "--model", model or DEFAULT_MODEL]
    result = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        # The CLI reports auth / usage-limit errors on stdout, not stderr.
        detail = " | ".join(x.strip()[:300] for x in (result.stdout, result.stderr) if x.strip())
        raise RuntimeError(f"claude exit {result.returncode}: {detail or '(no output)'}")
    return result.stdout.strip()


def health_check() -> str | None:
    """None if `claude -p` answers, else the error text."""
    try:
        call_claude("Reply with exactly: ok", timeout=120)
        return None
    except Exception as exc:  # noqa: BLE001
        return str(exc)


def strip_fence(text: str) -> str:
    """Drop a surrounding ```lang ... ``` fence if the model added one."""
    text = text.strip()
    if text.startswith("```"):
        first_nl = text.find("\n")
        text = text[first_nl + 1:] if first_nl >= 0 else ""
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
    return text.strip()


def call_claude_json(prompt: str, model: str | None = None, timeout: int = 300) -> object:
    return json.loads(strip_fence(call_claude(prompt, model, timeout)))
