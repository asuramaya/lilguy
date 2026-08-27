// Filtered Atom feeds -- e.g. /feed.atom?category=Software+%26+Technology
// or /feed.atom?company=openai&job_function=Data+Science. This is the
// "alert without an account" mechanism: subscribe the URL in any feed
// reader (or an RSS-to-Discord/Slack/IFTTT bot) and it polls on its own
// schedule. No email, no signup, nothing stored here about who
// subscribed to what -- the filter lives entirely in the URL the
// subscriber already holds.
//
// With no query params this falls through to the prebuilt static
// /feed.atom (top 50, unfiltered) so existing subscribers see no change.
const FEED_ID_PREFIX = "tag:internship-feed,2026:";

export async function onRequestGet(context) {
  const { request, env } = context;
  const url = new URL(request.url);

  if ([...url.searchParams.keys()].length === 0) {
    return env.ASSETS.fetch(request);
  }

  const feedRes = await env.ASSETS.fetch(new URL("/data/feed.json", url));
  if (!feedRes.ok) return env.ASSETS.fetch(request);
  const postings = await feedRes.json();

  const company = url.searchParams.get("company");
  const category = url.searchParams.get("category");
  const jobFunction = url.searchParams.get("job_function");
  const workArrangement = url.searchParams.get("work_arrangement");
  const cycleSeason = url.searchParams.get("cycle_season");
  const cycleYear = url.searchParams.get("cycle_year");
  const q = (url.searchParams.get("q") || "").toLowerCase();
  const maxAgeDays = parseInt(url.searchParams.get("max_age_days") || "", 10);
  const limit = Math.min(parseInt(url.searchParams.get("limit") || "100", 10) || 100, 500);

  const cutoff = Number.isFinite(maxAgeDays) ? Date.now() - maxAgeDays * 86400000 : null;

  const filtered = postings
    .filter((p) => {
      if (company && p.company_key !== company) return false;
      if (category && p.category !== category) return false;
      if (jobFunction && p.job_function !== jobFunction) return false;
      if (workArrangement && p.work_arrangement !== workArrangement) return false;
      if (cycleSeason && p.cycle_season !== cycleSeason) return false;
      if (cycleYear && String(p.cycle_year) !== cycleYear) return false;
      if (q && !`${p.title} ${p.company}`.toLowerCase().includes(q)) return false;
      if (cutoff !== null) {
        const t = Date.parse(p.posted_at_ts || p.first_seen || "");
        if (!Number.isFinite(t) || t < cutoff) return false;
      }
      return true;
    })
    .slice(0, limit);

  const bits = [company, category, jobFunction, workArrangement, cycleSeason, cycleYear, q && `matching '${q}'`].filter(Boolean);
  const title = "lilguy · Internships" + (bits.length ? ` (${bits.join(", ")})` : "");
  const xml = renderAtom(filtered, { title, selfUrl: url.toString(), feedSlug: "filtered" + url.search });

  return new Response(xml, {
    headers: {
      "content-type": "application/atom+xml; charset=utf-8",
      "cache-control": "public, max-age=300, s-maxage=300",
    },
  });
}

function rfc3339(value) {
  const d = value ? new Date(value) : new Date();
  return Number.isNaN(d.getTime()) ? new Date().toISOString().replace(/\.\d+Z$/, "Z") : d.toISOString().replace(/\.\d+Z$/, "Z");
}

function escapeXml(s) {
  return String(s ?? "").replace(/[<>&"']/g, (c) => ({ "<": "&lt;", ">": "&gt;", "&": "&amp;", '"': "&quot;", "'": "&apos;" }[c]));
}

function entry(p) {
  const title = p.title || "Untitled posting";
  const company = p.company || "";
  const location = p.location || "";
  const category = p.category || "";
  const url = p.url || "";
  const updated = p.posted_at_ts || p.first_seen;

  let approxNote = "";
  if (p.posted_at_ts && p.posted_at_approx) {
    approxNote = " (date approximate — the source gave a bound, not a date: this posting is AT LEAST this old and may be considerably older)";
  }

  const summary = [company, location, category].filter(Boolean).join(" · ") + approxNote;
  const entryId = FEED_ID_PREFIX + "posting/" + (p.id || url);

  return `  <entry>
    <title>${escapeXml(title)}</title>
    <id>${escapeXml(entryId)}</id>
    <link rel="alternate" href="${escapeXml(url)}"/>
    <updated>${rfc3339(updated)}</updated>
    <author><name>${escapeXml(company || "Unknown company")}</name></author>
    <summary>${escapeXml(summary)}</summary>
  </entry>`;
}

function renderAtom(postings, { title, selfUrl, feedSlug }) {
  const newest = postings.reduce((acc, p) => {
    const t = p.posted_at_ts || p.first_seen;
    return t && (!acc || new Date(t) > new Date(acc)) ? t : acc;
  }, null);

  const entries = postings.map(entry).join("\n");
  return `<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>${escapeXml(title)}</title>
  <id>${escapeXml(FEED_ID_PREFIX + feedSlug)}</id>
  <updated>${rfc3339(newest)}</updated>
  <link rel="self" href="${escapeXml(selfUrl)}"/>
  <subtitle>Internship postings sourced directly from company career sites.</subtitle>
${entries}
</feed>
`;
}
