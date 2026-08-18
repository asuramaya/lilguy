// The frontend is a single static file with no build step and no JS test
// runner, so this extracts the location helpers straight out of
// index.html and exercises them. Extraction rather than duplication is
// the point: a copied-out copy would drift from what actually ships.
//
// Run by scripts/run_tests.sh when node is available.
const fs = require("fs");
const path = require("path");

const html = fs.readFileSync(
  path.join(__dirname, "..", "..", "service", "static", "index.html"), "utf8");
const start = html.indexOf("const NON_PLACE_RE");
const end = html.indexOf("function locationHtml");
if (start < 0 || end < 0) {
  console.error("!! could not find the location helpers in index.html -- has it been renamed?");
  process.exit(1);
}
eval(html.slice(start, end));

// A map pin on a whole country is a worse answer than no link at all,
// which is why these are expected to produce nothing. Every "no link"
// string below was taken from the live corpus.
const cases = [
  ["Remote", null],
  ["Remote - United States", null],
  ["Remote, United States", null],
  ["Remote - USA", null],
  ["Flexible / Remote", null],
  ["United States - Remote", null],
  ["Remote  ", null],
  ["Remote United States of America", null],  // no separator to split on
  ["2 Locations", null],
  ["N/A", null],
  ["", null],
  // Real places keep their link, including when an arrangement word is
  // bolted on -- "Remote - New York" should pin New York.
  ["Austin, TX", "Austin, TX"],
  ["Remote - New York", "New York"],
  ["Chicago, IL", "Chicago, IL"],
  ["London, United Kingdom", "London, United Kingdom"],
];

let failures = 0;
for (const [input, expected] of cases) {
  const url = mapsUrl(input);
  const query = url ? decodeURIComponent(url.split("query=")[1]) : null;
  const ok = expected === null ? url === null : query === expected;
  if (!ok) {
    failures++;
    console.error(`FAIL ${JSON.stringify(input)}: expected ${JSON.stringify(expected)}, got ${JSON.stringify(query)}`);
  }
}
if (failures) {
  console.error(`${failures} frontend location failure(s)`);
  process.exit(1);
}
console.log(`frontend: ${cases.length} location cases passed`);
