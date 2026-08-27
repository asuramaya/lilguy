/*
 * lilguy autofill loader -- fetched fresh from lilguy.win by a tiny,
 * hash-pinned bookmarklet stub (see autofill.html) and run on whatever
 * application page you clicked it on.
 *
 * WHY THIS EXISTS ALONGSIDE autofill/autofill.user.js: that version
 * needs Tampermonkey/Violentmonkey installed first, which is real
 * friction for someone who just wants to try this once. A bookmarklet
 * needs nothing installed -- but naively baking your profile into a
 * `javascript:` bookmark URL means your name/email/GPA/address sit in
 * plaintext-adjacent (base64 isn't encryption) form inside your
 * browser's bookmark store, which typically syncs to your browser
 * vendor's servers, and inside a string you might paste/share without
 * realizing what's in it. Neither of those is true of this design:
 *
 *   1. The bookmarklet itself carries ZERO personal data -- it's the
 *      same fixed stub for every single person, safe to share/tweet.
 *   2. Your actual profile lives in localStorage on lilguy.win's own
 *      origin (set once at lilguy.win/autofill.html), never in a URL,
 *      never sent to any server.
 *   3. Crossing the origin boundary (this page you're applying on ->
 *      lilguy.win, where the profile actually lives) happens over a
 *      postMessage handshake with an explicit target origin, and the
 *      profile is only ever released after a VISIBLE, IN-IFRAME CLICK
 *      -- see autofill.html's embedded mode. That last part specifically
 *      defeats a hostile site secretly iframing the same bridge page:
 *      a hidden/zero-size iframe can't receive a real click, so it can
 *      never get past the consent step to exfiltrate anything.
 *
 * This file is fetched over the network on every use rather than baked
 * into the bookmarklet, so a fix or new-platform update reaches anyone
 * using it without them re-dragging anything -- the bookmarklet stub
 * hash-checks this file's contents before running it, so a version
 * mismatch (intentional update, or a tampered/compromised file) fails
 * loudly instead of silently running different code than you audited.
 *
 * SAFETY: identical invariant to the userscript version -- this fills
 * fields and stops. It never clicks Submit. Zero network calls other
 * than the postMessage handshake with lilguy.win itself; nothing here
 * ever contacts any third party.
 */
(function () {
  "use strict";

  if (window.__lilguyAutofillActive) return;
  window.__lilguyAutofillActive = true;

  const BRIDGE_ORIGIN = "https://lilguy.win";
  // Extensionless: Cloudflare Pages 308-redirects "/autofill.html" to
  // "/autofill" by default, which just adds a hop -- going straight to
  // the canonical form here skips it.
  const BRIDGE_URL = BRIDGE_ORIGIN + "/autofill?embed=1";

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

    console.log(`[lilguy-autofill] filled ${filledCount} field(s). Review before submitting -- this never clicks Submit for you.`);
  }

  // --- cross-origin handshake -------------------------------------------

  const iframe = document.createElement("iframe");
  iframe.src = BRIDGE_URL;
  iframe.title = "lilguy autofill";
  iframe.style.cssText =
    "position:fixed;bottom:16px;right:16px;width:320px;height:180px;border:0;" +
    "border-radius:10px;box-shadow:0 6px 24px rgba(0,0,0,.45);z-index:2147483647;" +
    "background:#0d0d0c;color-scheme:dark;";
  document.body.appendChild(iframe);

  function cleanup() {
    window.removeEventListener("message", onMessage);
    iframe.remove();
  }

  function onMessage(event) {
    // Trust ONLY messages that came from the iframe we ourselves created,
    // pointed at lilguy.win -- nothing else this page does listens for or
    // acts on a "lilguy-autofill:*" message from anywhere else.
    if (event.origin !== BRIDGE_ORIGIN || event.source !== iframe.contentWindow) return;
    const data = event.data || {};
    if (data.type === "lilguy-autofill:profile") {
      fillPage(data.profile || {});
      cleanup();
    } else if (data.type === "lilguy-autofill:cancelled") {
      cleanup();
    }
    // "lilguy-autofill:no-profile" -- the bridge is already showing its
    // own "set up your profile first" message inside the iframe; nothing
    // for this page to do but leave it visible until the user closes it.
  }

  window.addEventListener("message", onMessage);
  iframe.addEventListener("load", () => {
    iframe.contentWindow.postMessage({ type: "lilguy-autofill:hello" }, BRIDGE_ORIGIN);
  });
})();
