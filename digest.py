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
import os
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
FIRST_RUN_LOOKBACK_DAYS = 30  # recency window for a company with zero seen.json entries


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

    win = pairs.get("description_window")
    if win in (None, 0):
        pairs["description_window"] = None
    elif isinstance(win, int) and not isinstance(win, bool) and win > 0:
        pairs["description_window"] = win
    else:
        sys.exit("targets.yaml: keywords_and.description_window must be a positive whole "
                 "number of words, or omitted.")

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

    exp = filters.get("max_experience_years")
    if exp in (None, 0):
        filters["max_experience_years"] = None
    elif isinstance(exp, int) and not isinstance(exp, bool) and exp > 0:
        filters["max_experience_years"] = exp
    else:
        sys.exit("targets.yaml: filters.max_experience_years must be a positive whole "
                 "number of years, or omitted.")

    apollo = cfg.setdefault("apollo", {}) or {}
    cfg["apollo"] = apollo
    apollo.setdefault("enabled", False)
    if not isinstance(apollo["enabled"], bool):
        sys.exit("targets.yaml: apollo.enabled must be true or false.")
    cap = apollo.get("max_lookups_per_run", 5)
    if not (isinstance(cap, int) and not isinstance(cap, bool) and cap > 0):
        sys.exit("targets.yaml: apollo.max_lookups_per_run must be a positive whole number.")
    apollo["max_lookups_per_run"] = cap
    apollo.setdefault("titles", ["Revenue Operations", "RevOps", "Sales Operations", "GTM",
                                 "Marketing Operations", "Demand Generation", "Growth"])
    apollo.setdefault("seniorities", ["manager", "director"])
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

def _pair_within(text, domain, execution, window):
    """True if a `domain` token and an `execution` token occur within `window`
    word-positions of each other in `text` (both are exact lowercase tokens)."""
    toks = re.findall(r"[a-z]+", text.lower())
    di = [i for i, w in enumerate(toks) if w in domain]
    if not di:
        return False
    ei = [i for i, w in enumerate(toks) if w in execution]
    return any(abs(a - b) <= window for a in di for b in ei) if ei else False


def job_matches(job, filters, today=None, known_companies=None):
    """Return "title", "description", or "" (no match).

    Title keyword rules run first. If they miss and keywords_and.description_window
    is set, the same domain+execution pair is tried against title+description, but
    only counts when the two words land within that many word-positions of each
    other. keywords_exclude, locations, and the experience filter check the
    title-derived fields only, unchanged. `known_companies` (a set of "ats:slug"
    prefixes seen before, from seen.json) widens the recency window to
    FIRST_RUN_LOOKBACK_DAYS for a company with no prior entries; omit it (None) to
    disable that behavior entirely.
    """
    title = job["title"].lower()
    location = job["location"].lower()
    pairs = filters["keywords_and"]

    standalone = any(kw in title for kw in filters["keywords"])
    paired = (any(w in title for w in pairs["domain"])
              and any(w in title for w in pairs["execution"]))
    if standalone or paired:
        via = "title"
    elif pairs.get("description_window") and _pair_within(
            f"{title} {job.get('description', '')}",
            set(pairs["domain"]), set(pairs["execution"]), pairs["description_window"]):
        via = "description"
    else:
        return ""

    # Exclude term must be present and not run straight into another letter, so
    # "Sales Engineer" blocks that title but not "Sales Engineering Manager".
    if any(re.search(re.escape(bad) + r"(?![a-z])", title)
           for bad in filters["keywords_exclude"]):
        return ""
    if filters["locations"] and not any(loc in location for loc in filters["locations"]):
        return ""

    max_age = filters.get("max_age_days")
    if max_age and job["posted"] != "unknown":
        if known_companies is not None and job["key"].rsplit(":", 1)[0] not in known_companies:
            max_age = FIRST_RUN_LOOKBACK_DAYS  # company never in seen.json -- widen, once
        today = today or dt.date.today()
        try:
            if (today - dt.date.fromisoformat(job["posted"])).days > max_age:
                return ""
        except ValueError:
            pass  # unparseable date -> keep it, per the pass-through choice

    max_exp = filters.get("max_experience_years")
    if max_exp:
        min_years = min_experience_years(job.get("description", ""))
        if min_years is not None and min_years > max_exp:
            return ""
    return via


_YEAR_REQ_RE = re.compile(
    r"\b(\d{1,2})\s*(?:(\+)|(?:\s*(?:-|–|—|to)\s*(\d{1,2})\s*(\+)?))?\s*(?:years?|yrs?)\b",
    re.I,
)
_AT_LEAST_RE = re.compile(r"(?:at least|minimum(?:\s+of)?|min\.?)\s*$", re.I)
_TENURE_BEFORE_RE = re.compile(
    r"\b(ago|past|under|within|next|since|coming|company|combined|collective|arr)\b"
    r"|\b(?:over|for|in)\s+the\b|\bwe['\s]?(?:ve|have)\b|\bour\s+team\b",
    re.I,
)


def detect_experience(description):
    """Heuristically pull stated years-of-experience requirements from a job
    description. DISPLAY ONLY -- returns a deduped list of normalized phrases
    like ["3+ years", "5-8 years"], sorted by the leading number ascending.
    Empty when nothing requirement-shaped is found.
    """
    if not description:
        return []
    found = {}  # normalized phrase -> leading int (for dedupe + sort)
    for m in _YEAR_REQ_RE.finditer(description):
        lo, plus, hi, hiplus = m.groups()
        before = description[max(0, m.start() - 25):m.start()]
        # "requirement shape": trailing +, a range, or an "at least" lead-in.
        # Bare "N years" is almost always a timeframe -- skip it.
        if not (plus or hi is not None or _AT_LEAST_RE.search(before)):
            continue
        # Drop timeframes ("in under 2 years") and company tenure ("company has 10+ years").
        if _TENURE_BEFORE_RE.search(before):
            continue
        phrase = f"{lo}-{hi}{'+' if hiplus else ''} years" if hi is not None else f"{lo}+ years"
        found.setdefault(phrase, int(lo))
    return sorted(found, key=lambda p: (found[p], p))


def min_experience_years(description):
    """Smallest leading number across detect_experience()'s results, or None.

    detect_experience() sorts its phrases by leading number ascending, so the
    first phrase carries the minimum stated requirement.
    """
    phrases = detect_experience(description)
    if not phrases:
        return None
    return int(re.match(r"\d+", phrases[0]).group())


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
- If a CONTACT is given below, open with "Hi {their first name}," and write to them
  specifically. If no CONTACT is given, open with the literal placeholder "Hi [name],"
  -- never invent a name.
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


def apollo_find_contact(domain, apollo_cfg):
    """Search Apollo for a plausible GTM/RevOps contact at `domain`, then enrich the
    single best hit. Returns {"name": str, "title": str} or None -- never raises.

    Free api_search first; people/match (the paid call) is tried on only the first
    search hit, identified by its Apollo id (no name-matching ambiguity). One attempt
    per call, no retries -- conservative with a limited credit budget.
    """
    api_key = os.environ.get("APOLLO_API_KEY")
    if not api_key:
        print("  ! apollo.enabled is true but no APOLLO_API_KEY in environment -- "
              "skipping contact lookups")
        return None
    headers = {"x-api-key": api_key}
    try:
        r = requests.post(
            "https://api.apollo.io/api/v1/mixed_people/api_search",
            headers=headers, timeout=HTTP_TIMEOUT,
            json={
                "q_organization_domains_list": [domain],
                "person_titles": apollo_cfg["titles"],
                "person_seniorities": apollo_cfg["seniorities"],
                "per_page": 5,
            },
        )
        r.raise_for_status()
        people = r.json().get("people") or []
        if not people:
            return None

        r2 = requests.post(
            "https://api.apollo.io/api/v1/people/match",
            headers=headers, timeout=HTTP_TIMEOUT,
            json={"id": people[0]["id"]},
        )
        r2.raise_for_status()
        person = r2.json().get("person") or {}
        if not person.get("first_name"):
            return None
        return {"name": person.get("name") or person["first_name"],
                "title": person.get("title") or "unknown title"}
    except Exception as e:
        # Genuinely unexpected (bad key, rate limit, network, schema change) -- surface
        # it, since apollo failures otherwise look identical to "no contact found".
        print(f"  ! apollo lookup failed for {domain}: {type(e).__name__}")
        return None


def _build_user_prompt(job, cfg, contact=None):
    contact_block = (
        f"\n\nCONTACT AT THE COMPANY (address them by first name only; don't invent "
        f"facts about them beyond name/title):\nName: {contact['name']}\n"
        f"Title: {contact['title']}"
    ) if contact else ""
    return (
        f"ABOUT ME:\n{cfg['about_me'].strip()}\n\n"
        f"WHAT I'M LOOKING FOR:\n{cfg['looking_for'].strip()}\n\n"
        f"THE ROLE:\n"
        f"Company: {job['company']}\n"
        f"Title: {job['title']}\n"
        f"Location: {job['location'] or 'unspecified'}\n\n"
        f"JOB DESCRIPTION (trimmed):\n{job['description'] or '(none available)'}"
        f"{contact_block}"
    )


def draft_all(jobs, cfg, client):
    """Draft one message per job. Returns {job_key: {"text", "ok", "contact"}}.

    A failed call is recorded with ok=False and the run continues; the caller
    leaves ok=False jobs out of seen.json so the next run retries them. If
    cfg["apollo"]["enabled"], looks up a real contact per job (capped at
    max_lookups_per_run) and passes it into the prompt; any Apollo failure or a
    missing per-company `domain` just means no contact -- drafting proceeds as usual.
    """
    drafts = {}
    total = len(jobs)
    skipped = "_(draft skipped -- no ANTHROPIC_API_KEY)_"
    no_auth = client is None

    apollo_cfg = cfg.get("apollo") or {}
    company_domains = {c["name"]: c.get("domain") for c in cfg["companies"]}
    apollo_used = 0

    for i, job in enumerate(jobs, 1):
        contact = None
        apollo_attempted = False
        if apollo_cfg.get("enabled") and apollo_used < apollo_cfg["max_lookups_per_run"]:
            domain = company_domains.get(job["company"])
            if domain:
                apollo_attempted = True
                contact = apollo_find_contact(domain, apollo_cfg)
                apollo_used += 1

        if no_auth:
            drafts[job["key"]] = {"text": skipped, "ok": False,
                                  "contact": contact, "apollo_attempted": apollo_attempted}
            continue
        print(f"  drafting {i}/{total}: {job['title']}")
        try:
            resp = client.messages.create(
                model=DRAFT_MODEL,
                max_tokens=600,
                thinking={"type": "disabled"},
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": _build_user_prompt(job, cfg, contact)}],
            )
            text = "".join(b.text for b in resp.content if b.type == "text").strip()
            drafts[job["key"]] = {"text": text or "_(draft failed: empty response)_",
                                  "ok": bool(text), "contact": contact,
                                  "apollo_attempted": apollo_attempted}
        except Exception as e:
            # The SDK reports "no usable credentials" as a TypeError at first call;
            # a bad key is AuthenticationError. Either way, stop trying and skip the rest.
            is_auth = isinstance(e, anthropic.AuthenticationError) or (
                isinstance(e, TypeError) and "authentication" in str(e).lower()
            )
            if is_auth:
                print("  ! no usable Claude credentials -- set ANTHROPIC_API_KEY; skipping drafts")
                drafts[job["key"]] = {"text": skipped, "ok": False,
                                      "contact": contact, "apollo_attempted": apollo_attempted}
                no_auth = True
            else:
                # Any other API/network error: note it, keep going, retry next run.
                print(f"  ! draft failed for {job['title']}: {type(e).__name__}")
                drafts[job["key"]] = {"text": f"_(draft failed: {type(e).__name__})_",
                                      "ok": False, "contact": contact,
                                      "apollo_attempted": apollo_attempted}
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
                if job.get("match_via") == "description":
                    lines.append("- **Matched via:** description (title didn't match on its own)")
                if job.get("dupes_skipped"):
                    lines.append(f"- **Also posted:** {job['dupes_skipped']} near-identical "
                                 f"reposting(s) at this company, not separately drafted")
                if draft.get("contact"):
                    c = draft["contact"]
                    lines.append(f"- **Contact found:** {c['name']}, {c['title']} (via Apollo)")
                elif draft.get("apollo_attempted"):
                    lines.append("- **Contact:** none found via Apollo")
                exp = detect_experience(job.get("description", ""))
                if exp:
                    shown = ", ".join(exp[:3])
                    if len(exp) > 3:
                        shown += f" (+{len(exp) - 3} more)"
                else:
                    shown = "none found"
                lines.append(f"- **Experience (detected):** {shown}")
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


def write_digest_json(new_jobs, drafts, date_str, md_path):
    """Append this run's postings to digests/digest-<date>.json, next to the .md.

    Read by app.py (the local UI). Carries fields the .md doesn't: the posting
    key, ATS, contact status, and the posting description. Purely a record of what
    write_digest() just wrote -- nothing in this file reads it back.
    """
    path = md_path.with_suffix(".json")
    data = json.loads(path.read_text()) if path.exists() else {"date": date_str, "runs": []}
    run_n = sum(1 for ln in md_path.read_text().splitlines() if ln.startswith("**Run "))
    postings = []
    for job in new_jobs:
        draft = drafts[job["key"]]
        postings.append({
            "key": job["key"],
            "ats": job["key"].split(":", 1)[0],
            "company": job["company"],
            "title": job["title"],
            "location": job["location"],
            "url": job["url"],
            "posted": job["posted"],
            "match_via": job.get("match_via"),
            "dupes_skipped": job.get("dupes_skipped", 0),
            "experience": detect_experience(job.get("description", "")),
            "contact": draft.get("contact"),
            "apollo_attempted": bool(draft.get("apollo_attempted")),
            "draft_text": draft["text"],
            "draft_ok": bool(draft["ok"]),
            "description": job.get("description") or "",
        })
    data["runs"].append({"run": run_n, "time": dt.datetime.now().strftime("%H:%M:%S"),
                         "postings": postings})
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main():
    cfg = load_config()
    companies = cfg["companies"]

    print(f"Checking {len(companies)} companies...")
    all_jobs = fetch_all(companies)

    seen = load_seen()
    known_companies = {k.rsplit(":", 1)[0] for k in seen}

    matched = []
    for j in all_jobs:
        via = job_matches(j, cfg["filters"], known_companies=known_companies)
        if via:
            j["match_via"] = via
            matched.append(j)
    matched_count = len(matched)
    matched, deduped = dedupe_postings(matched)

    new_jobs = [j for j in matched if j["key"] not in seen]

    client = make_client() if new_jobs else None
    drafts = draft_all(new_jobs, cfg, client)

    date_str = dt.date.today().isoformat()
    path = write_digest(new_jobs, matched_count, drafts, date_str, deduped)
    try:
        write_digest_json(new_jobs, drafts, date_str, path)
    except Exception as e:  # the sidecar is for the UI only -- never break a run over it
        print(f"  (warning: couldn't write JSON sidecar: {e})")

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
