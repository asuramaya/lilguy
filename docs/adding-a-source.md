# Adding a company to `sources.yaml`

Every company posts jobs through one ATS (applicant tracking system) or
another. Three have public, unauthenticated JSON APIs the scraper can call
directly: **Greenhouse**, **Lever**, and **Workday**. Almost everything
else that cares about SEO also publishes **schema.org/JobPosting**
structured data on individual job pages, findable via that company's own
sitemap — that's the `jsonld` connector (see `docs/sourcing-model.md`'s
Tier 1.5 section), and it's worth trying BEFORE assuming you need a new
vendor-specific connector class. Only fall back to "not wired up yet"
(Oracle Recruiting Cloud, SuccessFactors, iCIMS, Taleo, Workable, custom —
see the bottom of this doc) once both have failed.

Note this doc is about SOURCING — "does a posting exist here at all."
Whether it shows up in YOUR feed once it's in `data/all_postings.json` is
a separate, later question answered by `filters.yaml` (or your own copy of
it), not by anything here.

## 0. Try the automated check first

```
python scraper/discover.py --company "Example Corp" careers.example.com
```

Probes Greenhouse/Lever token guesses and checks for a job sitemap with
JobPosting markup (JSON-LD or microdata), and tells you plainly when it
finds nothing — which usually means step 1 below (a browser session) is
actually necessary, not that something's broken. It won't find Workday,
Oracle Recruiting Cloud, or anything else that's JS-rendered — those
still need the manual steps below.

## 1. Figure out which ATS a company uses

Open the company's careers page in a normal browser and look at the URL
once you land on a job listing or search page:

- **Greenhouse**: URL contains `boards.greenhouse.io/<token>` or the API
  calls (visible in DevTools → Network, filter "greenhouse") hit
  `boards-api.greenhouse.io/v1/boards/<token>/jobs`. `<token>` is what you need.
- **Lever**: URL contains `jobs.lever.co/<token>`.
- **Workday**: URL contains `myworkdayjobs.com` — the pattern is
  `https://<tenant>.<wd_host>.myworkdayjobs.com/<site>`. `<wd_host>` is a
  pod name like `wd1`, `wd5`, `wd12` — you cannot guess it, read it off the
  URL.
- **Anything else** (careers page loads job data via some other domain,
  e.g. `*.oraclecloud.com` for Oracle Recruiting Cloud, `*.successfactors.com`,
  `*.icims.com`, `*.taleo.net`): not supported yet — see below.

If the URL alone isn't conclusive (a lot of companies proxy through their
own custom domain), open DevTools → Network tab, filter for `jobs`, and
reload the page — the actual API request's domain gives it away. This is
exactly how this project confirmed, while building it, that **Honeywell
runs Oracle Recruiting Cloud, not Workday** — don't assume the ATS from a
company's size, industry, or what you'd expect a similar company to use.

## 2. Add the entry to `sources.yaml`

**Greenhouse:**
```yaml
- company: Example Corp
  ats: greenhouse
  token: examplecorp
  category: Industrial Manufacturing
```

**Lever:**
```yaml
- company: Example Corp
  ats: lever
  token: examplecorp
  category: Logistics & Transportation
```

**Workday:**
```yaml
- company: Example Corp
  ats: workday
  tenant: examplecorp
  wd_host: wd1
  site: External
  category: Industrial Manufacturing
```

**Generic JSON-LD** (any ATS, if the company publishes a job sitemap):
```yaml
- company: Example Corp
  ats: jsonld
  sitemap_url: https://careers.examplecorp.com/sitemap2.xml
  url_pattern: "/job/"
  category: Industrial Manufacturing
```
See `scraper/connectors/jsonld.py`'s own docstring for how to find
`sitemap_url` and `url_pattern` — the short version: check
`<company>/robots.txt` for a `Sitemap:` line (there may be several — find
the one listing individual job URLs, not just category/landing pages),
then confirm one of those job pages has
`<script type="application/ld+json">` with `"@type":"JobPosting"` in it.

If you want this company's postings to show up in a filter regardless of
whether its text uses that filter's own keywords (e.g. a freight broker's
"Internship 2026" never says "logistics"), that's a `trusted_companies`
entry in `filters.yaml` (or your own copy) — not a `sources.yaml` setting.
Sourcing and filtering are deliberately separate; see
`docs/sourcing-model.md`.

## 3. Verify it before committing

```
python scraper/scrape.py
```

Check the printed `fetched -> internship-shaped` line for your new
company. Zero `fetched` with no error means the token/tenant/sitemap is
wrong — the connectors are written to fail loudly (a clear exception)
rather than silently return nothing, so a genuinely broken config should
show up as an error in stderr, not a quiet zero. Then check whether it
shows up in `FEED.md` (or run `build_feed.py` with a filter file where you
expect it to match) — a company fetching fine but never appearing in any
filtered view usually means its postings' text doesn't match any
`keywords_any` term and it isn't in `trusted_companies` either.

## Unsupported ATS platforms

Oracle Recruiting Cloud, SuccessFactors, iCIMS, and Taleo power a lot of the
largest industrial/CPG employers and don't have a documented, stable public
JSON API the way Greenhouse/Lever/Workday do, AND (unlike many companies)
don't necessarily publish a job sitemap the `jsonld` connector could use
either — each is more involved to reverse-engineer from DevTools and more
likely to change shape without notice. Adding a connector for one is a
legitimate contribution (`scraper/connectors/`, follow the pattern in
`workday.py`) but wasn't built out here rather than ship something
unverified.
