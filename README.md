# Operations / Logistics / Supply Chain Internship Prospecting

A general-purpose, reusable resource for finding and tracking internships in
**operations, logistics, and supply chain** at **large-to-mid sized industrial /
business companies**. Built for the junior-year recruiting cycle (rising junior →
senior). Designed to be forked or cloned by anyone targeting this internship
category — a student at Texas A&M or anywhere else — not tied to one
applicant's resume or background.

Two things beyond the static research:
- **A live opportunity feed** (`FEED.md` / `data/opportunities.json`) that
  scrapes real ATS job boards and updates itself daily via GitHub Actions —
  fork this repo, turn on Actions, get postings as they appear with zero
  server to run.
- **Application autofill** (`autofill/`) — a browser userscript that fills
  the repetitive fields on application forms from a stored profile, without
  ever auto-submitting for you.

## Layout

- `companies.md` — the full curated target list (~25 companies), grouped by
  category, with what's known about each program's size/structure/timing.
  This is the human research list — broader than what the scraper covers.
- `sources.yaml` — two tiers: broad **aggregators** (The Muse, optionally
  Adzuna) that cover many companies with zero per-company setup, plus a
  handful of **targeted** per-company connectors for higher precision on
  specific employers. See `docs/sourcing-model.md` for why it's split this
  way (and what existing open-source projects do instead), and
  `docs/adding-a-source.md` to add a targeted company.
- `scraper/` — the actual scraper (`scrape.py` + one connector per source
  type: `muse`, `adzuna`, `greenhouse`, `lever`, `workday`). Run it with
  `python scraper/scrape.py` (needs `pip install -r scraper/requirements.txt`
  first).
- `FEED.md` — auto-generated, human-readable current postings. Don't hand-edit.
- `data/opportunities.json` — the machine-readable version of the same feed,
  with `first_seen` dates so you can see what's new.
- `.github/workflows/scrape.yml` — runs the scraper daily and commits any
  change to the feed. Works automatically once you fork/clone this into your
  own GitHub repo and Actions is enabled — no server, no cost.
- `autofill/` — the application-autofill userscript, its profile schema, and
  the reasoning for why it fills but never submits. See `autofill/README.md`.
- `timeline.md` — the annual recruiting calendar for this internship category.
- `resources.md` — associations, search tactics, interview prep notes.
- `applications/tracker.md` — copy this template per applicant to track status.
- `prospects/` — one file per company once you're seriously targeting it —
  job description text, culture notes, who you know there, referral paths.
- `research/` — saved job postings, program details, anything pulled from the
  web worth keeping past the tab closing.

## How to use this

1. Skim `timeline.md` first — this category runs on a real calendar, and
   showing up at the wrong month is the most common way to miss a program.
2. Check `FEED.md` for what's live right now, and set up
   `.github/workflows/scrape.yml` (fork the repo, enable Actions) so it
   keeps updating without you having to remember to re-run it.
3. Work `companies.md` top-down by category that fits your interest
   (retail/distribution vs. CPG vs. industrial manufacturing vs. logistics
   carriers vs. conglomerate rotational programs) — they recruit on different
   schedules. Add any company you seriously want tracked to `sources.yaml`
   (`docs/adding-a-source.md`).
4. Set up `autofill/` once (`autofill/README.md`) so applications stop
   costing you the same 15 minutes of retyping your own name and school.
5. Copy `applications/tracker.md` and fill it in as applications go out.
6. Drop deeper notes on any company you're seriously pursuing into `prospects/`.
