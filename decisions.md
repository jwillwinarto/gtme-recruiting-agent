# Decisions Journal — GTME Recruiting Agent

## Day 1

**Project selection**
- Job-search signal agent (real feedback loop) over sponsorship prospecting (simulated one)

**Core matching**
- v1: title + location only — didn't trust my own filter criteria yet
- Sources: Greenhouse/Lever only — LinkedIn/Handshake scraping is fragile + against ToS

**Bugs**
- "Growth" alone false-matched unrelated roles — removed it

**Architecture**
- Confirmed this is a direct API call, not MCP

## Day 2

**Stage B — drafting**
- Claude drafts real outreach (LinkedIn-style, ~120 words)
- Draft failures retry next run instead of silently marking "seen"

**Coverage — 3 → 16 companies**
- Ashby support built after discovering Basis itself uses it, not Greenhouse/Lever
- Companies added across Greenhouse/Lever/Ashby, incl. Basis + Tracksuit
- Researched more platforms: added SmartRecruiters + Recruitee; ruled out Workday (no clean per-company slug, much more fragile)

**Bugs**
- "engineer" test keyword also false-matched — same root cause as Day 1
- Same-day reruns overwrote the digest, nearly lost a real draft — fixed to append per-run sections instead of overwriting

**Feedback loop**
- Built `feedback.py` — logs good/bad verdicts per match, idempotent on rerun
- Considered merging match-feedback and draft-feedback into one system — kept separate on purpose (different questions, can disagree)

**Matching — AND-logic**
- Built AND-logic keyword matching — fixes both false-positive bugs at the root, verified against real companies
- Attempted description-level matching too — reverted. Common words (Business/Operations/Systems) appear incidentally in any description, so the title-tuned rule doesn't generalize to prose. Needs a stricter design — noted in v2 ideas

**Platform expansion — SmartRecruiters + Recruitee**
- Built both fetchers, same pattern as Ashby. SmartRecruiters needed
  pagination + a different bad-slug behavior (200 + empty, not 404,
  unlike every other platform) — caught and handled explicitly
- Recruitee description left empty by design (SmartRecruiters' list
  endpoint has no description text; fetching it would cost one extra
  HTTP call per posting — not worth it before knowing if any real
  companies there matter)

**Bugs**
- targets.yaml got a bad indent (4 spaces instead of 2) while manually
  adding companies — crashed load_config() entirely. Caught before the
  next real run, fixed by hand

**Coverage — 16 → 30 companies**
- Added companies across all 5 platforms, incl. two "meta" finds
  (Greenhouse and Ashby themselves both hire for GTM Engineering roles
  on their own product)
- First full 30-company run: 7 new drafts, 6 good matches on first read
  — clear validation of AND-logic + broader coverage together

## Day 3

**Dedup near-identical postings**
- Built after finding Hypergen reposted "GTM Engineer" 3x — system drafted
  all 3 separately, 3x the Stage B cost for one real opportunity
- Exact match only (company + normalized title), no fuzzy matching —
  precise enough to catch the real case without risking false collapses
- Runs before the seen.json filter, not after — correct ordering matters:
  running it after would let losers resurface and redraft on later runs
- Keeps the newest-posted of each group; digest shows "N near-dupes
  merged" + an "Also posted" bullet so the drafts-vs-matches gap is
  never a mystery
- Verified live: 2 near-duplicates merged on a real run

**Recency filter**
- Added `max_age_days` (default 14) — only surface postings from the
  last N days. Directly motivated by the Saahil situation: a role can
  stay technically open while already being far along in process
- Found and fixed a real Greenhouse data bug in the process: 58% of
  Stripe's postings shared one fake bulk-edit date (`updated_at`)
  instead of their real post date — switched to `first_published`
- Missing/unparseable dates pass through rather than get excluded,
  consistent with how an empty locations list already behaves
- Two early runs both showed 4 matching postings — not enough data yet
  to know if 14 days is well-tuned or too tight

**Coverage — cleanup**
- Removed Whatnot: confirmed via live API test that Ashby's public
  Posting API 404s for them specifically — their hosted board works,
  but the REST API is opt-in and they haven't enabled it. Not fixable
  from this end; would need an undocumented internal GraphQL endpoint,
  not worth building for one non-essential company

## 2026-09-09

**GitHub**
- Pushed the project publicly (github.com/jwillwinarto/gtme-recruiting-agent)
  to have a shareable link ready for the Saahil call. Verified `.gitignore`
  correctly excluded `seen.json`, `feedback.md`, `.venv`, and the real API
  key — only 10 real project files went up

**Years-of-experience detection (display-only, then a real filter)**
- Motivated directly by a real miss: Basis "Marketing Operations Manager"
  looked like a strong match but required 4+ years, caught only by
  manually reading the description
- Built `detect_experience()` — recall-leaning regex for "N+ years",
  "N-M years", "at least N years", with guards against timeframes
  ("in under 2 years") and company-tenure mentions ("our company has
  10+ years..."). Deliberately over-catches at this stage rather than
  missing real requirements
- Shipped display-only first (an "Experience (detected)" digest line),
  not a filter — same "prove it before it gets power" discipline as
  every other filter built so far
- Accuracy sweep across 158 real Ashby postings: 111 flagged cleanly,
  zero false negatives on numeric phrasings
- Spot-checked all 47 unflagged postings by hand: confirmed genuinely
  no false negatives on numbers, but found a real, distinct gap —
  4 senior roles gated by qualitative language ("strong/deep/proven
  experience", "senior or staff scope") with no number at all. Logged
  as a known limitation, not something this build should try to fix
- Built Stage 2: `max_experience_years` filter, mirroring `max_age_days`
  in every respect (opt-in, undetected → pass through). Set to 3, based
  on being at zero real work experience in the field
- Live-verified against real data: dropped matching count from 7 to 3,
  and confirmed it would have blocked the exact real case that started
  the whole feature (Basis "Revenue Operations Analyst", 4-8 years) —
  a posting already drafted and logged `bad` in feedback.md earlier
  that same day

**Found, not fixed**
- A posting's listed title can differ from the title used inside its
  own description (Basis "Revenue Operations Analyst" internally called
  itself "Growth Engineer" throughout the body). Not a bug — Claude
  drafted correctly from the actual text — but worth a future decision
  on which title Stage B should prefer

## Still open
- Config-swapping (`targets.yaml` → parameter, not hardcoded) — paused
  in favor of improving the current product first
- Description-level matching v2 (needs proximity/narrower logic)
- Qualitative experience/seniority signal (senior/staff/principal scope,
  "strong/deep experience") as a second gate alongside the numeric one
- keywords_exclude bypass-on-description-match — now unblocked, since
  numeric experience detection exists; worth reconsidering
- Title mismatch handling (listed title vs. description title) for Stage B
- Contact discovery (who to actually send drafts to)
- Review analyzer for feedback.md — waiting on more real data
- Draft-quality feedback tooling — waiting on more real data
- AI-based fit-checking as a cheap second-pass filter (v3+)
- Broader company discovery, Excel output

## 2026-09-10

**Apollo.io contact enrichment**
- Motivated by every draft ending in "point me to the right person" —
  wanted a real name/title to address instead
- Researched 6 candidate MCP integrations (contact enrichment, web-search
  context, Excel, Slack, calendar, company-database verification);
  chose contact enrichment for highest fit + strongest technical story
- Signed up for Apollo with USC (.edu) email — confirmed institutional
  domains aren't blocked by the "personal email" restriction that
  affects Gmail/Yahoo/etc.; landed on personal-tier credits (limited,
  not corporate-tier)
- Important finding: MCP doesn't actually fit this task. Stage B makes
  one plain API call per draft with no agent tool-use loop — MCP
  servers need to be called BY a model inside such a loop. Apollo's
  official MCP server is also a local stdio process, not a hosted URL,
  so it likely wouldn't be reachable anyway. Building real MCP support
  would mean rewriting Stage B into a full agent loop — a much bigger,
  separate change, not a side effect of "add contact lookup"
- Decided: build with direct REST calls instead (same pattern as all 5
  existing fetchers), get real contact enrichment working now; treat a
  genuine MCP rebuild of Stage B as its own deliberate future project
- Built: apollo_find_contact() — free people search (title/seniority
  filtered) then one paid enrich call on the single best hit; any
  failure at any point returns None, never breaks a draft
- Safety design: max_lookups_per_run: 5, directly sized against a real
  precedent (a 24-posting backlog run happened once this project) so a
  repeat can't silently burn unlimited credits
- Discovered a real gap: Apollo can't search by company name, only by
  domain — targets.yaml needs an optional domain: field added per
  company, by hand, for this feature to work on that company
- Added digest transparency line ("Contact found: Name, Title (via
  Apollo)") — same audit-trail principle as "Matched via: description"
  and "Experience (detected)"
- Verified via mocked HTTP (no real Apollo calls yet): all failure
  paths correctly return None, no-contact path is byte-identical to
  prior drafting output, per-run cap correctly limits attempts
- Feature shipped disabled (apollo.enabled: false) — genuinely inert
  until a real smoke test resolves doc ambiguity about people/match's
  actual credit cost, before trusting any cost estimate for a real run
## Sheet sync for the SDR / GTM outreach track (Nov 14)

- Context: the outreach tracker (Google Sheet) needed an "is there an open
  role right now?" column per company, because an open role turns a coffee
  chat into a referral conversation; no open role means relationship-building
  until one opens
- First tried a Claude scheduled task that web-searches careers pages. Works
  for any page, but search results lag and it only gets through ~12 companies
  a day. The ATS JSON APIs this project already uses are exact and instant
- Decided: new sheet_sync.py instead of bending digest.py. digest.py answers
  "what's NEW since I last looked" (seen.json); the sheet needs "what's OPEN
  now". Different question, so a separate entry point that reuses the fetchers
- The company list lives in the sheet, not YAML, so adding a company in the
  sheet is enough. The track file (tracks/sdr_gtm.yaml) holds only filters and
  overrides; first step toward swappable tracks from V2-ideas
- Board discovery: try name-based slugs on Greenhouse, Ashby, Lever; accept a
  board only if it's verifiably that company (Greenhouse board name, or the
  company name appearing in a posting). Generic slugs like "modal" or
  "attention" can belong to someone else. The found board URL is written back
  to the sheet's Careers page column, so the sheet doubles as the cache
- Workday/Workable/custom pages: skipped on purpose and left for the Claude
  check, rather than adding scrapers
- Runs on GitHub Actions daily, no Claude API, Google auth via a service
  account stored as repo secrets
