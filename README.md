# CFP Conference Scout

Standalone keyword-driven discovery tool for conferences/tradeshows with an
open call for speakers. Searches the web, an Apify-backed event directory
(10times), and the free Confs.tech dataset; filters to active opportunities
(6–12 months out, CFP not closed); and surfaces contact info, submission
links, and promoter/venue data where available.

Extracted from a larger internal monorepo into a fully standalone project —
zero dependency on any other repo. Read-only: nothing here writes to a
database.

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
