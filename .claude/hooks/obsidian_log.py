"""PostToolUse hook: append each agent action to the project's log note in Obsidian.

Reads the hook JSON from stdin. Never fails the tool call: any error is swallowed.
Vault location can be overridden with the OBSIDIAN_VAULT environment variable.
"""
import datetime
import json
import os
import sys

VAULT = os.environ.get("OBSIDIAN_VAULT", r"C:\Users\shacharm\OneDrive - OSG\Documents\Obsidian Vault")
LOG = os.path.join(VAULT, "Projects", "Hechzer-Kal", "יומן פעולות.md")
HEADER = "---\ntags: [hechzer-kal, log]\n---\n# יומן פעולות — החזר קל\n\nנכתב אוטומטית על ידי Hook של Claude Code. ראו [[החזר קל]].\n"


def summarize(data):
    tool = data.get("tool_name", "?")
    inp = data.get("tool_input") or {}
    project = data.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR", "")
    path = inp.get("file_path") or inp.get("notebook_path")
    if path:
        if os.path.abspath(path) == os.path.abspath(LOG):
            return None
        try:
            rel = os.path.relpath(path, project) if project else path
            path = path if rel.startswith("..") else rel
        except ValueError:  # different drive
            pass
        verb = {"Write": "כתיבת קובץ", "Edit": "עריכת קובץ", "NotebookEdit": "עריכת מחברת"}.get(tool, tool)
        return f"{verb}: `{path}`"
    # Shell: prefer the human description so raw commands (and any secrets in them) stay out.
    text = inp.get("description") or (inp.get("command") or "")[:120]
    text = " ".join(text.split()).replace("`", "'")
    return f"{tool}: {text}"


def main():
    data = json.loads(sys.stdin.buffer.read().decode("utf-8") or "{}")
    line = summarize(data)
    if not line:
        return
    now = datetime.datetime.now()
    day = f"## {now:%Y-%m-%d}"
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    existing = ""
    if os.path.exists(LOG):
        with open(LOG, encoding="utf-8") as f:
            existing = f.read()
    with open(LOG, "a", encoding="utf-8") as f:
        if not existing:
            f.write(HEADER)
        if day not in existing:
            f.write(f"\n{day}\n\n")
        f.write(f"- {now:%H:%M} · {line}\n")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
