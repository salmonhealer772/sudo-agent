#!/usr/bin/env python3
"""Enable the sudo-watch-stream plugin in ONE agent's Hermes config.yaml.

    python3 watch_plugin_enable.py <config.yaml> [plugin-key]

Why this exists
---------------
A Hermes plugin of kind ``standalone`` is opt-in: it is only loaded when its
key (or name) appears in ``plugins.enabled`` AND ``plugins.stream_reasoning_
deltas: true`` is set, otherwise the token stream silently never appears —
exactly the class of silent breakage this repo refuses to ship. up.sh calls
this for every deploy, so a fresh agent gets the token stream with no manual
step, and an already-deployed agent is upgraded on its next roll.

Why a TEXT edit and not a YAML round-trip
-----------------------------------------
``config/<name>.yaml`` is hand-maintained: operators add credentials, API
server settings and per-agent skins. Passing it through a YAML parser would
rewrite the whole file, deleting every comment and reflowing the formatting of
settings this change has nothing to do with. So: a surgical edit that touches
only the ``plugins`` block, then a REAL YAML parse of the result to prove the
file still loads and carries what we need. The new text is written to a temp
file and validated BEFORE it replaces the original — a bad edit can never
leave a broken config behind.

Exit codes: 0 = enabled (or already enabled), 1 = usage/missing file,
2 = could not produce a valid config (original left untouched).
"""

import os
import re
import sys

PLUGIN_KEY_DEFAULT = "sudo-watch-stream"

_ENABLED_RE = re.compile(r"^(\s+)enabled:\s*(.*?)\s*$")
_SRD_RE = re.compile(r"^(\s+)stream_reasoning_deltas:\s*(.*?)\s*$")
_ITEM_RE = re.compile(r"^(\s+)-\s*(.*?)\s*$")
_PLUGINS_RE = re.compile(r"^plugins:\s*(#.*)?$")
TRUTHY = {"true", "yes", "on", "1"}


def _find_plugins_block(lines):
    """(start, end) line indexes of the top-level ``plugins:`` block, or None."""
    start = None
    for i, line in enumerate(lines):
        if _PLUGINS_RE.match(line.rstrip("\n")):
            start = i
            break
    if start is None:
        return None
    end = len(lines)
    for j in range(start + 1, len(lines)):
        raw = lines[j]
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if not raw[0].isspace():  # next top-level key
            end = j
            break
    return start, end


def _strip_comment(value):
    return value.split(" #", 1)[0].strip()


def enable_plugin(text, key):
    """Return (new_text, changes) — never raises on odd input, but may return
    changes=[] when nothing needed doing."""
    changes = []
    lines = text.splitlines(keepends=True)
    block = _find_plugins_block(lines)
    if block is None:
        if text and not text.endswith("\n"):
            text += "\n"
        if text and not text.endswith("\n\n"):
            text += "\n"
        text += ("plugins:\n  enabled:\n    - %s\n"
                 "  stream_reasoning_deltas: true\n" % key)
        return text, ["added a top-level plugins: block"]

    start, end = block
    body = lines[start + 1:end]

    # ── plugins.enabled ───────────────────────────────────────────────────
    en_idx, en_indent, en_val = None, "  ", ""
    for i, line in enumerate(body):
        m = _ENABLED_RE.match(line.rstrip("\n"))
        if m:
            en_idx, en_indent, en_val = i, m.group(1), _strip_comment(m.group(2))
            break
    if en_idx is None:
        body.insert(0, "%senabled:\n" % en_indent)
        body.insert(1, "%s  - %s\n" % (en_indent, key))
        changes.append("created plugins.enabled with %s" % key)
    elif en_val.startswith("["):
        inner = en_val[1:en_val.rindex("]")] if "]" in en_val else en_val[1:]
        items = [x.strip().strip("'\"") for x in inner.split(",") if x.strip()]
        if key not in items:
            items.append(key)
            body[en_idx] = "%senabled: [%s]\n" % (en_indent, ", ".join(items))
            changes.append("added %s to plugins.enabled" % key)
    else:
        j, item_indent, present = en_idx + 1, None, False
        while j < len(body):
            m2 = _ITEM_RE.match(body[j].rstrip("\n"))
            if not m2:
                break
            if item_indent is None:
                item_indent = m2.group(1)
            if m2.group(2).strip().strip("'\"") == key:
                present = True
            j += 1
        if not present:
            body.insert(en_idx + 1,
                        "%s- %s\n" % (item_indent or (en_indent + "  "), key))
            changes.append("added %s to plugins.enabled" % key)

    # ── plugins.stream_reasoning_deltas ───────────────────────────────────
    srd_idx, srd_indent, srd_val = None, en_indent, ""
    for i, line in enumerate(body):
        m = _SRD_RE.match(line.rstrip("\n"))
        if m:
            srd_idx, srd_indent, srd_val = i, m.group(1), _strip_comment(m.group(2))
            break
    if srd_idx is None:
        tail = "%sstream_reasoning_deltas: true\n" % en_indent
        if body and not body[-1].endswith("\n"):
            body[-1] += "\n"
        body.append(tail)
        changes.append("set plugins.stream_reasoning_deltas: true")
    elif srd_val.lower() not in TRUTHY:
        body[srd_idx] = "%sstream_reasoning_deltas: true\n" % srd_indent
        changes.append("plugins.stream_reasoning_deltas %r -> true" % srd_val)

    return "".join(lines[:start + 1]) + "".join(body) + "".join(lines[end:]), changes


def verify(text, key):
    """Prove the edited text parses AND carries the two settings we need."""
    try:
        import yaml
    except ImportError:
        # No YAML parser on this host: fall back to a strict textual check.
        if not re.search(r"(?m)^\s+enabled:.*\b%s\b" % re.escape(key), text) \
           and not re.search(r"(?m)^\s+-\s*%s\s*$" % re.escape(key), text):
            return False, "plugins.enabled does not list %s (textual check)" % key
        if not re.search(r"(?m)^\s+stream_reasoning_deltas:\s*true\s*$", text):
            return False, "plugins.stream_reasoning_deltas is not true (textual check)"
        return True, "textual check only (pyyaml unavailable)"
    try:
        data = yaml.safe_load(text)
    except Exception as exc:
        return False, "YAML no longer parses: %s" % exc
    if not isinstance(data, dict):
        return False, "config root is not a mapping"
    plugins = data.get("plugins")
    if not isinstance(plugins, dict):
        return False, "no plugins mapping after the edit"
    enabled = plugins.get("enabled")
    if not isinstance(enabled, list) or key not in [str(x) for x in enabled]:
        return False, "plugins.enabled does not list %s (got %r)" % (key, enabled)
    if plugins.get("stream_reasoning_deltas") is not True:
        return False, "plugins.stream_reasoning_deltas is %r, not true" % (
            plugins.get("stream_reasoning_deltas"),)
    return True, "plugins.enabled=%s stream_reasoning_deltas=true" % (enabled,)


def parses(text):
    """(True, "") / (False, reason) / (None, "pyyaml unavailable")."""
    try:
        import yaml
    except ImportError:
        return None, "pyyaml unavailable"
    try:
        yaml.safe_load(text)
    except Exception as exc:
        return False, str(exc)
    return True, ""


def main(argv):
    if len(argv) > 2:
        print("usage: watch_plugin_enable.py <config.yaml> [plugin-key]",
              file=sys.stderr)
        return 1
    path = argv[0] if argv else ""
    key = argv[1] if len(argv) > 1 else PLUGIN_KEY_DEFAULT
    if not path:
        print("usage: watch_plugin_enable.py <config.yaml> [plugin-key]",
              file=sys.stderr)
        return 1
    if not os.path.isfile(path):
        print("✗ %s does not exist — cannot enable %s" % (path, key),
              file=sys.stderr)
        return 1
    with open(path, encoding="utf-8") as f:
        original = f.read()

    if not original.strip():
        print("✗ %s is empty — refusing to guess (deploy aborted)" % path,
              file=sys.stderr)
        return 2
    ok_orig, why_orig = parses(original)
    if ok_orig is False:
        print("✗ %s does not parse as YAML (%s)." % (path, why_orig),
              file=sys.stderr)
        print("  Hermes would ignore this config entirely; fix it before "
              "rolling. The file was NOT modified.", file=sys.stderr)
        return 2

    edited, changes = enable_plugin(original, key)
    ok, why = verify(edited, key)
    if not ok:
        print("✗ refusing to write %s: %s" % (path, why), file=sys.stderr)
        return 2
    if not changes:
        print("→ plugins already enabled in %s (%s)" % (path, why))
        return 0

    tmp = path + ".watch-plugin.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(edited)
    ok2, why2 = verify(open(tmp, encoding="utf-8").read(), key)
    if not ok2:
        os.unlink(tmp)
        print("✗ edited config failed verification (%s) — original untouched"
              % why2, file=sys.stderr)
        return 2
    os.replace(tmp, path)
    print("→ %s: %s (%s)" % (path, "; ".join(changes), why2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
