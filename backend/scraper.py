"""Web search + page scraping — standalone.

Ported from speakeragent-api's src/agent/scraper.py: only the two
functions the CFP scout actually uses (web_search, scrape_page) and their
real dependencies, nothing else (no podcast-specific helpers, no profile-
based query generation). This file has zero dependency on the original
repo — it's a straight copy of the working implementation so this project
can run standalone.
"""

import logging
import os
import re
import time
from typing import Optional

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

SKIP_DOMAINS = {
    'linkedin.com', 'facebook.com', 'twitter.com', 'x.com',
    'instagram.com', 'youtube.com', 'indeed.com', 'glassdoor.com',
    'ziprecruiter.com', 'monster.com', 'reddit.com', 'pinterest.com',
    'tiktok.com', 'amazon.com', 'ebay.com',
    'deezer.com', 'spotify.com', 'podcasts.apple.com', 'music.apple.com',
    'stitcher.com', 'iheart.com', 'iheartradio.com', 'tunein.com',
    'podcastaddict.com', 'castbox.fm', 'overcast.fm', 'pocketcasts.com',
    'anchor.fm', 'audible.com', 'pandora.com', 'soundcloud.com',
}

SKIP_EXTENSIONS = {'.pdf', '.doc', '.docx', '.ppt', '.pptx', '.xls', '.xlsx'}

EMAIL_RE = re.compile(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}')
DATE_RE = re.compile(
    r'(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|'
    r'Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|'
    r'Dec(?:ember)?)\s+\d{1,2}(?:\s*[-–]\s*\d{1,2})?\s*,?\s*\d{4}',
    re.IGNORECASE
)
LOCATION_RE = re.compile(
    r'([A-Z][a-z]+(?:\s[A-Z][a-z]+)*),\s*'
    r'([A-Z]{2}|[A-Z][a-z]+(?:\s[A-Z][a-z]+)*)'
)


def should_skip_url(url: str) -> bool:
    """Check if URL should be skipped."""
    from urllib.parse import urlparse
    parsed = urlparse(url)
    domain = parsed.netloc.lower().replace('www.', '')
    if domain in SKIP_DOMAINS:
        return True
    path = parsed.path.lower()
    for ext in SKIP_EXTENSIONS:
        if path.endswith(ext):
            return True
    return False


def _firecrawl_scrape(url: str, api_key: str, timeout: int = 20) -> Optional[dict]:
    """Scrape a page via Firecrawl API — handles JS-rendered pages, returns clean markdown.

    Falls back to BeautifulSoup scraper if this fails or key is not set.
    """
    try:
        resp = requests.post(
            'https://api.firecrawl.dev/v1/scrape',
            headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'},
            json={'url': url, 'formats': ['markdown']},
            timeout=timeout,
        )
        if resp.status_code != 200:
            logger.warning(f"[FIRECRAWL] {resp.status_code} for {url}")
            return None

        data = resp.json().get('data', {})
        if not data:
            return None

        markdown = data.get('markdown', '')
        metadata = data.get('metadata', {})
        if not markdown:
            return None

        title = metadata.get('title', '') or metadata.get('ogTitle', '')
        if not title:
            h1_match = re.search(r'^#\s+(.+)$', markdown, re.MULTILINE)
            if h1_match:
                title = h1_match.group(1).strip()

        full_text = re.sub(r'\[([^\]]+)\]\([^\)]+\)', r'\1', markdown)
        full_text = re.sub(r'[#*`_~>]', ' ', full_text)
        full_text = re.sub(r'\s+', ' ', full_text).strip()
        full_text_trimmed = full_text[:2000]
        text_lower = full_text_trimmed.lower()

        description = (
            metadata.get('description', '')
            or metadata.get('ogDescription', '')
            or metadata.get('og:description', '')
        )
        if not description:
            paras = [p.strip() for p in markdown.split('\n\n') if len(p.strip()) > 50]
            if paras:
                description = re.sub(r'[#*`_~>\[\]]', '', paras[0])[:500]

        dates_found = DATE_RE.findall(full_text)
        event_date_str = dates_found[0] if dates_found else ''

        location = ''
        if 'virtual' in text_lower or 'online' in text_lower:
            location = 'Virtual'
        else:
            loc_matches = LOCATION_RE.findall(full_text)
            if loc_matches:
                location = f"{loc_matches[0][0]}, {loc_matches[0][1]}"

        emails = list(set(EMAIL_RE.findall(full_text)))
        emails = [
            e for e in emails
            if not any(x in e.lower() for x in ['noreply', 'no-reply', 'example.com', 'sentry'])
        ]

        linkedin_re = re.compile(r'https?://(?:www\.)?linkedin\.com/in/[A-Za-z0-9_%-]+/?')
        linkedin_links = list(set(linkedin_re.findall(markdown)))

        twitter_re = re.compile(r'https?://(?:www\.)?(?:twitter|x)\.com/[A-Za-z0-9_]+/?')
        twitter_links = [
            u for u in twitter_re.findall(markdown)
            if '/share' not in u and '/intent' not in u
        ]
        twitter_links = list(set(twitter_links))

        guest_form_url = ''
        form_keywords = ['pitch', 'be-a-guest', 'be_a_guest', 'guest-form',
                         'guest_form', 'guest-application', 'typeform', 'calendly']
        all_md_links = re.findall(r'\[([^\]]*)\]\(([^\)]+)\)', markdown)
        raw_links = [link_url for _, link_url in all_md_links]
        for _, link_url in all_md_links:
            if any(kw in link_url.lower() for kw in form_keywords):
                guest_form_url = link_url
                break

        cfp_keywords = [
            'call for speakers', 'call for proposals', 'submit a talk',
            'speaker application', 'speaker submission', 'become a speaker',
            'speaker registration', 'call for abstracts', 'submit abstract',
            'call for presentations',
        ]
        pay_keywords = [
            'honorarium', 'speaker fee', 'compensation',
            'paid speaker', 'speaker stipend', 'travel reimbursement', 'speaker payment',
        ]
        no_pay_keywords = ['volunteer speaker', 'unpaid', 'no compensation', 'pro bono']

        return {
            'url': url,
            'title': title[:200] if title else '',
            'description': description[:500] if description else '',
            'event_date_raw': event_date_str,
            'location': location,
            'emails': emails[:5],
            'linkedin_links': linkedin_links[:3],
            'twitter_links': twitter_links[:3],
            'has_cfp': any(kw in text_lower for kw in cfp_keywords),
            'mentions_payment': any(kw in text_lower for kw in pay_keywords),
            'mentions_no_payment': any(kw in text_lower for kw in no_pay_keywords),
            'guest_form_url': guest_form_url,
            'full_text': full_text_trimmed,
            'raw_links': raw_links[:50],
            'scrape_backend': 'Firecrawl',
        }
    except Exception as e:
        logger.warning(f"[FIRECRAWL] Scrape failed for {url}: {e}")
        return None


def scrape_page(url: str, timeout: int = 10) -> Optional[dict]:
    """Scrape a conference/event page and extract structured data.

    Returns dict with keys:
        url, title, description, dates, location, emails,
        linkedin_links, has_cfp, mentions_payment, full_text
    Returns None on failure.
    """
    if should_skip_url(url):
        logger.debug(f"Skipping URL: {url}")
        return None

    firecrawl_key = os.getenv('FIRECRAWL_API_KEY', '')
    if firecrawl_key:
        result = _firecrawl_scrape(url, firecrawl_key, timeout=timeout + 10)
        if result:
            logger.info(f"[SCRAPE] Firecrawl OK — {url}")
            return result
        logger.warning(f"[SCRAPE] Firecrawl failed -> fallback to BeautifulSoup — {url}")

    try:
        headers = {
            'User-Agent': (
                'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                'AppleWebKit/537.36 (KHTML, like Gecko) '
                'Chrome/120.0.0.0 Safari/537.36'
            )
        }
        resp = requests.get(url, headers=headers, timeout=timeout,
                            allow_redirects=True)
        resp.raise_for_status()
    except Exception as e:
        logger.warning(f"Failed to fetch {url}: {e}")
        return None

    try:
        soup = BeautifulSoup(resp.text, 'html.parser')

        for tag in soup(['script', 'style', 'nav', 'footer', 'header']):
            tag.decompose()

        title = ''
        title_tag = soup.find('title')
        if title_tag:
            title = title_tag.get_text(strip=True)
        if not title:
            h1 = soup.find('h1')
            if h1:
                title = h1.get_text(strip=True)

        full_text = soup.get_text(separator=' ', strip=True)
        full_text_trimmed = full_text[:2000]

        description = ''
        meta_desc = soup.find('meta', attrs={'name': 'description'})
        if meta_desc:
            description = meta_desc.get('content', '')
        if not description:
            og_desc = soup.find('meta', attrs={'property': 'og:description'})
            if og_desc:
                description = og_desc.get('content', '')
        if not description:
            for p in soup.find_all('p'):
                text = p.get_text(strip=True)
                if len(text) > 50:
                    description = text[:500]
                    break

        dates_found = DATE_RE.findall(full_text)
        event_date_str = dates_found[0] if dates_found else ''

        location = ''
        text_lower = full_text.lower()
        if 'virtual' in text_lower or 'online' in text_lower:
            location = 'Virtual'
        else:
            loc_matches = LOCATION_RE.findall(full_text)
            if loc_matches:
                location = f"{loc_matches[0][0]}, {loc_matches[0][1]}"

        emails = list(set(EMAIL_RE.findall(full_text)))
        emails = [
            e for e in emails
            if not any(
                x in e.lower()
                for x in ['noreply', 'no-reply', 'example.com', 'sentry']
            )
        ]

        linkedin_links = []
        twitter_links = []
        guest_form_url = ''
        raw_links = []
        form_keywords = ['pitch', 'be-a-guest', 'be_a_guest', 'guest-form',
                         'guest_form', 'guest-application', 'typeform', 'calendly']
        for a_tag in soup.find_all('a', href=True):
            href = a_tag['href']
            raw_links.append(href)
            if 'linkedin.com/in/' in href:
                linkedin_links.append(href)
            if ('twitter.com/' in href or 'x.com/' in href) and \
                    '/share' not in href and '/intent' not in href:
                twitter_links.append(href)
            if not guest_form_url and any(kw in href.lower() for kw in form_keywords):
                guest_form_url = href
        linkedin_links = list(set(linkedin_links))
        twitter_links = list(set(twitter_links))

        cfp_keywords = [
            'call for speakers', 'call for proposals', 'submit a talk',
            'speaker application', 'speaker submission',
            'become a speaker', 'speaker registration',
            'call for abstracts', 'submit abstract',
            'call for presentations',
        ]
        has_cfp = any(kw in text_lower for kw in cfp_keywords)

        pay_keywords = [
            'honorarium', 'speaker fee', 'compensation',
            'paid speaker', 'speaker stipend', 'travel reimbursement',
            'speaker payment',
        ]
        no_pay_keywords = [
            'volunteer speaker', 'unpaid', 'no compensation',
            'pro bono',
        ]
        mentions_payment = any(kw in text_lower for kw in pay_keywords)
        mentions_no_payment = any(kw in text_lower for kw in no_pay_keywords)

        return {
            'url': url,
            'title': title[:200],
            'description': description[:500],
            'event_date_raw': event_date_str,
            'location': location,
            'emails': emails[:5],
            'linkedin_links': linkedin_links[:3],
            'twitter_links': twitter_links[:3],
            'has_cfp': has_cfp,
            'mentions_payment': mentions_payment,
            'mentions_no_payment': mentions_no_payment,
            'guest_form_url': guest_form_url,
            'full_text': full_text_trimmed,
            'raw_links': raw_links[:50],
            'scrape_backend': 'BeautifulSoup',
        }
    except Exception as e:
        logger.warning(f"Failed to parse {url}: {e}")
        return None


def web_search(queries: list[str],
               results_per_query: int = 5,
               delay: float = 1.0) -> list[tuple[str, str]]:
    """Search the web and collect unique (url, source_backend) pairs.

    Search backends (in priority order, ALL configured ones are used):
    1. Tavily AI Search — requires TAVILY_API_KEY
    2. SerpAPI (Google Search) — requires SERP_API_KEY
    3. Serper (Google Search) — requires SERPER_API_KEY
    4. Exa neural search — requires EXA_API_KEY
    5. Bing scraping — fallback when no API keys
    """
    all_url_sources: list[tuple[str, str]] = []
    seen: set = set()

    tavily_key = os.getenv('TAVILY_API_KEY', '')
    serp_key = os.getenv('SERP_API_KEY', '')
    serper_key = os.getenv('SERPER_API_KEY', '')
    exa_key = os.getenv('EXA_API_KEY', '')

    tagged_results: list[tuple[str, str]] = []
    backends_used: list[str] = []

    if tavily_key:
        logger.info("[SEARCH] Backend: Tavily")
        tagged_results += [(u, 'Tavily') for u in _tavily_search(queries, results_per_query, delay)]
        backends_used.append('Tavily')
    if serp_key:
        logger.info("[SEARCH] Backend: SerpAPI (organic + news + events + jobs)")
        tagged_results += [(u, 'SerpAPI') for u in _serpapi_search(queries, results_per_query, delay)]
        tagged_results += [(u, 'SerpAPI') for u in _serpapi_news_search(queries, min(results_per_query, 5), delay)]
        tagged_results += [(u, 'SerpAPI') for u in _serpapi_events_search(queries, delay)]
        tagged_results += [(u, 'SerpAPI') for u in _serpapi_jobs_search(queries, delay)]
        backends_used.append('SerpAPI')
    if serper_key:
        logger.info("[SEARCH] Backend: Serper (organic + news)")
        tagged_results += [(u, 'Serper') for u in _serper_search(queries, results_per_query, delay)]
        tagged_results += [(u, 'Serper') for u in _serper_news_search(queries, min(results_per_query, 5), delay)]
        backends_used.append('Serper')
    if exa_key:
        logger.info("[SEARCH] Backend: Exa (neural search)")
        tagged_results += [(u, 'Exa') for u in _exa_search(queries, results_per_query, delay)]
        backends_used.append('Exa')
    if not tagged_results:
        logger.info("[SEARCH] Fallback: Bing (no API results from any backend)")
        tagged_results = [(u, 'Bing') for u in _bing_search(queries, results_per_query, delay)]
        backends_used.append('Bing')

    logger.info(f"[SEARCH] Backends used: {', '.join(backends_used)} — {len(tagged_results)} raw results")

    for url, source in tagged_results:
        if url not in seen:
            seen.add(url)
            all_url_sources.append((url, source))

    logger.info(f"[SEARCH] Total unique URLs collected: {len(all_url_sources)}")
    return all_url_sources


def _tavily_search(queries: list[str],
                   results_per_query: int = 10,
                   delay: float = 1.0) -> list[str]:
    """Search via Tavily AI search API. Requires TAVILY_API_KEY env var."""
    tavily_key = os.getenv('TAVILY_API_KEY', '')
    if not tavily_key:
        return []

    urls = []
    seen = set()
    for i, query in enumerate(queries):
        logger.info(f"Tavily [{i+1}/{len(queries)}]: {query}")
        try:
            resp = requests.post(
                'https://api.tavily.com/search',
                json={
                    'api_key': tavily_key,
                    'query': query,
                    'max_results': results_per_query,
                    'search_depth': 'basic',
                    'days': 365,
                },
                timeout=15,
            )
            if resp.status_code != 200:
                logger.warning(f"Tavily {resp.status_code} for: {query}")
                continue
            for r in resp.json().get('results', [])[:results_per_query]:
                url = r.get('url', '')
                if url and url not in seen and not should_skip_url(url):
                    seen.add(url)
                    urls.append(url)
        except Exception as e:
            logger.warning(f"Tavily failed for '{query}': {e}")
        if i < len(queries) - 1:
            time.sleep(delay)

    logger.info(f"Tavily found {len(urls)} unique URLs")
    return urls


def _serpapi_search(queries: list[str],
                    results_per_query: int = 10,
                    delay: float = 1.0) -> list[str]:
    """Search via SerpAPI Google organic. Requires SERP_API_KEY env var."""
    serp_key = os.getenv('SERP_API_KEY', '')
    if not serp_key:
        return []

    urls = []
    seen = set()
    consecutive_429s = 0
    for i, query in enumerate(queries):
        logger.info(f"SerpAPI organic [{i+1}/{len(queries)}]: {query}")
        try:
            resp = requests.get(
                'https://serpapi.com/search.json',
                params={'q': query, 'api_key': serp_key, 'num': results_per_query,
                        'hl': 'en', 'gl': 'us', 'engine': 'google'},
                timeout=10,
            )
            if resp.status_code == 429:
                consecutive_429s += 1
                logger.warning(f"SerpAPI organic {resp.status_code} for: {query}")
                if consecutive_429s >= 2:
                    logger.warning("[SERPAPI] Rate-limited on 2+ consecutive queries — aborting SerpAPI organic")
                    break
                continue
            consecutive_429s = 0
            if resp.status_code != 200:
                logger.warning(f"SerpAPI organic {resp.status_code} for: {query}")
                continue
            for r in resp.json().get('organic_results', [])[:results_per_query]:
                url = r.get('link', '')
                if url and url not in seen and not should_skip_url(url):
                    seen.add(url)
                    urls.append(url)
        except Exception as e:
            logger.warning(f"SerpAPI organic failed for '{query}': {e}")
        if i < len(queries) - 1:
            time.sleep(delay)

    logger.info(f"SerpAPI organic found {len(urls)} unique URLs")
    return urls


def _serpapi_news_search(queries: list[str],
                         results_per_query: int = 5,
                         delay: float = 1.0) -> list[str]:
    """Search via SerpAPI Google News (tbm=nws). Finds recent conference announcements."""
    serp_key = os.getenv('SERP_API_KEY', '')
    if not serp_key:
        return []

    news_queries = [
        q for q in queries
        if any(kw in q.lower() for kw in ['call for', 'conference', 'summit', 'event', 'podcast'])
    ]
    if not news_queries:
        news_queries = queries[:5]
    news_queries = news_queries[:8]

    urls = []
    seen = set()
    consecutive_429s = 0
    for i, query in enumerate(news_queries):
        logger.info(f"SerpAPI news [{i+1}/{len(news_queries)}]: {query}")
        try:
            resp = requests.get(
                'https://serpapi.com/search.json',
                params={'q': query, 'api_key': serp_key, 'tbm': 'nws',
                        'num': results_per_query, 'hl': 'en', 'gl': 'us'},
                timeout=10,
            )
            if resp.status_code == 429:
                consecutive_429s += 1
                logger.warning(f"SerpAPI news {resp.status_code} for: {query}")
                if consecutive_429s >= 2:
                    logger.warning("[SERPAPI] Rate-limited on 2+ consecutive news queries — aborting")
                    break
                continue
            consecutive_429s = 0
            if resp.status_code != 200:
                logger.warning(f"SerpAPI news {resp.status_code} for: {query}")
                continue
            for r in resp.json().get('news_results', [])[:results_per_query]:
                url = r.get('link', '')
                if url and url not in seen and not should_skip_url(url):
                    seen.add(url)
                    urls.append(url)
        except Exception as e:
            logger.warning(f"SerpAPI news failed for '{query}': {e}")
        if i < len(news_queries) - 1:
            time.sleep(delay)

    logger.info(f"SerpAPI news found {len(urls)} unique URLs")
    return urls


def _serpapi_events_search(queries: list[str],
                           delay: float = 1.0) -> list[str]:
    """Search via SerpAPI Google Events engine. Returns actual event listing URLs."""
    serp_key = os.getenv('SERP_API_KEY', '')
    if not serp_key:
        return []

    event_queries = [
        q for q in queries
        if any(kw in q.lower() for kw in ['conference', 'summit', 'event', 'meetup', 'keynote'])
    ]
    if not event_queries:
        event_queries = queries[:5]
    event_queries = event_queries[:6]

    urls = []
    seen = set()
    for i, query in enumerate(event_queries):
        logger.info(f"SerpAPI events [{i+1}/{len(event_queries)}]: {query}")
        try:
            resp = requests.get(
                'https://serpapi.com/search.json',
                params={'q': query, 'api_key': serp_key, 'engine': 'google_events',
                        'hl': 'en', 'gl': 'us'},
                timeout=10,
            )
            if resp.status_code != 200:
                logger.warning(f"SerpAPI events {resp.status_code} for: {query}")
                continue
            for r in resp.json().get('events_results', []):
                url = r.get('link', '')
                if url and url not in seen and not should_skip_url(url):
                    seen.add(url)
                    urls.append(url)
                for ticket in r.get('ticket_info', []):
                    turl = ticket.get('link', '')
                    if turl and turl not in seen and not should_skip_url(turl):
                        seen.add(turl)
                        urls.append(turl)
        except Exception as e:
            logger.warning(f"SerpAPI events failed for '{query}': {e}")
        if i < len(event_queries) - 1:
            time.sleep(delay)

    logger.info(f"SerpAPI events found {len(urls)} unique URLs")
    return urls


def _serpapi_jobs_search(queries: list[str],
                         delay: float = 1.0) -> list[str]:
    """Search via SerpAPI Google Jobs. Surfaces speaking gigs posted as job listings."""
    serp_key = os.getenv('SERP_API_KEY', '')
    if not serp_key:
        return []

    jobs_queries = [
        q for q in queries
        if any(kw in q.lower() for kw in ['speaker', 'keynote', 'presenter', 'podcast'])
    ]
    if not jobs_queries:
        jobs_queries = queries[:4]
    jobs_queries = jobs_queries[:5]

    urls = []
    seen = set()
    for i, query in enumerate(jobs_queries):
        logger.info(f"SerpAPI jobs [{i+1}/{len(jobs_queries)}]: {query}")
        try:
            resp = requests.get(
                'https://serpapi.com/search.json',
                params={'q': query, 'api_key': serp_key, 'engine': 'google_jobs',
                        'hl': 'en', 'gl': 'us'},
                timeout=10,
            )
            if resp.status_code != 200:
                logger.warning(f"SerpAPI jobs {resp.status_code} for: {query}")
                continue
            for r in resp.json().get('jobs_results', []):
                for opt in r.get('apply_options', []):
                    turl = opt.get('link', '')
                    if turl and turl not in seen and not should_skip_url(turl):
                        seen.add(turl)
                        urls.append(turl)
                share_link = r.get('share_link', '')
                if share_link and share_link not in seen and not should_skip_url(share_link):
                    seen.add(share_link)
                    urls.append(share_link)
        except Exception as e:
            logger.warning(f"SerpAPI jobs failed for '{query}': {e}")
        if i < len(jobs_queries) - 1:
            time.sleep(delay)

    logger.info(f"SerpAPI jobs found {len(urls)} unique URLs")
    return urls


def _serper_search(queries: list[str],
                   results_per_query: int = 10,
                   delay: float = 1.0) -> list[str]:
    """Search via Serper.dev Google organic. Requires SERPER_API_KEY env var."""
    import datetime as _dt
    serper_key = os.getenv('SERPER_API_KEY', '')
    if not serper_key:
        return []

    sixty_ago = _dt.date.today() - _dt.timedelta(days=60)
    tbs_filter = f'cdr:1,cd_min:{sixty_ago.month}/{sixty_ago.day}/{sixty_ago.year}'

    urls = []
    seen = set()
    consecutive_errors = 0
    for i, query in enumerate(queries):
        logger.info(f"Serper organic [{i+1}/{len(queries)}]: {query}")
        try:
            resp = requests.post(
                'https://google.serper.dev/search',
                headers={'X-API-KEY': serper_key, 'Content-Type': 'application/json'},
                json={'q': query, 'num': results_per_query, 'hl': 'en', 'gl': 'us', 'tbs': tbs_filter},
                timeout=10,
            )
            if resp.status_code != 200:
                consecutive_errors += 1
                logger.warning(f"Serper organic {resp.status_code} for: {query}")
                if consecutive_errors >= 3:
                    logger.warning("[SERPER] 3+ consecutive errors — aborting (likely bad key or query too long)")
                    break
                continue
            consecutive_errors = 0
            for r in resp.json().get('organic', [])[:results_per_query]:
                url = r.get('link', '')
                if url and url not in seen and not should_skip_url(url):
                    seen.add(url)
                    urls.append(url)
        except Exception as e:
            logger.warning(f"Serper organic failed for '{query}': {e}")
        if i < len(queries) - 1:
            time.sleep(delay)

    logger.info(f"Serper organic found {len(urls)} unique URLs")
    return urls


def _serper_news_search(queries: list[str],
                        results_per_query: int = 5,
                        delay: float = 1.0) -> list[str]:
    """Search via Serper.dev Google News. Finds recent conference announcements."""
    serper_key = os.getenv('SERPER_API_KEY', '')
    if not serper_key:
        return []

    news_queries = [
        q for q in queries
        if any(kw in q.lower() for kw in ['call for', 'conference', 'summit', 'event', 'podcast'])
    ]
    if not news_queries:
        news_queries = queries[:5]
    news_queries = news_queries[:8]

    urls = []
    seen = set()
    for i, query in enumerate(news_queries):
        logger.info(f"Serper news [{i+1}/{len(news_queries)}]: {query}")
        try:
            resp = requests.post(
                'https://google.serper.dev/news',
                headers={'X-API-KEY': serper_key, 'Content-Type': 'application/json'},
                json={'q': query, 'num': results_per_query, 'hl': 'en', 'gl': 'us', 'tbs': 'qdr:y', 'sort': 'date'},
                timeout=10,
            )
            if resp.status_code != 200:
                logger.warning(f"Serper news {resp.status_code} for: {query}")
                continue
            for r in resp.json().get('news', [])[:results_per_query]:
                url = r.get('link', '')
                if url and url not in seen and not should_skip_url(url):
                    seen.add(url)
                    urls.append(url)
        except Exception as e:
            logger.warning(f"Serper news failed for '{query}': {e}")
        if i < len(news_queries) - 1:
            time.sleep(delay)

    logger.info(f"Serper news found {len(urls)} unique URLs")
    return urls


def _exa_search(queries: list[str],
                results_per_query: int = 10,
                delay: float = 1.0) -> list[str]:
    """Search via Exa neural search API. Requires EXA_API_KEY env var."""
    exa_key = os.getenv('EXA_API_KEY', '')
    if not exa_key:
        return []

    urls = []
    seen = set()
    for i, query in enumerate(queries):
        logger.info(f"Exa [{i+1}/{len(queries)}]: {query}")
        try:
            resp = requests.post(
                'https://api.exa.ai/search',
                headers={'x-api-key': exa_key, 'Content-Type': 'application/json'},
                json={
                    'query': query,
                    'numResults': results_per_query,
                    'type': 'neural',
                    'useAutoprompt': True,
                },
                timeout=15,
            )
            if resp.status_code != 200:
                logger.warning(f"Exa {resp.status_code} for: {query}")
                continue
            for r in resp.json().get('results', [])[:results_per_query]:
                url = r.get('url', '')
                if url and url not in seen and not should_skip_url(url):
                    seen.add(url)
                    urls.append(url)
        except Exception as e:
            logger.warning(f"Exa failed for '{query}': {e}")
        if i < len(queries) - 1:
            time.sleep(delay)

    logger.info(f"Exa found {len(urls)} unique URLs")
    return urls


def _bing_search(queries: list[str],
                 results_per_query: int = 3,
                 delay: float = 2.0) -> list[str]:
    """Search via Bing HTML scraping (no API key needed)."""
    urls = []
    seen = set()
    headers = {
        'User-Agent': (
            'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
            'AppleWebKit/537.36 (KHTML, like Gecko) '
            'Chrome/120.0.0.0 Safari/537.36'
        )
    }

    for i, query in enumerate(queries):
        logger.info(f"Bing [{i+1}/{len(queries)}]: {query}")
        try:
            resp = requests.get(
                'https://www.bing.com/search',
                params={'q': query, 'count': str(results_per_query * 2)},
                headers=headers,
                timeout=10,
            )
            if resp.status_code != 200:
                logger.warning(f"Bing returned {resp.status_code}")
                continue

            soup = BeautifulSoup(resp.text, 'html.parser')
            count = 0
            for li in soup.find_all('li', class_='b_algo'):
                a_tag = li.find('a', href=True)
                if a_tag:
                    href = a_tag['href']
                    if (href.startswith('http') and
                            href not in seen and
                            not should_skip_url(href)):
                        seen.add(href)
                        urls.append(href)
                        count += 1
                        if count >= results_per_query:
                            break
        except Exception as e:
            logger.warning(f"Bing search failed for '{query}': {e}")

        if i < len(queries) - 1:
            time.sleep(delay)

    logger.info(f"Bing search found {len(urls)} unique URLs")
    return urls
