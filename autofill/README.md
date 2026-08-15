# Application Autofill

A browser userscript that fills the repetitive fields on Greenhouse and
Lever application forms — name, contact info, school, major, GPA, address,
work-authorization questions — from a profile you fill in once. It never
clicks Submit. You review every filled field and submit yourself.

## Why autofill and not auto-apply

This project deliberately does not build a fully automated "click apply on
everything matching" tool. Three concrete reasons, not just caution for its
own sake:

1. **Most ATS platforms rate-limit or bot-detect automated submission** —
   an autofill that stops before submit doesn't trip these; a script that
   also clicks submit repeatedly across many companies looks like exactly
   what those systems exist to block, and can get an account/IP flagged.
2. **A submitted application is not reversible** — a wrong graduation year,
   a stale GPA, a field that matched the wrong question, all go out under
   your name to a real employer. A human glancing at the filled-in form
   before hitting submit is the actual safety check, not a nice-to-have.
3. Every real internship application asks at least one question specific
   to that role ("why this company", a short-answer prompt) that a stored
   profile can't answer honestly — full auto-apply either skips those
   (a visibly incomplete application) or fabricates an answer, neither of
   which serves the applicant.

## Install

1. Install a userscript manager: [Tampermonkey](https://www.tampermonkey.net/)
   (Chrome/Edge/Firefox) or Violentmonkey.
2. Open `autofill.user.js` from this folder, copy its contents into a new
   script in Tampermonkey (Dashboard → + → paste over the template → save).
3. On any Greenhouse (`job-boards.greenhouse.io`, `boards.greenhouse.io`) or
   Lever (`jobs.lever.co`) application page, click the Tampermonkey icon →
   **Set autofill profile (paste JSON)** and paste in your filled-out copy
   of `profile.example.json`.
4. Optional: **Set stored resume (pick a file)** to enable auto-attaching
   your resume to file-upload fields labeled "resume" or "CV".
5. Reload the application page. Filled fields get a green outline —
   scan for anything wrong before submitting.

Your profile and resume are stored in Tampermonkey's own storage (not this
repo, not localStorage on the page) — they don't leave your browser and
aren't committed if you keep `profile.json` out of git (already covered by
`.gitignore`).

## Coverage

Works on Greenhouse and Lever, both of which render plain HTML form
elements. **Does not cover Workday** — Workday builds its forms out of
custom web components rather than standard `<input>` elements, so the
label/attribute matching this script uses doesn't find them. Extending
coverage to Workday is a legitimate follow-up (inspect one real Workday
application form's DOM and write selectors against its actual structure,
the same way `docs/adding-a-source.md` describes for the scraper side).

Actively attempted twice now, not just deferred: this project has several
real Workday sources (ConocoPhillips, GE Vernova, GE Aerospace,
C.H. Robinson, Unilever — `sources.yaml`), so Workday coverage here would
close a real gap.

**Second attempt (2026-08-15), browser access available, still blocked —
this time by Workday itself, not by tooling.** Tried four live application
URLs across four distinct Workday hosts our own sources actually use
(`conocophillips.wd1`, `chrobinson.wd5`, `unilever.wd3`,
`gevernova.wd501`) — every single one redirected to
`community.workday.com/maintenance-page`, Workday's own platform-wide
maintenance page, not a per-tenant error. Retried `chrobinson.wd5` a
second time a few minutes later; same result. This reads as a genuine
Workday-side maintenance window affecting the whole `myworkdayjobs.com`
platform at the time of the attempt, not a broken URL or a guessed
tenant/host — the same tenant/host values our own `discovery.py` already
confirmed live (they're `active` sources with successful scrape history).
Real inspection of a live form's DOM still hasn't happened; the
`data-automation-id` claim from the first attempt remains unverified.
**Next session: just retry the same four URLs (or any `sources.yaml`
Workday entry) — this is very likely a transient outage, not a dead end.**

Radio buttons and checkboxes (common for yes/no work-authorization
questions) are intentionally skipped rather than guessed at — matching the
right *option* within a group needs comparing the option's own label text
against the profile value, which the current matcher doesn't attempt yet.
