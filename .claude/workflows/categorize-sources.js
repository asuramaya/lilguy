export const meta = {
  name: 'categorize-sources',
  description: 'Research and categorize discovery-promoted sources still sitting Uncategorized, fixing raw-slug display names too',
  whenToUse: 'Run whenever the Uncategorized backlog is worth clearing -- pull the current list (SELECT company FROM sources WHERE category=\'Uncategorized\' ORDER BY company) and pass it as args (a flat array of company tokens). Auto-chunks into batches of ~20 internally, one agent per batch. Apply the result with scripts/apply_categorization.py -- see docs/service-architecture.md\'s "Categorizing discovery-promoted sources" section for the full recipe.',
  phases: [
    { title: 'Research', detail: 'one agent per batch of ~20 tokens, WebSearch to identify real company + category' },
  ],
}

const BATCH_SIZE = 20

const TAXONOMY_NOTES = `
This is for an internship-discovery project (sourcing is deliberately domain-unfiltered --
it pulls postings from ANY company on Greenhouse/Workday, not just logistics ones -- so
you will see companies from every industry, not just supply chain/ops).

Category rules established for this project, follow them exactly:
- ONE specific category per company. No slash-joined categories ("X / Y").
- "&" is allowed ONLY for a real, standard, recognized sector name (e.g. "Aerospace & Defense",
  "Freight & Trucking", "Power & Electrical Equipment") -- never as a lazy join of two unrelated
  things for convenience.
- Granularity preferred: split by real business model where it matters, don't lump into an
  overbroad bucket. Reuse an EXISTING category string if it genuinely fits (query
  "SELECT DISTINCT category FROM sources" for the current live list before inventing a new one),
  otherwise invent a new specific one following the same style (Title Case, 1-4 words).
- Verify with a real web search for anything you don't already know with confidence -- do not
  guess a category from the token name alone. This project's whole discipline has been "go find
  out, don't assume."

Each "token" is a lowercase, punctuation-stripped Greenhouse/Workday URL slug (e.g. "10xgenomics"
for a company actually named "10x Genomics"), NOT a real display name. For each token, identify
the REAL company and provide its proper display name (correct capitalization/spacing/punctuation)
as well as its category. If a token is ambiguous, a dead/rebranded company, or you genuinely
cannot identify what real company it refers to after a real search, set "company_name" to null
and "category" to "Unidentified" -- do not fabricate a guess.
`

async function categorizeBatch(batch, index) {
  const prompt = `Research these ${batch.length} company tokens (each is a lowercase URL slug from
a Greenhouse or Workday careers-page URL) and identify the real company behind each one, its
proper display name, and a specific industry category.

${TAXONOMY_NOTES}

Tokens to research:
${batch.map(t => `- ${t}`).join('\n')}

Return a result for EVERY token listed above, in the same order, even if you couldn't identify
one (use company_name: null, category: "Unidentified" for those, with a one-line note in the
"note" field explaining why).`

  return agent(prompt, {
    label: `batch-${index}`,
    schema: {
      type: 'object',
      properties: {
        results: {
          type: 'array',
          items: {
            type: 'object',
            properties: {
              token: { type: 'string' },
              company_name: { type: ['string', 'null'] },
              category: { type: 'string' },
              note: { type: 'string' },
            },
            required: ['token', 'company_name', 'category'],
          },
        },
      },
      required: ['results'],
    },
  })
}

const tokens = args
if (!Array.isArray(tokens) || tokens.length === 0) {
  throw new Error(
    'categorize-sources needs args: a flat array of company tokens, e.g. ' +
    'Workflow({name: "categorize-sources", args: ["acme", "beta", ...]}). ' +
    'Pull the current list with: SELECT company FROM sources WHERE category=\'Uncategorized\' ORDER BY company;'
  )
}

const batches = []
for (let i = 0; i < tokens.length; i += BATCH_SIZE) batches.push(tokens.slice(i, i + BATCH_SIZE))
log(`${tokens.length} tokens -> ${batches.length} batches of up to ${BATCH_SIZE}`)

phase('Research')
const outcomes = await parallel(batches.map((batch, i) => () => categorizeBatch(batch, i)))
return outcomes.filter(Boolean).flatMap(o => o.results || [])
