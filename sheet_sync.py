"""Sync open GTM-related roles into the outreach tracker Google Sheet.

Run it:   python3 sheet_sync.py tracks/sdr_gtm.yaml
Test it:  python3 sheet_sync.py tracks/sdr_gtm.yaml --dry-run --companies "Aircall,Strella"

For every company on the sheet's Companies tab it finds the company's public job
board (Greenhouse, Ashby, Lever, SmartRecruiters, Recruitee), pulls ALL current
postings, keeps the SDR / BDR / GTM / growth / sales-ops ones in NYC, SF or US
remote, and writes four cells per company: Open GTM role?, Roles found,
Careers page, Checked on.

Unlike digest.py this reports what is open RIGHT NOW, not just what's new, so it
never reads or writes seen.json, digests/, or feedback.md, and it never calls the
Claude API.

Finding a board:
  1. an `overrides` entry in the track file (ats + slug), else
  2. a board URL already in the sheet's Careers page column (the sheet is the
     cache, so discovery only happens once per company), else
  3. auto-discovery: try slugs built from the company name on each ATS, and only
     accept a board that is verifiably that company's.
Companies whose board can't be found or read are left untouched, so the Claude
scheduled check can cover them instead.

Needs (for real runs, not --dry-run):
  SHEET_ID                     the Google Sheet's ID
  GOOGLE_SERVICE_ACCOUNT_JSON  the service account key JSON (the sheet must be
                               shared with that account's email as an Editor)
"""

import argparse
import datetime as dt
import json
import os
import re
import sys
import types
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import yaml

# digest.py imports `anthropic` at module load for its drafting step. The sync
# never drafts, so let it import without the package installed.
sys.modules.setdefault("anthropic", types.ModuleType("anthropic"))
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import digest  # noqa: E402  (reuses its tested fetchers)
TZ = ZoneInfo("America/Los_Angeles")
SHEETS_API = "https://sheets.googleapis.com/v4/spreadsheets"

BOARD_URLS = {
    "greenhouse": "https://job-boards.greenhouse.io/{slug}",
    "lever": "https://jobs.lever.co/{slug}",
    "ashby": "https://jobs.ashbyhq.com/{slug}",
    "smartrecruiters": "https://jobs.smartrecruiters.com/{slug}",
    "recruitee": "https://{slug}.recruitee.com",
}
BOARD_URL_PATTERNS = [
    ("greenhouse", r"(?:job-boards|boards)\.greenhouse\.io/([A-Za-z0-9_-]+)"),
    ("lever", r"jobs\.lever\.co/([A-Za-z0-9_.-]+)"),
    ("ashby", r"jobs\.ashbyhq\.com/([A-Za-z0-9_.%-]+)"),
    ("smartrecruiters", r"jobs\.smartrecruiters\.com/([A-Za-z0-9_-]+)"),
    ("recruitee", r"([A-Za-z0-9-]+)\.recruitee\.com"),
]
DISCOVERY_ORDER = ["greenhouse", "ashby", "lever"]
GENERIC_WORDS = {"software", "labs", "lab", "inc", "technologies", "technology", "ai", "data",
                 "health", "hq", "io", "co", "app", "the"}


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

def load_track(path):
    cfg = yaml.safe_load(Path(path).read_text()) or {}
    f = cfg.setdefault("filters", {})
    for key in ("roles", "senior", "exclude", "locations", "non_us_markers"):
        f[key] = [str(x).lower() for x in (f.get(key) or [])]
    f.setdefault("max_listed", 4)
    if not f["roles"]:
        sys.exit(f"{path}: filters.roles is empty -- nothing would ever match.")
    cfg["overrides"] = cfg.get("overrides") or {}
    sheet = cfg.setdefault("sheet", {})
    sheet.setdefault("tab", "Companies")
    sheet.setdefault("header_row", 1)
    sheet.setdefault("skip_priority", ["Cut"])
    return cfg


# --------------------------------------------------------------------------
# Role filtering
# --------------------------------------------------------------------------

def _has(term, text):
    """Whole-word match for short tokens (SDR, GTM), substring for phrases."""
    if len(term) <= 4 and " " not in term:
        return re.search(rf"(?<![a-z]){re.escape(term)}(?![a-z])", text) is not None
    return term in text


def location_counts(location, f):
    """NYC / SF anywhere in the string counts; otherwise only US-wide or US remote."""
    loc = (location or "").lower()
    if any(_has(x, loc) for x in f["locations"]):
        return True
    if any(_has(m, loc) for m in f["non_us_markers"]):
        return False
    return "remote" in loc or loc.strip(" .") in ("us", "usa", "united states", "u.s")


def classify(job, f):
    """Return None (not relevant) or a dict {title, location, senior, url}."""
    title = job["title"].lower()
    if not any(_has(r, title) for r in f["roles"]):
        return None
    if any(_has(x, title) for x in f["exclude"]):
        return None
    if not location_counts(job["location"], f):
        return None
    senior = any(_has(s, title) for s in f["senior"])
    return {"title": job["title"], "location": job["location"] or "?", "senior": senior,
            "url": job.get("url", "")}


def summarize(roles, f):
    """Sheet values for K and L from a company's relevant roles."""
    # Entry-level first, then by title so SDR/BDR land near the top.
    rank = lambda r: (r["senior"], not any(_has(t, r["title"].lower())
                                            for t in ("sdr", "bdr", "sales development",
                                                      "business development rep")), r["title"])
    roles = sorted(roles, key=rank)
    seen, listed = set(), []
    for r in roles:
        key = (r["title"].lower(), r["location"].lower())
        if key in seen:
            continue
        seen.add(key)
        tag = " (senior)" if r["senior"] else ""
        listed.append(f"{r['title']} ({r['location']}){tag}")
    extra = len(listed) - f["max_listed"]
    text = "; ".join(listed[:f["max_listed"]]) + (f"; +{extra} more" if extra > 0 else "")
    has_entry = any(not r["senior"] for r in roles)
    return ("Yes" if has_entry else "None found"), (text if roles else "")


# --------------------------------------------------------------------------
# Finding each company's board
# --------------------------------------------------------------------------

def _norm(s):
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def name_variants(name):
    """Strings that should appear in a genuine board's text, most specific first."""
    full = _norm(name)
    core = " ".join(w for w in full.split() if w not in GENERIC_WORDS) or full
    out = [full, full.replace(" ", "")]
    if core != full and len(core) >= 6:  # "cockroach" yes, "norm" or "vast" no (too common)
        out.append(core)
    return list(dict.fromkeys(out))


def slug_candidates(name, extra=()):
    full = _norm(name)
    core = " ".join(w for w in full.split() if w not in GENERIC_WORDS) or full
    cands = list(extra) + [full.replace(" ", ""), full.replace(" ", "-"),
                           core.replace(" ", ""), core.replace(" ", "-")]
    return [c for c in dict.fromkeys(cands) if c]


def board_from_url(url):
    for ats, pat in BOARD_URL_PATTERNS:
        m = re.search(pat, url or "")
        if m and m.group(1).lower() not in ("www", "jobs", "app"):
            return ats, m.group(1)
    return None


def _mentions(jobs, name):
    variants = name_variants(name)
    for j in jobs:
        blob = _norm(f"{j.get('description', '')} {j.get('url', '')} {j.get('title', '')}")
        squashed = blob.replace(" ", "")
        if any((f" {v} " in f" {blob} ") or (" " not in v and len(v) >= 5 and v in squashed)
               for v in variants):
            return True
    return False


def _greenhouse_board_name(slug):
    try:
        data = digest._get_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}")
        return data.get("name") or ""
    except requests.RequestException:
        return None


def try_board(name, ats, slug, verify):
    """Fetch a board. Returns its jobs, or None if it doesn't exist / isn't this company."""
    company = {"name": name, "ats": ats, "slug": slug}
    if verify and ats == "greenhouse":
        board = _greenhouse_board_name(slug)
        if board is None:
            return None
        if not any(v in _norm(board) or v in _norm(board).replace(" ", "")
                   for v in name_variants(name)):
            return None
    try:
        jobs = digest.FETCHERS[ats](company)
    except (requests.RequestException, ValueError, KeyError, TypeError):
        return None
    if verify and ats != "greenhouse":
        if not jobs or not _mentions(jobs, name):
            return None  # empty board or a different company with the same slug
    return jobs


def find_board(name, cached_url, overrides):
    """Returns (ats, slug, jobs, how) or None. ats == "skip" means deliberately skipped."""
    ov = overrides.get(name) or {}
    if ov.get("ats") == "skip":
        return ("skip", None, None, "override")
    if ov.get("ats") and ov.get("slug"):
        jobs = try_board(name, ov["ats"], ov["slug"], verify=False)
        return (ov["ats"], ov["slug"], jobs, "override") if jobs is not None else None
    cached = board_from_url(cached_url)
    if cached:
        jobs = try_board(name, cached[0], cached[1], verify=False)
        if jobs is not None:
            return (cached[0], cached[1], jobs, "sheet")
    for slug in slug_candidates(name, ov.get("slugs") or ()):
        for ats in DISCOVERY_ORDER:
            jobs = try_board(name, ats, slug, verify=True)
            if jobs is not None:
                return (ats, slug, jobs, "discovered")
    return None


# --------------------------------------------------------------------------
# Google Sheets (plain REST + a service account token)
# --------------------------------------------------------------------------

def sheets_token():
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not raw:
        sys.exit("GOOGLE_SERVICE_ACCOUNT_JSON is not set (use --dry-run to test without it).")
    from google.oauth2 import service_account  # only needed for real runs
    from google.auth.transport.requests import Request
    creds = service_account.Credentials.from_service_account_info(
        json.loads(raw), scopes=["https://www.googleapis.com/auth/spreadsheets"])
    creds.refresh(Request())
    return creds.token


def col_letter(idx):  # 0 -> A
    s = ""
    idx += 1
    while idx:
        idx, r = divmod(idx - 1, 26)
        s = chr(65 + r) + s
    return s


def read_sheet(sheet_id, tab, token):
    url = f"{SHEETS_API}/{sheet_id}/values/{requests.utils.quote(tab)}"
    r = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=30)
    r.raise_for_status()
    return r.json().get("values", [])


def write_cells(sheet_id, updates, token):
    if not updates:
        return
    body = {"valueInputOption": "USER_ENTERED", "data": updates}
    r = requests.post(f"{SHEETS_API}/{sheet_id}/values:batchUpdate", json=body,
                      headers={"Authorization": f"Bearer {token}"}, timeout=30)
    r.raise_for_status()


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def check_company(name, cached_url, cfg):
    """Returns (open_role, roles_text, careers_url, how) or None if not checkable."""
    found = find_board(name, cached_url, cfg["overrides"])
    if found is None:
        return None
    ats, slug, jobs, how = found
    if ats == "skip":
        return ("skip", None, None, how)
    roles = [r for r in (classify(j, cfg["filters"]) for j in jobs) if r]
    open_role, text = summarize(roles, cfg["filters"])
    return (open_role, text, BOARD_URLS[ats].format(slug=slug), f"{how} {ats}:{slug}, "
            f"{len(jobs)} postings, {len(roles)} relevant")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("track", help="track file, e.g. tracks/sdr_gtm.yaml")
    ap.add_argument("--dry-run", action="store_true", help="print results, don't touch the sheet")
    ap.add_argument("--companies", help="comma-separated names (dry-run without the sheet)")
    args = ap.parse_args()

    cfg = load_track(args.track)
    sh = cfg["sheet"]
    today = dt.datetime.now(TZ).date()
    date_cell = f"{today.month}/{today.day}/{today.year}"

    if args.companies:
        rows = [{"name": n.strip(), "row": None, "prev": "", "careers": ""}
                for n in args.companies.split(",") if n.strip()]
        sheet_id = token = colmap = None
    else:
        sheet_id = os.environ.get("SHEET_ID") or sys.exit("SHEET_ID is not set.")
        token = sheets_token()
        values = read_sheet(sheet_id, sh["tab"], token)
        header = values[sh["header_row"] - 1] if len(values) >= sh["header_row"] else []
        colmap = {}
        for key, label in sh["columns"].items():
            if label not in header:
                sys.exit(f"Column {label!r} not found in the {sh['tab']} header row.")
            colmap[key] = header.index(label)
        rows = []
        for i, row in enumerate(values[sh["header_row"]:], start=sh["header_row"] + 1):
            cell = lambda k: row[colmap[k]].strip() if colmap[k] < len(row) else ""
            if not cell("company") or cell("priority") in sh["skip_priority"]:
                continue
            rows.append({"name": cell("company"), "row": i, "prev": cell("open_role"),
                         "careers": cell("careers")})

    print(f"Checking {len(rows)} companies...")
    updates, report = [], {"new_yes": [], "closed": [], "unchecked": [], "skipped": []}
    for r in rows:
        result = check_company(r["name"], r["careers"], cfg)
        if result is None:
            print(f"  ? {r['name']}: no readable job board found -- left for the Claude check")
            report["unchecked"].append(r["name"])
            continue
        open_role, text, url, how = result
        if open_role == "skip":
            print(f"  - {r['name']}: skipped ({how}) -- left for the Claude check")
            report["skipped"].append(r["name"])
            continue
        print(f"  {'+' if open_role == 'Yes' else '-'} {r['name']}: {open_role} [{how}]"
              + (f"\n      {text}" if text else ""))
        if open_role == "Yes" and r["prev"] != "Yes":
            report["new_yes"].append(r["name"])
        if open_role != "Yes" and r["prev"] == "Yes":
            report["closed"].append(r["name"])
        if r["row"]:
            first, last = col_letter(colmap["open_role"]), col_letter(colmap["checked"])
            if colmap["checked"] - colmap["open_role"] == 3:  # K..N side by side
                updates.append({"range": f"{sh['tab']}!{first}{r['row']}:{last}{r['row']}",
                                "values": [[open_role, text, url, date_cell]]})
            else:
                for key, val in (("open_role", open_role), ("roles", text),
                                 ("careers", url), ("checked", date_cell)):
                    updates.append({"range": f"{sh['tab']}!{col_letter(colmap[key])}{r['row']}",
                                    "values": [[val]]})

    if not args.dry_run and not args.companies:
        write_cells(sheet_id, updates, token)
        print(f"Wrote {len(updates)} range(s) to the sheet.")

    lines = [
        f"New openings: {', '.join(report['new_yes']) or 'none'}",
        f"Closed since last check: {', '.join(report['closed']) or 'none'}",
        f"No readable board (Claude check covers these): {', '.join(report['unchecked'] + report['skipped']) or 'none'}",
    ]
    print("\n".join(lines))
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as fh:
            fh.write("## Sheet sync\n\n" + "\n".join(f"- {l}" for l in lines) + "\n")


if __name__ == "__main__":
    main()
