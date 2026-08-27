/*
 * lilguy autofill loader -- fetched fresh from lilguy.win by a tiny,
 * hash-pinned bookmarklet stub (see autofill.html) and run on whatever
 * application page you clicked it on.
 *
 * WHY THIS EXISTS ALONGSIDE autofill/autofill.user.js: that version
 * needs Tampermonkey/Violentmonkey installed first, which is real
 * friction for someone who just wants to try this once.
 *
 * WHERE THE PROFILE LIVES: nowhere but a file on your own computer,
 * picked fresh every time you click this. Two earlier designs were
 * tried and rejected, live:
 *   1. Baking the profile into the bookmarklet's own URL -- works, but
 *      the URL becomes a plaintext-adjacent copy of your name/email/
 *      GPA/address that leaks in full if you ever paste or share that
 *      specific link.
 *   2. A hidden iframe reading a saved profile from lilguy.win's own
 *      localStorage -- defeated by browser storage partitioning
 *      (Chrome, and Safari more strictly, give a same-origin iframe a
 *      DIFFERENT storage bucket depending on which site embeds it, so
 *      the profile saved by visiting lilguy.win directly was invisible
 *      from inside the iframe on a real ATS page -- confirmed live, not
 *      theoretical). The Storage Access API doesn't reliably fix this
 *      either: in current Chrome it reports access as granted while
 *      still not actually unlocking localStorage in that bucket.
 * A plain <input type="file"> sidesteps both: nothing to leak in a URL,
 * nothing in any storage bucket for a browser to partition. The cost is
 * real and permanent -- you pick the file every time, browsers won't
 * remember it across page loads (a deliberate security choice on their
 * part, not something to work around).
 *
 * This file is fetched over the network on every use rather than baked
 * into the bookmarklet, so a fix or new-platform update reaches anyone
 * using it without them re-dragging anything -- the bookmarklet stub
 * hash-checks this file's contents before running it, so a version
 * mismatch (intentional update, or a tampered/compromised file) fails
 * loudly instead of silently running different code than you audited.
 *
 * SAFETY: identical invariant to the userscript version -- this fills
 * fields and stops. It never clicks Submit. Zero network calls of any
 * kind once this file itself has loaded -- your chosen profile.json is
 * read locally and never transmitted anywhere.
 */
(function () {
  "use strict";

  if (window.__lilguyAutofillActive) return;
  window.__lilguyAutofillActive = true;

  // --- field matching (ported from autofill/autofill.user.js -- keep
  // the two in sync by hand; they're separate delivery channels with
  // different runtime constraints, not meant to share a build step) ---

  const MATCHERS = [
    { key: "firstName", patterns: [/first\s*name/i] },
    { key: "lastName", patterns: [/last\s*name/i, /surname/i] },
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
    { key: "workAuthorized", patterns: [/authorized to work/i, /work authorization/i] },
    { key: "needsSponsorship", patterns: [/require.*sponsorship/i, /visa sponsorship/i] },
  ];

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
    if (el.name) parts.push(splitIdWords(el.name));
    if (el.id) parts.push(splitIdWords(el.id));
    const automationId = el.getAttribute("data-automation-id");
    if (automationId) parts.push(splitIdWords(automationId));
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

  function isLikelyHoneypot(el) {
    const rect = el.getBoundingClientRect();
    return rect.width < 2 || rect.height < 2;
  }

  function setNativeValue(el, value) {
    const proto = el.tagName === "TEXTAREA" ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, "value")?.set;
    if (setter) setter.call(el, value);
    else el.value = value;
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
  }

  const SPL_HOST_TAGS = ["SPL-INPUT", "SPL-TEXTAREA"];

  function deepQueryAll(root, selector) {
    const found = Array.from(root.querySelectorAll(selector));
    for (const el of root.querySelectorAll("*")) {
      if (el.shadowRoot) found.push(...deepQueryAll(el.shadowRoot, selector));
    }
    return found;
  }

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
    const opt = Array.from(el.options).find((o) => o.textContent.trim().toLowerCase() === String(value).toLowerCase());
    if (!opt) return false;
    el.value = opt.value;
    el.dispatchEvent(new Event("change", { bubbles: true }));
    markFilled(el);
    return true;
  }

  function fillPage(profile) {
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
        // Same call as the userscript: guessing which option in a
        // radio/checkbox group means "yes" is worse than leaving it.
      } else ok = fillTextLike(el, value);

      if (ok) filledCount++;
    });

    deepQueryAll(document, SPL_HOST_TAGS.join(", ")).forEach((el) => {
      if (el.disabled || isLikelyHoneypot(el)) return;
      const key = matchKey(el);
      if (!key || !(key in profile)) return;
      if (fillSplHost(el, profile[key])) filledCount++;
    });

    return filledCount;
  }

  // --- UI: a small card this script creates directly on whatever page
  // it's running on. No iframe, no cross-origin messaging, no network
  // call of any kind from here on -- the file you pick never leaves
  // this page's own memory. ---

  function buildCard() {
    const card = document.createElement("div");
    card.style.cssText =
      "position:fixed;bottom:16px;right:16px;width:290px;padding:16px;" +
      "background:#0d0d0c;color:#f0ede4;border-radius:10px;box-shadow:0 6px 24px rgba(0,0,0,.45);" +
      "z-index:2147483647;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;" +
      "font-size:13px;line-height:1.5;";
    card.innerHTML = `
      <div><strong style="color:#e8952a;">lilguy autofill</strong></div>
      <div style="margin:8px 0 6px;color:#a39d8c;">Choose your profile.json:</div>
      <input type="file" accept="application/json,.json" id="lilguy-file-input"
        style="width:100%;font-size:12px;color:#a39d8c;box-sizing:border-box;">
      <div id="lilguy-status" style="margin-top:8px;color:#8a8474;font-size:12px;"></div>
      <button id="lilguy-close-btn"
        style="margin-top:10px;background:transparent;border:1px solid #2b2823;color:#8a8474;border-radius:6px;padding:6px 12px;cursor:pointer;">
        Close
      </button>
    `;
    document.body.appendChild(card);
    return card;
  }

  const card = buildCard();
  const status = card.querySelector("#lilguy-status");

  card.querySelector("#lilguy-file-input").addEventListener("change", (event) => {
    const file = event.target.files && event.target.files[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => {
      let profile;
      try {
        profile = JSON.parse(reader.result);
      } catch (err) {
        status.textContent = "That wasn't a valid profile.json -- get one from lilguy.win/autofill.html.";
        return;
      }
      const count = fillPage(profile);
      status.textContent = `Filled ${count} field(s). Review before submitting.`;
      setTimeout(() => card.remove(), 3000);
    };
    reader.readAsText(file);
  });

  card.querySelector("#lilguy-close-btn").addEventListener("click", () => card.remove());
})();
