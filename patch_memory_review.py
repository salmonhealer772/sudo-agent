"""Patch: auto-save every user message to memory programmatically, no LLM decision."""
import sys

PATH = "/opt/hermes/agent/background_review.py"

with open(PATH) as f:
    code = f.read()

# The review worker `_run_review_in_thread` opens with two local imports that
# avoid a hard circular dep at module load. That import block is the first
# executable statement of the function body and is stable code (not a comment),
# so it survives the base-image churn that bit-rotted the earlier anchors:
#   * "    st = _ReviewForkState()\n"  -> gone (worker refactored, no fork-state line)
#   * "# Silence stdout/stderr ..."    -> comment, reworded by upstream
# We insert the auto-save block immediately AFTER the local imports, before the
# fork is built, so the parent agent's real memory store captures the user's
# latest message on every background review.
old = "    from tools.terminal_tool import set_approval_callback as _set_approval_callback\n"

new = """    from tools.terminal_tool import set_approval_callback as _set_approval_callback
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
    if "PROGRAMMATIC AUTO-SAVE" in code:
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
    print("ERROR: Could not find insertion point (anchor: 'from tools.terminal_tool import set_approval_callback') in %s" % PATH, file=sys.stderr)
    sys.exit(1)
