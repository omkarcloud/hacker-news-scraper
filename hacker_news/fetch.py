"""Hacker News transport: plain curl_cffi with browser TLS. No browser, no
login, no cookie. One session per request thread on the exit
config.HACKER_NEWS_EXIT names (the server's own IP in production); a
retryable failure drops the session, so the retry rides a fresh connection.

Four upstreams (every surface answered 200 on 2026-11-21):

  the search index   the search page's own data calls (hn.algolia.com sends
                     them from the browser, with the public search key its
                     script carries)
    POST <index host>/1/indexes/<index>/query
                     one result page. Item_dev orders by popularity,
                     Item_dev_sort_date by date. The body is JSON sent as
                     a form post, just as the page does. hitsPerPage is
                     served up to 1,000 and a query is served 1,000 rows
                     deep, whatever its total says.
    POST <index host>/1/indexes/*/objects
                     up to 1,000 items by id in one call; an id the index
                     does not hold (a dead or deleted item, an unknown id)
                     comes back as null in its place. 1,001 ids: HTTP 400.

  the search site    https://hn.algolia.com/api/v1/items/<id>
                     an item with its whole comment tree in one answer
                     (1,846 comments: 983 KB in 2.3 s). The search page
                     calls it when a visitor unfolds a story's comments.
                     Unknown, dead or deleted id: 404 {"error": "Not Found"}.

  the official API   https://hacker-news.firebaseio.com/v0
    item/<id>.json   any item, dead and deleted ones included; an unknown
                     id answers HTTP 200 with the body `null`
    user/<name>.json a user (case-sensitive); unknown: `null`
    topstories / newstories / beststories / askstories / showstories /
    jobstories.json  the ids of a list, in the site's own order
    updates.json, maxitem.json
                     what changed in the last moments, and the newest id

  the site's pages   https://news.ycombinator.com/front?day=YYYY-MM-DD&p=N
                     the front page of a past day, which no data call
                     rebuilds. The site limits how fast one address may
                     read its pages (about one a second: 12 of 12 passed
                     at one per 2 s, half were refused at one per 0.5 s)
                     and answers a refused one with HTTP 503 "Sorry, we're
                     not able to serve your requests this quickly." Page
                     requests are therefore paced process-wide
                     (config.HACKER_NEWS_PAGE_INTERVAL), and a refused one
                     is tried again as the next browser profile of
                     config.HACKER_NEWS_PAGE_PROFILES: on 2026-11-21 the
                     site refused one profile for minutes while the others
                     passed from the same address.

Failure taxonomy (scraper_errors, mapped to HTTP by route_glue):
  HackerNewsUpstreamError  transport failure / 5xx / unexpected body   retryable
  HackerNewsBlocked        401 / 403 / 429 / the 503 "this quickly" page retryable
  HackerNewsBadRequest     the site rejected the parameters             never retried
  HackerNewsNotFound       unknown item / user                          never retried
"""
import json
import re
import threading
import time

import config
import egress
import identity_pool
from scraper_errors import BadRequest, Blocked, NotFound, UpstreamError
from hacker_news.refs import MAX_BATCH_ITEMS, SITE
from scraper_utils import pages, retry, sessions

IMPERSONATE = "chrome"
TIMEOUT = 25
TREE_TIMEOUT = 40           # a thread of 1,846 comments is 983 KB and took 2.3 s

SEARCH_SITE = "https://hn.algolia.com"
INDEX_HOST = f"https://{config.HACKER_NEWS_SEARCH_APP_ID.lower()}-dsn.algolia.net"
INDEX_AGENT = "Algolia for JavaScript (4.13.1); Browser (lite)"
INDEX_POPULARITY = "Item_dev"
INDEX_DATE = "Item_dev_sort_date"
MAX_HITS_PER_PAGE = 1000    # 1,001 and 5,000 are served as 1,000
MAX_HITS_DEPTH = 1000       # a query is served this many rows deep

FIREBASE = "https://hacker-news.firebaseio.com/v0"

# ---- headers, one dict per kind of call ------------------------------------------------
# The search page's own query call (samples/calls/p1-02-…Item_dev_query.request.json).
INDEX_HEADERS = {
    "accept": "*/*",
    "content-type": "application/x-www-form-urlencoded",
    "origin": SEARCH_SITE,
    "referer": SEARCH_SITE + "/",
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "cross-site",
}
# The page's fetch of /api/v1/items/<id>: the same page, its own host.
TREE_HEADERS = {
    "accept": "*/*",
    "referer": SEARCH_SITE + "/",
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
}
# The official API is called by programs, not by a page.
FIREBASE_HEADERS = {"accept": "application/json"}
# A page opened by its address (samples/pages/3-news_….html.request.json).
PAGE_HEADERS = {
    "accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,image/jxl,image/avif,image/webp,image/apng,"
               "*/*;q=0.8,application/signed-exchange;v=b3;q=0.7"),
    "sec-fetch-dest": "document",
    "sec-fetch-mode": "navigate",
    "sec-fetch-site": "none",
    "sec-fetch-user": "?1",
    "upgrade-insecure-requests": "1",
}

RATE_LIMIT_MARKER = "not able to serve your requests this quickly"
NO_DATA_YET = "We don't have that data yet"
BEFORE_THE_SITE = "HN didn't exist yet"


class HackerNewsUpstreamError(UpstreamError):
    """Transport failure, 5xx or an unexpected payload — retryable."""


class HackerNewsBlocked(HackerNewsUpstreamError, Blocked):
    """401 / 403 / 429, a challenge page or the site's "this quickly" page — retryable."""


class HackerNewsPageQueueFull(HackerNewsBlocked):
    """Too many page requests are waiting for their turn here: nothing was
    sent, so the site refused no browser profile."""


class HackerNewsBadRequest(BadRequest):
    """The site rejected the parameters — never retried."""


class HackerNewsNotFound(NotFound):
    """No such item or user — never retried."""


# ---- sessions ------------------------------------------------------------------------

# One curl session per request thread for the data calls (a curl handle must
# not be shared across threads); they are stateless, no cookie is carried.
_local = threading.local()
# The site's pages get a session of their own, built as the browser profile
# the process is on: it moves down config.HACKER_NEWS_PAGE_PROFILES (and
# around) when the site refuses the current one.
_page_local = threading.local()
_page_profile_lock = threading.Lock()
_page_profile_index = [0]


def _session():
    return sessions.thread_session(
        _local, lambda: egress.exit_session(config.HACKER_NEWS_EXIT, impersonate=IMPERSONATE))


def _drop_session():
    """Close the thread's session so the retry rides a fresh connection / exit."""
    sessions.drop(_local)


def _page_profile():
    """The browser profile page sessions are built as right now."""
    profiles = config.HACKER_NEWS_PAGE_PROFILES
    return profiles[_page_profile_index[0] % len(profiles)]


def _build_page_session():
    profile = _page_profile()
    _page_local.profile = profile
    return egress.exit_session(config.HACKER_NEWS_EXIT, impersonate=profile)


def _page_session():
    return sessions.thread_session(_page_local, _build_page_session)


def _drop_page_session(error=None):
    """Close the thread's page session. After a refusal every thread's next
    page session is built as the next browser profile: the step is taken
    once per refused profile, however many threads met the refusal."""
    refused = getattr(_page_local, "profile", None)
    sessions.drop(_page_local)
    if isinstance(error, HackerNewsBlocked) and not isinstance(error, HackerNewsPageQueueFull):
        with _page_profile_lock:
            if refused == _page_profile():
                _page_profile_index[0] += 1


def _retrying(fn, on_error=None):
    """fn() under the shared retry policy: only HackerNewsUpstreamError
    (Blocked included) is retried; NotFound and BadRequest surface at once."""
    return retry.run(fn, config, HackerNewsUpstreamError, on_error or (lambda e: _drop_session()))


# ---- one request ---------------------------------------------------------------------

def _decoded(resp, label):
    try:
        return resp.json()
    except Exception:
        if pages.is_challenged(resp.status_code, resp.headers, resp.text or ""):
            raise HackerNewsBlocked(f"challenge page for {label}")
        raise HackerNewsUpstreamError(f"non-JSON answer for {label}: {(resp.text or '')[:120]!r}")


def _classify(resp, label):
    """A 200 passes. 401 / 403 / 429 and a challenge page -> Blocked;
    anything else -> upstream error. 404 and 400 are read by the caller,
    since each upstream means something else by them."""
    status = resp.status_code
    if status == 200:
        return
    if status in (401, 403, 429) or pages.is_challenged(status, resp.headers, resp.text or ""):
        raise HackerNewsBlocked(f"HTTP {status} for {label}")
    raise HackerNewsUpstreamError(f"HTTP {status} for {label}")


def _send(method, url, label, headers, timeout=TIMEOUT, session=None, **kw):
    try:
        return (session or _session)().request(method, url, headers=headers, timeout=timeout, **kw)
    except Exception as e:
        raise HackerNewsUpstreamError(f"request failed for {label}: {type(e).__name__}: {e}")


# ---- the search index ----------------------------------------------------------------
# The public search key is a constant of the search page's script
# (main-<hash>.js: algoliasearch("<app id>", "<key>")). It lives in a
# shared identity_pool seeded from config: a rejection wave across the
# request threads costs ONE page + script read, the other threads get the
# winner's key, and a second refresh inside KEY_REFRESH_INTERVAL returns the
# same key, so a real refusal fails instead of looping.
_SCRIPT_RE = re.compile(r'<script[^>]+src="([^"]*main-[0-9a-f]+\.js)"')
_KEY_RE = re.compile(r'"%s",\s*"([0-9a-f]{20,64})"' % re.escape(config.HACKER_NEWS_SEARCH_APP_ID))
KEY_REFRESH_INTERVAL = 60      # seconds between two reads of the search page's script


def _mint_search_key(key, hint):
    """The pool's mint: the key in the script the search page loads."""
    resp = _send("GET", SEARCH_SITE + "/", "the search page", PAGE_HEADERS)
    _classify(resp, "the search page")
    match = _SCRIPT_RE.search(resp.text or "")
    if not match:
        raise HackerNewsUpstreamError("the search page names no main script (layout change?)")
    src = match.group(1)
    script = _send("GET", src if src.startswith("http") else SEARCH_SITE + src, "the search page's script",
                   {"accept": "*/*", "referer": SEARCH_SITE + "/"}, timeout=TREE_TIMEOUT)
    _classify(script, "the search page's script")
    found = _KEY_RE.search(script.text or "")
    if not found:
        raise HackerNewsUpstreamError("no search key in the search page's script")
    return found.group(1)


_search_keys = identity_pool.Pool("hacker-news-search-key", _mint_search_key, min_interval=KEY_REFRESH_INTERVAL,
                                  seed={"": config.HACKER_NEWS_SEARCH_KEY})


def _index_url(path, key):
    from urllib.parse import quote
    return (f"{INDEX_HOST}/1/indexes/{path}?x-algolia-agent={quote(INDEX_AGENT, safe='()')}"
            f"&x-algolia-api-key={key}&x-algolia-application-id={config.HACKER_NEWS_SEARCH_APP_ID}")


def _index_post(path, payload, label):
    """POST one index call -> the decoded answer. A refused key is replaced
    once (the page's script is read again) before the call fails."""
    body = json.dumps(payload, separators=(",", ":"))

    def once():
        used = None
        for attempt in (1, 2):
            used = _search_keys.get(replace=used if attempt == 2 else None)
            resp = _send("POST", _index_url(path, used.value), label, INDEX_HEADERS, data=body)
            if resp.status_code == 403 and attempt == 1:
                continue        # a rotated key: read the current one and try once more
            if resp.status_code == 400:
                message = ""
                try:
                    message = str(resp.json().get("message") or "")[:200]
                except Exception:
                    pass
                raise HackerNewsBadRequest(message or f"the search index rejected {label}")
            _classify(resp, label)
            answer = _decoded(resp, label)
            if not isinstance(answer, dict):
                raise HackerNewsUpstreamError(f"unexpected payload for {label}: {(resp.text or '')[:120]!r}")
            return answer
        raise HackerNewsBlocked(f"the search key was refused for {label}")
    return _retrying(once)


def search_body(query="", tags=(), any_tags=("story",), numeric=(), page=1, per_page=100, by_date=False,
                searchable=("title", "comment_text", "url", "story_text", "author"), stories=True):
    """The search page's own request body, with the parts a call varies.
    `any_tags` is the page's first tag group (any of them matches: the
    type); each of `tags` is a group of its own after it (every one must
    match: an author, a story), since the index reads the tags inside one
    group as "any of these" and the groups as "all of them".
    The page asks for less typo tolerance when it sorts by date, and for
    words closer together when it searches stories."""
    return {
        "query": query or "",
        "analyticsTags": ["web"],
        "page": max(page - 1, 0),
        "hitsPerPage": min(per_page, MAX_HITS_PER_PAGE),
        "minWordSizefor1Typo": 4,
        "minWordSizefor2Typos": 8,
        "advancedSyntax": True,
        "ignorePlurals": False,
        "clickAnalytics": True,
        "minProximity": 7 if stories else 1,
        "numericFilters": list(numeric),
        "tagFilters": [list(any_tags)] + ([[tag] for tag in tags] or [[]]),
        "typoTolerance": "min" if by_date else True,
        "queryType": "prefixNone",
        "restrictSearchableAttributes": list(searchable),
        "getRankingInfo": True,
    }


def fetch_search(body, by_date=False):
    """One result page of the index -> {hits, nbHits, nbPages, page, hitsPerPage, …}."""
    index = INDEX_DATE if by_date else INDEX_POPULARITY
    answer = _index_post(f"{index}/query", body, f"search {body.get('query')!r}")
    if not isinstance(answer.get("hits"), list):
        raise HackerNewsUpstreamError(f"search answered no hits: {str(answer)[:120]!r}")
    return answer


def fetch_objects(ids):
    """The index's rows for up to 1,000 ids in one call, aligned with
    `ids`: None where the index does not hold the item."""
    ids = list(ids)
    if not ids:
        return []
    if len(ids) > MAX_BATCH_ITEMS:
        raise HackerNewsBadRequest(f"at most {MAX_BATCH_ITEMS} items in one call")
    payload = {"requests": [{"indexName": INDEX_POPULARITY, "objectID": str(i)} for i in ids]}
    answer = _index_post("*/objects", payload, f"{len(ids)} items")
    rows = answer.get("results")
    if not isinstance(rows, list) or len(rows) != len(ids):
        raise HackerNewsUpstreamError(f"items answered {str(answer)[:120]!r}")
    return rows


# ---- the search site: an item with its comment tree ----------------------------------

def fetch_tree(item_id):
    """An item with every comment under it, nested. Unknown to the index
    (never existed, dead or deleted) -> NotFound."""
    label = f"comment tree {item_id}"

    def once():
        resp = _send("GET", f"{SEARCH_SITE}/api/v1/items/{item_id}", label, TREE_HEADERS, timeout=TREE_TIMEOUT)
        if resp.status_code == 404:
            raise HackerNewsNotFound(f"item {item_id} not found")
        _classify(resp, label)
        tree = _decoded(resp, label)
        if not isinstance(tree, dict) or "id" not in tree:
            raise HackerNewsUpstreamError(f"unexpected payload for {label}: {(resp.text or '')[:120]!r}")
        return tree
    return _retrying(once)


# ---- the official API ----------------------------------------------------------------

def _firebase(path, label):
    """GET one resource of the official API -> its decoded value (None for
    the `null` it answers for an unknown item or user)."""
    def once():
        resp = _send("GET", f"{FIREBASE}/{path}.json", label, FIREBASE_HEADERS)
        _classify(resp, label)
        return _decoded(resp, label)
    return _retrying(once)


def fetch_item(item_id):
    """One item as the official API holds it (dead and deleted ones too),
    or None when there is no such item."""
    item = _firebase(f"item/{item_id}", f"item {item_id}")
    if item is not None and not isinstance(item, dict):
        raise HackerNewsUpstreamError(f"unexpected payload for item {item_id}: {str(item)[:120]!r}")
    return item


def fetch_user(username):
    """One user, or None when there is no such user (names are case-sensitive)."""
    user = _firebase(f"user/{username}", f"user {username}")
    if user is not None and not isinstance(user, dict):
        raise HackerNewsUpstreamError(f"unexpected payload for user {username}: {str(user)[:120]!r}")
    return user


def fetch_story_list(name):
    """The ids of one of the site's lists (topstories, newstories, …), in its order."""
    ids = _firebase(name, name)
    if not isinstance(ids, list):
        raise HackerNewsUpstreamError(f"{name} answered no list: {str(ids)[:120]!r}")
    return [i for i in ids if isinstance(i, int)]


def fetch_updates():
    """{"items": [ids], "profiles": [usernames]}: what changed in the last moments."""
    updates = _firebase("updates", "updates")
    if not isinstance(updates, dict):
        raise HackerNewsUpstreamError(f"updates answered {str(updates)[:120]!r}")
    return updates


def fetch_max_item():
    """The newest item id."""
    newest = _firebase("maxitem", "maxitem")
    if not isinstance(newest, int):
        raise HackerNewsUpstreamError(f"maxitem answered {str(newest)[:120]!r}")
    return newest


# ---- the site's pages ----------------------------------------------------------------
# One page request at a time leaves this process, config.HACKER_NEWS_PAGE_INTERVAL
# seconds after the one before it: the site's limit is per address.
_pace_lock = threading.Lock()
_next_page_at = [0.0]


def _wait_for_page_slot():
    interval = config.HACKER_NEWS_PAGE_INTERVAL
    with _pace_lock:
        now = time.monotonic()
        slot = max(now, _next_page_at[0])
        wait = slot - now
        if wait > config.HACKER_NEWS_PAGE_MAX_WAIT:
            raise HackerNewsPageQueueFull("too many page requests are waiting for their turn")
        _next_page_at[0] = slot + interval
    if wait > 0:
        time.sleep(wait)


def fetch_front_page(day, page=1):
    """The HTML of the front page of `day` (YYYY-MM-DD), 30 stories a page.
    A day the site has no data for -> BadRequest with the site's words."""
    label = f"front page of {day}, page {page}"
    params = {"day": day}
    if page > 1:
        params["p"] = page

    def once():
        _wait_for_page_slot()
        resp = _send("GET", SITE + "/front", label, PAGE_HEADERS, session=_page_session, params=params)
        text = resp.text or ""
        if resp.status_code == 503 or RATE_LIMIT_MARKER in text[:4000]:
            raise HackerNewsBlocked(f"the site is rate-limiting this address ({label}, as {_page_profile()})")
        _classify(resp, label)
        if len(text) < 200:
            if NO_DATA_YET in text or BEFORE_THE_SITE in text:
                raise HackerNewsBadRequest(f"date: {text.strip()}")
            raise HackerNewsUpstreamError(f"unexpected page for {label}: {text[:120]!r}")
        if 'op="front"' not in text[:300]:
            raise HackerNewsUpstreamError(f"not a front page for {label}: {pages.page_title(text)!r}")
        return text
    return _retrying(once, on_error=_drop_page_session)
