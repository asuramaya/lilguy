// ==UserScript==
// @name         Internship Application Autofill
// @namespace    internships-repo
// @version      1.2.0
// @description  Fills known-repetitive fields on Greenhouse/Lever/Ashby/Rippling/SmartRecruiters/Workday application forms from a stored profile. Never submits — you always review before clicking Submit yourself.
// @match        https://job-boards.greenhouse.io/*
// @match        https://boards.greenhouse.io/*
// @match        https://jobs.lever.co/*
// @match        https://jobs.ashbyhq.com/*
// @match        https://ats.rippling.com/*
// @match        https://*.smartrecruiters.com/*
// @match        https://*.myworkdayjobs.com/*
// @grant        GM_setValue
// @grant        GM_getValue
// @grant        GM_registerMenuCommand
// @run-at       document-idle
// ==/UserScript==

/*
 * SCOPE: Greenhouse and Lever render plain HTML form elements matched by
 * label text. Workday support (added after live DOM inspection of a real
 * C.H. Robinson application form, 2026-08-15 — see autofill/README.md for
 * the two earlier blocked attempts) works differently: Workday's own
 * design system stamps every real form control with a stable, human-
 * readable `data-automation-id` (confirmed live: "email", "password",
 * "verifyPassword" on a real account-creation step) — reusing that
 * beats label-text matching, which the original docstring here wrongly
 * assumed was the only option for a "custom web components" platform. In
 * fact the confirmed fields were plain native <input> elements, same as
 * Greenhouse/Lever; only the identification strategy differs.
 *
 * WORKDAY HONEYPOT: the same inspected form had a bot-trap field —
 * `name="website"`, `data-automation-id="beecatcher"` — sized 1x0.01px
 * but NOT hidden via display/visibility (those both read normal/visible;
 * only the bounding-rect size gives it away). A naive fill-by-name-or-
 * label matcher would hit this directly, since "website" is already one
 * of this script's own MATCHERS patterns. isLikelyHoneypot() below
 * checks real rendered size, not CSS visibility properties, and is
 * applied on every platform, not just Workday — cheap insurance.
 *
 * SAFETY: this script fills fields and stops. It never clicks Submit, and
 * never uploads a resume without you having explicitly stored one first
 * via the menu command below. Read what got filled before you submit —
 * a filled field is a draft, not a verified answer.
 *
 * ASHBY / RIPPLING / SMARTRECRUITERS (added 2026-08-25, see README.md for
 * the full live-DOM findings): Ashby and Rippling turned out to already
 * work with plain label/placeholder matching once a real bug was fixed
 * (fullName's pattern was anchored against labelTextFor's full joined
 * signal, which can never match once anything else joins it — see
 * MATCHERS below). SmartRecruiters is architecturally different: its
 * fields are custom `<spl-*>` elements with the real `<input>` inside a
 * shadow root, invisible to a plain querySelectorAll — see
 * deepQueryAll()/fillSplHost() below.
 */

(function () {
  "use strict";

  const PROFILE_KEY = "internship-autofill-profile";
  const RESUME_KEY = "internship-autofill-resume"; // {name, type, base64}

  function getProfile() {
    const raw = GM_getValue(PROFILE_KEY, null);
    return raw ? JSON.parse(raw) : null;
  }

  GM_registerMenuCommand("Set autofill profile (paste JSON)", () => {
    const current = GM_getValue(PROFILE_KEY, "");
    const input = prompt(
      "Paste your profile JSON (see profile.example.json in the repo):",
      current
    );
    if (input === null) return;
    try {
      JSON.parse(input); // validate before storing
      GM_setValue(PROFILE_KEY, input);
      alert("Profile saved. Reload the application page to autofill.");
    } catch (e) {
      alert("That wasn't valid JSON — nothing was saved.\n\n" + e.message);
    }
  });

  GM_registerMenuCommand("Set stored resume (pick a file)", () => {
    const picker = document.createElement("input");
    picker.type = "file";
    picker.accept = ".pdf,.doc,.docx";
    picker.style.display = "none";
    document.body.appendChild(picker);
    picker.addEventListener("change", () => {
      const file = picker.files[0];
      if (!file) return;
      const reader = new FileReader();
      reader.onload = () => {
        const base64 = reader.result.split(",")[1];
        GM_setValue(
          RESUME_KEY,
          JSON.stringify({ name: file.name, type: file.type, base64 })
        );
        alert(`Stored "${file.name}" as your autofill resume.`);
        picker.remove();
      };
      reader.readAsDataURL(file);
    });
    picker.click();
  });

  // --- field matching -------------------------------------------------

  const MATCHERS = [
    { key: "firstName", patterns: [/first\s*name/i] },
    { key: "lastName", patterns: [/last\s*name/i, /surname/i] },
    // \bname\b, not /^name$/i -- labelTextFor always appends el.name/el.id
    // AFTER the label text into one joined signal string, so an anchored
    // pattern can never match once anything else is appended (which is
    // every field, on every platform). Confirmed live on Ashby: a bare
    // "Name" field's signal is "name type here... _systemfield_name
    // _systemfield_name", and /^name$/i tests the WHOLE string, not just
    // the label part. Checked after firstName/lastName in MATCHERS order
    // (matchKey returns the first match), so this can't steal a "First
    // Name" field that already matched its own, more specific pattern.
    { key: "fullName", patterns: [/full\s*name/i, /\bname\b/i] },
    { key: "email", patterns: [/e-?mail/i] },
    { key: "phone", patterns: [/phone/i, /mobile/i] },
    { key: "linkedin", patterns: [/linkedin/i] },
    { key: "website", patterns: [/website/i, /portfolio/i] },
    { key: "github", patterns: [/github/i] },
    { key: "school", patterns: [/school/i, /university/i, /college/i] },
    { key: "degree", patterns: [/degree/i] },
    { key: "major", patterns: [/major/i, /field of study/i, /discipline/i] },
    { key: "gpa", patterns: [/gpa/i] },
    { key: "addressLine1", patterns: [/address(?!.*(email|ip))/i] },
    { key: "city", patterns: [/city/i] },
    { key: "state", patterns: [/state|province/i] },
    { key: "zip", patterns: [/zip|postal/i] },
    { key: "country", patterns: [/country/i] },
    {
      key: "workAuthorized",
      patterns: [/authorized to work/i, /work authorization/i],
    },
    {
      key: "needsSponsorship",
      patterns: [/require.*sponsorship/i, /visa sponsorship/i],
    },
  ];

  // "firstName" / "legal-name-section_firstName" -> "first Name section first Name"
  // — Workday's data-automation-id values are readable camelCase/kebab-case
  // words, not opaque hashes (confirmed live, see file header). Splitting
  // them lets the SAME regex patterns below match Workday's own attribute
  // instead of needing a separate hardcoded Workday field-name list.
  function splitIdWords(id) {
    return id.replace(/[-_]/g, " ").replace(/([a-z])([A-Z])/g, "$1 $2");
  }

  function labelTextFor(el) {
    const parts = [];
    if (el.id) {
      const lbl = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (lbl) parts.push(lbl.textContent);
    }
    const wrappingLabel = el.closest("label");
    if (wrappingLabel) parts.push(wrappingLabel.textContent);
    if (el.getAttribute("aria-label")) parts.push(el.getAttribute("aria-label"));
    if (el.placeholder) parts.push(el.placeholder);
    // Split, not raw -- SmartRecruiters' own internal ids are exactly as
    // stable and readable as Workday's data-automation-id (confirmed
    // live: "first-name-input", "confirm-email-input", "linkedin-input"),
    // but a raw hyphenated id joins "first" and "name" with a "-", not
    // whitespace, so /first\s*name/i never matched it. Same fix applies
    // to el.name for platforms that use one attribute for the other.
    if (el.name) parts.push(splitIdWords(el.name));
    if (el.id) parts.push(splitIdWords(el.id));
    const automationId = el.getAttribute("data-automation-id");
    if (automationId) parts.push(splitIdWords(automationId));
    // Greenhouse/Lever often put the visible label in a preceding sibling
    // or a parent's earlier child rather than a real <label for>.
    const container = el.closest("div, li, fieldset");
    if (container) {
      const heading = container.querySelector("label, .application-label, legend");
      if (heading && heading !== el) parts.push(heading.textContent);
    }
    return parts.join(" ").toLowerCase();
  }

  function matchKey(el) {
    const signal = labelTextFor(el);
    for (const { key, patterns } of MATCHERS) {
      if (patterns.some((p) => p.test(signal))) return key;
    }
    return null;
  }

  // Bot-trap fields stay technically display:block/visibility:visible
  // (confirmed live on a real Workday form: name="website",
  // data-automation-id="beecatcher") specifically so a check against
  // those CSS properties alone misses them. Rendered size does not lie —
  // a real field a human is meant to fill is never ~1x0.01px.
  function isLikelyHoneypot(el) {
    const rect = el.getBoundingClientRect();
    return rect.width < 2 || rect.height < 2;
  }

  // React-controlled inputs (Greenhouse/Lever are both React apps) ignore
  // a plain `el.value = x` because React's own value tracker doesn't see
  // it — go through the native setter, then dispatch the events React
  // actually listens for.
  function setNativeValue(el, value) {
    const proto = el.tagName === "TEXTAREA" ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, "value")?.set;
    if (setter) setter.call(el, value);
    else el.value = value;
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
  }

  // SmartRecruiters' apply form (confirmed live on a real AECOM posting,
  // 2026-08-24) doesn't render plain <input> elements at the top level at
  // all -- every field is a custom element from its own "SPL" design
  // system (<spl-input>, <spl-textarea>, ...) with the REAL <input> buried
  // inside that element's shadow root. A plain
  // document.querySelectorAll("input, textarea, select") sees none of
  // them; it has to walk shadow roots explicitly.
  // <spl-select>, <spl-phone-field>, <spl-autocomplete> deliberately left
  // out -- they're dropdown-driven (click an option, not type a value),
  // a different interaction model than a text `.value` assignment covers.
  // Same call as the existing radio/checkbox skip below: don't guess.
  const SPL_HOST_TAGS = ["SPL-INPUT", "SPL-TEXTAREA"];

  function deepQueryAll(root, selector) {
    const found = Array.from(root.querySelectorAll(selector));
    for (const el of root.querySelectorAll("*")) {
      if (el.shadowRoot) found.push(...deepQueryAll(el.shadowRoot, selector));
    }
    return found;
  }

  // The host custom element (<spl-input id="first-name-input">) carries
  // the SAME id as the real <input> nested in its shadow root -- confirmed
  // live -- and exposes its own working `value` property setter that
  // updates both its internal shadow-DOM input AND the visible UI, so
  // there's no need to reach past it into the shadow root at all. It is
  // NOT an HTMLInputElement instance, though (confirmed live: `instanceof
  // HTMLInputElement` is false), so setNativeValue's native-setter trick
  // throws on it -- a plain assignment is both correct and necessary here.
  function fillSplHost(el, value) {
    if (!value) return false;
    el.value = value;
    el.dispatchEvent(new Event("input", { bubbles: true, composed: true }));
    el.dispatchEvent(new Event("change", { bubbles: true, composed: true }));
    markFilled(el);
    return true;
  }

  function markFilled(el) {
    el.style.outline = "2px solid #2e7d32";
    el.style.outlineOffset = "1px";
  }

  function fillTextLike(el, value) {
    if (!value) return false;
    setNativeValue(el, value);
    markFilled(el);
    return true;
  }

  function fillSelect(el, value) {
    if (!value) return false;
    const opt = Array.from(el.options).find(
      (o) => o.textContent.trim().toLowerCase() === String(value).toLowerCase()
    );
    if (!opt) return false;
    el.value = opt.value;
    el.dispatchEvent(new Event("change", { bubbles: true }));
    markFilled(el);
    return true;
  }

  function maybeFillResume() {
    const stored = GM_getValue(RESUME_KEY, null);
    if (!stored) return;
    const { name, type, base64 } = JSON.parse(stored);
    const fileInputs = document.querySelectorAll('input[type="file"]');
    fileInputs.forEach((input) => {
      const signal = labelTextFor(input);
      if (!/resume|cv/i.test(signal)) return;
      const bytes = atob(base64);
      const arr = new Uint8Array(bytes.length);
      for (let i = 0; i < bytes.length; i++) arr[i] = bytes.charCodeAt(i);
      const file = new File([arr], name, { type });
      const dt = new DataTransfer();
      dt.items.add(file);
      input.files = dt.files;
      input.dispatchEvent(new Event("change", { bubbles: true }));
      markFilled(input);
    });
  }

  function run() {
    const profile = getProfile();
    if (!profile) {
      console.log(
        "[internship-autofill] no profile stored yet — Tampermonkey menu -> 'Set autofill profile'"
      );
      return;
    }

    let filledCount = 0;
    document.querySelectorAll("input, textarea, select").forEach((el) => {
      if (el.type === "hidden" || el.type === "file" || el.disabled) return;
      if (isLikelyHoneypot(el)) return;
      const key = matchKey(el);
      if (!key || !(key in profile)) return;
      const value = profile[key];
      if (!value) return;

      let ok = false;
      if (el.tagName === "SELECT") ok = fillSelect(el, value);
      else if (el.type === "radio" || el.type === "checkbox") {
        // Radio/checkbox groups need the option's own label matched against
        // the value, not the group's label against the profile key — skip
        // for now rather than guess which option is "yes".
      } else ok = fillTextLike(el, value);

      if (ok) filledCount++;
    });

    // SmartRecruiters' SPL custom elements never showed up in the
    // querySelectorAll above -- they live inside shadow roots (see
    // deepQueryAll's own comment). deepQueryAll(document, ...) intentionally
    // starts a fresh, separate top-level walk rather than trying to fold
    // this into the loop above, since these hosts need their own honeypot/
    // fill logic (not an HTMLInputElement, no .type, its own write path).
    deepQueryAll(document, SPL_HOST_TAGS.join(", ")).forEach((el) => {
      if (el.disabled || isLikelyHoneypot(el)) return;
      const key = matchKey(el);
      if (!key || !(key in profile)) return;
      if (fillSplHost(el, profile[key])) filledCount++;
    });

    maybeFillResume();

    console.log(`[internship-autofill] filled ${filledCount} field(s). Review before submitting.`);
  }

  // Greenhouse/Lever forms often render after an initial React mount —
  // give it a moment, then run once more if the form grew.
  setTimeout(run, 800);
  setTimeout(run, 2500);
})();
