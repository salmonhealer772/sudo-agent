"""Patch: auto-save every user message to memory programmatically, no LLM decision."""
import re

with open("/opt/hermes/agent/background_review.py") as f:
    code = f.read()

# Find the _run_review_in_thread function and insert auto-save at the start
# We patch inside the function, right after the try: block opens

old = '''    try:
        # Silence stdout/stderr for THIS worker thread only.'''

new = '''    try:
        # ---- PROGRAMMATIC AUTO-SAVE: save every user message to memory ----
        try:
            from tools.memory_tool import MemoryStore
            store = agent._memory_store
            if store and messages_snapshot:
                # Find the last user message
                for msg in reversed(messages_snapshot):
                    if msg.get("role") == "user":
                        content = msg.get("content", "")
                        if content and len(content) < 5000:
                            store.add("memory", content)
                            store.add("user", content[:2000])
                            logger.info("Auto-saved user message to memory")
                        break
        except Exception as ex:
            logger.warning("Auto-save failed (non-critical): %s", ex)
        # ---- end auto-save ----
        
        # Silence stdout/stderr for THIS worker thread only.'''

if old in code:
    code = code.replace(old, new)
    with open("/opt/hermes/agent/background_review.py", "w") as f:
        f.write(code)
    print("Patched _run_review_in_thread with programmatic auto-save")
else:
    print("ERROR: Could not find insertion point")
    # Debug: find the location
    idx = code.find("try:")
    if idx >= 0:
        print(f"Found 'try:' at position {idx}")
        print(code[idx:idx+200])
