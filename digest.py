"""Build a dated Markdown digest of new job postings from target companies.

Run it by hand:  python3 digest.py

It reads targets.yaml, pulls current listings from Greenhouse and Lever's
public JSON APIs, keeps only postings whose title/location match your filters,
drops anything already recorded in seen.json, and writes the survivors to
digests/digest-YYYY-MM-DD.md -- each with an outreach message drafted by Claude
(see draft_all() below). Needs ANTHROPIC_API_KEY set for the drafting step;
without it the postings are still listed, just without drafts.
"""

import datetime as dt
import html
import json
import re
import sys
from pathlib import Path

import requests
import yaml

try:
    import anthropic
except ImportError:
    sys.exit("Missing the 'anthropic' package -- run: pip install -r requirements.txt")

ROOT = Path(__file__).resolve().parent
TARGETS_FILE = ROOT / "targets.yaml"
SEEN_FILE = ROOT / "seen.json"
DIGESTS_DIR = ROOT / "digests"

HTTP_TIMEOUT = 10  # seconds
HEADERS = {"User-Agent": "personal-job-digest"}

DRAFT_MODEL = "claude-sonnet-5"
DESC_WORD_CAP = 500  # trim each job description to this many words before prompting
SR_MAX_POSTINGS = 1000  # SmartRecruiters safety cap: stop paginating past this many


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

def load_config():
    """Read and lightly validate targets.yaml."""
    if not TARGETS_FILE.exists():
        sys.exit(f"Missing {TARGETS_FILE.name} -- copy the template and fill it in.")

    cfg = yaml.safe_load(TARGETS_FILE.read_text()) or {}

    cfg.setdefault("about_me", "")
    cfg.setdefault("looking_for", "")
    filters = cfg.setdefault("filters", {})
    filters.setdefault("keywords", [])
    pairs = filters.get("keywords_and") or {}
    filters["keywords_and"] = pairs
    filters.setdefault("keywords_exclude", [])
    filters.setdefault("locations", [])
    cfg.setdefault("companies", [])

    if not cfg["companies"]:
        sys.exit("No companies listed in targets.yaml -- add at least one.")

    # Lower-case every filter term once, here, so matching stays simple later.
    filters["keywords"] = [k.lower() for k in filters["keywords"]]
    pairs["domain"] = [w.lower() for w in (pairs.get("domain") or [])]
    pairs["execution"] = [w.lower() for w in (pairs.get("execution") or [])]
    filters["keywords_exclude"] = [k.lower() for k in filters["keywords_exclude"]]
    filters["locations"] = [l.lower() for l in filters["locations"]]

    if not filters["keywords"] and not (pairs["domain"] and pairs["execution"]):
        sys.exit("targets.yaml: set filters.keywords, or both keywords_and.domain and "
                 "keywords_and.execution -- there must be at least one way to match a title.")

    age = filters.get("max_age_days")
    if age in (None, 0):
        filters["max_age_days"] = None
    elif isinstance(age, int) and not isinstance(age, bool) and age > 0:
        filters["max_age_days"] = age
    else:
        sys.exit("targets.yaml: filters.max_age_days must be a positive whole number "
                 "of days, or omitted.")
    return cfg


# --------------------------------------------------------------------------
# Fetching -- each function returns a list of normalized job dicts:
#   {key, company, title, location, url, posted, description}
# --------------------------------------------------------------------------

def _get_json(url):
    resp = requests.get(url, headers=HEADERS, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def _clean_html(raw):
    """Turn a chunk of HTML into plain text, trimmed to DESC_WORD_CAP words."""
    if not raw:
        return ""
    text = html.unescape(str(raw))
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)  # entities sometimes survive one pass
    words = text.split()
    if len(words) > DESC_WORD_CAP:
        words = words[:DESC_WORD_CAP] + ["..."]
    return " ".join(words)


def _iso_date(value):
    """Best-effort 'YYYY-MM-DD' from an ISO timestamp or epoch-millis int."""
    if not value:
        return "unknown"
    try:
        if isinstance(value, (int, float)):
            return dt.datetime.fromtimestamp(value / 1000, dt.timezone.utc).date().isoformat()
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00")).date().isoformat()
    except (ValueError, OverflowError, OSError):
        return "unknown"


def fetch_greenhouse(company):
    slug = company["slug"]
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
    data = _get_json(url)
    jobs = []
    for job in data.get("jobs", []):
        jobs.append({
            "key": f"greenhouse:{slug}:{job['id']}",
            "company": company["name"],
            "title": job.get("title", "").strip(),
            "location": (job.get("location") or {}).get("name", "").strip(),
            "url": job.get("absolute_url", ""),
            "posted": _iso_date(job.get("first_published") or job.get("updated_at")),
            "description": _clean_html(job.get("content", "")),
        })
    return jobs


def fetch_lever(company):
    slug = company["slug"]
    url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    data = _get_json(url)
    jobs = []
    for job in data:
        categories = job.get("categories") or {}
        location = categories.get("location") or job.get("workplaceType") or ""
        description = job.get("descriptionPlain") or _clean_html(job.get("description", ""))
        jobs.append({
            "key": f"lever:{slug}:{job['id']}",
            "company": company["name"],
            "title": job.get("text", "").strip(),
            "location": location.strip(),
            "url": job.get("hostedUrl", ""),
            "posted": _iso_date(job.get("createdAt")),
            "description": " ".join(description.split()[:DESC_WORD_CAP]),
        })
    return jobs


def fetch_ashby(company):
    slug = company["slug"]
    url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
    data = _get_json(url)
    jobs = []
    for job in data.get("jobs", []):
        if job.get("isListed") is False:  # hidden from the public board
            continue
        description = job.get("descriptionPlain") or _clean_html(job.get("descriptionHtml", ""))
        jobs.append({
            "key": f"ashby:{slug}:{job['id']}",
            "company": company["name"],
            "title": (job.get("title") or "").strip(),
            "location": (job.get("location") or "").strip(),
            "url": job.get("jobUrl", ""),
            "posted": _iso_date(job.get("publishedAt")),
            "description": " ".join(description.split()[:DESC_WORD_CAP]),
        })
    return jobs


def fetch_smartrecruiters(company):
    slug = company["slug"]
    base = f"https://api.smartrecruiters.com/v1/companies/{slug}/postings"
    postings, offset, total = [], 0, 0
    while True:
        data = _get_json(f"{base}?limit=100&offset={offset}")
        total = data.get("totalFound", 0)
        batch = data.get("content", [])
        postings.extend(batch)
        offset += len(batch)
        if not batch or offset >= total or offset >= SR_MAX_POSTINGS:
            break
    if total == 0:
        print(f"  ! {company['name']}: SmartRecruiters returned 0 postings -- check the slug")
    elif offset < total:
        print(f"  ! {company['name']}: {total} SmartRecruiters postings, only first {offset} checked")

    jobs = []
    for p in postings:
        loc = p.get("location") or {}
        location = loc.get("fullLocation") or ", ".join(
            x for x in (loc.get("city"), loc.get("region"), (loc.get("country") or "").upper()) if x
        )
        if loc.get("remote") and "remote" not in location.lower():
            location = f"{location} (Remote)".lstrip(", ")
        jobs.append({
            "key": f"smartrecruiters:{slug}:{p['id']}",
            "company": company["name"],
            "title": (p.get("name") or "").strip(),
            "location": location.strip(),
            "url": f"https://jobs.smartrecruiters.com/{slug}/{p['id']}",
            "posted": _iso_date(p.get("releasedDate")),
            "description": "",  # SmartRecruiters list endpoint has no description text
        })
    return jobs


def fetch_recruitee(company):
    slug = company["slug"]
    url = f"https://{slug}.recruitee.com/api/offers/"
    data = _get_json(url)
    jobs = []
    for offer in data.get("offers", []):
        if offer.get("status") != "published":
            continue
        description = _clean_html(f"{offer.get('description', '')} {offer.get('requirements', '')}")
        posted = (offer.get("published_at") or offer.get("created_at") or "").split(" ")[0]
        jobs.append({
            "key": f"recruitee:{slug}:{offer['id']}",
            "company": company["name"],
            "title": (offer.get("title") or "").strip(),
            "location": (offer.get("location") or "").strip(),
            "url": offer.get("careers_url", ""),
            "posted": _iso_date(posted),
            "description": " ".join(description.split()[:DESC_WORD_CAP]),
        })
    return jobs


FETCHERS = {"greenhouse": fetch_greenhouse, "lever": fetch_lever, "ashby": fetch_ashby,
            "smartrecruiters": fetch_smartrecruiters, "recruitee": fetch_recruitee}


def fetch_all(companies):
    """Fetch every company's postings, skipping (with a warning) any that fail."""
    all_jobs = []
    for company in companies:
        name = company.get("name", company.get("slug", "?"))
        fetcher = FETCHERS.get(company.get("ats"))
        if fetcher is None:
            print(f"  ! {name}: unknown ats {company.get('ats')!r} "
                  f"(use 'greenhouse', 'lever', 'ashby', 'smartrecruiters', or 'recruitee')")
            continue
        try:
            jobs = fetcher(company)
        except requests.HTTPError as e:
            code = e.response.status_code
            hint = " (check slug)" if code == 404 else ""
            print(f"  ! {name}: HTTP {code}{hint}")
            continue
        except requests.RequestException as e:
            print(f"  ! {name}: request failed ({e})")
            continue
        print(f"  - {name}: {len(jobs)} postings")
        all_jobs.extend(jobs)
    return all_jobs


# --------------------------------------------------------------------------
# Filtering
# --------------------------------------------------------------------------

def job_matches(job, filters, today=None):
    title = job["title"].lower()
    location = job["location"].lower()

    pairs = filters["keywords_and"]
    standalone = any(kw in title for kw in filters["keywords"])
    paired = (any(w in title for w in pairs["domain"])
              and any(w in title for w in pairs["execution"]))
    if not (standalone or paired):
        return False
    # Exclude term must be present and not run straight into another letter, so
    # "Sales Engineer" blocks that title but not "Sales Engineering Manager".
    if any(re.search(re.escape(bad) + r"(?![a-z])", title)
           for bad in filters["keywords_exclude"]):
        return False
    if filters["locations"] and not any(loc in location for loc in filters["locations"]):
        return False

    max_age = filters.get("max_age_days")
    if max_age and job["posted"] != "unknown":
        today = today or dt.date.today()
        try:
            if (today - dt.date.fromisoformat(job["posted"])).days > max_age:
                return False
        except ValueError:
            pass  # unparseable date -> keep it, per the pass-through choice
    return True


def dedupe_postings(jobs):
    """Collapse near-identical postings (same company + normalized title).

    Catches the same role reposted under different job IDs, which seen.json
    (exact-ID only) misses. Keeps the newest-posted of each group; that survivor
    gets a 'dupes_skipped' count. Returns (kept_jobs, num_removed); kept_jobs
    preserves first-seen order.
    """
    groups, order = {}, []
    for job in jobs:
        k = (job["company"], " ".join(job["title"].lower().split()))
        if k not in groups:
            groups[k] = []
            order.append(k)
        groups[k].append(job)

    kept, removed = [], 0
    for k in order:
        grp = groups[k]
        if len(grp) == 1:
            kept.append(grp[0])
            continue
        winner = max(grp, key=lambda j: (j["posted"] != "unknown", j["posted"]))
        winner["dupes_skipped"] = len(grp) - 1
        removed += len(grp) - 1
        kept.append(winner)
    return kept, removed


# --------------------------------------------------------------------------
# Seen-tracking
# --------------------------------------------------------------------------

def load_seen():
    if SEEN_FILE.exists():
        return set(json.loads(SEEN_FILE.read_text()))
    return set()


def save_seen(seen):
    SEEN_FILE.write_text(json.dumps(sorted(seen), indent=2) + "\n")


# --------------------------------------------------------------------------
# Drafting
# --------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You draft short, personal outreach messages for someone exploring a job opening.

Write a LinkedIn-style message the user could send to a person at the company -- a \
recruiter, hiring manager, or someone on the team.

Rules:
- 100 to 140 words. Plain text only: no markdown, no bullet points, no headings.
- Open with "Hi [name]," -- keep the literal bracket placeholder, never invent a name.
- Name the specific role by its title and the company by name.
- Weave in ONE concrete, relevant detail from the user's background -- not a list.
- Warm and direct. No flattery, no buzzwords. Do not use the phrases "I'm passionate
  about", "I came across your posting", or "I hope this message finds you well".
- End with one low-friction ask (a brief call, or being pointed to the right person).
- Sign off exactly with:
  Best,
  [your name]
- Output only the message. No preamble, no explanation, no subject line.
"""


def make_client():
    """Return an Anthropic client, or None if no usable API key is configured."""
    try:
        return anthropic.Anthropic()
    except Exception as e:  # missing/blank ANTHROPIC_API_KEY, or other setup failure
        print(f"  ! no Claude client ({type(e).__name__}); drafts will be skipped")
        return None


def _build_user_prompt(job, cfg):
    return (
        f"ABOUT ME:\n{cfg['about_me'].strip()}\n\n"
        f"WHAT I'M LOOKING FOR:\n{cfg['looking_for'].strip()}\n\n"
        f"THE ROLE:\n"
        f"Company: {job['company']}\n"
        f"Title: {job['title']}\n"
        f"Location: {job['location'] or 'unspecified'}\n\n"
        f"JOB DESCRIPTION (trimmed):\n{job['description'] or '(none available)'}"
    )


def draft_all(jobs, cfg, client):
    """Draft one message per job. Returns {job_key: {"text": str, "ok": bool}}.

    A failed call is recorded with ok=False and the run continues; the caller
    leaves ok=False jobs out of seen.json so the next run retries them.
    """
    drafts = {}
    total = len(jobs)
    skipped = "_(draft skipped -- no ANTHROPIC_API_KEY)_"
    no_auth = client is None
    for i, job in enumerate(jobs, 1):
        if no_auth:
            drafts[job["key"]] = {"text": skipped, "ok": False}
            continue
        print(f"  drafting {i}/{total}: {job['title']}")
        try:
            resp = client.messages.create(
                model=DRAFT_MODEL,
                max_tokens=600,
                thinking={"type": "disabled"},
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": _build_user_prompt(job, cfg)}],
            )
            text = "".join(b.text for b in resp.content if b.type == "text").strip()
            drafts[job["key"]] = {"text": text or "_(draft failed: empty response)_",
                                  "ok": bool(text)}
        except Exception as e:
            # The SDK reports "no usable credentials" as a TypeError at first call;
            # a bad key is AuthenticationError. Either way, stop trying and skip the rest.
            is_auth = isinstance(e, anthropic.AuthenticationError) or (
                isinstance(e, TypeError) and "authentication" in str(e).lower()
            )
            if is_auth:
                print("  ! no usable Claude credentials -- set ANTHROPIC_API_KEY; skipping drafts")
                drafts[job["key"]] = {"text": skipped, "ok": False}
                no_auth = True
            else:
                # Any other API/network error: note it, keep going, retry next run.
                print(f"  ! draft failed for {job['title']}: {type(e).__name__}")
                drafts[job["key"]] = {"text": f"_(draft failed: {type(e).__name__})_", "ok": False}
    return drafts


# --------------------------------------------------------------------------
# Digest output
# --------------------------------------------------------------------------

def write_digest(new_jobs, matched_count, drafts, date_str, deduped_count=0):
    """Append this run's results as a labeled section to that day's digest file.

    The first run of the day creates the file with the `# Job digest -- <date>`
    title; later runs append `--- ` + a new `**Run N ...**` section rather than
    overwriting, so same-day runs accumulate.
    """
    DIGESTS_DIR.mkdir(exist_ok=True)
    path = DIGESTS_DIR / f"digest-{date_str}.md"
    existing = path.read_text() if path.exists() else None

    run_n = 1
    if existing:
        run_n += sum(1 for ln in existing.splitlines() if ln.startswith("**Run "))
    stamp = dt.datetime.now().strftime("%H:%M:%S")
    drafted = sum(1 for d in drafts.values() if d["ok"])

    parts = [f"{matched_count} matching across targets"]
    if deduped_count:
        parts.append(f"{deduped_count} near-dupes merged")
    if new_jobs:
        parts.append(f"{drafted} drafted")
    lines = [
        f"**Run {run_n} · {stamp}** -- {len(new_jobs)} new ({', '.join(parts)})",
        "",
    ]

    if not new_jobs:
        lines.append("_No new matches this run._")
    else:
        # Group by company, preserving first-seen order.
        by_company = {}
        for job in new_jobs:
            by_company.setdefault(job["company"], []).append(job)

        for company, jobs in by_company.items():
            lines.append(f"## {company}")
            lines.append("")
            for job in jobs:
                draft = drafts[job["key"]]
                lines.append(f"### {job['title'] or '(untitled role)'}")
                lines.append(f"- **Location:** {job['location'] or 'unspecified'}")
                lines.append(f"- **Posted:** {job['posted']}")
                lines.append(f"- **Link:** {job['url'] or 'n/a'}")
                if job.get("dupes_skipped"):
                    lines.append(f"- **Also posted:** {job['dupes_skipped']} near-identical "
                                 f"reposting(s) at this company, not separately drafted")
                lines.append("")
                body = f"*Draft outreach:*\n\n{draft['text']}" if draft["ok"] else draft["text"]
                for para in body.split("\n"):
                    lines.append(f"> {para}" if para else ">")
                lines.append("")

    block = "\n".join(lines).rstrip("\n") + "\n"
    if existing is None:
        path.write_text(f"# Job digest -- {date_str}\n\n{block}")
    else:
        path.write_text(existing.rstrip("\n") + "\n\n---\n\n" + block)
    return path


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main():
    cfg = load_config()
    companies = cfg["companies"]

    print(f"Checking {len(companies)} companies...")
    all_jobs = fetch_all(companies)

    matched = [j for j in all_jobs if job_matches(j, cfg["filters"])]
    matched_count = len(matched)
    matched, deduped = dedupe_postings(matched)

    seen = load_seen()
    new_jobs = [j for j in matched if j["key"] not in seen]

    client = make_client() if new_jobs else None
    drafts = draft_all(new_jobs, cfg, client)

    date_str = dt.date.today().isoformat()
    path = write_digest(new_jobs, matched_count, drafts, date_str, deduped)

    # Only remember postings we actually drafted -- failures retry next run.
    drafted = 0
    for job in new_jobs:
        if drafts[job["key"]]["ok"]:
            seen.add(job["key"])
            drafted += 1
    save_seen(seen)

    print(
        f"Checked {len(companies)} companies, "
        f"found {matched_count} matching postings, {deduped} near-duplicates merged, "
        f"{len(new_jobs)} new, {drafted} drafted. "
        f"Wrote {path.relative_to(ROOT)}"
    )


if __name__ == "__main__":
    main()
