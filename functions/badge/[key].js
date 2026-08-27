// Embeddable shields.io-style badges: /badge/openai.svg (a company's own
// open-role count) or /badge/total.svg (site-wide). Meant for a README
// or careers-page link back to lilguy.win -- reads only the already-
// public companies.json/meta.json, no accounts or tracking involved.
export async function onRequestGet(context) {
  const { params, env, request } = context;
  const url = new URL(request.url);
  const key = String(params.key || "").replace(/\.svg$/i, "");

  let label = "lilguy.win";
  let count = null;

  try {
    if (key === "" || key === "total") {
      const res = await env.ASSETS.fetch(new URL("/data/meta.json", url));
      if (res.ok) {
        const meta = await res.json();
        count = meta.total_open_postings;
        label = "open internships";
      }
    } else {
      const res = await env.ASSETS.fetch(new URL("/data/companies.json", url));
      if (res.ok) {
        const companies = await res.json();
        const c = companies.find((row) => row.key === key);
        if (c) {
          count = c.postings_count;
          label = `${c.name} internships`;
        }
      }
    }
  } catch (err) {
    count = null;
  }

  const value = count === null ? "not found" : String(count);
  const color = count === null ? "#8a8474" : "#e8952a";
  const svg = renderBadge(label, value, color);

  return new Response(svg, {
    headers: {
      "content-type": "image/svg+xml; charset=utf-8",
      "cache-control": "public, max-age=1800, s-maxage=1800",
    },
  });
}

function escapeXml(s) {
  return String(s).replace(/[<>&"']/g, (c) => ({ "<": "&lt;", ">": "&gt;", "&": "&amp;", '"': "&quot;", "'": "&apos;" }[c]));
}

function renderBadge(label, value, color) {
  const charW = 6.5;
  const pad = 10;
  const labelW = Math.round(label.length * charW) + pad * 2;
  const valueW = Math.round(value.length * charW) + pad * 2;
  const totalW = labelW + valueW;
  const h = 20;

  return `<svg xmlns="http://www.w3.org/2000/svg" width="${totalW}" height="${h}" role="img" aria-label="${escapeXml(label)}: ${escapeXml(value)}">
  <linearGradient id="s" x2="0" y2="100%">
    <stop offset="0" stop-color="#fff" stop-opacity=".1"/>
    <stop offset="1" stop-opacity=".1"/>
  </linearGradient>
  <clipPath id="r"><rect width="${totalW}" height="${h}" rx="3" fill="#fff"/></clipPath>
  <g clip-path="url(#r)">
    <rect width="${labelW}" height="${h}" fill="#2b2823"/>
    <rect x="${labelW}" width="${valueW}" height="${h}" fill="${color}"/>
    <rect width="${totalW}" height="${h}" fill="url(#s)"/>
  </g>
  <g fill="#fff" text-anchor="middle" font-family="Verdana,Geneva,DejaVu Sans,sans-serif" font-size="11">
    <text x="${labelW / 2}" y="14">${escapeXml(label)}</text>
    <text x="${labelW + valueW / 2}" y="14">${escapeXml(value)}</text>
  </g>
</svg>`;
}
