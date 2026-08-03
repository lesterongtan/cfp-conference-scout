# CFP Conference Scout

Standalone discovery tool for conferences/tradeshows with an open call for
speakers. Searches the web, an Apify-backed event directory (10times), and
the free Confs.tech dataset; filters to active opportunities (6–12 months
out by default, CFP not closed); and surfaces contact info, submission
links, and promoter/venue data where available.

Discovery queries rotate through the full event-type vocabulary
(conference, convention, summit, symposium, congress, forum, annual
meeting, workshop, expo) instead of only ever searching for "conference".

Beyond plain keywords, an optional speaker profile (primary/secondary
niches, speaking topics, expertise keywords, audiences, geography, date
window, event formats, exclusions) can shape discovery — click "Advanced:
speaker profile" in the UI, or pass a `profile` object to
`POST /api/cfp-scout/run` (see `CfpProfile` in `backend/app.py`). This is
Phase 1 (discovery) only: profile fields widen or narrow which candidates
get found and kept — nothing scores, ranks, or weights results against the
profile.

Extracted from a larger internal monorepo into a fully standalone project —
zero dependency on any other repo. Read-only: nothing here writes to a
database.

Contact info goes through a waterfall, cheapest/free first: page text →
`mailto:` links → one contact/about page on the same domain → (optional,
last resort, capped) AI extraction via Claude. The AI stage only ever runs
on the final result set, never the raw candidate pool, and never invents a
value — an AI-reported email is discarded unless it's verified to actually
appear verbatim in the scraped source text.

## Structure

- `backend/` — FastAPI app (`app.py`), the discovery pipeline (`cfp_scout.py`),
  and the search/scrape helpers it depends on (`scraper.py`).
- `frontend/` — Next.js app with the scout UI.

## Running locally

**Backend:**
```bash
cd backend
python -m venv venv
venv\Scripts\activate   # or: source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env    # set API_KEY at minimum
uvicorn app:app --reload --port 8010
```

**Frontend:**
```bash
cd frontend
npm install
cp .env.example .env.local   # API_BASE_URL + matching API_KEY
npm run dev
```

Then open http://localhost:3000.

## Deploying to Railway

This is a monorepo with two different runtimes, so it needs **two separate
Railway services** pointing at this same repo:

1. Create a new Railway project, add a service from this GitHub repo.
2. In that service's Settings → **Root Directory**, set it to `backend`.
   Railway will pick up `backend/railway.toml` and run
   `uvicorn app:app --host 0.0.0.0 --port $PORT` automatically.
3. Add the backend env vars (see `backend/.env.example`) in that service's
   Variables tab — `API_KEY` at minimum.
4. Add a second service from the same repo, set its **Root Directory** to
   `frontend`. It picks up `frontend/railway.toml` (`npm run build` then
   `npm run start -- -p $PORT`).
5. Add the frontend env vars: `API_KEY` (same value as the backend) and
   `API_BASE_URL` set to the backend service's Railway-assigned URL
   (Railway → backend service → Settings → Networking → public domain).

Both services build with Nixpacks auto-detection (Python for `backend/`,
Node for `frontend/`) — no Dockerfile needed.

## Configuration

See `backend/.env.example` for all options. Nothing is required to run —
without search-backend keys it falls back to unauthenticated Bing scraping;
without `APIFY_API_TOKEN` + `CFP_DIRECTORY_APIFY_ACTOR_ID` the directories
lane stays dormant; without `FIRECRAWL_API_KEY` scraping falls back to plain
BeautifulSoup. `API_KEY` is the one required setting — it gates both API
endpoints.

## Notes

- Results are capped at 200 per run (`HARD_MAX_RESULTS` in `cfp_scout.py`),
  a sanity ceiling rather than a target.
- The directories lane (10times via Apify) is capped at $1.00 total spend
  per run via Apify's own `maxTotalChargeUsd`, split evenly across keywords.
- Category→keyword mapping for the directories lane is best-effort, not
  Apify-verified — extend `_TENTIMES_CATEGORY_BY_KEYWORD` in `cfp_scout.py`
  as gaps surface.
- AI contact enrichment is capped at 20 calls per run by default
  (`CFP_AI_ENRICHMENT_MAX_CALLS`), and only runs at all if `CLAUDE_API_KEY`
  is set — leave it unset to disable entirely, no cost either way.
