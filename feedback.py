#!/usr/bin/env python3
"""Seed match-quality feedback rows from a digest into feedback.md (Level 1).

Usage:
  python3 feedback.py add [YYYY-MM-DD]

Reads that day's digest (the newest digests/digest-*.md by default, or the given
date) and appends one stub row per posting to feedback.md -- skipping any already
logged for that date. You then open feedback.md and change each `verdict: ?` to
`good` or `bad`, adding an optional reason.

The CLI never reads feedback.md back -- it is a log for you to review by hand
(open it, or `grep 'verdict: bad' feedback.md`). This script does not touch
digest.py, seen.json, or targets.yaml. app.py (the local UI) also writes here via
set_verdict() and log_detection() below, in the same row format.
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIGESTS_DIR = ROOT / "digests"
FEEDBACK_FILE = ROOT / "feedback.md"

HEADER = """\
# Match feedback log

Level 1 -- my own judgments on match quality. For each posting, set the
`verdict:` line to `good` or `bad` and add an optional short reason after it.
Seed new rows after a digest run with:  python3 feedback.py add
"""

NO_MATCHES = "_(no matching postings this run)_"

DATE_RE = re.compile(r"^# Job digest -- (\d{4}-\d{2}-\d{2})\s*$")
DIGEST_NAME_RE = re.compile(r"digest-(\d{4}-\d{2}-\d{2})\.md$")
DATE_ARG_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def die(msg):
    sys.exit(f"feedback.py: {msg}")


def count_unrated(text):
    return sum(1 for ln in text.splitlines() if ln.strip() == "verdict: ?  reason:")


def find_digest(date_arg):
    if date_arg:
        if not DATE_ARG_RE.fullmatch(date_arg):
            die(f"not a date: {date_arg!r} (expected YYYY-MM-DD)")
        path = DIGESTS_DIR / f"digest-{date_arg}.md"
        if not path.exists():
            die(f"no digest for {date_arg} (looked for {path.relative_to(ROOT)})")
        return path
    if not DIGESTS_DIR.is_dir():
        die(f"no {DIGESTS_DIR.name}/ directory yet -- run digest.py first")
    files = sorted(DIGESTS_DIR.glob("digest-*.md"))
    if not files:
        die("no digest files yet -- run digest.py first")
    return max(files, key=lambda p: p.stat().st_mtime)


def parse_digest(path):
    """Return (date_str, [(company, title), ...]) from a digest .md file."""
    date_str = None
    postings = []
    company = None
    for line in path.read_text().splitlines():
        if line.startswith(">"):
            continue  # inside a draft blockquote
        m = DATE_RE.match(line)
        if m:
            date_str = m.group(1)
        elif line.startswith("## "):
            company = line[3:].strip()
        elif line.startswith("### ") and company:
            postings.append((company, line[4:].strip()))
    if date_str is None:
        m = DIGEST_NAME_RE.search(path.name)
        date_str = m.group(1) if m else "unknown-date"
    return date_str, postings


def add(date_arg):
    digest_path = find_digest(date_arg)
    date_str, postings = parse_digest(digest_path)
    rows = [f"{company} | {title}" for company, title in postings]

    text = FEEDBACK_FILE.read_text() if FEEDBACK_FILE.exists() else HEADER
    lines = text.rstrip("\n").split("\n")
    heading = f"## {date_str}"

    if heading in lines:
        start = lines.index(heading)
        end = next((i for i in range(start + 1, len(lines))
                    if lines[i].startswith("## ")), len(lines))
        existing = {ln[2:].strip() for ln in lines[start:end] if ln.startswith("- ")}
        new_rows = [r for r in rows if r not in existing]

        if not new_rows:
            print(f"feedback.py: added 0 new rows for {date_str} "
                  f"({len(existing)} already logged). {count_unrated(text)} row(s) still "
                  f"awaiting a verdict.")
            return

        section = [ln for ln in lines[start:end] if ln.strip() and ln != NO_MATCHES]
        for r in new_rows:
            section += [f"- {r}", "  verdict: ?  reason:"]
        lines[start:end] = section + [""]
        already = len(existing)
    else:
        new_rows = rows
        already = 0
        while lines and lines[-1].strip() == "":
            lines.pop()
        lines += ["", heading, ""]
        if rows:
            for r in rows:
                lines += [f"- {r}", "  verdict: ?  reason:"]
        else:
            lines.append(NO_MATCHES)

    out = "\n".join(lines).rstrip("\n") + "\n"
    FEEDBACK_FILE.write_text(out)
    print(f"feedback.py: added {len(new_rows)} new row(s) for {date_str} "
          f"({already} already logged). {count_unrated(out)} row(s) in "
          f"{FEEDBACK_FILE.name} still awaiting a verdict.")


# --------------------------------------------------------------------------
# Writers used by app.py (the local UI). Same file, same `## date` /
# `- Company | Title` layout as add(), so feedback.md stays the one log.
# --------------------------------------------------------------------------

def _load_lines():
    text = FEEDBACK_FILE.read_text() if FEEDBACK_FILE.exists() else HEADER
    return text.rstrip("\n").split("\n")


def _save_lines(lines):
    FEEDBACK_FILE.write_text("\n".join(lines).rstrip("\n") + "\n")


def _section(lines, date_str):
    """(start, end) of the `## date` section, creating it at the end if missing."""
    heading = f"## {date_str}"
    if heading not in lines:
        while lines and not lines[-1].strip():
            lines.pop()
        lines += ["", heading, ""]
    start = lines.index(heading)
    end = next((i for i in range(start + 1, len(lines))
                if lines[i].startswith("## ")), len(lines))
    return start, end


def _one_line(text):
    return " ".join((text or "").split())


def _find_entry(lines, start, end, row, field):
    """Index of `row` whose next line starts with `  <field>:`, or None."""
    for i in range(start + 1, end - 1):
        if lines[i] == row and lines[i + 1].lstrip().startswith(f"{field}:"):
            return i
    return None


def _upsert(date_str, company, title, field, body):
    """Replace (or append) the `field` entry for this posting under its date.

    `body` is the list of indented lines that follow the `- Company | Title` row.
    """
    lines = _load_lines()
    start, end = _section(lines, date_str)
    row = f"- {company} | {title}"
    i = _find_entry(lines, start, end, row, field)
    if i is not None:
        j = i + 1
        while j < end and lines[j].startswith("  "):  # old entry + wrapped reason lines
            j += 1
        lines[i + 1:j] = body
    else:
        section = [lines[start], ""] + [ln for ln in lines[start + 1:end]
                                        if ln.strip() and ln != NO_MATCHES]
        lines[start:end] = section + [row] + body + [""]
    _save_lines(lines)


def set_verdict(date_str, company, title, verdict, reason=""):
    """Set `verdict: good|bad  reason: ...` for one posting (seeding its row if needed)."""
    _upsert(date_str, company, title, "verdict",
            [f"  verdict: {verdict}  reason: {_one_line(reason)}".rstrip()])


def log_detection(date_str, company, title, detected, override, snippet):
    """Log an experience-detection miss/correction, with the posting text it came from."""
    kind = "wrong" if detected else "missed"
    _upsert(date_str, company, title, "detection", [
        f"  detection: {kind}  detected: {_one_line(detected) or 'none found'}  "
        f"override: {_one_line(override)}",
        f'  snippet: "{_one_line(snippet) or "(no posting text saved for this digest)"}"',
    ])


def read_verdicts():
    """{(date, company, title): (verdict, reason)} for every rated row in feedback.md."""
    out = {}
    if not FEEDBACK_FILE.exists():
        return out
    date_str = None
    lines = FEEDBACK_FILE.read_text().splitlines()
    for i, ln in enumerate(lines):
        if ln.startswith("## "):
            date_str = ln[3:].strip()
        elif ln.startswith("- ") and " | " in ln and i + 1 < len(lines):
            m = re.match(r"\s+verdict:\s*(\S+)\s+reason:\s?(.*)$", lines[i + 1])
            if m and m.group(1) != "?":
                company, title = ln[2:].split(" | ", 1)
                out.setdefault((date_str, company.strip(), title.strip()),
                               (m.group(1).lower(), m.group(2).strip()))
    return out


def main():
    args = sys.argv[1:]
    if args and args[0] in ("-h", "--help"):
        print(__doc__)
        return
    if args and args[0] == "review":
        die("`review` isn't built yet -- open feedback.md or `grep 'verdict: bad' feedback.md`.")
    if args and args[0] == "add":
        args = args[1:]
    if len(args) > 1:
        die("too many arguments. Usage: python3 feedback.py add [YYYY-MM-DD]")
    add(args[0] if args else None)


if __name__ == "__main__":
    main()
