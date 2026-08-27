# Application Autofill

Fills the repetitive fields on Greenhouse, Lever, Ashby, Rippling,
SmartRecruiters, and Workday application forms: name, contact info,
school, major, GPA, address, work-authorization questions, all from a
profile you fill in once. It never clicks Submit. You review every
filled field and submit yourself.

Two ways to get it, same coverage:

- **No install: [lilguy.win/autofill.html](https://lilguy.win/autofill.html)**
  -- optionally import a PDF resume to pre-fill the form (parsed
  entirely client-side via pdf.js, never uploaded), review it, then
  download a `profile.json` and keep it on your own computer. Drag a
  bookmarklet to your bookmarks bar; clicking it on an application page
  opens a plain file picker, and choosing that file fills the page on
  the spot. No extension, no userscript manager, and nothing saved to
  any server, account, or even this browser -- two earlier designs
  (profile baked into the bookmarklet URL, profile in this site's own
  localStorage read via an iframe) were tried and rejected live, the
  first for leaking your data if the link is ever shared, the second
  because browser storage partitioning makes that iframe's storage a
  different bucket than the one you saved to directly. Picking the file
  fresh every time is deliberate, not a missing feature: it's the one
  approach that's neither a leak risk nor dependent on browser storage
  behavior that keeps changing out from under it. This is
  `service/static/autofill.html` + `service/static/autofill-loader.js`
  in this repo -- read those before trusting the deployed version, since
  trusting the code is exactly what this option asks of you.
- **This folder's userscript** (below) -- needs Tampermonkey/Violentmonkey
  first, but the profile is set once via a menu command (not re-picked
  per use) and it also auto-attaches a stored resume.

## Why autofill and not auto-apply

This project deliberately does not build a fully automated "click apply on
everything matching" tool. Three concrete reasons, not just caution for its
own sake:

1. **Most ATS platforms rate-limit or bot-detect automated submission**:
   an autofill that stops before submit doesn't trip these; a script that
   also clicks submit repeatedly across many companies looks like exactly
   what those systems exist to block, and can get an account/IP flagged.
2. **A submitted application is not reversible.** A wrong graduation year,
   a stale GPA, a field that matched the wrong question, all go out under
   your name to a real employer. A human glancing at the filled-in form
   before hitting submit is the actual safety check, not a nice-to-have.
3. Every real internship application asks at least one question specific
   to that role ("why this company", a short-answer prompt) that a stored
   profile can't answer honestly. Full auto-apply either skips those
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
5. Reload the application page. Filled fields get a green outline;
   scan for anything wrong before submitting.

Your profile and resume are stored in Tampermonkey's own storage (not this
repo, not localStorage on the page). They don't leave your browser and
aren't committed if you keep `profile.json` out of git (already covered by
`.gitignore`).

## Coverage

Works on Greenhouse, Lever, Ashby, Rippling, SmartRecruiters, and Workday.

**Ashby, Rippling, and SmartRecruiters (added 2026-08-25)** were previously
listed in the script's `@match` list with zero actual field-mapping logic
behind them -- a gap the earlier Workday/SmartRecruiters connector work
created and never closed. Checked live against real application forms
(Venti Technologies on Ashby, Rippling's own careers site, AECOM on
SmartRecruiters) rather than assumed:

- **Ashby and Rippling already worked** once one real bug was fixed: the
  `fullName` pattern (`/^name$/i`) was anchored against the FULL joined
  signal string (label text + placeholder + name + id, always appended by
  `labelTextFor`), so it could never match once anything else joined in --
  which is every field, on every platform. A bare "Name" field (Ashby) or
  a form using `placeholder` text instead of `<label for>` (Rippling)
  needed this fixed to `/\bname\b/i`. Separately, `el.id`/`el.name` are now
  split into words the same way `data-automation-id` already was, since a
  raw hyphenated id like `first-name-input` never matched `/first\s*name/i`
  as one token (a hyphen isn't whitespace).
- **SmartRecruiters needed real new logic.** Its apply form renders zero
  plain `<input>` elements at the top level -- every field is a custom
  element from its own "SPL" design system (`<spl-input>`, `<spl-textarea>`,
  ...) with the real `<input>` buried inside that element's shadow root, so
  a normal `document.querySelectorAll` sees nothing. Fixed with a
  `deepQueryAll()` that walks shadow roots explicitly, matching on the SPL
  host's own semantic, stable id (confirmed live: `first-name-input`,
  `last-name-input`, `email-input`, `confirm-email-input`,
  `linkedin-input`) and writing through the host's own working `.value`
  setter, which updates both the shadow-nested input and the visible UI --
  confirmed by screenshot, not just internal state. `<spl-select>`,
  `<spl-phone-field>`, and `<spl-autocomplete>` are deliberately NOT
  covered: they're dropdown-driven (click an option), a different
  interaction model than a text `.value` assignment, same reasoning as the
  existing radio/checkbox skip below.

**Workday support (added 2026-08-15) came after two earlier blocked
attempts.** The first had no browser access; the second hit a genuine
platform-wide Workday maintenance window (confirmed by trying four
distinct hosts our own sources use, all redirecting to
`community.workday.com/maintenance-page`). Third attempt reached a real,
live C.H. Robinson application form and inspected its actual DOM. Two
things confirmed live, not assumed:

1. **The original "Workday uses custom web components" assumption was
   wrong**, at least for the account-creation step inspected: the email/
   password fields are plain native `<input>` elements, same as
   Greenhouse/Lever. What differs is identification: Workday stamps every
   real form control with a stable, human-readable `data-automation-id`
   (confirmed: `"email"`, `"password"`, `"verifyPassword"`). The script
   now splits that attribute into words (`legalName_firstName` ->
   `"legal Name first Name"`) and runs it through the same MATCHERS regex
   patterns already used for label text, rather than needing a hardcoded
   list of every possible Workday field ID.
2. **A honeypot field exists:** `name="website"`,
   `data-automation-id="beecatcher"`, sized ~1x0.01px but with normal
   `display`/`visibility` CSS (deliberately, so a check against those
   properties alone misses it). This would have been filled directly by
   this script's own pre-existing `website` pattern. Fixed with
   `isLikelyHoneypot()`, which checks actual rendered bounding-rect size
   instead. It's applied on every platform, not just Workday, since a
   near-zero-size field is never something a human is meant to fill in
   regardless of which ATS renders it.

**Permanently blocked, not just deferred: the personal-info step past
account creation** (dropdowns for country/state/work authorization, which
Workday's design system likely renders as custom combobox widgets needing
click-to-open handling rather than a plain `<select>`). Confirmed live
(2026-08-16, this session, real browser access available) that there is
no guest/no-account path through Workday's own "Start Your Application"
flow. All three options (Autofill with Resume / Apply Manually / Use My
Last Application) route through account creation first, and creating an
account is explicitly prohibited for an automated agent regardless of
approval, no exceptions. This isn't an effort gap to revisit with more
browser access; it needs a **human** to create a real Workday account by
hand, reach the personal-info step, and either share what
`data-automation-id`/DOM structure they see there, or test the userscript
against it directly and report back what does/doesn't fill correctly. If
dropdown fills don't work once tested against a real form, that's the
next thing to fix, but the account-creation step itself is a wall this
project's tooling can't get past on its own.

Radio buttons and checkboxes (common for yes/no work-authorization
questions) are intentionally skipped rather than guessed at. Matching the
right *option* within a group needs comparing the option's own label text
against the profile value, which the current matcher doesn't attempt yet.
