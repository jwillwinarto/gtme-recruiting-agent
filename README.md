# Job digest

A script you run by hand. It reads `targets.yaml`, pulls current job postings from
Greenhouse and Lever's public JSON APIs, keeps the ones whose **title and location**
match your filters, drops anything it has shown you before, and writes the rest to
`digests/digest-YYYY-MM-DD.md` with a drafted outreach message for each.

## One-time setup

1. **Install Python 3.12** -- download the macOS installer from
   <https://www.python.org/downloads/> and run it. Check it worked:

   ```bash
   python3.12 --version
   ```

   (The system Python 3.9 that ships with macOS is too old for the Anthropic SDK
   used in Stage B.)

2. **Create a virtual environment** in this folder and install the packages:

   ```bash
   cd "/Users/willsstuff/Documents/GTME Recruiting Agent"
   python3.12 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

3. **Fill in `targets.yaml`** -- replace the placeholder bio, filters, and companies
   with your own. The file has comments explaining each field. To find a company's
   `slug`, look at its careers-page URL:
   - Greenhouse: `boards.greenhouse.io/<slug>` (or `job-boards.greenhouse.io/<slug>`)
   - Lever: `jobs.lever.co/<slug>`
   - Ashby: `jobs.ashbyhq.com/<slug>`
   - SmartRecruiters: `jobs.smartrecruiters.com/<slug>` (case-sensitive)
   - Recruitee: `<slug>.recruitee.com`

4. **Claude API key (Stage B only).** Drafting real messages needs an Anthropic API
   key, which is **separate from your Claude Code login** and billed on its own
   (pennies at this volume, but the account needs a payment method or credits):
   1. <https://console.anthropic.com> -> sign in
   2. **Billing** -> add a payment method or buy a few dollars of credit
   3. **API keys** -> create one, copy it (shown only once)
   4. In your terminal:

      ```bash
      export ANTHROPIC_API_KEY="sk-ant-..."
      ```

      Add that line to `~/.zshrc` so it survives new terminal windows.

5. **Apollo API key (optional -- real contact lookup).** If you set `apollo.enabled: true`
   in `targets.yaml`, drafts can address a real person by name instead of "the right
   person on your team." This costs Apollo credits and is **off by default**.
   1. Get your key from your Apollo.io account (Settings -> API), then in your terminal:

      ```bash
      export APOLLO_API_KEY="..."
      ```

      Add that line to `~/.zshrc` too.
   2. In `targets.yaml`, add a `domain:` (the company's own domain, e.g. `cresta.com`) to
      each company you want this for -- Apollo can only search by domain, not company
      name, so a company with no `domain:` is skipped for contact lookup (drafts stay
      generic for it, exactly like before).
   3. `apollo.max_lookups_per_run` (default 5) caps how many postings get a lookup
      attempt per run -- the rest just draft generically. Raise it once you've confirmed
      the real per-call credit cost against your Apollo dashboard (see the plan/PR notes
      for why this is worth checking before you rely on an estimate).

## Running it

```bash
source .venv/bin/activate          # once per terminal session
python3 digest.py
```

It prints a one-line summary and appends a section to `digests/digest-<today>.md` -- one
labeled `**Run N**` section per run that day, so re-running doesn't lose earlier matches.
Open that file, edit the drafts, and send the ones worth sending.

## How "new" works

`seen.json` (created on first run, git-ignored) is the list of postings already shown.
Delete it to make everything show up as new again; hand-edit it to un-hide a specific
posting.

## Match feedback (Level 1)

A running log of your own judgment on whether each surfaced posting was a good or bad
match, kept in `feedback.md` (git-ignored). Nothing reads it automatically -- it's
there for you to look back on and spot patterns.

After a digest run:

```bash
python3 feedback.py add        # newest digest; or: feedback.py add 2026-09-01
```

That appends one stub row per posting to `feedback.md`, skipping any already logged for
that date (safe to run more than once). Then open `feedback.md` and, for each posting,
change `verdict: ?` to `good` or `bad` and optionally add a reason:

```
- Cresta | GTM Engineer
  verdict: good  reason: exactly the role I want, remote
```

To review later, just read the file, or e.g. `grep 'verdict: bad' feedback.md`.
This is separate from `digest.py` -- it only reads the files in `digests/`.

## Scope

This version does exactly one thing. No scheduler, no database, no web UI, no reply
tracking, no learning from your edits, and no signal sources beyond Greenhouse and
Lever. Those come later, if at all.
