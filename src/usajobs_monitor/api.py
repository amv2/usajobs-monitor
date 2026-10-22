"""USAJOBS Search API client: one request with retry, full paging, and the multi-search fan-out."""

from __future__ import annotations

import json
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request

from . import net

USAJOBS_SEARCH_URL = "https://data.usajobs.gov/api/search"
RESULTS_PER_PAGE = 500  # USAJOBS Search API maximum
MAX_PAGES_PER_SEARCH = 10
REQUEST_TIMEOUT = 45
MAX_ATTEMPTS = 4
BACKOFF_SECONDS = 2.0
MAX_RETRY_AFTER = 60.0


def urlopen(request: Request, timeout: float):
    """No redirects: urllib would forward the Authorization-Key header to the new location."""
    return net.open_url(request, timeout)


def _retryable(exc: Exception) -> bool:
    if isinstance(exc, HTTPError):
        return exc.code == 429 or 500 <= exc.code < 600
    return isinstance(exc, (URLError, TimeoutError))


def _retry_delay(exc: Exception, attempt: int) -> float:
    if isinstance(exc, HTTPError):
        header = exc.headers.get("Retry-After") if exc.headers is not None else None
        try:
            return min(max(float(str(header).strip()), 0.0), MAX_RETRY_AFTER)
        except (TypeError, ValueError):
            pass
    return BACKOFF_SECONDS * (2 ** (attempt - 1))


def fetch_usajobs(params: dict[str, Any], email: str, api_key: str, *,
                  attempts: int = MAX_ATTEMPTS) -> dict[str, Any]:
    """GET one search page, retrying network errors, HTTP 429 and 5xx with backoff."""
    clean_params = {k: v for k, v in params.items() if v not in (None, "")}
    url = f"{USAJOBS_SEARCH_URL}?{urlencode(clean_params)}"
    request = Request(url, headers={"Host": "data.usajobs.gov", "User-Agent": email, "Authorization-Key": api_key})
    for attempt in range(1, attempts + 1):
        try:
            with urlopen(request, timeout=REQUEST_TIMEOUT) as response:
                return json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError) as exc:
            if attempt < attempts and _retryable(exc):
                time.sleep(_retry_delay(exc, attempt))
                continue
            if isinstance(exc, HTTPError):
                body = exc.read().decode("utf-8", errors="replace") if exc.fp is not None else ""
                raise RuntimeError(f"USAJOBS API returned HTTP {exc.code}: {body[:500]}") from exc
            raise RuntimeError(f"Unable to reach USAJOBS API: {exc}") from exc
    raise AssertionError("unreachable")


def api_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return payload.get("SearchResult", {}).get("SearchResultItems", []) or []


def descriptor(item: dict[str, Any]) -> dict[str, Any]:
    return item.get("MatchedObjectDescriptor", {}) or {}


def result_count_all(payload: dict[str, Any]) -> int:
    try:
        return int(payload.get("SearchResult", {}).get("SearchResultCountAll", 0) or 0)
    except (TypeError, ValueError):
        return 0


def fetch_all_pages(
    params: dict[str, Any], email: str, api_key: str, max_pages: int = MAX_PAGES_PER_SEARCH
) -> tuple[list[dict[str, Any]], int, str]:
    """Fetch every result page for one search; return items, calls, and any truncation warning."""
    items: list[dict[str, Any]] = []
    calls = 0
    page = 1
    while True:
        payload = fetch_usajobs({**params, "ResultsPerPage": RESULTS_PER_PAGE, "Page": page}, email, api_key)
        calls += 1
        page_items = api_items(payload)
        items.extend(page_items)
        total = result_count_all(payload)
        if not page_items or len(items) >= total:
            return items, calls, ""
        if page >= max_pages:
            return items, calls, f"{search_label(params)}: fetched {len(items)} of {total} results (page cap {max_pages})"
        page += 1


def search_label(params: dict[str, Any]) -> str:
    label = params.get("Keyword") or "search"
    where = params.get("LocationName") or ("remote" if params.get("RemoteIndicator") else "all locations")
    return f"'{label}' ({where})"


class SearchFailed(RuntimeError):
    """Too many of a run's searches failed for its results to be useful."""


def search_all_jobs(config: dict[str, Any], *, failures: list[str] | None = None
                    ) -> tuple[list[dict[str, Any]], int, list[str]]:
    """Run every keyword in every location (plus remote if enabled); dedupe by position.

    Without ``failures`` the first failed search raises. With it, a search that
    still fails after its retries is skipped and its label appended, so one bad
    search doesn't cost the whole run. If more than half of the searches fail
    (an outage, or bad credentials), it raises ``SearchFailed`` instead, so the
    run fails and sends its failure notice rather than an empty digest.
    """
    credentials = config["credentials"]
    search = config["search"]
    seen: set[str] = set()
    jobs: list[dict[str, Any]] = []
    warnings: list[str] = []
    calls = 0

    base_params = {
        "Radius": search.get("radius_miles"),
        "DatePosted": search.get("date_posted_days"),
        "RemunerationMinimumAmount": search.get("salary_query_floor") or None,
    }

    attempted = 0

    def collect(params: dict[str, Any]) -> None:
        nonlocal calls, attempted
        attempted += 1
        if failures is None:
            items, used, warning = fetch_all_pages(params, credentials["email"], credentials["api_key"])
        else:
            try:
                items, used, warning = fetch_all_pages(params, credentials["email"], credentials["api_key"])
            except (RuntimeError, ValueError):
                # The error text can echo the API response, so only the search is named.
                failures.append(search_label(params))
                return
        calls += used
        if warning:
            warnings.append(warning)
        for item in items:
            job = descriptor(item)
            key = str(job.get("PositionID") or job.get("PositionURI") or job.get("PositionTitle"))
            if key not in seen:
                seen.add(key)
                jobs.append(job)

    # search_only_keywords widen the fetch but are not relevance signals; their
    # phrases (e.g. "human capital office") are common in application boilerplate.
    for keyword in search["target_role_keywords"] + search.get("search_only_keywords", []):
        for location in search["locations"]:
            collect({**base_params, "Keyword": keyword, "LocationName": location})
        if search.get("include_remote_jobs"):
            collect({**base_params, "Keyword": keyword, "RemoteIndicator": "true"})

    if failures and len(failures) * 2 > attempted:
        raise SearchFailed(f"{len(failures)} of {attempted} searches failed")
    return jobs, calls, warnings


def fetch_job(control_number: str, credentials: dict[str, str]) -> dict[str, Any] | None:
    """One posting by its control number (the number in its USAJOBS link), or None."""
    number = str(control_number).strip()
    if not number.isdigit():
        raise ValueError("a control number is the number in the job's USAJOBS link, e.g. 812345600")
    payload = fetch_usajobs({"ControlNumber": number}, credentials["email"], credentials["api_key"])
    jobs = [descriptor(item) for item in api_items(payload)]
    from .jobs import job_id
    return next((job for job in jobs if job_id(job) == number), None)
