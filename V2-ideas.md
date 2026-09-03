# v2 ideas (not building yet)

- Description-level matching (v2, redo): first attempt (title+desc
  combined, same AND-logic categories) produced ~224 false-positive
  matches ("Backend Developer", "Account Executive" both matched) —
  reverted. Root cause: domain/execution word lists were tuned for
  short, deliberate titles; common words like Business/Operations/
  Systems/Technical appear incidentally throughout almost any 500-word
  description, so the same AND rule doesn't safely generalize to prose.
  A real fix would need something stricter than "appears anywhere in
  the text" — e.g. requiring domain+execution words within N words of
  each other (proximity), a separate/narrower keyword set specifically
  for description matching, or checking only a specific section
  (responsibilities) rather than the whole description. Needs real
  design work, not a quick reapplication of the title rule.
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