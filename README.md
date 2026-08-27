# lilguy.win ⚡

[![Live Web App](https://img.shields.io/badge/Live-lilguy.win-e8952a?style=for-the-badge&logo=cloudflare&logoColor=white)](https://lilguy.win)
[![Tests](https://img.shields.io/badge/tests-349%20passing-4a9b6e?style=for-the-badge&logo=pytest&logoColor=white)](tests/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg?style=for-the-badge)](LICENSE)
[![Indexed Postings](https://img.shields.io/badge/Indexed-8%2C300%2B%20Postings-707070?style=for-the-badge)](data/all_postings.json)

**lilguy.win** is a fast, serverless internship search engine and aggregator indexing **8,300+ open roles across 1,000+ employers** in tech, finance, consulting, healthcare, engineering, logistics, and more.

Explore the live feed at **[lilguy.win](https://lilguy.win)** (mirrored at **[lilguy.pages.dev](https://lilguy.pages.dev)**).

---

## 🌟 Highlights

- **⚡ Sub-Millisecond Edge Architecture**: Zero-backend static distribution powered by Cloudflare Pages. Indexes 8,300+ postings in an ultra-lean ~8 MB bundle with on-demand description shards.
- **🔍 Instant Search & Multi-Axis Filtering**: Search role titles and company names with realtime debounce, combobox auto-suggest, and filters across **Industry**, **Job Function**, **Workplace Type** (Remote, Hybrid, On-site), **Location**, **Recruiting Cycle**, and **Posting Age**.
- **🏢 Deep ATS & Harvester Coverage**: Ingests directly from Greenhouse, Lever, Workday, Oracle Recruiting Cloud, SmartRecruiters, Ashby, Schema.org JSON-LD microdata, and major aggregators.
- **📱 Responsive & Accessible UI**: Mobile-first layout with native bottom drawer filters, auto-zoom prevention on mobile inputs, persistent pagination, full keyboard navigation, and WCAG AA contrast.
- **🤖 Application Autofill Suite**: Browser userscript (`autofill/`) that fills repetitive application fields from a local profile without auto-submitting.
- **🎯 Forkable Filter Presets**: Ready-made filter configs in `presets/` for Software Engineering, Data & Analytics, Finance, Marketing, Sales, HR, and Supply Chain & Operations.
- **📡 Filtered Feeds, Badges & Trends**: Subscribe to any filter combo as an Atom feed, embed a live posting-count badge, or pull weekly hiring-pace stats — see [Feeds, Badges & Trends](#-feeds-badges--trends) below. No account, no email, no data collected about who's watching.

---

## 📡 Feeds, Badges & Trends

Everything here runs off the same static bundle everything else does — no signup, no API key, nothing stored server-side about who's asking.

**Filtered Atom feeds** — subscribe in any feed reader, or point an RSS-to-Discord/Slack/IFTTT bot at it for a poll-based alert with zero infrastructure on either side:

```
https://lilguy.win/feed.atom?category=Software+%26+Technology
https://lilguy.win/feed.atom?company=openai&job_function=Data+Science
https://lilguy.win/feed.atom?q=robotics&max_age_days=7&limit=50
```

Supported params: `company` (a key from `/data/companies.json`), `category`, `job_function`, `work_arrangement`, `cycle_season`, `cycle_year`, `q` (free text), `max_age_days`, `limit` (≤500). No params returns the default unfiltered feed.

**Embeddable badges** — a company's own open-role count, or the site total, as an SVG:

```markdown
![Open internships](https://lilguy.win/badge/total.svg)
![OpenAI internships](https://lilguy.win/badge/openai.svg)
```

**Hiring-pace trends** — [`/data/trends.json`](https://lilguy.win/data/trends.json) publishes a 12-week weekly opened/closed series and the week's fastest-hiring employers, computed straight from postings' own `first_seen`/`closed_at` history (see `service/edge_export.py`'s `build_trends`). The homepage footer surfaces the latest week inline.

**Contextual share previews** — a link to `?posting=<id>`, `?company=<key>`, or `?category=<name>` shows that specific role, employer, or category's title/description **and a matching share-image** when pasted into Slack, Discord, or iMessage, instead of the generic homepage card (`functions/_middleware.js` + `functions/og-card.svg.js`, both Cloudflare Pages Functions). The per-entity image is SVG, rendered at the edge from the same static data the site itself reads — Twitter/X's card validator is pickier about non-raster `og:image` and may show no image there, a deliberate tradeoff against pulling a browser-rendering toolchain into this project just for image generation. The generic homepage share image (`og-image.png`) is a real PNG, regenerated on every export with that export's live open-role/employer counts (`service/og_image.py`, Pillow) — it used to be a hand-rendered screenshot with the numbers typed in as literal text.

---

## 🏗️ Architecture

```
                                  ┌─────────────────────────────┐
                                  │   Continuous Ingestion /    │
                                  │    Automated ATS Discovery  │
                                  └──────────────┬──────────────┘
                                                 │
                                                 ▼
┌─────────────────────────────┐   ┌─────────────────────────────┐
│       Self-Hosted API       │   │     Edge Static Bundler     │
│   FastAPI + PostgreSQL DB   │   │   (service/edge_export.py)  │
│    (service/ / Docker)      │   └──────────────┬──────────────┘
└─────────────────────────────┘                  │
                                                 ▼
                                  ┌─────────────────────────────┐
                                  │      Cloudflare Pages       │
                                  │  - dist/index.html          │
                                  │  - dist/data/feed.json      │
                                  │  - dist/data/descriptions/  │
                                  └──────────────┬──────────────┘
                                                 │
                                                 ▼
                                        https://lilguy.win
```

1. **Client Feed (`dist/data/feed.json`)**: Contains indexed metadata for all 8,300+ open opportunities, stripped of bulky HTML descriptions to keep network payload minimal.
2. **On-Demand Description Shards (`dist/data/descriptions/<id>.json`)**: Individual job descriptions are fetched on-demand when a user expands or inspects a posting.
3. **Automated Discovery**: Self-updating crawlers that verify and discover new ATS endpoints across companies.

---

## 🚀 Quick Start

### 1. View or Deploy the Static Edge App

To build the static distribution and deploy to Cloudflare Pages:

```bash
# 1. Export static edge bundle (generates dist/)
python3 -m service.edge_export

# 2. Deploy to Cloudflare Pages (requires wrangler)
./scripts/publish_edge.sh
```

To preview locally:
```bash
cd dist && python3 -m http.server 8080
# Open http://localhost:8080
```

---

### 2. Run the Self-Hosted Service (FastAPI + Postgres)

```bash
# Launch database and background ingestion workers
docker compose up -d --build

# Open API docs at http://localhost:8000/docs
# Web interface available at http://localhost:8000
```

---

### 3. Run the Daily Scraper & Generate Custom Markdown Feeds

```bash
# Install scraper dependencies
pip install -r scraper/requirements.txt

# Run full scrape against sources.yaml
python scraper/scrape.py

# Build a customized feed using a preset filter (e.g. Software Engineering)
python scraper/build_feed.py --filters presets/software-engineering.yaml --out SE_FEED.md
```

---

## 📁 Repository Layout

| Directory / File | Description |
|---|---|
| **`service/static/index.html`** | Single-page client web app with responsive UI, search, and edge fallback |
| **`service/edge_export.py`** | Sharding and export pipeline for Cloudflare Pages edge distribution |
| **`service/standardize.py`** | Taxonomy normalization, location cleaning, and category classifiers |
| **`scripts/publish_edge.sh`** | Automated deployment script for `lilguy.win` via Wrangler |
| **`data/all_postings.json`** | Canonical raw dataset of all active internship opportunities |
| **`sources.yaml`** | Monitored company career sites and ATS endpoint configurations |
| **`presets/`** | Ready-made filter definitions for major college majors and roles |
| **`autofill/`** | Application autofill browser userscript and profile templates |
| **`scraper/`** | Python scraping engine and vendor ATS connector drivers |
| **`tests/`** | Pytest test suite covering normalization, categorization, and API routes |

---

## 🧪 Testing

Run the comprehensive test suite with `pytest`:

```bash
pip install -r requirements-dev.txt
pytest -v tests/
```

---

## 🤝 Contributing & License

Contributions are welcome! Please see [`CONTRIBUTING.md`](CONTRIBUTING.md) for guidelines on testing and verifying ATS connectors.

Distributed under the **MIT License**. See [`LICENSE`](LICENSE) for details.
