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
