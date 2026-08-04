"""CFP / conference discovery scout — standalone and read-only.

Searches the web for conferences/events with an open call for speakers,
driven by free-text keywords (not a speaker profile). Nothing here writes
to any database — results are returned in-memory only.

Ported from speakeragent-api's src/agent/cfp_scout.py into a fully
standalone project: uses the local scraper.py module (a copy of the
original's web_search/scrape_page, see that file's docstring), with no
dependency on the original repo at all.

Early-build additions on top of the v1 discovery pass:
  - Event date extraction (schema.org JSON-LD first, free-text regex fallback)
    and a 6-12 month "active window" filter, with confidence labeling.
  - A CFP-closed heuristic so expired/closed calls don't show as active.
  - Domain-based deduplication (official domain as the dedup key).
  - A "directories" discovery lane via Apify. Verified and ready to use with
    zen-studio~10times-events-scraper (real query/date input schema, real
    nested output shape — confirmed against the actor's public docs, not
    guessed) — set APIFY_API_TOKEN + CFP_DIRECTORY_APIFY_ACTOR_ID to turn it
    on. Any other actor falls back to a configurable JSON input template
    (CFP_DIRECTORY_APIFY_INPUT_TEMPLATE) since schemas vary actor to actor.
    Sessionize and Sched were evaluated and dropped from this lane — both
    require already knowing a specific event's ID, with no cross-platform
    search, so neither can discover anything new.
  - A "confs_tech" discovery lane against the free, keyless Confs.tech
    open-source dataset (github.com/tech-conferences/conference-data).
    Only fires for keywords matching one of its tech topics (python,
    javascript, security, devops, etc.) — contributes nothing for non-tech
    niches like healthcare, at zero cost either way.
"""

import json
import logging
import os
import re
import threading
import time
from functools import partial
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from typing import Optional
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from scraper import scrape_page, web_search

logger = logging.getLogger(__name__)

# Raised from the original 50-result testing cap: at 50, the events lane
# (which submits far more scrape tasks than the directories/confs_tech
# lanes) would finish filling the cap before slower directory-lane results
# ever landed in the truncated output, even though they were found. This is
# a sanity ceiling, not a target — most runs won't get close to it.
HARD_MAX_RESULTS = 200
_MAX_KEYWORDS = 8
_SCRAPE_WORKERS = 8

# "Active window" — event date must fall between these many days from today.
# 6 months / 12 months by default, per the original business requirement
# (not stale, not too far out) — but overridable per-run via a profile's
# date window. Passed as explicit function params throughout, never read
# as bare globals: the app can serve concurrent scout runs, and mutating a
# module-level global per-request would corrupt other runs in flight.
_DEFAULT_MIN_DAYS_OUT = 180
_DEFAULT_MAX_DAYS_OUT = 365

_UA_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}

# Regex, not literal phrases — confirmed a real page ("Call for Speakers at
# ICHOM 2026 has now closed" / "The submission window is now closed") that
# exact-substring matching missed entirely, because inserted words (an event
# name, "now") or minor rewording (singular "submission window" vs. plural
# "submissions") broke every literal phrase in the old list. These allow a
# bounded gap between the key terms instead of requiring an exact phrase.
_CFP_CLOSED_PATTERNS = [
    re.compile(r"call for (?:speakers|papers|proposals|abstracts|presentations)\b.{0,60}?\bclosed\b", re.I | re.S),
    re.compile(r"\bcfp\b.{0,30}?\bclosed\b", re.I | re.S),
    re.compile(r"\bsubmissions?\b.{0,30}?\bclosed\b", re.I | re.S),
    re.compile(r"\bno longer accepting\b", re.I),
    re.compile(r"\bdeadline\s+has\s+passed\b", re.I),
]

_MONTH_DATE_FORMATS = [
    "%B %d, %Y",   # March 15, 2026
    "%B %d %Y",    # March 15 2026
    "%b %d, %Y",   # Mar 15, 2026
    "%b %d %Y",    # Mar 15 2026
    "%m/%d/%Y",    # 03/15/2026
    "%Y-%m-%d",    # 2026-03-15
]


# Full event-type vocabulary from the Phase 1 discovery brief. Every query
# used to hardcode the literal word "conference" — a real gap, since an
# event that self-describes as a "Congress" or "Symposium" got no
# deliberate query coverage at all.
_EVENT_TYPE_TERMS = [
    "conference", "convention", "summit", "symposium", "congress",
    "forum", "annual meeting", "workshop", "expo",
]


def _build_queries(keywords: list[str], geography: str = "") -> list[str]:
    """Build CFP-flavored search queries from free-text keywords.

    Rotates two event-type terms per keyword across the query templates
    (instead of only ever saying "conference"), so a multi-keyword profile
    naturally spreads coverage across most of the vocabulary without a
    combinatorial blowup in query count. `geography`, if given, is appended
    as a plain search-text qualifier (e.g. "Europe", "United States") —
    there's no structured location field for the free-text web-search lane.
    """
    cleaned = [kw.strip() for kw in keywords if kw and kw.strip()][:_MAX_KEYWORDS]
    if not cleaned:
        cleaned = ["conference"]

    year = str(date.today().year)
    next_year = str(int(year) + 1)
    geo_suffix = f" {geography.strip()}" if geography and geography.strip() else ""

    queries: list[str] = []
    for i, kw in enumerate(cleaned):
        et1 = _EVENT_TYPE_TERMS[(i * 2) % len(_EVENT_TYPE_TERMS)]
        et2 = _EVENT_TYPE_TERMS[(i * 2 + 1) % len(_EVENT_TYPE_TERMS)]
        queries.append(f'{kw} "call for speakers" {et1}{geo_suffix} {year}')
        queries.append(f'{kw} "call for speakers" {et2}{geo_suffix} {next_year}')
        queries.append(f'{kw} {et1} "call for proposals"{geo_suffix} {year}')
        queries.append(f'{kw} {et2} "call for papers"{geo_suffix} {year}')
        queries.append(f'{kw} {et1} "speaker applications" open{geo_suffix}')

    seen: set = set()
    deduped = []
    for q in queries:
        if q not in seen:
            seen.add(q)
            deduped.append(q)
    return deduped


def _interleave_by_source(url_sources: list[tuple]) -> list[tuple]:
    """Round-robin (url, source) pairs across search backends.

    web_search() dedupes URLs in a fixed backend priority order (Tavily,
    then SerpAPI, then Serper, then Exa, then Bing fallback) — so if the
    first backends alone already exceed the candidate cap, later backends
    (e.g. Exa) never even reach the scrape step. Interleaving first means
    every configured backend gets a fair shot before truncation.
    """
    by_source: dict[str, list[tuple]] = {}
    order: list[str] = []
    for item in url_sources:
        source = item[1]
        if source not in by_source:
            by_source[source] = []
            order.append(source)
        by_source[source].append(item)

    interleaved: list[tuple] = []
    idx = 0
    while any(by_source.values()):
        source = order[idx % len(order)]
        bucket = by_source[source]
        if bucket:
            interleaved.append(bucket.pop(0))
        idx += 1
    return interleaved


def _domain_of(url: str) -> str:
    try:
        parsed = urlparse(url if url.startswith("http") else f"https://{url}")
        host = parsed.netloc.replace("www.", "")
        return host or url
    except Exception:
        return url


def _detect_cfp_closed(text: str) -> bool:
    if not text:
        return False
    return any(p.search(text) for p in _CFP_CLOSED_PATTERNS)


# ── Date parsing ─────────────────────────────────────────────────────────────

def _parse_loose_date(date_str: str) -> Optional[date]:
    """Parse a free-text date like scraper.py's DATE_RE extracts (regex fallback)."""
    if not date_str:
        return None
    cleaned = re.sub(r"(\d{1,2})\s*[-–]\s*\d{1,2}", r"\1", date_str.strip())
    for fmt in _MONTH_DATE_FORMATS:
        try:
            return datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue
    return None


def _parse_iso_date(date_str: str) -> Optional[date]:
    """Parse an ISO 8601 date/datetime string (schema.org JSON-LD dates)."""
    if not date_str:
        return None
    try:
        return datetime.fromisoformat(date_str.strip().replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _classify_window(
    event_date: Optional[date],
    min_days_out: int = _DEFAULT_MIN_DAYS_OUT,
    max_days_out: int = _DEFAULT_MAX_DAYS_OUT,
) -> str:
    """Classify a known event date against the active date window.

    Takes the window bounds as explicit params (defaulting to the original
    6-12 months) rather than reading module-level globals — the app can
    serve concurrent scout runs with different profile date windows, and
    mutating a shared global per-request would corrupt other runs in flight.
    """
    if event_date is None:
        return "unknown"
    days_out = (event_date - date.today()).days
    if days_out < 0:
        return "expired"
    if days_out < min_days_out:
        return "too_soon"
    if days_out > max_days_out:
        return "too_far_out"
    return "in_window"


_VIRTUAL_LOCATION_TERMS = ("virtual", "online", "webinar", "remote")
_HYBRID_LOCATION_TERMS = ("hybrid",)


def _infer_event_format(location: str, jsonld_attendance_mode: str = "") -> str:
    """Best-effort virtual/in_person/hybrid classification.

    No source in this pipeline reliably reports attendance mode as
    structured data (10times only exposes it via the costlier
    scrapeDetails=True mode we deliberately don't enable — see
    _build_directory_input), so this is inferred from free text, not
    verified. Returns "unknown" rather than guessing when there's nothing
    to go on, so callers can choose not to drop unknowns during filtering.
    """
    mode = (jsonld_attendance_mode or "").lower()
    if "mixed" in mode or "hybrid" in mode:
        return "hybrid"
    if "online" in mode:
        return "virtual"
    if "offline" in mode:
        return "in_person"

    text = (location or "").lower()
    if any(term in text for term in _HYBRID_LOCATION_TERMS):
        return "hybrid"
    if any(term in text for term in _VIRTUAL_LOCATION_TERMS):
        return "virtual"
    if text.strip():
        return "in_person"
    return "unknown"


# ── schema.org JSON-LD extraction (separate lightweight fetch) ──────────────
# scrape_page() strips <script> tags before we'd ever see them, so structured
# Event data needs its own raw fetch — kept independent to avoid touching
# scraper.py at all.

def _flatten_jsonld(data) -> list[dict]:
    if isinstance(data, dict):
        if "@graph" in data and isinstance(data["@graph"], list):
            return [obj for obj in data["@graph"] if isinstance(obj, dict)]
        return [data]
    if isinstance(data, list):
        return [obj for obj in data if isinstance(obj, dict)]
    return []


def _jsonld_address_text(location) -> str:
    """City/region text from a schema.org Place's address, if present."""
    if not isinstance(location, dict):
        return ""
    address = location.get("address")
    if isinstance(address, dict):
        parts = [
            address.get("addressLocality", ""),
            address.get("addressRegion", "") or address.get("addressCountry", ""),
        ]
        return ", ".join(p for p in parts if p)
    if isinstance(address, str):
        return address
    return ""


def _jsonld_location_name(location) -> str:
    """City/region text for display — prefers structured address over the
    venue's own name (see _jsonld_venue_name for that), since a venue name
    like "Austin Convention Center" isn't itself a location a user can
    filter/search geography by."""
    if isinstance(location, dict):
        return _jsonld_address_text(location) or location.get("name", "") or ""
    if isinstance(location, str):
        return location
    return ""


def _jsonld_venue_name(location) -> str:
    if not isinstance(location, dict):
        return ""
    loc_type = location.get("@type", "")
    loc_type = " ".join(loc_type) if isinstance(loc_type, list) else str(loc_type)
    if "virtual" in loc_type.lower():
        return ""
    return location.get("name", "") or ""


def _jsonld_organizer_name(organizer) -> str:
    for org in (organizer if isinstance(organizer, list) else [organizer]):
        if isinstance(org, dict) and org.get("name"):
            return org["name"]
        if isinstance(org, str) and org.strip():
            return org.strip()
    return ""


def _jsonld_organizer_url(organizer) -> str:
    for org in (organizer if isinstance(organizer, list) else [organizer]):
        if isinstance(org, dict) and org.get("url"):
            return org["url"]
    return ""


# Fallback for pages with no schema.org organizer — a plain-text pattern
# match, not verified structured data. Only used when JSON-LD gives us
# nothing, same spirit as the AI contact stage: best-effort, never invented.
# Tokens require a mandatory space between them and no embedded punctuation,
# so the match naturally stops at the first lowercase word or punctuation —
# e.g. "Organized by Acme Events LLC every year" stops at "every", and
# "Organized by Acme Events LLC. Program Chair: ..." stops at "LLC" rather
# than running on into the next sentence.
_ORGANIZER_TEXT_RE = re.compile(
    r"(?:[Oo]rganized|[Pp]roduced|[Pp]resented|[Hh]osted)\s+by\s+"
    r"([A-Z][\w&'-]*(?:\s+[A-Z][\w&'-]*){0,5})"
)


def _extract_organizer_from_text(full_text: str) -> str:
    if not full_text:
        return ""
    m = _ORGANIZER_TEXT_RE.search(full_text)
    return m.group(1).strip() if m else ""


def _jsonld_price(offers) -> str:
    candidates = offers if isinstance(offers, list) else [offers]
    for offer in candidates:
        if not isinstance(offer, dict):
            continue
        price = offer.get("price") or offer.get("lowPrice")
        if price not in (None, "", "0"):
            currency = offer.get("priceCurrency", "")
            return f"{currency} {price}".strip()
    return ""


def _fetch_jsonld_event(url: str, timeout: int = 8) -> dict:
    """Best-effort fetch of schema.org Event JSON-LD. Returns {} on any failure."""
    try:
        resp = requests.get(url, headers=_UA_HEADERS, timeout=timeout, allow_redirects=True)
        resp.raise_for_status()
    except Exception:
        return {}

    try:
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
            raw = tag.string or tag.get_text() or ""
            if not raw.strip():
                continue
            try:
                data = json.loads(raw)
            except Exception:
                continue
            for obj in _flatten_jsonld(data):
                types = obj.get("@type")
                types = types if isinstance(types, list) else [types]
                if not any(str(t).lower() == "event" for t in types if t):
                    continue
                location = obj.get("location")
                organizer = obj.get("organizer")
                return {
                    "start_date": obj.get("startDate", ""),
                    "location_name": _jsonld_location_name(location),
                    "venue_name": _jsonld_venue_name(location),
                    "price": _jsonld_price(obj.get("offers")),
                    "attendance_mode": obj.get("eventAttendanceMode", "") or "",
                    "organizer_name": _jsonld_organizer_name(organizer),
                    "organizer_url": _jsonld_organizer_url(organizer),
                }
    except Exception as exc:
        logger.debug(f"[CFP-SCOUT] JSON-LD parse failed for {url}: {exc}")
    return {}


# ── Directories lane (Apify) — plumbing only, dormant until configured ──────

# Default hard cap on Apify spend per directories-lane run. Applies via the
# platform's own maxTotalChargeUsd (query param on the Run Actor endpoint,
# https://docs.apify.com/api/v2/act-runs-post) — Apify stops the actor and
# never bills past this, it's not something we track/enforce ourselves.
_DEFAULT_MAX_CHARGE_USD = 1.00


_TERMINAL_RUN_STATUSES = {"SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"}


def _run_apify_actor(
    actor_id: str,
    run_input: dict,
    apify_token: str,
    wait_for_finish: int = 60,
    timeout: int = 70,
    max_total_charge_usd: Optional[float] = _DEFAULT_MAX_CHARGE_USD,
    max_poll_seconds: int = 240,
    poll_interval_seconds: int = 5,
) -> list[dict]:
    """Generic Apify actor runner: start a run, poll until it finishes, fetch items.

    Confirmed live: a real run can still be "RUNNING" well past a single
    120s waitForFinish window (observed 200s+ with scrapeDetails on) — the
    old version checked status exactly once and returned [] if not already
    SUCCEEDED, silently discarding a run that would have finished fine.
    This lane runs inside our own already-async background job, so polling
    for real completion is cheap here. Returns [] on any failure or if the
    run never reaches SUCCEEDED within max_poll_seconds — never raises.
    """
    if not apify_token or not actor_id:
        return []
    try:
        run_url = f"https://api.apify.com/v2/acts/{actor_id}/runs"
        params = {"waitForFinish": wait_for_finish}
        if max_total_charge_usd is not None:
            params["maxTotalChargeUsd"] = max_total_charge_usd
        # The actor's input fields go directly at the top level of the POST
        # body — confirmed against Apify's own docs (docs.apify.com/api/v2/
        # act-runs-post: "the POST payload ... is passed as INPUT to the
        # Actor"). Wrapping it in {"input": run_input} was a real, confirmed
        # bug: verified via a live run's actual stored INPUT record that the
        # actor received 100% schema defaults (category="", eventType="all",
        # maxItems=0/unlimited, scrapeDetails=true) — none of our values ever
        # reached it, on every run to date.
        resp = requests.post(
            run_url,
            headers={"Authorization": f"Bearer {apify_token}"},
            json=run_input,
            params=params,
            timeout=timeout,
        )
        if resp.status_code not in (200, 201):
            logger.warning(f"[CFP-SCOUT] Apify actor {actor_id} failed to start: {resp.status_code}")
            return []
        run_data = resp.json().get("data", {})
        run_id = run_data.get("id", "")
        status = run_data.get("status", "")

        elapsed = 0
        while status not in _TERMINAL_RUN_STATUSES and elapsed < max_poll_seconds and run_id:
            time.sleep(poll_interval_seconds)
            elapsed += poll_interval_seconds
            poll_resp = requests.get(
                f"https://api.apify.com/v2/actor-runs/{run_id}",
                headers={"Authorization": f"Bearer {apify_token}"},
                timeout=30,
            )
            if poll_resp.status_code != 200:
                logger.warning(f"[CFP-SCOUT] Apify actor {actor_id} status poll failed: {poll_resp.status_code}")
                break
            run_data = poll_resp.json().get("data", {})
            status = run_data.get("status", "")

        if status != "SUCCEEDED":
            logger.warning(
                f"[CFP-SCOUT] Apify actor {actor_id} run {run_id} did not succeed within "
                f"~{wait_for_finish + elapsed}s (final status={status or 'unknown'})"
            )
            return []

        dataset_id = run_data.get("defaultDatasetId", "")
        if not dataset_id:
            return []
        items_resp = requests.get(
            f"https://api.apify.com/v2/datasets/{dataset_id}/items",
            headers={"Authorization": f"Bearer {apify_token}"},
            timeout=30,
        )
        if items_resp.status_code != 200:
            return []
        items = items_resp.json()
        return items if isinstance(items, list) else []
    except Exception as exc:
        logger.warning(f"[CFP-SCOUT] Apify actor {actor_id} error: {exc}")
        return []


# Verified against the actor's public Apify Store page (input schema + example
# output) — not guessed. ~$3.49 per 1,000 events on the free tier as of this
# writing; check apify.com/zen-studio/10times-events-scraper for current pricing.
_TENTIMES_ACTOR_ID = "zen-studio~10times-events-scraper"


def _normalize_10times_item(item: dict) -> Optional[dict]:
    """Verified output shape for zen-studio/10times-events-scraper."""
    name = item.get("name") or ""
    # Prefer the organizer's own site over the 10times.com listing page.
    # Confirmed live: 10times.com returns 403 Forbidden on every scrape
    # attempt, so using it left contact/submission-form/CFP-status signal
    # empty on every single directory-lane result. `website` is a normal
    # organizer domain and usually scrapable — it's our only real shot at
    # detecting actual CFP status for this source, since nothing in the
    # actor's own schema indicates "looking for speakers" (checked: status
    # is always "Active"; type is Conference/Tradeshow/Workshop only;
    # speakers/schedule/faq are null without scrapeDetails).
    url = item.get("website") or item.get("url") or ""
    if not name or not url:
        return None
    location = item.get("location") if isinstance(item.get("location"), dict) else {}
    # `.get(key, "")` only falls back when the key is MISSING — the real API
    # returns explicit JSON nulls for some fields (confirmed live: this
    # crashed on real data), so every field here needs `or ""` to also
    # catch a present-but-null value.
    location_parts = [
        location.get("venueName") or "",
        location.get("cityName") or "",
        location.get("state") or location.get("countryName") or "",
    ]
    organizer = item.get("organizer") if isinstance(item.get("organizer"), dict) else {}
    return {
        "name": name,
        "url": url if str(url).startswith("http") else f"https://{url}",
        "location_raw": ", ".join(p for p in location_parts if p),
        "date_raw": item.get("startDate") or "",
        "description": item.get("description") or "",
        # Real fields the actor already returns — Conference/Tradeshow/
        # Workshop, the organizer (Promoter), and the venue — previously
        # fetched and discarded without ever reaching our output.
        "event_type": item.get("type") or "",
        "venue_name": location.get("venueName") or "",
        "promoter_name": organizer.get("name") or "",
        "promoter_website": organizer.get("website") or "",
    }


def _normalize_directory_item(item: dict) -> Optional[dict]:
    """Map a directory actor's output to our shape.

    Tries the verified 10times output shape first (nested `location` object
    with cityName/venueName), then falls back to a lenient flat-field guess
    for any other configured actor.
    """
    location = item.get("location")
    if isinstance(location, dict) and ("cityName" in location or "venueName" in location):
        normalized = _normalize_10times_item(item)
        if normalized:
            return normalized

    name = item.get("title") or item.get("name") or item.get("eventName") or ""
    url = item.get("url") or item.get("website") or item.get("link") or item.get("eventUrl") or ""
    if not name or not url:
        return None
    return {
        "name": name,
        "url": url if str(url).startswith("http") else f"https://{url}",
        "location_raw": location if isinstance(location, str) else (item.get("venue") or ""),
        "date_raw": item.get("startDate") or item.get("date") or item.get("eventDate") or "",
        "description": item.get("description", ""),
        "event_type": "",
        "venue_name": "",
        "promoter_name": "",
        "promoter_website": "",
    }


# Verified against the actor's real input schema (console dropdown) — these
# IDs are exact, not guessed. "query" alone was unreliable in practice
# (confirmed: a manual run with just a category, no query, returned real
# results; query-only runs did not) — category is what actually filters.
_TENTIMES_CATEGORIES = {
    "3": "Apparel & Clothing", "5": "Auto & Automotive", "7": "Building & Construction",
    "13": "Electric & Electronics", "16": "Agriculture & Forestry", "19": "Arts & Crafts",
    "27": "Medical & Pharma", "30": "Packing & Packaging", "34": "Industrial Engineering",
    "37": "Telecommunication", "50": "Travel & Tourism", "53": "Science & Research",
    "55": "Business Services", "61": "Education & Training", "63": "Miscellaneous",
    "80": "Logistics & Transportation", "106": "Environment & Waste", "107": "Fashion & Beauty",
    "108": "Food & Beverages", "113": "Baby, Kids & Maternity", "125": "Wellness, Health & Fitness",
    "128": "Power & Energy", "146": "Banking & Finance", "150": "Security & Defense",
    "156": "IT & Technology", "158": "Animals & Pets", "159": "Home & Office",
    "160": "Hospitality", "161": "Entertainment & Media",
}

# Keyword -> category ID. The IDs above are verified; these English synonym
# mappings are our own best-effort guesses, not pulled from Apify — expect
# to extend this list as real usage surfaces gaps.
_TENTIMES_CATEGORY_BY_KEYWORD = {
    # 3 — Apparel & Clothing
    "apparel": "3", "clothing": "3", "fashion": "3", "textile": "3", "textiles": "3",
    # 5 — Auto & Automotive
    "auto": "5", "automotive": "5", "vehicles": "5", "cars": "5",
    # 7 — Building & Construction
    "construction": "7", "architecture": "7", "building": "7",
    # 13 — Electric & Electronics
    "electric": "13", "electronics": "13", "electrical": "13",
    # 16 — Agriculture & Forestry
    "agriculture": "16", "farming": "16", "forestry": "16", "agtech": "16",
    # 19 — Arts & Crafts
    "arts": "19", "crafts": "19", "craft": "19",
    # 27 — Medical & Pharma
    "healthcare": "27", "health": "27", "medical": "27", "medicine": "27",
    "pharma": "27", "pharmaceutical": "27", "hospital": "27", "clinical": "27",
    "physician": "27", "nursing": "27",
    # 30 — Packing & Packaging
    "packing": "30", "packaging": "30",
    # 34 — Industrial Engineering
    "industrial": "34", "manufacturing": "34", "engineering": "34",
    # 37 — Telecommunication
    "telecom": "37", "telecommunications": "37", "telecommunication": "37",
    # 50 — Travel & Tourism
    "travel": "50", "tourism": "50",
    # 53 — Science & Research
    "science": "53", "research": "53", "biotech": "53", "biotechnology": "53",
    # 55 — Business Services
    "business": "55", "consulting": "55", "leadership": "55", "marketing": "55",
    # 61 — Education & Training
    "education": "61", "training": "61", "edtech": "61", "academic": "61",
    # 80 — Logistics & Transportation
    "logistics": "80", "transportation": "80", "shipping": "80", "freight": "80",
    # 106 — Environment & Waste
    "environment": "106", "environmental": "106", "waste": "106", "recycling": "106",
    # 107 — Fashion & Beauty
    "beauty": "107", "cosmetics": "107",
    # 108 — Food & Beverages
    "food": "108", "beverage": "108", "beverages": "108", "culinary": "108",
    # 113 — Baby, Kids & Maternity
    "baby": "113", "kids": "113", "maternity": "113", "childcare": "113",
    # 125 — Wellness, Health & Fitness
    "wellness": "125", "fitness": "125", "nutrition": "125",
    # 128 — Power & Energy
    "energy": "128", "renewable": "128", "sustainability": "128", "power": "128",
    # 146 — Banking & Finance
    "finance": "146", "financial": "146", "banking": "146", "fintech": "146",
    # 150 — Security & Defense
    "security": "150", "defense": "150", "cybersecurity": "150", "military": "150",
    # 156 — IT & Technology
    "tech": "156", "technology": "156", "software": "156", "ai": "156", "cloud": "156",
    "startup": "156", "startups": "156",
    # 158 — Animals & Pets
    "animals": "158", "pets": "158", "veterinary": "158",
    # 159 — Home & Office
    "home": "159", "office": "159", "furniture": "159",
    # 160 — Hospitality
    "hospitality": "160", "hotel": "160", "hotels": "160", "restaurant": "160",
    # 161 — Entertainment & Media
    "entertainment": "161", "media": "161", "gaming": "161", "film": "161",
}


def _resolve_tentimes_category(keyword: str) -> str:
    """Exact-word match a keyword to a 10times category ID, or '' (all categories)."""
    for word in re.findall(r"[a-zA-Z]+", keyword.lower()):
        category_id = _TENTIMES_CATEGORY_BY_KEYWORD.get(word)
        if category_id:
            return category_id
    return ""


def _build_directory_input(
    actor_id: str,
    keyword: str,
    max_items: int = 30,
    country: str = "WW",
    min_days_out: int = _DEFAULT_MIN_DAYS_OUT,
    max_days_out: int = _DEFAULT_MAX_DAYS_OUT,
) -> Optional[dict]:
    """Build the actor's run input.

    Uses the verified schema automatically for the 10times actor. For any
    other actor, requires CFP_DIRECTORY_APIFY_INPUT_TEMPLATE to be set —
    we do not guess an unverified actor's input fields.
    """
    if actor_id == _TENTIMES_ACTOR_ID:
        today = date.today()
        category_id = _resolve_tentimes_category(keyword)
        run_input = {
            "category": category_id,  # "" = all categories
            "country": country or "WW",
            "eventType": "conference",
            "startDate": (today + timedelta(days=min_days_out)).isoformat(),
            "endDate": (today + timedelta(days=max_days_out)).isoformat(),
            "maxItems": max_items,
            "onlineOnly": False,
            # False: we only use name/description/startDate/location, all of
            # which are in the basic response. scrapeDetails=True (schedule,
            # speaker info, ticket pricing, images) is what made a live test
            # run take 200s+ — well past our old single wait window — for
            # data we never use. Off by default; polling in _run_apify_actor
            # is a real fix for that regardless, this is just faster too.
            "scrapeDetails": False,
        }
        # Only fall back to free-text query when we couldn't resolve a
        # category — category is the field that's confirmed to actually
        # filter; query-only runs were confirmed not to return results.
        if not category_id:
            run_input["query"] = keyword
        else:
            logger.info(
                f"[CFP-SCOUT] 10times: '{keyword}' -> category {category_id} "
                f"({_TENTIMES_CATEGORIES.get(category_id, '?')})"
            )
        return run_input
    template_raw = os.getenv("CFP_DIRECTORY_APIFY_INPUT_TEMPLATE", "")
    if not template_raw:
        logger.warning(
            f"[CFP-SCOUT] No verified input template for actor '{actor_id}' — "
            "set CFP_DIRECTORY_APIFY_INPUT_TEMPLATE to configure it"
        )
        return None
    try:
        return json.loads(template_raw.replace("{keyword}", keyword))
    except Exception as exc:
        logger.warning(f"[CFP-SCOUT] CFP_DIRECTORY_APIFY_INPUT_TEMPLATE invalid JSON: {exc}")
        return None


def _search_directories(
    keywords: list[str],
    max_results: int = HARD_MAX_RESULTS,
    country: str = "WW",
    min_days_out: int = _DEFAULT_MIN_DAYS_OUT,
    max_days_out: int = _DEFAULT_MAX_DAYS_OUT,
) -> list[dict]:
    """Directories lane — pulls from a structured event-directory Apify actor.

    Disabled (returns []) unless both APIFY_API_TOKEN and
    CFP_DIRECTORY_APIFY_ACTOR_ID are set.
    """
    apify_token = os.getenv("APIFY_API_TOKEN", "") or os.getenv("APIFY_TOKEN_PODCAST_SCRAPER", "")
    actor_id = os.getenv("CFP_DIRECTORY_APIFY_ACTOR_ID", "")
    if not apify_token or not actor_id:
        logger.info(
            "[CFP-SCOUT] Directories lane not configured "
            "(APIFY_API_TOKEN / CFP_DIRECTORY_APIFY_ACTOR_ID) — skipping"
        )
        return []

    max_charge_usd = float(os.getenv("CFP_DIRECTORY_APIFY_MAX_CHARGE_USD", _DEFAULT_MAX_CHARGE_USD))
    run_keywords = keywords[:3]  # keep directory-lane spend bounded
    # Split the total cap evenly across keyword runs so the SUM across all
    # of them can never exceed max_charge_usd — Apify enforces maxTotalChargeUsd
    # per individual run, not across multiple runs, so this division is what
    # actually makes it a total cap rather than a per-keyword one.
    per_run_charge_usd = max_charge_usd / len(run_keywords) if run_keywords else max_charge_usd
    # No artificial ceiling here — maxTotalChargeUsd (below) is the real,
    # Apify-enforced spend cap and will stop the run regardless of maxItems.
    # We still pass a real (non-zero) maxItems rather than 0: confirmed live
    # that 0/absent triggers the actor's "complete-coverage" mode, which is
    # both slower and ~2-3x pricier per Apify's own log output.
    max_items = max(10, max_results)

    # Build every keyword's input up front. _build_directory_input only
    # returns None when the actor is unverified AND no input template is
    # configured — a condition that doesn't depend on the keyword at all, so
    # if it fails once it fails for all of them. Check once instead of
    # rediscovering that on every loop iteration.
    keyword_inputs: list[tuple[str, dict]] = []
    for kw in run_keywords:
        run_input = _build_directory_input(
            actor_id, kw, max_items=max_items, country=country,
            min_days_out=min_days_out, max_days_out=max_days_out,
        )
        if run_input is None:
            return []
        keyword_inputs.append((kw, run_input))

    def _run_one(kw_input: tuple[str, dict]) -> list[dict]:
        kw, run_input = kw_input
        items = _run_apify_actor(actor_id, run_input, apify_token, max_total_charge_usd=per_run_charge_usd)
        normalized: list[dict] = []
        for item in items:
            n = _normalize_directory_item(item)
            if n:
                normalized.append(n)
        logger.info(
            f"[CFP-SCOUT] Directories lane: '{kw}' -> {len(items)} raw items, "
            f"{len(normalized)} normalized (capped at ${per_run_charge_usd:.2f}/run, "
            f"${max_charge_usd:.2f} total across all keywords)"
        )
        if items and not normalized:
            # Every raw item failed to normalize — almost certainly a field-name
            # mismatch against the actor's real (vs. documented) output shape.
            # Log the first item's top-level keys so this is diagnosable from
            # logs alone, without needing to reproduce locally.
            logger.warning(
                f"[CFP-SCOUT] Directories lane: got {len(items)} raw items for '{kw}' but "
                f"none normalized — first item's keys: {sorted(items[0].keys())}"
            )
        return normalized

    # Each keyword hits Apify independently — running them one at a time was
    # pure wasted wall-clock time (up to 3x the actual work for no reason).
    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=max(1, len(keyword_inputs))) as executor:
        for normalized in executor.map(_run_one, keyword_inputs):
            results.extend(normalized)

    return results


def _process_directory_item(
    item: dict,
    min_days_out: int = _DEFAULT_MIN_DAYS_OUT,
    max_days_out: int = _DEFAULT_MAX_DAYS_OUT,
) -> Optional[dict]:
    """Directory items already carry name/date/location — verify + normalize.

    Only falls back to scrape_page() when the directory source's own data is
    sparse. Confirmed live: 10times.com returns 403 Forbidden on every
    generic scrape attempt (anti-bot), so for that source this was a wasted
    round trip on every single item, adding real latency for zero signal —
    has_cfp/pay always came back empty regardless. When Apify already gave
    us location + description, there's nothing useful left to scrape for.
    """
    url = item["url"]
    needs_scrape = not (item.get("location_raw") and item.get("description"))
    scraped = scrape_page(url) if needs_scrape else None
    # The directory source's own date (e.g. 10times' startDate) is a
    # structured API field, not scraped free text. Was previously run through
    # the free-text regex parser, which only handles bare "YYYY-MM-DD" /
    # "Month D, YYYY" — a real ISO datetime with a time component (very
    # plausible from a paid API) would silently fail to parse there. Route
    # it through the ISO-first parser instead, same as JSON-LD dates.
    structured_date_raw = item.get("date_raw", "")
    scraped_date_raw = scraped.get("event_date_raw", "") if scraped else ""
    location = item.get("location_raw") or (scraped.get("location") if scraped else "")
    description = item.get("description") or (scraped.get("description") if scraped else "")
    full_text = scraped.get("full_text", "") if scraped else ""

    if _detect_cfp_closed(full_text):
        return None

    event_date, date_confidence = _resolve_event_date(
        text_date_raw=scraped_date_raw,
        jsonld_start_date=structured_date_raw,
    )
    window_status = _classify_window(event_date, min_days_out, max_days_out)
    if window_status in ("expired", "too_far_out", "too_soon"):
        return None

    has_cfp = bool(scraped and scraped.get("has_cfp"))
    contact = _extract_contact_signals(scraped, url)
    return {
        "name": item.get("name") or _domain_of(url),
        "url": url,
        "lane": "directories",
        "found_via": "Apify Directory",
        "found_at": _domain_of(url),
        "cfp_status": "Open — Call for Speakers" if has_cfp else "Unknown",
        "description": description,
        "location": location,
        "event_date": event_date.isoformat() if event_date else "",
        "event_date_raw": structured_date_raw or scraped_date_raw,
        "date_confidence": date_confidence,
        "window_status": window_status,
        "pay": "Compensation mentioned" if (scraped and scraped.get("mentions_payment")) else "",
        "contact_email": contact["contact_email"],
        "contact_name": contact["contact_name"],
        "contact_role": contact.get("contact_role", ""),
        "contact_source": contact["contact_source"],
        "submission_form_url": contact["submission_form_url"],
        "event_type": item.get("event_type", ""),
        "event_format": _infer_event_format(location),
        "venue_name": item.get("venue_name", ""),
        # 10times already gives us a verified promoter (organizer.name) —
        # only fall back to the text-pattern guess for actors that don't.
        "promoter_name": item.get("promoter_name") or _extract_organizer_from_text(full_text),
        "promoter_website": item.get("promoter_website", ""),
        "_full_text": contact["_full_text"],
    }


# ── Confs.tech lane — free, keyless, tech-topic-only ─────────────────────────
# Verified against github.com/tech-conferences/conference-data: entries live
# at conferences/{year}/{topic}.json, one file per tech topic. No API key,
# no rate limit beyond GitHub's raw-content CDN. Only fires when a keyword
# matches a known topic, so it's a no-op (zero requests) for niches like
# healthcare — exactly the "only when needed" behavior asked for.

_CONFS_TECH_BASE_URL = "https://raw.githubusercontent.com/tech-conferences/conference-data/main/conferences"

# Verified topic filenames (conferences/2026/*.json) as of this writing —
# the actual repo is the source of truth; a topic missing here just means
# that keyword won't trigger this lane, it won't error.
_CONFS_TECH_TOPICS = {
    "android", "accessibility", "api", "clojure", "cpp", "css", "data", "devops",
    "dotnet", "graphql", "groovy", "ios", "iot", "java", "javascript", "kotlin",
    "leadership", "networking", "opensource", "performance", "php", "product",
    "python", "rust", "security", "sre", "testing", "typescript", "ux", "general",
}


# Exact-word aliases only — deliberately not fuzzy/substring matching, since
# that produced false collisions (e.g. "javascript" also matching "java").
_CONFS_TECH_ALIASES = {"js": "javascript", "ts": "typescript"}


def _confs_tech_topics_for_keywords(keywords: list[str]) -> list[str]:
    matched: list[str] = []
    for kw in keywords:
        for word in re.findall(r"[a-zA-Z]+", kw.lower()):
            topic = word if word in _CONFS_TECH_TOPICS else _CONFS_TECH_ALIASES.get(word)
            if topic and topic not in matched:
                matched.append(topic)
    return matched


def _fetch_confs_tech_topic(year: int, topic: str) -> list[dict]:
    try:
        resp = requests.get(f"{_CONFS_TECH_BASE_URL}/{year}/{topic}.json", timeout=8)
        if resp.status_code != 200:
            return []
        data = resp.json()
        return data if isinstance(data, list) else []
    except Exception as exc:
        logger.debug(f"[CFP-SCOUT] confs.tech fetch failed for {topic}/{year}: {exc}")
        return []


def _process_confs_tech_entry(
    entry: dict,
    topic: str,
    min_days_out: int = _DEFAULT_MIN_DAYS_OUT,
    max_days_out: int = _DEFAULT_MAX_DAYS_OUT,
) -> Optional[dict]:
    name = entry.get("name", "")
    url = entry.get("url", "")
    if not name or not url:
        return None

    event_date = _parse_iso_date(entry.get("startDate", ""))
    window_status = _classify_window(event_date, min_days_out, max_days_out)
    if window_status in ("expired", "too_far_out", "too_soon"):
        return None

    cfp_end = _parse_iso_date(entry.get("cfpEndDate", ""))
    if cfp_end and cfp_end < date.today():
        return None  # CFP deadline confirmed passed — not active

    cfp_status = (
        "Open — Call for Speakers"
        if entry.get("cfpUrl") and (cfp_end is None or cfp_end >= date.today())
        else "Unknown"
    )
    location = ", ".join(p for p in (entry.get("city", ""), entry.get("country", "")) if p)

    return {
        "name": name,
        "url": url if url.startswith("http") else f"https://{url}",
        "lane": "confs_tech",
        "found_via": "Confs.tech",
        "found_at": _domain_of(url),
        "cfp_status": cfp_status,
        "description": f"Topic: {topic}",
        "location": location,
        "event_date": event_date.isoformat() if event_date else "",
        "event_date_raw": entry.get("startDate", ""),
        "date_confidence": "structured" if event_date else "unknown",
        "window_status": window_status,
        "pay": "",
        "contact_email": "",
        "contact_name": "",
        "contact_role": "",
        "contact_source": "",
        # Confs.tech's own cfpUrl field is literally the submission page —
        # no scraping needed, it's already structured data.
        "submission_form_url": entry.get("cfpUrl", ""),
        # No page is scraped for this lane, so there's no text for the AI
        # enrichment stage to work with — it'll correctly skip these.
        "_full_text": "",
        # Confs.tech is conference listings only — no tradeshow distinction
        # and no organizer/venue data in its schema.
        "event_type": "Conference",
        "event_format": _infer_event_format(location),
        "venue_name": "",
        "promoter_name": "",
        "promoter_website": "",
    }


def _search_confs_tech(
    keywords: list[str],
    min_days_out: int = _DEFAULT_MIN_DAYS_OUT,
    max_days_out: int = _DEFAULT_MAX_DAYS_OUT,
) -> list[dict]:
    """Free discovery lane against the open-source Confs.tech dataset.

    Only fires for keywords matching a known tech topic — zero network
    calls otherwise, so it's safe to always include in the pipeline.
    """
    topics = _confs_tech_topics_for_keywords(keywords)
    if not topics:
        return []

    this_year = date.today().year
    results: list[dict] = []
    for topic in topics:
        for year in (this_year, this_year + 1):
            for entry in _fetch_confs_tech_topic(year, topic):
                processed = _process_confs_tech_entry(entry, topic, min_days_out, max_days_out)
                if processed:
                    results.append(processed)
    logger.info(f"[CFP-SCOUT] Confs.tech: topics={topics} -> {len(results)} in-window results")
    return results


# ── Contact / submission-form extraction ─────────────────────────────────────
# scrape_page() already returns 'emails' and 'raw_links' (both existing,
# unmodified fields) — this just reads them for a speaker-relevant signal.
# Its own 'guest_form_url' is podcast-flavored (pitch/be-a-guest/calendly)
# and won't catch a CFP's "Apply to Speak" / "Submit a Proposal" link, so we
# scan raw_links separately with CFP-specific keywords.

_SUBMISSION_FORM_KEYWORDS = (
    "cfp", "call-for-speakers", "callforspeakers", "speaker-application",
    "speakerapplication", "apply-to-speak", "applytospeak", "submit-a-proposal",
    "submit-proposal", "speaker-form", "speakerform", "become-a-speaker",
    "typeform.com", "forms.gle", "docs.google.com/forms", "jotform",
    "submittable.com", "papercall.io", "sessionize.com",
)

# Contact-info waterfall, cheapest/free first:
#   1. Emails already in the page's visible text (existing)
#   2. mailto: links — a real gap before this: a link like
#      <a href="mailto:speakers@x.com">Contact Us</a> has no email in its
#      visible text at all, so stage 1 alone silently missed it.
#   3. One likely contact/about page on the same domain, re-running 1+2 on it
#   4. AI extraction (see _ai_extract_contact) — last resort, capped, and
#      only ever run on the final result set, not every raw candidate.

_MAILTO_RE = re.compile(r"^mailto:([^?]+)", re.I)
_EMAIL_FORMAT_RE = re.compile(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$")
_CONTACT_PAGE_KEYWORDS = ("contact", "about", "team", "speakers")


def _extract_mailto_email(raw_links: list[str]) -> str:
    for href in raw_links or []:
        m = _MAILTO_RE.match(href.strip())
        if not m:
            continue
        candidate = m.group(1).strip()
        if _EMAIL_FORMAT_RE.match(candidate):
            return candidate
    return ""


def _emails_from_scrape(scraped: Optional[dict]) -> str:
    """Stages 1+2 of the waterfall against one already-scraped page."""
    if not scraped:
        return ""
    emails = scraped.get("emails") or []
    if emails:
        return emails[0]
    return _extract_mailto_email(scraped.get("raw_links") or [])


# ── Coordinator name/role — free-stage best-effort, not just an email ───────
# Previously contact_name was only ever set by the paid AI stage; these are
# the same free signals already being scraped, just not read for a name.

_GENERIC_LINK_TEXT = {
    "email", "email us", "contact", "contact us", "here", "click here",
    "info", "send email", "mail", "write to us", "get in touch", "contact me",
}

# Real names essentially never start with an imperative verb — this catches
# CTA-style mailto anchor text ("Submit Request", "Email Now", "Get In
# Touch") that the word-count/capitalization check alone lets through,
# since CTAs are often capitalized multi-word phrases too. Confirmed live:
# "Submit Request" on papercall.io passed the old heuristic.
_CTA_FIRST_WORDS = {
    "submit", "contact", "email", "click", "apply", "request", "get",
    "join", "register", "send", "learn", "view", "see", "read", "book",
    "buy", "sign", "download", "subscribe", "reserve", "ask", "reach",
    "write", "say", "drop", "message", "ping", "talk", "connect",
    "follow", "visit", "start", "try", "shop", "order", "call", "chat",
    "find", "explore", "discover", "watch", "listen", "share",
}


def _looks_like_person_name(text: str) -> bool:
    text = (text or "").strip()
    if not text or "@" in text or len(text) > 60:
        return False
    if text.lower() in _GENERIC_LINK_TEXT:
        return False
    words = text.split()
    if not (2 <= len(words) <= 4):
        return False
    if words[0].lower() in _CTA_FIRST_WORDS:
        return False
    return all(w[0].isupper() for w in words if w[:1].isalpha())


# Best-effort plain-text pattern for "Program Chair: Jane Doe" style
# credits — not verified structured data, same spirit as
# _extract_organizer_from_text.
_COORDINATOR_ROLE_TERMS = (
    "program committee chair", "speaker coordinator", "conference coordinator",
    "event coordinator", "program chair", "speaker chair", "cfp chair",
    "content chair", "program director",
)
_NAME_AFTER_ROLE_RE = re.compile(r"^\s*[:,\-–]?\s*([A-Z][\w.'-]*(?:\s+[A-Z][\w.'-]*){0,3})")


def _extract_role_contact(full_text: str) -> tuple[str, str]:
    """Best-effort (name, role) from patterns like 'Program Chair: Jane Doe'."""
    if not full_text:
        return "", ""
    lowered = full_text.lower()
    for term in _COORDINATOR_ROLE_TERMS:
        idx = lowered.find(term)
        if idx == -1:
            continue
        after = full_text[idx + len(term): idx + len(term) + 80]
        m = _NAME_AFTER_ROLE_RE.match(after)
        if m and _looks_like_person_name(m.group(1)):
            return m.group(1).strip(), full_text[idx: idx + len(term)]
    return "", ""


def _resolve_contact_name(
    original_scraped: Optional[dict],
    contact_page_scraped: Optional[dict],
    contact_email: str,
) -> tuple[str, str]:
    """Best-effort (name, role) for whichever email the waterfall already
    found — never invents a name, only reads it from mailto anchor text or
    a nearby role-title pattern already present in the scraped text."""
    for source in (contact_page_scraped, original_scraped):
        if not source:
            continue
        for contact in source.get("mailto_contacts") or []:
            if contact.get("email", "").strip().lower() == (contact_email or "").strip().lower():
                if _looks_like_person_name(contact.get("name", "")):
                    return contact["name"].strip(), ""
    for source in (original_scraped, contact_page_scraped):
        if not source:
            continue
        name, role = _extract_role_contact(source.get("full_text", ""))
        if name:
            return name, role
    return "", ""


def _find_contact_page_url(base_url: str, raw_links: list[str]) -> str:
    """Best candidate contact/about/team page, or '' if none look promising."""
    best_rank = None
    best_href = ""
    for href in raw_links or []:
        href_lower = href.lower()
        for rank, kw in enumerate(_CONTACT_PAGE_KEYWORDS):
            if kw in href_lower and (best_rank is None or rank < best_rank):
                best_rank, best_href = rank, href
                break
    return urljoin(base_url, best_href) if best_href else ""


def _extract_contact_signals(scraped: Optional[dict], page_url: str) -> dict:
    """Best-effort contact email + speaker-submission-form URL from a scrape.

    Fields are empty (not missing) when nothing is found — never guesses or
    invents a value. Also returns _full_text: an internal scratch field for
    the later AI-enrichment stage, stripped before results are returned from
    run_cfp_discovery().
    """
    if not scraped:
        return {
            "contact_email": "", "contact_name": "", "contact_role": "",
            "contact_source": "", "submission_form_url": "", "_full_text": "",
        }

    contact_email = _emails_from_scrape(scraped)
    contact_source = "scraped" if contact_email else ""
    contact_page_scraped: Optional[dict] = None

    if not contact_email:
        contact_page_url = _find_contact_page_url(page_url, scraped.get("raw_links") or [])
        if contact_page_url and contact_page_url != page_url:
            contact_page_scraped = scrape_page(contact_page_url)
            contact_email = _emails_from_scrape(contact_page_scraped)
            if contact_email:
                contact_source = "scraped"

    contact_name, contact_role = _resolve_contact_name(scraped, contact_page_scraped, contact_email)

    submission_form_url = ""
    for href in scraped.get("raw_links") or []:
        href_lower = href.lower()
        if any(kw in href_lower for kw in _SUBMISSION_FORM_KEYWORDS):
            submission_form_url = urljoin(page_url, href)
            break
    if not submission_form_url and scraped.get("guest_form_url"):
        # Not CFP-specific, but still a real application/contact link found
        # on the page — better than nothing.
        submission_form_url = urljoin(page_url, scraped["guest_form_url"])

    return {
        "contact_email": contact_email,
        "contact_name": contact_name,
        "contact_role": contact_role,
        "contact_source": contact_source,
        "submission_form_url": submission_form_url,
        "_full_text": scraped.get("full_text", ""),
    }


# ── AI-assisted contact extraction (Claude, last resort, capped) ────────────
# Only ever called on the FINAL result set (post-dedup, post-window-filter —
# see _enrich_missing_contacts), never on the raw candidate pool, and only
# for results the free waterfall stages above already failed on. Bounded by
# CFP_AI_ENRICHMENT_MAX_CALLS so cost stays predictable regardless of how
# many results come back, same spirit as the Apify $ cap.

_AI_CONTACT_PROMPT = """You are extracting contact information from scraped text of a conference/event web page. Find a real, specific contact email address, or a named contact person (e.g. "program chair", "speaker coordinator"), for who to reach out to about speaking at this event.

Rules:
- Only report information that is ACTUALLY present in the text below, quoted exactly as written. Never invent, guess, or construct an email address or name.
- If no real contact email or named contact person is present, say NONE for that field.

Page text:
\"\"\"
{text}
\"\"\"

Respond in exactly this format, nothing else:
EMAIL: <email or NONE>
CONTACT_NAME: <name or NONE>"""

_AI_MODEL = "claude-haiku-4-5"


def _ai_extract_contact(full_text: str, api_key: str) -> dict:
    """Best-effort contact extraction via Claude. Returns {} on failure or nothing found."""
    if not full_text or not api_key:
        return {}
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model=_AI_MODEL,
            max_tokens=100,
            messages=[{"role": "user", "content": _AI_CONTACT_PROMPT.format(text=full_text[:2000])}],
        )
        raw = response.content[0].text.strip()
        email, name = "", ""
        for line in raw.splitlines():
            if line.upper().startswith("EMAIL:"):
                val = line.split(":", 1)[1].strip()
                if val and val.upper() != "NONE":
                    email = val
            elif line.upper().startswith("CONTACT_NAME:"):
                val = line.split(":", 1)[1].strip()
                if val and val.upper() != "NONE":
                    name = val
        # Anti-hallucination guard: an extracted email must actually appear
        # verbatim in the source text, not just look well-formed. The prompt
        # instructs this already — this is a hard check on top of it.
        if email and (not _EMAIL_FORMAT_RE.match(email) or email.lower() not in full_text.lower()):
            logger.warning(f"[CFP-SCOUT] AI returned an email not present in source text — discarding: {email}")
            email = ""
        return {"contact_email": email, "contact_name": name}
    except Exception as exc:
        logger.warning(f"[CFP-SCOUT] AI contact extraction failed: {exc}")
        return {}


def _enrich_missing_contacts(results: list[dict]) -> list[dict]:
    """Run the AI stage on final results still missing a contact email.

    No-op if CLAUDE_API_KEY isn't set, or CFP_AI_ENRICHMENT_MAX_CALLS is 0.
    """
    api_key = os.getenv("CLAUDE_API_KEY", "") or os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        return results
    max_calls = max(0, int(os.getenv("CFP_AI_ENRICHMENT_MAX_CALLS", "20")))
    if max_calls == 0:
        return results

    candidates = [r for r in results if not r.get("contact_email") and r.get("_full_text")][:max_calls]
    if not candidates:
        return results

    def _enrich_one(r: dict) -> None:
        ai_result = _ai_extract_contact(r["_full_text"], api_key)
        if ai_result.get("contact_email"):
            r["contact_email"] = ai_result["contact_email"]
            r["contact_source"] = "ai"
        if ai_result.get("contact_name"):
            r["contact_name"] = ai_result["contact_name"]
            r.setdefault("contact_source", "ai")

    with ThreadPoolExecutor(max_workers=min(8, len(candidates))) as executor:
        list(executor.map(_enrich_one, candidates))

    logger.info(f"[CFP-SCOUT] AI contact enrichment: {len(candidates)} call(s) made")
    return results


# ── Shared helpers ───────────────────────────────────────────────────────────

def _resolve_event_date(text_date_raw: str, jsonld_start_date: str) -> tuple[Optional[date], str]:
    """Prefer structured (JSON-LD) date over free-text regex; return (date, confidence)."""
    if jsonld_start_date:
        parsed = _parse_iso_date(jsonld_start_date)
        if parsed:
            return parsed, "structured"
    if text_date_raw:
        parsed = _parse_loose_date(text_date_raw)
        if parsed:
            return parsed, "text"
    return None, "unknown"


def _dedupe_by_domain(results: list[dict]) -> list[dict]:
    """Dedupe events by official domain; dedupe directory/confs_tech items by full URL.

    Confirmed a real bug: every result from an aggregator source (10times,
    Confs.tech) shares that aggregator's domain (e.g. every 10times event
    lives under 10times.com) — domain-based dedup was collapsing dozens of
    genuinely distinct conferences down to one. Domain is still the right
    key for the events lane, where the goal is catching the same
    conference's *own* official site found twice via different search
    backends.
    """
    by_key: dict[str, dict] = {}
    order: list[str] = []
    for r in results:
        key = r["found_at"] if r.get("lane") == "events" else r["url"]
        if key not in by_key:
            by_key[key] = r
            order.append(key)
        elif by_key[key].get("date_confidence") == "unknown" and r.get("date_confidence") != "unknown":
            by_key[key] = r
    return [by_key[key] for key in order]


def _process_event_url(
    item: tuple,
    min_days_out: int = _DEFAULT_MIN_DAYS_OUT,
    max_days_out: int = _DEFAULT_MAX_DAYS_OUT,
) -> Optional[dict]:
    url, source_backend = item
    scraped = scrape_page(url)
    if not scraped:
        return None

    if _detect_cfp_closed(scraped.get("full_text", "")):
        return None

    jsonld = _fetch_jsonld_event(url)
    event_date, date_confidence = _resolve_event_date(
        text_date_raw=scraped.get("event_date_raw", ""),
        jsonld_start_date=jsonld.get("start_date", ""),
    )
    window_status = _classify_window(event_date, min_days_out, max_days_out)
    if window_status in ("expired", "too_far_out", "too_soon"):
        return None

    pay = jsonld.get("price", "") or ("Compensation mentioned" if scraped.get("mentions_payment") else "")
    contact = _extract_contact_signals(scraped, url)
    location = jsonld.get("location_name") or scraped.get("location", "")
    promoter_name = jsonld.get("organizer_name", "") or _extract_organizer_from_text(scraped.get("full_text", ""))

    return {
        "name": scraped.get("title") or _domain_of(url),
        "url": url if url.startswith("http") else f"https://{url}",
        "lane": "events",
        "found_via": source_backend,
        "found_at": _domain_of(url),
        "cfp_status": "Open — Call for Speakers" if scraped.get("has_cfp") else "Unknown",
        "description": scraped.get("description", ""),
        "location": location,
        "event_date": event_date.isoformat() if event_date else "",
        "event_date_raw": jsonld.get("start_date") or scraped.get("event_date_raw", ""),
        "date_confidence": date_confidence,
        "window_status": window_status,
        "pay": pay,
        "contact_email": contact["contact_email"],
        "contact_name": contact["contact_name"],
        "contact_role": contact.get("contact_role", ""),
        "contact_source": contact["contact_source"],
        "submission_form_url": contact["submission_form_url"],
        "_full_text": contact["_full_text"],
        # Our search queries are all conference-flavored ("call for
        # speakers" conference, etc.) — no reliable Tradeshow/Workshop
        # signal exists for this lane the way 10times' own `type` field
        # gives us one, so this is an inference, not verified data.
        "event_type": "Conference",
        "event_format": _infer_event_format(location, jsonld.get("attendance_mode", "")),
        "venue_name": jsonld.get("venue_name", ""),
        "promoter_name": promoter_name,
        # Only trust a promoter website from structured JSON-LD data — the
        # text-pattern fallback only ever gives us a name, never a URL, and
        # guessing one (e.g. from the event's own domain) would be wrong
        # whenever the promoter runs multiple events under other domains.
        "promoter_website": jsonld.get("organizer_url", ""),
    }


def _matches_exclusion(result: dict, exclusions: list[str]) -> bool:
    """True if any exclusion term appears (case-insensitive) in the result's
    name, description, promoter, or domain."""
    haystack = " ".join([
        result.get("name", ""),
        result.get("description", ""),
        result.get("promoter_name", ""),
        result.get("found_at", ""),
    ]).lower()
    return any(term.lower() in haystack for term in exclusions if term.strip())


def run_cfp_discovery(
    keywords: list[str],
    max_results: int = HARD_MAX_RESULTS,
    geography: str = "",
    country_code: str = "WW",
    min_days_out: int = _DEFAULT_MIN_DAYS_OUT,
    max_days_out: int = _DEFAULT_MAX_DAYS_OUT,
    event_formats: Optional[list[str]] = None,
    exclusions: Optional[list[str]] = None,
) -> dict:
    """Search + lightly scrape for active conferences/CFPs matching keywords.

    Filters out CFPs detected as closed and events outside the active date
    window (expired, too soon, or beyond max_days_out — defaults to 6-12
    months out). Events with no confidently-parsed date are kept
    (window_status='unknown') rather than silently dropped, since date
    extraction is best-effort. `event_formats`, if given, filters similarly
    without dropping results whose format couldn't be inferred. `exclusions`
    drops any result whose name/description/promoter/domain matches a term.

    This is Phase 1 (discovery) only — profile fields (geography, date
    window, formats, exclusions) shape which candidates get found and kept,
    but nothing here scores, ranks, or weights results against the profile.

    Hard-capped at HARD_MAX_RESULTS regardless of the requested max_results.
    Read-only: no database writes of any kind happen here.
    """
    max_results = max(1, min(int(max_results or HARD_MAX_RESULTS), HARD_MAX_RESULTS))
    event_formats = [f.strip().lower() for f in (event_formats or []) if f and f.strip()]
    exclusions = [e for e in (exclusions or []) if e and e.strip()]
    queries = _build_queries(keywords, geography=geography)
    logger.info(f"[CFP-SCOUT] Running {len(queries)} queries for keywords={keywords}")

    # The three discovery lanes are fully independent — they used to run
    # strictly sequentially (web search, then wait for the directories lane's
    # Apify run to fully poll to completion — anywhere from 45s to several
    # minutes — then Confs.tech), so total time was the SUM of all three.
    # Running them concurrently makes it the MAX of the three instead, which
    # is the single biggest lever on wall-clock time for a scout run.
    search_directories_bound = partial(
        _search_directories, country=country_code, min_days_out=min_days_out, max_days_out=max_days_out,
    )
    search_confs_tech_bound = partial(
        _search_confs_tech, min_days_out=min_days_out, max_days_out=max_days_out,
    )
    with ThreadPoolExecutor(max_workers=3) as discovery_executor:
        web_search_future = discovery_executor.submit(web_search, queries, results_per_query=15, delay=0.8)
        directories_future = discovery_executor.submit(search_directories_bound, keywords, max_results)
        confs_tech_future = discovery_executor.submit(search_confs_tech_bound, keywords)

        url_sources = web_search_future.result()
        directory_items = directories_future.result()
        confs_tech_results = confs_tech_future.result()  # already fully processed, no scrape needed

    logger.info(f"[CFP-SCOUT] {len(url_sources)} unique URLs discovered")

    # Scrape a bounded candidate pool — more than max_results since the date/
    # CFP-closed filters now drop some candidates that used to be kept.
    # Interleaved by backend first so an early, high-volume backend (e.g.
    # Tavily) can't crowd out a later one (e.g. Exa) before the cap hits.
    event_candidates = _interleave_by_source(url_sources)[: max_results * 2]

    raw_results: list[dict] = list(confs_tech_results)
    lock = threading.Lock()

    process_event_url_bound = partial(_process_event_url, min_days_out=min_days_out, max_days_out=max_days_out)
    process_directory_item_bound = partial(
        _process_directory_item, min_days_out=min_days_out, max_days_out=max_days_out,
    )
    with ThreadPoolExecutor(max_workers=_SCRAPE_WORKERS) as executor:
        futures = {executor.submit(process_event_url_bound, item): item for item in event_candidates}
        futures.update(
            {executor.submit(process_directory_item_bound, item): item for item in directory_items}
        )
        for future in as_completed(futures):
            try:
                res = future.result()
            except Exception as exc:
                logger.warning(f"[CFP-SCOUT] Candidate processing failed: {exc}")
                continue
            if res:
                with lock:
                    raw_results.append(res)

    if event_formats:
        raw_results = [
            r for r in raw_results
            if r.get("event_format") in ("unknown", "") or r.get("event_format") in event_formats
        ]
    if exclusions:
        raw_results = [r for r in raw_results if not _matches_exclusion(r, exclusions)]

    deduped = _dedupe_by_domain(raw_results)
    final = deduped[:max_results]

    # AI enrichment is a last resort, run only here — on the final result
    # set, after dedup and truncation — never on the raw candidate pool.
    # No-op if CLAUDE_API_KEY isn't set.
    final = _enrich_missing_contacts(final)

    # _full_text was internal scratch data for the AI stage above; never
    # meant to reach the API response.
    for r in final:
        r.pop("_full_text", None)

    return {
        "query_count": len(queries),
        "urls_found": len(url_sources),
        "directory_items_found": len(directory_items),
        "confs_tech_items_found": len(confs_tech_results),
        "candidates_scraped": len(event_candidates) + len(directory_items),
        "results_count": len(final),
        "results": final,
    }
