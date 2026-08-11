# Adding a company to `sources.yaml`

Every company posts jobs through one ATS (applicant tracking system) or
another. Three have public, unauthenticated JSON APIs the scraper can call
directly: **Greenhouse**, **Lever**, and **Workday**. Everything else
(Oracle Recruiting Cloud, SuccessFactors, iCIMS, Taleo, Workable, custom)
isn't wired up yet — see the bottom of this doc.

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

Add `domain_native: true` if the company's entire business already is
operations/logistics/supply chain (a freight broker, a 3PL, a logistics
software platform) — see `filters.py` for why: their internship postings
often don't say "supply chain" or "logistics" in the title, so the keyword
filter would otherwise drop them.

## 3. Verify it before committing

```
python scraper/scrape.py
```

Check the printed `fetched -> relevant` line for your new company. Zero
`fetched` with no error means the token/tenant is wrong — the connectors
are written to fail loudly (a clear exception) rather than silently return
nothing, so a genuinely broken config should show up as an error in stderr,
not a quiet zero.

## Unsupported ATS platforms

Oracle Recruiting Cloud, SuccessFactors, iCIMS, and Taleo power a lot of the
largest industrial/CPG employers and don't have a documented, stable public
JSON API the way Greenhouse/Lever/Workday do — each is more involved to
reverse-engineer from DevTools and more likely to change shape without
notice. Adding a connector for one is a legitimate contribution
(`scraper/connectors/`, follow the pattern in `workday.py`) but wasn't
built out here rather than ship something unverified.
