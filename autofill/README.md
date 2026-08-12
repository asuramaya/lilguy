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

Actively attempted, not just deferred: this project now has a real
Workday source (Unilever, `sources.yaml`), so Workday coverage here would
close a real gap. Blocked on tooling, not effort — building it requires
inspecting a live Workday application form's actual DOM in a browser
session, which wasn't available when this was attempted. General
knowledge suggests Workday forms consistently expose `data-automation-id`
attributes (a stable pattern used across Workday-hosted sites, referenced
in various public write-ups on scripting Workday applications) as a more
reliable selector base than label-text matching — but that's an
unverified claim from training knowledge, not something confirmed against
a real page, and this project's own standard (see `CONTRIBUTING.md`) is
that a claim like that doesn't ship until it's actually been checked.
Next session with browser access: open a real Workday application page,
confirm or refute the `data-automation-id` pattern, then extend
`autofill.user.js` with a Workday-specific field matcher.

Radio buttons and checkboxes (common for yes/no work-authorization
questions) are intentionally skipped rather than guessed at — matching the
right *option* within a group needs comparing the option's own label text
against the profile value, which the current matcher doesn't attempt yet.
