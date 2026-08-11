// ==UserScript==
// @name         Internship Application Autofill
// @namespace    internships-repo
// @version      1.0.0
// @description  Fills known-repetitive fields on Greenhouse/Lever application forms from a stored profile. Never submits — you always review before clicking Submit yourself.
// @match        https://job-boards.greenhouse.io/*
// @match        https://boards.greenhouse.io/*
// @match        https://jobs.lever.co/*
// @grant        GM_setValue
// @grant        GM_getValue
// @grant        GM_registerMenuCommand
// @run-at       document-idle
// ==/UserScript==

/*
 * SCOPE: this fills plain HTML form fields reliably (Greenhouse, Lever —
 * both render standard <input>/<select>/<textarea> elements). It does NOT
 * target Workday, which builds its application forms out of custom web
 * components rather than plain form elements — a generic label/attribute
 * matcher like this one can't reliably find them. Extending this to
 * Workday means writing selectors against its specific component DOM,
 * which is a real but separate piece of work (see docs/adding-a-source.md
 * for the same "don't assume, go inspect it" approach applied to the
 * scraper side).
 *
 * SAFETY: this script fills fields and stops. It never clicks Submit, and
 * never uploads a resume without you having explicitly stored one first
 * via the menu command below. Read what got filled before you submit —
 * a filled field is a draft, not a verified answer.
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
    { key: "fullName", patterns: [/full\s*name/i, /^name$/i] },
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
    if (el.name) parts.push(el.name);
    if (el.id) parts.push(el.id);
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

    maybeFillResume();

    console.log(`[internship-autofill] filled ${filledCount} field(s). Review before submitting.`);
  }

  // Greenhouse/Lever forms often render after an initial React mount —
  // give it a moment, then run once more if the form grew.
  setTimeout(run, 800);
  setTimeout(run, 2500);
})();
