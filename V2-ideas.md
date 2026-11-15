# v2 ideas (not building yet)

- Description-level matching (v3, still unsolved): two real attempts now,
  both reverted.
  Attempt 1 (co-occurrence anywhere in text): ~224 false positives
  ("Backend Developer", "Account Executive") — common words like
  Business/Operations/Systems appear incidentally throughout any prose.
  Attempt 2 (proximity — domain + execution word within N words of each
  other): better, but still too noisy on real data. Root cause is more
  precise now: genuine, common phrases like "sales operations" (staffing
  context) or "collaborate with product engineering and marketing teams"
  sit at distance 1-2, structurally identical to a real case like
  "marketing operations platform" — proximity alone can't distinguish an
  incidental adjacent phrase from an actual role description. Tightening
  the window (5→3) barely helped, since these false positives are already
  at distance 1-2, below any reasonable window.
  Real, unfixed limitation: also confirmed the original motivating case
  (Grafana "Staff AI Engineer") would never surface anyway, since it's
  blocked earlier by the "Staff" seniority exclude — description matching
  can only ever help NON-excluded titles, a narrower win than originally
  imagined.
  Untried next idea: a collaboration-word guard (skip a match if
  "collaborate/partner/teams/stakeholders" sits within ~3 words of the
  pair) — not tested yet, would directly target the exact false positives
  seen tonight ("collaborate with... engineering and marketing teams").
  Worth a fresh, careful attempt with this guard before trying proximity
  again from scratch.
- Draft-quality feedback tooling (mirrors feedback.py): once several
  drafts across different companies exist in saved-drafts.md, consider
  a script to seed/track draft-quality verdicts the same way
  feedback.py does for matches. Two early data points already show a
  recurring pattern (hedged closing paragraph, unexplained 88rising
  reference) — worth confirming it holds across more companies before
  building tooling around it.
- Revisit keywords_exclude bypass-on-description-match (currently kept
  title-only/absolute) once years-of-experience/seniority detection
  from job descriptions (Problem B) is actually built — at that point,
  seniority filtering can move from blunt title-word matching to real
  description-based detection, and a title like "Senior Director" with
  a strong description could be evaluated properly instead of being
  auto-blocked.
- Build feedback.py review analyzer (good/bad counts, grouped by company,
  word-frequency over bad-match titles/reasons) once feedback.md has
  enough real entries to show an actual pattern — don't build against
  no data.
- Title mismatch between listing and description: found a real case
  (Basis "Revenue Operations Analyst" on Ashby) where the job's listed
  title differs from the title used repeatedly inside its own
  description ("Growth Engineer"). Not a bug in our system - Claude
  correctly drafted based on the description's actual wording. Worth
  considering: have Stage B prefer the platform-listed title over
  anything found in description text, or flag when they diverge, so
  drafts don't reference a title the reader won't recognize from the
  actual posting.
- Stage 2 experience filter should include a qualitative signal, not just
  numeric: spot-check of 47 unflagged postings found 4 senior roles
  gated by phrasing like "strong/deep/proven experience" or "senior or
  staff scope" with no number - detect_experience() correctly returns []
  for these since it's scoped to numeric patterns, but it means "none
  found" shouldn't be read as "junior-friendly." Some overlap already
  caught by the title-based seniority exclude (e.g. "Sr." in title).
- Real MCP rebuild of Stage B (v3, bigger): to  genuinely use MCP (not
  just a direct REST call), Stage B's drafting would need to become a
  real agent tool-use loop, since MCP servers are called BY a model
  inside such a loop, not from a one-shot API call. This is a much
  bigger, deliberate rewrite - worth doing as its own project step if
  I want genuine hands-on MCP experience, rather than forcing it into
  a task (like contact lookup) that doesn't structurally need it.
  Apollo (or another tool) could become the first real tool such a
  loop uses, once it exists.

V3+
- Excel integration (MCP): instead of a markdown digest, have Claude connect
  directly to an Excel sheet and add a row for each new matching posting/lead
  — so results accumulate in one running spreadsheet instead of separate
  daily files.
- Broader company discovery (v3+): instead of hand-picking companies in
  targets.yaml, plug into an existing index/database of companies using
  Greenhouse/Lever (e.g. a paid job-board aggregation API) so keyword
  matching runs across many companies automatically, not just the ones
  I manually added.
- Contact discovery: find a real person to send the drafted outreach
  to (name + LinkedIn/email) at each matching company, instead of
  just producing a draft with no recipient. Likely needs a paid
  enrichment tool (e.g. something like Clay) rather than scraping,
  for the same reasons LinkedIn/Handshake automation was ruled out
  earlier.
- AI-based fit-checking (v3+): instead of relying only on keyword
  matching, send postings that FAIL the keyword filter (or fall just
  short) to Claude for a cheap "does this look like real GTM/RevOps
  work, yes/no + why" second-pass check. NOT a replacement for keyword
  filtering — keyword matching stays the primary filter because it's
  fast, cheap, and every decision is explainable/debuggable (I can
  point to exactly why something matched or didn't). AI judgment is
  slower, costs real money at scale (~2,500 postings/run), and is
  harder to defend ("the model thought so" vs. "Growth + Engineering
  both present"). Worth it only as a periodic spot-check on what
  keyword matching is missing, once description-level matching has
  been tested and still shows real gaps.
- Workday support (v3+, harder): real JSON API exists (POST to
  /wday/cxs/{tenant}/{site}/jobs) but requires 3 unguessable identifiers
  per company (tenant, datacenter shard number like wd5/wd12, site ID) —
  no single slug like Greenhouse/Lever/Ashby. Also sits behind Cloudflare,
  may need browser-fingerprint mimicry to avoid being blocked. Breaks the
  clean "add a slug to targets.yaml" pattern — each company needs manual
  discovery. Worth it only if a specific must-have company is Workday-only.