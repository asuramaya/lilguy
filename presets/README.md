# Ready-made filters

Each of these is a complete `filters.yaml`-shaped file for a different
internship interest, sanity-checked against the real raw store before
being added here (not just written and assumed to work). Use one directly:

```
python scraper/build_feed.py --filters presets/marketing.yaml --out MY_FEED.md
```

Or copy one as a starting point and tune it further — these are meant to
save you the first draft, not be the definitive word on any category.

| Preset | What it's for |
|---|---|
| `operations-logistics-supply-chain.yaml` | This fork's own default (also lives at `../filters.yaml`) |
| `software-engineering.yaml` | Backend/frontend/full-stack/platform/SRE/devops/security |
| `data-analytics.yaml` | Data analyst/scientist/engineer, BI, ML, quant |
| `marketing.yaml` | Brand, social, content, digital/growth/product marketing, PR |
| `finance.yaml` | Financial analyst, accounting, investment banking, treasury, audit, tax |
| `human-resources.yaml` | HR, talent acquisition/recruiting, people ops |
| `sales.yaml` | Sales development, BD, account management, customer success |

Missing your interest? Copy the one closest to it, edit `keywords_any`,
and consider opening a PR — see `../CONTRIBUTING.md`.
