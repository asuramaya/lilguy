// Contextual share-image cards: /og-card.svg?posting=<id>,
// ?company=<key>, or ?category=<name> -- rendered as SVG at the edge
// from the same static data the site itself reads (og_lookup.json,
// companies.json, meta.json), no DB call. Falls back to the generic
// branded card (same composition as og-image.png) when nothing
// matches, so a bad/old link never renders a broken image.
//
// SVG rather than a rasterized PNG: a pixel-perfect universal renderer
// (satori+resvg or a headless browser) means a whole new build
// toolchain in an otherwise dependency-light Python project (see
// CONTRIBUTING.md). Slack, Discord, and iMessage all render an SVG
// og:image fine. Twitter/X's card validator is pickier about non-
// raster og:image and may fall back to no image there -- a known,
// deliberate tradeoff, not an oversight.
const CANVAS = "#0d0d0c";
const INK = "#f0ede4";
const INK_SECONDARY = "#a39d8c";
const INK_TERTIARY = "#8a8474";
const ACCENT = "#e8952a";
const FONT = "-apple-system,BlinkMacSystemFont,Segoe UI,Helvetica,Arial,sans-serif";
const BOLT_PATH = "M13 2L3 14h9l-1 8 10-12h-9l1-8z";

export async function onRequestGet(context) {
  const { request, env } = context;
  const url = new URL(request.url);

  const postingId = url.searchParams.get("posting");
  const companyKey = url.searchParams.get("company");
  const category = url.searchParams.get("category");

  let card = null;
  try {
    if (postingId) card = await postingCard(env, url, postingId);
    else if (companyKey) card = await companyCard(env, url, companyKey);
    else if (category) card = await categoryCard(env, url, category);
  } catch (err) {
    card = null;
  }

  const svg = renderCard(card || genericCard());
  return new Response(svg, {
    headers: {
      "content-type": "image/svg+xml; charset=utf-8",
      "cache-control": "public, max-age=1800, s-maxage=1800",
    },
  });
}

function genericCard() {
  return {
    title: "Every open internship, in one fast feed.",
    subtitle: "Search thousands of verified internship roles across hundreds of employers.",
  };
}

async function postingCard(env, url, postingId) {
  const res = await env.ASSETS.fetch(new URL("/data/og_lookup.json", url));
  if (!res.ok) return null;
  const lookup = await res.json();
  const p = lookup[postingId];
  if (!p) return null;
  const bits = [p.company, p.location, p.category && p.category !== "Uncategorized" ? p.category : null].filter(Boolean);
  return { title: p.title || "Open internship", subtitle: bits.join(" · ") };
}

async function companyCard(env, url, companyKey) {
  const res = await env.ASSETS.fetch(new URL("/data/companies.json", url));
  if (!res.ok) return null;
  const companies = await res.json();
  const c = companies.find((row) => row.key === companyKey);
  if (!c) return null;
  const n = c.postings_count;
  const cats = (c.categories || []).slice(0, 2).join(", ");
  return {
    title: c.name,
    subtitle: `${n} open internship${n === 1 ? "" : "s"}${cats ? " · " + cats : ""}`,
  };
}

async function categoryCard(env, url, category) {
  const res = await env.ASSETS.fetch(new URL("/data/meta.json", url));
  if (!res.ok) return null;
  const meta = await res.json();
  const n = meta.categories ? meta.categories[category] : undefined;
  if (n === undefined) return null;
  return {
    title: category,
    subtitle: `${n} open internship${n === 1 ? "" : "s"} in this category`,
  };
}

function escapeXml(s) {
  return String(s ?? "").replace(/[<>&"']/g, (c) => ({ "<": "&lt;", ">": "&gt;", "&": "&amp;", '"': "&quot;", "'": "&apos;" }[c]));
}

// Naive word-wrap by character budget -- good enough for a fixed-size
// card where the font is a browser/renderer default, not a metric we
// can measure server-side without a real font engine.
function wrapText(text, maxChars, maxLines) {
  const words = String(text || "").split(/\s+/).filter(Boolean);
  const lines = [];
  let current = "";
  for (const word of words) {
    const candidate = current ? `${current} ${word}` : word;
    if (candidate.length > maxChars && current) {
      lines.push(current);
      current = word;
      if (lines.length === maxLines - 1) break;
    } else {
      current = candidate;
    }
  }
  if (current) lines.push(current);
  if (lines.length > maxLines) lines.length = maxLines;
  const consumed = lines.join(" ").length;
  if (consumed < text.length && lines.length) {
    lines[lines.length - 1] = lines[lines.length - 1].replace(/.{0,3}$/, "...");
  }
  return lines;
}

function renderCard({ title, subtitle }) {
  const titleLines = wrapText(title, 30, 2);
  const titleSize = titleLines.length > 1 ? 54 : 60;
  const titleLineHeight = titleSize * 1.15;
  const titleStartY = 300 - ((titleLines.length - 1) * titleLineHeight) / 2;

  const titleTspans = titleLines
    .map((line, i) => `<tspan x="96" y="${titleStartY + i * titleLineHeight}">${escapeXml(line)}</tspan>`)
    .join("");

  const subtitleLines = wrapText(subtitle, 70, 2);
  const subtitleY = titleStartY + titleLines.length * titleLineHeight + 20;
  const subtitleTspans = subtitleLines
    .map((line, i) => `<tspan x="96" y="${subtitleY + i * 34}">${escapeXml(line)}</tspan>`)
    .join("");

  return `<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="630" viewBox="0 0 1200 630">
  <rect width="1200" height="630" fill="${CANVAS}"/>
  <defs>
    <radialGradient id="glow" cx="90%" cy="0%" r="60%">
      <stop offset="0%" stop-color="${ACCENT}" stop-opacity="0.16"/>
      <stop offset="70%" stop-color="${ACCENT}" stop-opacity="0"/>
    </radialGradient>
  </defs>
  <rect width="1200" height="630" fill="url(#glow)"/>
  <g transform="translate(730,110)" opacity="0.06">
    <path d="${BOLT_PATH}" fill="${ACCENT}" transform="scale(23.3)"/>
  </g>
  <g transform="translate(96,64)">
    <path d="${BOLT_PATH}" fill="${ACCENT}" transform="scale(2)"/>
    <text x="60" y="30" font-family="${FONT}" font-size="34" font-weight="700" fill="${INK}">lilguy<tspan fill="${INK_TERTIARY}" font-weight="500">.win</tspan></text>
  </g>
  <text font-family="${FONT}" font-size="${titleSize}" font-weight="700" fill="${INK}" letter-spacing="-0.5">${titleTspans}</text>
  <text font-family="${FONT}" font-size="24" fill="${INK_SECONDARY}">${subtitleTspans}</text>
</svg>`;
}
