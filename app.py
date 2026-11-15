"""Local UI for the job digest: browse digests as cards, rate matches, fix
experience detection, manage digest history, and trigger a run.

Start it with:  ./.venv/bin/streamlit run app.py   (or double-click run_app.command)

Reads digests/digest-<date>.json (written by digest.py since the UI was added)
and falls back to parsing the .md for older digests. Verdicts and detection
corrections go into feedback.md through feedback.py; experience overrides also
go into experience_overrides.json. Nothing here changes how digest.py matches,
filters, or drafts -- "Run digest" just runs digest.py itself.
"""

import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys

import streamlit as st

import digest
import feedback

ROOT = digest.ROOT
DIGESTS_DIR = digest.DIGESTS_DIR
TRASH_DIR = DIGESTS_DIR / "_trash"
OVERRIDES_FILE = ROOT / "experience_overrides.json"

DIGEST_NAME_RE = re.compile(r"^digest-(\d{4}-\d{2}-\d{2})\.md$")
RUN_RE = re.compile(r"^\*\*Run (\d+) · ([\d:]+)\*\*")
FIELD_RE = re.compile(r"^- \*\*(.+?):\*\*\s?(.*)$")
ATS_NAMES = ("greenhouse", "lever", "ashby", "smartrecruiters", "recruitee")


# --------------------------------------------------------------------------
# Loading digests
# --------------------------------------------------------------------------

def list_digests():
    """[(date_str, md_path)] newest first. Ignores digests/_trash/."""
    out = []
    for p in DIGESTS_DIR.glob("digest-*.md") if DIGESTS_DIR.is_dir() else []:
        m = DIGEST_NAME_RE.match(p.name)
        if m:
            out.append((m.group(1), p))
    return sorted(out, reverse=True)


def load_targets():
    """{company name: (ats, slug)} from targets.yaml, without load_config()'s exits."""
    try:
        import yaml
        cfg = yaml.safe_load(digest.TARGETS_FILE.read_text()) or {}
        return {c["name"]: (c.get("ats"), c.get("slug")) for c in cfg.get("companies") or []
                if isinstance(c, dict) and c.get("name")}
    except Exception:
        return {}


def guess_key(company, url, targets, seen):
    """Best-effort posting key for an old .md entry: a seen.json key for this
    company's board whose job id appears in the link. None when not found."""
    ats, slug = targets.get(company, (None, None))
    if not (ats and slug and url):
        return None
    prefix = f"{ats}:{slug}:"
    for k in seen:
        if k.startswith(prefix) and k[len(prefix):] in url:
            return k
    return None


def guess_ats(company, url, targets):
    ats = targets.get(company, (None, None))[0]
    if ats:
        return ats
    return next((name for name in ATS_NAMES if name in (url or "").lower()), "unknown")


def parse_md(path, targets, seen):
    """Best-effort parse of a pre-sidecar .md digest into the sidecar's shape."""
    runs, run, company, post = [], None, None, None
    for line in path.read_text().splitlines():
        m = RUN_RE.match(line)
        if m:
            run = {"run": int(m.group(1)), "time": m.group(2), "postings": []}
            runs.append(run)
            company = post = None
            continue
        if line.startswith("## "):
            company, post = line[3:].strip(), None
            continue
        if line.startswith("### ") and company:
            if run is None:  # very early digests had postings before any Run header
                run = {"run": 0, "time": "", "postings": []}
                runs.append(run)
            post = {"company": company, "title": line[4:].strip(), "location": "",
                    "url": "", "posted": "", "match_via": None, "dupes_skipped": 0,
                    "experience": None, "contact": None, "apollo_attempted": False,
                    "description": "", "_draft": []}
            run["postings"].append(post)
            continue
        if post is None:
            continue
        if line.startswith(">"):
            post["_draft"].append(line[2:] if line.startswith("> ") else "")
            continue
        m = FIELD_RE.match(line)
        if not m:
            continue
        name, val = m.group(1), m.group(2).strip()
        if name == "Location":
            post["location"] = val
        elif name == "Posted":
            post["posted"] = val
        elif name == "Link":
            post["url"] = "" if val == "n/a" else val
        elif name == "Matched via":
            post["match_via"] = "description"
        elif name == "Also posted":
            post["dupes_skipped"] = int(re.match(r"\d+", val).group()) if val[:1].isdigit() else 1
        elif name == "Contact found":
            val = val.removesuffix("(via Apollo)").strip()
            cname, _, ctitle = val.partition(", ")
            post["contact"] = {"name": cname, "title": ctitle}
            post["apollo_attempted"] = True
        elif name == "Contact":
            post["apollo_attempted"] = True
        elif name == "Experience (detected)":
            val = re.sub(r"\s*\(\+\d+ more\)$", "", val)
            post["experience"] = [] if val == "none found" else val.split(", ")

    for run in runs:
        for p in run["postings"]:
            text = "\n".join(p.pop("_draft")).strip()
            p["draft_ok"] = text.startswith("*Draft outreach:*")
            p["draft_text"] = text.removeprefix("*Draft outreach:*").strip() if p["draft_ok"] else text
            p["key"] = guess_key(p["company"], p["url"], targets, seen)
            p["ats"] = guess_ats(p["company"], p["url"], targets)
    return runs


def load_digest(date_str, md_path, targets, seen):
    """(postings, has_sidecar). One entry per posting -- if the same posting
    shows up in several same-day runs, the latest run wins. Newest run first."""
    sidecar = md_path.with_suffix(".json")
    has_sidecar = sidecar.exists()
    if has_sidecar:
        runs = json.loads(sidecar.read_text()).get("runs", [])
    else:
        runs = parse_md(md_path, targets, seen)

    latest = {}
    for run in runs:
        for p in run["postings"]:
            p = dict(p, run=run["run"], run_time=run["time"], date=date_str)
            p["id"] = p.get("key") or f"{p['company']}|{p['title']}"
            latest[p["id"]] = p  # later runs overwrite earlier ones
    postings = sorted(latest.values(), key=lambda p: -p["run"])
    return postings, has_sidecar


# --------------------------------------------------------------------------
# Overrides + snippets
# --------------------------------------------------------------------------

def load_overrides():
    try:
        return json.loads(OVERRIDES_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_override(p, value):
    data = load_overrides()
    data[p["id"]] = {
        "override": value,
        "detected": p.get("experience"),
        "company": p["company"],
        "title": p["title"],
        "digest_date": p["date"],
        "url": p.get("url"),
        "saved_at": dt.datetime.now().isoformat(timespec="seconds"),
    }
    OVERRIDES_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")


SNIPPET_PATTERNS = [
    r"\d+\s*\+?\s*(?:-|–|to)?\s*\d*\s*\+?\s*years?",
    r"\byears?\b",
    r"\bexperience\b",
]


def snippet_around(text, width=220):
    """~2*width chars of the posting around its first experience-ish mention."""
    text = " ".join((text or "").split())
    if not text:
        return ""
    for pat in SNIPPET_PATTERNS:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            a, b = max(0, m.start() - width), min(len(text), m.end() + width)
            return ("..." if a else "") + text[a:b] + ("..." if b < len(text) else "")
    return text[:2 * width] + ("..." if len(text) > 2 * width else "")


# --------------------------------------------------------------------------
# Delete (move to _trash) + run digest
# --------------------------------------------------------------------------

def trash_digest(md_path, forget_keys=None):
    """Move the .md (and its .json, if any) into digests/_trash/. seen.json is
    only touched when forget_keys is given (the opt-in checkbox)."""
    TRASH_DIR.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    for src in (md_path, md_path.with_suffix(".json")):
        if src.exists():
            dest = TRASH_DIR / src.name
            if dest.exists():
                dest = TRASH_DIR / f"{src.stem}-{stamp}{src.suffix}"
            shutil.move(str(src), str(dest))
    if forget_keys:
        seen = digest.load_seen()
        digest.save_seen(seen - set(forget_keys))


def run_digest():
    """Run digest.py exactly as from the terminal -- same caps, same seen.json."""
    return subprocess.run([sys.executable, str(ROOT / "digest.py")], cwd=ROOT,
                          capture_output=True, text=True, timeout=1800)


# --------------------------------------------------------------------------
# UI helpers
# --------------------------------------------------------------------------

def esc(text):
    """Keep '$' from turning into LaTeX in st.markdown."""
    return str(text or "").replace("$", r"\$")


def is_seen(p, seen):
    """In seen.json now. Old .md entries with no recoverable key (e.g. Recruitee,
    whose links don't carry the job id) fall back to "drafted" -- digest.py only
    adds successfully drafted postings to seen.json."""
    return p["key"] in seen if p.get("key") else bool(p.get("draft_ok"))


def apollo_line(p):
    c = p.get("contact")
    if c:
        return f"found: {esc(c.get('name'))}, {esc(c.get('title'))}"
    return "none found" if p.get("apollo_attempted") else "not attempted"


def experience_line(p):
    exp = p.get("experience")
    if exp is None:
        return "not recorded"
    return ", ".join(exp) if exp else "none found"


def render_card(p, seen, verdicts, overrides):
    pid = f"{p['date']}|{p['id']}"
    seen_now = is_seen(p, seen)
    verdict, reason = verdicts.get((p["date"], p["company"], p["title"]), (None, ""))
    ov = overrides.get(p["id"], {}).get("override")

    with st.container(border=True):
        badge = ":gray-badge[Already seen]" if seen_now else ":green-badge[New]"
        vbadge = {"good": " :green-badge[Good]", "bad": " :red-badge[Bad]"}.get(verdict, "")
        st.markdown(f"#### {esc(p['company'])} · {esc(p['title'])}")
        st.markdown(f"{badge}{vbadge}")
        run_label = f"Run {p['run']} · {p['run_time']}" if p.get("run_time") else "early run"
        st.caption(f"Posted {p.get('posted') or 'n/a'} · ATS: {p.get('ats') or 'unknown'} · "
                   f"{esc(p.get('location') or 'location unspecified')} · {run_label}")
        if p.get("url"):
            st.markdown(f"[Open posting]({p['url']})")
        st.markdown(f"**Experience (detected):** {esc(experience_line(p))}")
        if ov:
            st.markdown(f"**Experience (your override):** {esc(ov)}")
        st.markdown(f"**Apollo contact:** {apollo_line(p)}")
        if p.get("match_via") == "description":
            st.caption("Matched via description (title didn't match on its own)")
        if p.get("dupes_skipped"):
            st.caption(f"Also posted: {p['dupes_skipped']} near-identical reposting(s)")

        label = "Draft outreach" if p.get("draft_ok") else "Draft outreach (not drafted)"
        with st.expander(label, expanded=False):
            if p.get("draft_text"):
                st.code(p["draft_text"], language=None, wrap_lines=True)
            else:
                st.caption("No draft text.")

        reason_in = st.text_input("Why? (saved with your verdict)", value=reason,
                                  key=f"reason|{pid}")
        c1, c2, _ = st.columns([1, 1, 4])
        for col, v, label in ((c1, "good", "👍 Good"), (c2, "bad", "👎 Bad")):
            if col.button(label, key=f"{v}|{pid}",
                          type="primary" if verdict == v else "secondary"):
                feedback.set_verdict(p["date"], p["company"], p["title"], v, reason_in)
                st.toast(f"Saved '{v}' for {p['company']} · {p['title']}")
                st.rerun()

        o1, o2 = st.columns([3, 1], vertical_alignment="bottom")
        ov_in = o1.text_input("Real experience requirement",
                              placeholder='e.g. "2", "3-5", "unknown"',
                              value=ov or "", key=f"ov|{pid}")
        if o2.button("Save override", key=f"ovsave|{pid}"):
            value = ov_in.strip()
            if not value:
                st.warning('Type a number, a range, or "unknown" first.')
            else:
                save_override(p, value)
                feedback.log_detection(p["date"], p["company"], p["title"],
                                       experience_line(p) if p.get("experience") else "",
                                       value, snippet_around(p.get("description")))
                st.toast(f"Override saved for {p['company']} · {p['title']}")
                st.rerun()


# --------------------------------------------------------------------------
# Page
# --------------------------------------------------------------------------

def sidebar_history(digests):
    st.sidebar.header("Digest history")
    if not digests:
        st.sidebar.info("No digests yet. Use Run digest below.")
        return None
    dates = [d for d, _ in digests]
    if "pending_select" in st.session_state:  # set by a run or delete on the previous pass
        st.session_state["selected"] = st.session_state.pop("pending_select")
    if st.session_state.get("selected") not in dates:
        st.session_state["selected"] = dates[0]
    return st.sidebar.radio("Pick a digest", dates, key="selected",
                            label_visibility="collapsed")


def sidebar_delete(date_str, md_path, postings, has_sidecar):
    if st.session_state.get("confirm_delete") != date_str:
        if st.sidebar.button(f"Delete digest {date_str}…", key="delete_start"):
            st.session_state["confirm_delete"] = date_str
            st.rerun()
        return
    box = st.sidebar.container(border=True)
    box.warning(f"Move digest-{date_str} to digests/_trash/? You can move it back by hand.")
    forget = box.checkbox(
        "Also forget these postings in seen.json, so the next run can re-draft them",
        value=False, disabled=not has_sidecar,
        help=None if has_sidecar else "Only possible for digests with a .json sidecar "
                                      "(older .md-only digests don't record posting keys).")
    b1, b2 = box.columns(2)
    if b1.button("Confirm delete", type="primary", key="delete_ok"):
        keys = [p["key"] for p in postings if p.get("key")] if forget else None
        trash_digest(md_path, keys)
        st.session_state.pop("confirm_delete", None)
        st.session_state["pending_select"] = None
        st.toast(f"Moved digest-{date_str} to _trash/" + (" and forgot its postings" if keys else ""))
        st.rerun()
    if b2.button("Cancel", key="delete_cancel"):
        st.session_state.pop("confirm_delete", None)
        st.rerun()


def sidebar_filters(postings, seen):
    st.sidebar.header("Filters")
    companies = sorted({p["company"] for p in postings})
    atss = sorted({p.get("ats") or "unknown" for p in postings})
    pick_c = st.sidebar.multiselect("Company", companies, placeholder="All companies")
    pick_a = st.sidebar.multiselect("ATS", atss, placeholder="All ATSs")
    status = st.sidebar.radio("Status", ["All", "New", "Already seen"], horizontal=True,
                              help="Already seen = in seen.json (successfully drafted). "
                                   "New = not in seen.json (draft skipped or failed).")
    out = []
    for p in postings:
        if pick_c and p["company"] not in pick_c:
            continue
        if pick_a and (p.get("ats") or "unknown") not in pick_a:
            continue
        seen_now = is_seen(p, seen)
        if status == "New" and seen_now or status == "Already seen" and not seen_now:
            continue
        out.append(p)
    return out


def sidebar_run():
    st.sidebar.header("Run digest")
    try:
        cfg = digest.load_config()
    except SystemExit as e:
        st.sidebar.error(f"targets.yaml problem: {e}")
        return
    apollo = cfg.get("apollo") or {}
    lines = [
        f"- Companies: {len(cfg['companies'])}",
        f"- max_age_days: {cfg['filters'].get('max_age_days', 'off')}",
        f"- max_experience_years: {cfg['filters'].get('max_experience_years', 'off')}",
        f"- Apollo: {'on' if apollo.get('enabled') else 'off'}"
        + (f", max_lookups_per_run {apollo.get('max_lookups_per_run')}" if apollo.get("enabled") else ""),
        f"- ANTHROPIC_API_KEY: {'set' if os.environ.get('ANTHROPIC_API_KEY') else 'NOT set (no drafts)'}",
        f"- APOLLO_API_KEY: {'set' if os.environ.get('APOLLO_API_KEY') else 'NOT set'}",
    ]
    st.sidebar.markdown("\n".join(lines))

    if not st.session_state.get("confirm_run"):
        if st.sidebar.button("Run digest", key="run_start"):
            st.session_state["confirm_run"] = True
            st.rerun()
    else:
        box = st.sidebar.container(border=True)
        box.warning("This runs digest.py for real: it fetches every board and spends "
                    "Anthropic (and Apollo, if on) credits on new matches, within the caps above.")
        r1, r2 = box.columns(2)
        if r1.button("Yes, run it", type="primary", key="run_ok"):
            st.session_state.pop("confirm_run", None)
            with st.spinner("Running digest.py... (fetching boards and drafting)"):
                try:
                    res = run_digest()
                    st.session_state["run_output"] = (res.returncode, res.stdout + res.stderr)
                except subprocess.TimeoutExpired:
                    st.session_state["run_output"] = (1, "digest.py timed out after 30 minutes.")
            st.session_state["pending_select"] = dt.date.today().isoformat()
            st.rerun()
        if r2.button("Cancel", key="run_cancel"):
            st.session_state.pop("confirm_run", None)
            st.rerun()


def main():
    st.set_page_config(page_title="Job digest", page_icon="📬", layout="wide")
    st.title("Job digest")

    if "run_output" in st.session_state:
        code, out = st.session_state["run_output"]
        with st.expander("Last run output" + (" (failed)" if code else ""), expanded=bool(code)):
            st.code(out or "(no output)", language=None)

    digests = sidebar_history(list_digests())
    date_str = st.session_state.get("selected") if digests else None
    md_path = DIGESTS_DIR / f"digest-{date_str}.md" if date_str else None

    seen = digest.load_seen()
    postings, has_sidecar = ([], False)
    if md_path and md_path.exists():
        postings, has_sidecar = load_digest(date_str, md_path, load_targets(), seen)
        sidebar_delete(date_str, md_path, postings, has_sidecar)

    shown = sidebar_filters(postings, seen)
    sidebar_run()

    if not md_path:
        return
    st.subheader(f"Digest {date_str}")
    st.caption(f"{len(postings)} posting(s), {len(shown)} shown"
               + ("" if has_sidecar else " · read from the .md (older digest, best-effort)"))
    if not postings:
        st.info("No new matches in this digest.")
        return
    verdicts = feedback.read_verdicts()
    overrides = load_overrides()
    for p in shown:
        render_card(p, seen, verdicts, overrides)


main()
