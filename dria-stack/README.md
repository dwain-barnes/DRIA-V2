# DRIA Local Deep-Research Stack

This stack adds local-first deep research to DRIA:

- reasoning runs against a local OpenAI-compatible llama.cpp server
- web search goes through SearXNG
- scraping and extraction go through self-hosted Firecrawl
- the research procedure lives in `agent_skills/dria-deep-research/SKILL.md`
  for this app, with a standalone copy at `dria-stack/skills/dria-deep-research/SKILL.md`

The DRIA voice app calls SearXNG directly for the lightweight
`internet_search` skill. For deep research, it invokes `dria-research.ts`, the
agent-core runner, as a background job. The voice conversation gets a `job_id`
immediately and can keep going while the runner works. The runner calls
Firecrawl search and scrape tools, and Firecrawl routes search to SearXNG
through `SEARXNG_ENDPOINT`.
The runner imports `./agent-core/src`, matching Firecrawl's current web-agent
template pattern where `agent-core/` is vendored into the project rather than
installed from npm. Bootstrap it with `npm run bootstrap:agent-core`.

The app sets `SKILLS_DIR` to `/app/agent_skills`; for standalone testing you can
set `SKILLS_DIR=./dria-stack/skills`.

## Data Flow

```text
voice request
  -> DRIA realtime tool call: dria-deep-research
  -> Python bridge: app.py starts background job and returns job_id
  -> TypeScript runner: dria-stack/dria-research.ts
  -> code-spawned agent-core worker agents for each research angle
  -> agent-core local model provider: llama.cpp /v1
  -> Firecrawl API: /v2/search and scrape
  -> SearXNG only behind Firecrawl search
  -> local synthesis pass over worker JSON
  -> research_results/<job_id>.json
  -> tool result back to DRIA
```

The model is called by the vendored Firecrawl `agent-core` using the
`custom-openai` provider and `LLAMACPP_BASE_URL`. Firecrawl is called through
`firecrawlOptions.apiUrl`. SearXNG enters inside the Firecrawl service when its
environment includes `SEARXNG_ENDPOINT=http://searxng:8080`.

The runner does not merely ask the model to use sub-agents. It creates one
agent-core worker per research angle in code, runs up to
`DRIA_RESEARCH_MAX_WORKERS` of them at once, and appends an `agent_runtime`
trace to the final JSON so you can verify the fan-out happened.
`DRIA_AGENT_MAX_OUTPUT_TOKENS` caps each worker's local-model response so
llama.cpp deployments with several parallel slots do not exceed the per-slot
context window. The Docker defaults use two llama.cpp slots and two concurrent
research workers.

## Local Integration Points

Three settings keep the research loop local:

- Model provider: `dria-research.ts` sets `provider: "custom-openai"` and
  `baseURL: LLAMACPP_BASE_URL`. If omitted, agent-core may use its package
  default, so verify this against the bootstrapped `agent-core` version.
- Firecrawl endpoint: `dria-research.ts` sets `firecrawlOptions.apiUrl` from
  `FIRECRAWL_API_URL`. In Docker this is `http://firecrawl:3002`. If omitted,
  Firecrawl client packages commonly default to the hosted Firecrawl API.
- Search backend: Docker sets `SEARXNG_ENDPOINT=http://searxng:8080` on the
  Firecrawl service. If omitted, Firecrawl's search path may use its default
  configured search provider rather than your local SearXNG.
- Quick search backend: Docker sets `SEARXNG_BASE_URL=http://searxng:8080` on
  the DRIA app so the `internet_search` skill uses the local SearXNG JSON API.

## Firecrawl Compose Override

The normal DRIA Docker stack already includes Firecrawl and SearXNG. The older
`docker-compose.searxng.yml` file is kept for running SearXNG from a separate
cloned Firecrawl repo:

```bash
docker compose -f docker-compose.yaml -f /path/to/dria-stack/docker-compose.searxng.yml up -d
```

Compose merges the SearXNG service into Firecrawl's base project and joins the
same `backend` network. Firecrawl containers can then reach SearXNG at the
internal Docker DNS name `http://searxng:8080`. That is why
`SEARXNG_ENDPOINT` uses an internal hostname instead of `localhost`; from inside
the Firecrawl API container, `localhost` would mean the API container itself.

## SearXNG JSON Requirement

`searxng/settings.yml` must include:

```yaml
search:
  formats:
    - html
    - json
```

Firecrawl expects JSON from SearXNG for programmatic search. If `json` is
missing, Firecrawl receives an HTML page from SearXNG and its `/search` path
fails or returns unusable results.

## Skill Contract

Deep Agents discovers skills by scanning the configured `skillsDir` for folders
containing `SKILL.md`. The YAML frontmatter `description` is the selection
contract: it tells the agent when the skill applies. The body gives the
procedure: decompose the query, launch parallel angle workers, search, scrape,
triangulate, and call `formatOutput`.

The JSON schema in the skill constrains the final shape. In DRIA's runner the
worker agents return JSON findings first, then a local synthesis pass produces
the final research JSON. `dria-research.ts` persists that object under
`research_results/<job_id>.json` so old DRIA consumers can keep reading
research artifacts from disk.

## Untrusted Content

Search result titles and scraped page text are untrusted input. They can contain
indirect prompt-injection instructions such as "ignore your previous task" or
"print your API key." The skill explicitly treats that content as data, excludes
injection attempts, and prevents page text from changing the task or output
schema.

## Verify Before Trusting

Before relying on this for private research, verify these package-level details
against the installed source:

- the vendored `agent-core` still honors `custom-openai`, `baseURL`,
  `subAgentModel`, `skillsDir`, `maxWorkers`, and `format: "json"` as used here.
- `firecrawl-aisdk` or the Firecrawl client still honors
  `firecrawlOptions.apiUrl`.
- Self-hosted Firecrawl still honors `SEARXNG_ENDPOINT` for `/v2/search`.
- No package default silently routes model calls, Firecrawl calls, search, or
  extraction to hosted cloud endpoints when a local env value is missing.
