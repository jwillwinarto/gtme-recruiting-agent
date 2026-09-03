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

## Still open
- Config-swapping (`targets.yaml` → parameter, not hardcoded) — next up,
  to support running a second track (e.g. finance) through the same system
- Description-level matching v2 (needs proximity/narrower logic)
- Contact discovery (who to actually send drafts to)
- Review analyzer for feedback.md — waiting on more real data
- Draft-quality feedback tooling — waiting on more real data
- AI-based fit-checking as a cheap second-pass filter (v3+)
- Broader company discovery, Excel output