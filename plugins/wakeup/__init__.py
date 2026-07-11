"""
wakeup plugin — auto-inject context at session start.

On session start (I/O + cache):
  1. Read MEMORY.md (full) — long-term curated facts
  2. Read today's daily log (memory/YYYY-MM-DD.md) — current session context
  3. Search recent sessions via session_search() — past conversation snippets

On first LLM turn: inject the cached payload into the user message.
Subsequent turns: no-op.

Failures in any one source are isolated — a ⚠️ line is emitted, the rest
still injects. Never raises into the agent loop.
"""

import datetime as _dt
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

# Module-level cache populated by on_session_start, consumed by pre_llm_call.
_cached_payload: str | None = None


# ---------- readers (each isolated, returns (section_text, error_or_None)) ----------

def _get_hermes_home() -> Path:
    """Return the Hermes home directory (where MEMORY.md and memory/ live)."""
    env_home = os.environ.get("HERMES_HOME", "")
    if env_home:
        return Path(env_home)
    home = Path.home() / ".hermes"
    # Fallback for containerized setups where HOME may differ
    alt = Path("/opt/data")
    if alt.is_dir() and (alt / "MEMORY.md").exists():
        return alt
    return home


def _read_memory() -> tuple[str, str | None]:
    """Read MEMORY.md — long-term curated facts."""
    try:
        path = _get_hermes_home() / "MEMORY.md"
        text = path.read_text(encoding="utf-8").strip()
        if not text:
            return "", "MEMORY.md is empty"
        return f"# MEMORY (full)\n\n{text}", None
    except FileNotFoundError:
        return "", f"MEMORY.md not found"
    except Exception as e:
        return "", f"MEMORY.md read failed: {e}"


def _read_today_log() -> tuple[str, str | None]:
    """Read today's daily session log from memory/YYYY-MM-DD.md."""
    try:
        today = _dt.date.today().isoformat()
        path = _get_hermes_home() / "memory" / f"{today}.md"
        if not path.exists():
            return "", ""  # No log yet today — not an error
        text = path.read_text(encoding="utf-8").strip()
        if not text:
            return "", ""
        return f"# TODAY'S LOG ({today})\n\n{text}", None
    except Exception as e:
        return "", f"Today's log read failed: {e}"


def _search_recent_sessions() -> tuple[str, str | None]:
    """Search recent sessions for context using Hermes' built-in session search.

    Pulls the most recent 3 sessions as previews, plus uses the content
    of the most recent session as a query to find related past conversations.
    """
    try:
        from tools.session_search_tool import session_search

        # Browse: get recent sessions as previews
        recent = session_search(limit=3)

        parts = []
        if recent and "no sessions found" not in recent.lower():
            parts.append(f"# RECENT SESSIONS\n\n{recent}")

            # Use the first 200 chars of the most recent session as a search
            # query to find related past conversations. This chains context
            # from what was last discussed.
            try:
                # The browse result starts with the session title
                query = recent[:200]
                related = session_search(query=query, limit=2)
                if related and "no sessions found" not in related.lower():
                    parts.append(f"# RELATED PAST SESSIONS\n\n{related}")
            except Exception:
                pass

        if not parts:
            return "", ""

        return "\n\n".join(parts), None
    except ImportError:
        return "", "session_search not available (Hermes < 0.10?)"
    except Exception as e:
        return "", f"Session search failed: {e}"


# ---------- payload builder ----------

def _build_payload() -> str:
    parts: list[str] = ["=== WAKEUP CONTEXT ==="]
    for reader_name, reader in [
        ("memory", _read_memory),
        ("today_log", _read_today_log),
        ("recent_sessions", _search_recent_sessions),
    ]:
        try:
            section, err = reader()
        except Exception as e:
            parts.append(f"⚠️ WAKEUP: {reader_name} unhandled exception: {e}")
            continue
        if err:
            parts.append(f"⚠️ WAKEUP: {err}")
        if section:
            parts.append(section)
    parts.append("=== END WAKEUP CONTEXT ===")
    return "\n\n".join(parts)


# ---------- hook callbacks ----------

def on_session_start(**kwargs):
    """Build the wakeup payload once, cache it for the first turn."""
    global _cached_payload
    try:
        _cached_payload = _build_payload()
        logger.info("wakeup: payload built (%d chars)", len(_cached_payload))
    except Exception as e:
        _cached_payload = f"⚠️ WAKEUP: build failed: {e}"
        logger.exception("wakeup: payload build failed")


_injected_sessions: set[str] = set()


def pre_llm_call(session_id=None, user_message=None, is_first_turn=False, **kwargs):
    """Inject wakeup context on the first LLM call of each session.

    Tracks which sessions have already received the payload so we
    inject exactly once per session.
    """
    global _cached_payload

    sid = session_id or ""
    if sid in _injected_sessions:
        return None

    logger.info("wakeup: pre_llm_call fired (session=%s, is_first_turn=%s, has_payload=%s)",
                sid, is_first_turn, bool(_cached_payload))

    if not _cached_payload:
        logger.info("wakeup: no cached payload, building on-demand")
        try:
            _cached_payload = _build_payload()
            logger.info("wakeup: on-demand payload built (%d chars)", len(_cached_payload))
        except Exception as e:
            logger.warning("wakeup: on-demand build failed: %s", e)
            _injected_sessions.add(sid)
            return None

    payload = _cached_payload
    _cached_payload = None
    _injected_sessions.add(sid)
    logger.info("wakeup: injecting payload (%d chars) for session %s", len(payload), sid)
    return {"context": payload}


# ---------- plugin entry ----------

def register(ctx):
    ctx.register_hook("on_session_start", on_session_start)
    ctx.register_hook("pre_llm_call", pre_llm_call)
