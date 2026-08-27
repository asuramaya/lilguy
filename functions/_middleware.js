// Rewrites share-link previews (title, description, OG/Twitter tags,
// image, canonical) for `/?posting=<id>`, `/?company=<key>`, and
// `/?category=<name>` so a link pasted into Slack/Discord/iMessage/
// Twitter shows the actual role, employer, or category instead of the
// generic homepage card -- this is a client-rendered SPA, so link-
// preview crawlers (which don't run JS) would otherwise see the same
// static shell for every URL.
//
// Reads only the pre-built static bundle (og_lookup.json / companies.json
// / meta.json, see service/edge_export.py) via env.ASSETS -- no database,
// no per-visitor state, nothing collected. Fails open: any lookup miss or
// error just serves the normal page untouched.
export async function onRequest(context) {
  const { request, next, env } = context;
  const url = new URL(request.url);

  // autofill.html is deliberately embedded in an iframe from arbitrary
  // employer ATS domains (see autofill-loader.js) -- the sitewide
  // `X-Frame-Options: DENY` in _headers would block that, and _headers
  // has no way to unset a header a broader rule already set (only to
  // add more), so it's stripped here instead, at the one path that
  // needs it gone. Safety for that path comes from the in-iframe
  // visible consent click, not from restricting who can frame it --
  // the set of ATS domains is open-ended by design.
  //
  // Matches BOTH "/autofill.html" and "/autofill": Cloudflare Pages
  // 308-redirects the former to the latter (its default clean-URL
  // canonicalization) before this ever reaches a browser, so checking
  // only the .html form left the header un-stripped on the page that
  // actually loads -- confirmed live, the iframe rendered blank until
  // both forms were covered here.
  if (url.pathname === "/autofill.html" || url.pathname === "/autofill") {
    const response = await next();
    const headers = new Headers(response.headers);
    headers.delete("X-Frame-Options");
    headers.set("Content-Security-Policy", "frame-ancestors *");
    return new Response(response.body, { status: response.status, statusText: response.statusText, headers });
  }

  const postingId = url.searchParams.get("posting");
  const companyKey = url.searchParams.get("company");
  const category = url.searchParams.get("category");
  if (url.pathname !== "/" || (!postingId && !companyKey && !category)) {
    return next();
  }

  const response = await next();
  const contentType = response.headers.get("content-type") || "";
  if (!contentType.includes("text/html")) return response;

  let card = null;
  try {
    if (postingId) {
      card = await lookupPosting(env, url, postingId);
    } else if (companyKey) {
      card = await lookupCompany(env, url, companyKey);
    } else if (category) {
      card = await lookupCategory(env, url, category);
    }
  } catch (err) {
    return response;
  }
  if (!card) return response;

  const { title, description, canonicalUrl, imageUrl } = card;

  return new HTMLRewriter()
    .on("title", { element(el) { el.setInnerContent(title); } })
    .on('meta[name="description"]', { element(el) { el.setAttribute("content", description); } })
    .on('meta[property="og:title"]', { element(el) { el.setAttribute("content", title); } })
    .on('meta[property="og:description"]', { element(el) { el.setAttribute("content", description); } })
    .on('meta[property="og:url"]', { element(el) { el.setAttribute("content", canonicalUrl); } })
    .on('meta[property="og:image"]', { element(el) { el.setAttribute("content", imageUrl); } })
    .on('meta[property="og:image:alt"]', { element(el) { el.setAttribute("content", title); } })
    .on('meta[name="twitter:title"]', { element(el) { el.setAttribute("content", title); } })
    .on('meta[name="twitter:description"]', { element(el) { el.setAttribute("content", description); } })
    .on('meta[name="twitter:url"]', { element(el) { el.setAttribute("content", canonicalUrl); } })
    .on('meta[name="twitter:image"]', { element(el) { el.setAttribute("content", imageUrl); } })
    .on('meta[name="twitter:image:alt"]', { element(el) { el.setAttribute("content", title); } })
    .on('link[rel="canonical"]', { element(el) { el.setAttribute("href", canonicalUrl); } })
    .transform(response);
}

async function lookupPosting(env, url, postingId) {
  const res = await env.ASSETS.fetch(new URL("/data/og_lookup.json", url));
  if (!res.ok) return null;
  const lookup = await res.json();
  const p = lookup[postingId];
  if (!p) return null;

  const bits = [p.company, p.location, p.category && p.category !== "Uncategorized" ? p.category : null].filter(Boolean);
  const qs = `posting=${encodeURIComponent(postingId)}`;
  return {
    title: `${p.title} at ${p.company} · lilguy.win`,
    description: `${bits.join(" · ")} -- see it and thousands of other open internships, tracked live on lilguy.win.`,
    canonicalUrl: `https://lilguy.win/?${qs}`,
    imageUrl: `https://lilguy.win/og-card.svg?${qs}`,
  };
}

async function lookupCompany(env, url, companyKey) {
  const res = await env.ASSETS.fetch(new URL("/data/companies.json", url));
  if (!res.ok) return null;
  const companies = await res.json();
  const c = companies.find((row) => row.key === companyKey);
  if (!c) return null;

  const n = c.postings_count;
  const cats = (c.categories || []).slice(0, 2).join(", ");
  const qs = `company=${encodeURIComponent(companyKey)}`;
  return {
    title: `${c.name} internships (${n} open) · lilguy.win`,
    description: `${n} open internship${n === 1 ? "" : "s"} at ${c.name}${cats ? " in " + cats : ""}, tracked live on lilguy.win.`,
    canonicalUrl: `https://lilguy.win/?${qs}`,
    imageUrl: `https://lilguy.win/og-card.svg?${qs}`,
  };
}

async function lookupCategory(env, url, category) {
  const res = await env.ASSETS.fetch(new URL("/data/meta.json", url));
  if (!res.ok) return null;
  const meta = await res.json();
  const n = meta.categories ? meta.categories[category] : undefined;
  if (n === undefined) return null;

  const qs = `category=${encodeURIComponent(category)}`;
  return {
    title: `${category} internships (${n} open) · lilguy.win`,
    description: `${n} open internship${n === 1 ? "" : "s"} in ${category}, tracked live on lilguy.win.`,
    canonicalUrl: `https://lilguy.win/?${qs}`,
    imageUrl: `https://lilguy.win/og-card.svg?${qs}`,
  };
}
