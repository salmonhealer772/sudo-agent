"""Patch: auto-save every user message to memory programmatically, no LLM decision."""
import sys

PATH = "/opt/hermes/agent/background_review.py"

with open(PATH) as f:
    code = f.read()

# Anchor on the STRUCTURAL line that marks the start of the review worker's
# body (``st = _ReviewForkState()``), NOT on a comment. The old anchor
# (``# Silence stdout/stderr for THIS worker thread only.``) bit-rotted: the
# base image reworded that comment, so the patch silently printed
# "ERROR: Could not find insertion point" and shipped an UNPATCHED image.
# This anchor is unique in the file (verified: 1 occurrence) and stable across
# base-image comment churn.
old = "    st = _ReviewForkState()\n"

new = """    st = _ReviewForkState()
    # ---- PROGRAMMATIC AUTO-SAVE: save every user message to memory ----
    try:
        store = agent._memory_store
        if store is not None and messages_snapshot:
            for msg in reversed(messages_snapshot):
                if isinstance(msg, dict) and msg.get("role") == "user":
                    content = msg.get("content", "")
                    if isinstance(content, str) and content and len(content) < 5000:
                        store.add("memory", content)
                        store.add("user", content[:2000])
                        logger.info("Auto-saved user message to memory")
                    break
    except Exception as ex:
        logger.warning("Auto-save failed (non-critical): %s", ex)
    # ---- end auto-save ----
"""

if old in code:
    if new in code:
        print("Already patched (auto-save present) — no-op")
    else:
        code = code.replace(old, new, 1)
        with open(PATH, "w") as f:
            f.write(code)
        print("Patched _run_review_in_thread with programmatic auto-save")
else:
    # FAIL LOUD: a silently unpatched image would ship without save-every-message
    # memory. The Dockerfile runs this script with `&& rm`, so a non-zero exit
    # aborts the build instead of quietly producing an unpatched agent.
    print("ERROR: Could not find insertion point (anchor: 'st = _ReviewForkState()') in %s" % PATH, file=sys.stderr)
    sys.exit(1)
