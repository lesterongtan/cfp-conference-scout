"""CFP / Conference Scout — standalone FastAPI app.

Ported from speakeragent-api: this used to be a router (cfp_scout_api.py)
mounted inside a much larger multi-tenant app, authenticated by a
Supabase-backed automation-key system (src/api/deps.py). Neither of those
exist here — this is the whole app, with a single static API key instead.

Endpoints:
    GET  /health
    POST /api/cfp-scout/run
    GET  /api/cfp-scout/status/{run_id}

Nothing here writes to a database. Job state is an in-memory dict —
ephemeral by design, cleared on restart.
"""

import hmac
import logging
import os
import threading
import time
import uuid
from typing import Optional

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

load_dotenv()

from cfp_scout import (
    HARD_MAX_RESULTS,
    _DEFAULT_MAX_DAYS_OUT,
    _DEFAULT_MIN_DAYS_OUT,
    run_cfp_discovery,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="CFP Conference Scout")

# Permissive CORS: this is a standalone internal tool, not a multi-tenant
# product — the frontend may call it directly from the browser rather than
# proxying server-side. Tighten CORS_ALLOW_ORIGINS if you want to restrict it.
_allowed_origins = [o.strip() for o in os.getenv("CORS_ALLOW_ORIGINS", "*").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

# ── Auth: one static API key, not the original's multi-tenant system ────────

_API_KEY = os.getenv("API_KEY", "")


def verify_api_key(x_api_key: str = Header(None, alias="X-API-Key")) -> None:
    if not _API_KEY:
        raise HTTPException(status_code=503, detail="Server is not configured with an API_KEY")
    if not x_api_key or not hmac.compare_digest(x_api_key, _API_KEY):
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


# ── In-memory job store — ephemeral, no database ─────────────────────────────

_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()
_JOB_TTL_SECONDS = 30 * 60


# Phase 1 (discovery) profile — see cfp_scout.run_cfp_discovery's docstring.
# Deliberately no scoring/weighting fields: niches/topics/expertise/audiences
# all flatten into a flat search-term list, on equal footing, per explicit
# instruction to skip matching/ranking for this phase.
class CfpProfile(BaseModel):
    primary_niche: str = ""
    secondary_niches: list[str] = Field(default_factory=list)
    speaking_topics: list[str] = Field(default_factory=list)
    expertise_keywords: list[str] = Field(default_factory=list)
    audiences: list[str] = Field(default_factory=list)
    geography: str = ""
    # Raw 10times country code (e.g. "US", "GB"), not a free-text place name
    # — "WW" (worldwide) is the actor's own default.
    country_code: str = "WW"
    min_days_out: int = _DEFAULT_MIN_DAYS_OUT
    max_days_out: int = _DEFAULT_MAX_DAYS_OUT
    event_formats: list[str] = Field(default_factory=list)
    exclusions: list[str] = Field(default_factory=list)


class CfpScoutRunRequest(BaseModel):
    keywords: list[str] = Field(default_factory=list)
    max_results: int = HARD_MAX_RESULTS
    profile: Optional[CfpProfile] = None


def _flatten_profile_keywords(base_keywords: list[str], profile: Optional[CfpProfile]) -> list[str]:
    """Merge profile niches/topics/expertise/audiences into one flat keyword
    list, alongside any plain keywords — no weighting between them."""
    terms = list(base_keywords)
    if profile:
        if profile.primary_niche:
            terms.append(profile.primary_niche)
        terms.extend(profile.secondary_niches)
        terms.extend(profile.speaking_topics)
        terms.extend(profile.expertise_keywords)
        terms.extend(profile.audiences)

    seen: set = set()
    deduped: list[str] = []
    for term in terms:
        term = term.strip()
        if term and term.lower() not in seen:
            seen.add(term.lower())
            deduped.append(term)
    return deduped


def _prune_expired_jobs() -> None:
    cutoff = time.monotonic() - _JOB_TTL_SECONDS
    expired = [job_id for job_id, job in _jobs.items() if job["created_at"] < cutoff]
    for job_id in expired:
        _jobs.pop(job_id, None)


def _run_job(job_id: str, keywords: list[str], max_results: int, profile: Optional[CfpProfile]) -> None:
    try:
        kwargs = {}
        if profile:
            kwargs = dict(
                geography=profile.geography,
                country_code=profile.country_code or "WW",
                min_days_out=profile.min_days_out,
                max_days_out=profile.max_days_out,
                event_formats=profile.event_formats,
                exclusions=profile.exclusions,
            )
        outcome = run_cfp_discovery(keywords=keywords, max_results=max_results, **kwargs)
        with _jobs_lock:
            _jobs[job_id].update(status="completed", **outcome)
    except Exception as exc:
        logger.exception(f"[CFP-SCOUT] Job {job_id} failed")
        with _jobs_lock:
            _jobs[job_id].update(status="error", error=str(exc))


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/api/cfp-scout/run")
def start_cfp_scout(payload: CfpScoutRunRequest, _: None = Depends(verify_api_key)):
    keywords = _flatten_profile_keywords(payload.keywords, payload.profile)[:8]
    if not keywords:
        raise HTTPException(status_code=400, detail="At least one keyword is required")
    max_results = max(1, min(payload.max_results or HARD_MAX_RESULTS, HARD_MAX_RESULTS))

    with _jobs_lock:
        _prune_expired_jobs()
        job_id = str(uuid.uuid4())
        _jobs[job_id] = {
            "status": "running",
            "keywords": keywords,
            "max_results": max_results,
            "created_at": time.monotonic(),
            "results": [],
            "results_count": 0,
            "urls_found": 0,
        }

    thread = threading.Thread(
        target=_run_job, args=(job_id, keywords, max_results, payload.profile), daemon=True
    )
    thread.start()

    logger.info(f"[CFP-SCOUT] Started run {job_id} keywords={keywords} max_results={max_results}")
    return {"run_id": job_id, "status": "started"}


@app.get("/api/cfp-scout/status/{run_id}")
def get_cfp_scout_status(run_id: str, _: None = Depends(verify_api_key)):
    with _jobs_lock:
        job = _jobs.get(run_id)
    if not job:
        raise HTTPException(status_code=404, detail="Run not found")
    return {
        "run_id": run_id,
        "status": job.get("status"),
        "results": job.get("results", []),
        "results_count": job.get("results_count", 0),
        "urls_found": job.get("urls_found", 0),
        "directory_items_found": job.get("directory_items_found", 0),
        "confs_tech_items_found": job.get("confs_tech_items_found", 0),
        "error": job.get("error"),
    }
