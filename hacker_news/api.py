"""The /hacker-news/* endpoint functions (route_glue's route_table calls
them with the schema's loaded kwargs).

Where each answer comes from (hacker_news/fetch.py names the upstreams):

  stories/search, comments/search,     one page of the search index, the
  users/stories, users/comments        search page's own call
  jobs/search                          the same call, on the top-level
                                       comments of the month's "Who is
                                       hiring?" thread
  stories/list                         the official API's id list, then the
                                       page's rows from the index in ONE
                                       call (an id the index lacks is read
                                       from the official API)
  stories/past                         the site's /front?day= page (the
                                       ranks), then the rows from the index
  stories/details, comments/details    the official API's item, which also
                                       holds dead and deleted ones
  stories/comments                     the search site's whole-tree call,
                                       its top level put in the site's own
                                       ranking (the official API's `kids`);
                                       when that call fails the tree is
                                       built from the index's flat rows
  stories/batch                        the index's "get objects" call, up
                                       to 1,000 ids; what it lacks is read
                                       from the official API
  users/details, items/updates         the official API

Caching (cache.cached_call) sits here, keyed on the validated params.
"""
import math
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from cache import cached_call
from hacker_news import cache_config as ttl
from hacker_news import fetch, parsers, refs
from hacker_news.fetch import HackerNewsNotFound, HackerNewsUpstreamError
from scraper_utils import fanout

SEARCH_PER_PAGE = 100       # rows of one index page here (the site's page shows 30; the call serves up to 1,000)
LIST_PER_PAGE = 30          # the site's own page size for its lists and its past front pages
FLAT_PAGE = 1000            # the tree fallback reads the index 1,000 comments at a time
FLAT_MAX_PAGES = 20         # 20,000 comments: more than any thread has
WORKERS = 40                # 500 official-API items came back in 4.1 s at 50 at a time (2026-11-21)
JOB_THREADS_READ = 24       # two threads a month: the last year of them, to find the latest
JOBS_AUTHOR = "whoishiring"     # the account that posts the monthly threads

# One long-lived pool for the calls an endpoint makes side by side: its
# threads keep their own curl sessions between requests.
_pool = ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix="hacker-news")


def _side_by_side(*fns):
    return fanout.on_pool(_pool, list(fns))


def _missing(fn):
    """fn() or None when the upstream does not hold the item."""
    def run():
        try:
            return fn()
        except HackerNewsNotFound:
            return None
    return run


def _optional(fn, default=None):
    """fn(), or `default` when that upstream fails: for a call that only
    adds to an answer another call already carries."""
    return fanout.soft(fn, default, (HackerNewsUpstreamError,))


# ---- shared lookups --------------------------------------------------------------------

def _item_or_404(item_id, param):
    item = fetch.fetch_item(item_id)
    if item is None:
        raise HackerNewsNotFound(f"{param} {item_id} not found")
    return item


def _require_user(username):
    """An empty result for a username: say so when there is no such user
    (the index answers no rows for an unknown name and for the wrong case)."""
    if fetch.fetch_user(username) is None:
        raise HackerNewsNotFound(f"user {username} not found (usernames are case-sensitive)")


def _require_story(story_id):
    if fetch.fetch_item(story_id) is None:
        raise HackerNewsNotFound(f"story {story_id} not found")


def _date_filters(data):
    numeric = []
    if data.get("date_from"):
        numeric.append(f"created_at_i>={refs.day_bounds(data['date_from'])[0]}")
    if data.get("date_to"):
        numeric.append(f"created_at_i<{refs.day_bounds(data['date_to'])[1]}")
    return numeric


def _empty_first_page(result, page):
    return page == 1 and not result["items"]


# ---- search ----------------------------------------------------------------------------

def _stories_search(**data):
    kind, page = data["type"], data["page"]
    sort = data.get("sort") or ("date" if kind == "job" else "popularity")
    by_date = sort == "date"
    numeric = _date_filters(data)
    if data.get("min_points"):
        numeric.append(f"points>={data['min_points']}")
    if data.get("min_comments"):
        numeric.append(f"num_comments>={data['min_comments']}")
    tags = [f"author_{data['author']}"] if data.get("author") else []
    if data.get("site"):
        # the words of the domain, side by side, in the story's link: what the site's own from?site= page lists
        query, searchable = f'"{data["site"]}"', ("url",)
    else:
        query, searchable = data.get("query") or "", ("title", "comment_text", "url", "story_text", "author")
    body = fetch.search_body(query, tags=tags, any_tags=(refs.STORY_TYPES[kind],), numeric=numeric, page=page,
                             per_page=SEARCH_PER_PAGE, by_date=by_date, searchable=searchable,
                             stories=kind == "story")
    result = parsers.parse_search(fetch.fetch_search(body, by_date), parsers.story_from_hit, page, SEARCH_PER_PAGE)
    if data.get("author") and _empty_first_page(result, page):
        _require_user(data["author"])
    return {"query": data.get("query"), "site": data.get("site"), "type": kind, "sort": sort, **result}


def stories_search(**data):
    return cached_call("hacker_news.stories_search", data, _stories_search, ttl.SEARCH_CACHE)


def _comments_search(**data):
    page, by_date = data["page"], data["sort"] == "date"
    tags = []
    if data.get("author"):
        tags.append(f"author_{data['author']}")
    if data.get("story"):
        tags.append(f"story_{data['story']}")
    body = fetch.search_body(data.get("query") or "", tags=tags, any_tags=("comment",), numeric=_date_filters(data),
                             page=page, per_page=SEARCH_PER_PAGE, by_date=by_date, stories=False)
    result = parsers.parse_search(fetch.fetch_search(body, by_date), parsers.comment_from_hit, page, SEARCH_PER_PAGE)
    if _empty_first_page(result, page):
        if data.get("story"):
            _require_story(data["story"])
        if data.get("author"):
            _require_user(data["author"])
    return {"query": data.get("query"), "sort": data["sort"], **result}


def comments_search(**data):
    return cached_call("hacker_news.comments_search", data, _comments_search, ttl.SEARCH_CACHE)


# ---- lists -----------------------------------------------------------------------------

def _list_ids(name):
    return fetch.fetch_story_list(refs.STORY_LISTS[name])


def _stories_by_id(ids):
    """The story shape for each id, in order: the index's rows in one call,
    the official API for what the index lacks (a dead story still in a
    list). An id neither holds is left out."""
    if not ids:
        return []
    hits = fetch.fetch_objects(ids)
    lacking = [item_id for item_id, hit in zip(ids, hits) if hit is None]
    filled = dict(zip(lacking, fanout.on_pool(_pool, [(lambda i=i: fetch.fetch_item(i)) for i in lacking])))
    stories = []
    for item_id, hit in zip(ids, hits):
        if hit is not None:
            stories.append(parsers.story_from_hit(hit))
        elif filled.get(item_id):
            stories.append(parsers.story_from_item(filled[item_id]))
    return stories


def _stories_list(**data):
    name, page = data["list"], data["page"]
    ids = cached_call("hacker_news.list_ids", {"name": name}, _list_ids, ttl.LIST_IDS_CACHE)
    start = (page - 1) * LIST_PER_PAGE
    page_ids = ids[start:start + LIST_PER_PAGE]
    rank = {item_id: start + n for n, item_id in enumerate(page_ids, 1)}
    return {
        "list": name,
        "items": [{"rank": rank[story["id"]], **story} for story in _stories_by_id(page_ids)],
        "pagination": {
            "page": page,
            "items_per_page": LIST_PER_PAGE,
            "total_pages": math.ceil(len(ids) / LIST_PER_PAGE),
            "total_count": len(ids),
        },
    }


def stories_list(**data):
    return cached_call("hacker_news.stories_list", data, _stories_list, ttl.LIST_CACHE)


def _stories_past(**data):
    page = data["page"]
    parsed = parsers.parse_front_page(fetch.fetch_front_page(data["date"], page))
    rows = parsed["stories"]
    # the page carries rank, title, link, points, author and counts; the index adds the text and the tags
    full = _optional(lambda: {s["id"]: s for s in _stories_by_id([r["id"] for r in rows])}, {})()
    return {
        "date": parsed["date"] or data["date"],
        "items": [{"rank": row["rank"], **full.get(row["id"], {k: v for k, v in row.items() if k != "rank"})}
                  for row in rows],
        "pagination": {
            "page": page,
            "items_per_page": LIST_PER_PAGE,
            "total_pages": None,
            "total_count": None,
            "has_next": parsed["has_next"],
        },
    }


def stories_past(**data):
    if not data.get("date"):
        # the site's own default: the day before today, UTC
        data["date"] = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()
    return cached_call("hacker_news.stories_past", data, _stories_past, ttl.past_cache)


# ---- one story, one comment ------------------------------------------------------------

def _story_details(**data):
    story_id = data["story"]
    item, hits = _side_by_side(lambda: _item_or_404(story_id, "story"),
                               _optional(lambda: fetch.fetch_objects([story_id]), [None]))
    kind = item.get("type")
    if kind == "comment":
        raise ValueError(f"story: item {story_id} is a comment; read it with /hacker-news/comments/details.")
    if kind == "pollopt":
        raise ValueError(f"story: item {story_id} is an option of poll {item.get('poll')}; ask for the poll.")
    hit = hits[0] if hits else None
    story = parsers.story_from_item(item, tags=hit.get("_tags") if isinstance(hit, dict) else None)
    options = None
    if kind == "poll":
        parts = [p for p in item.get("parts") or [] if isinstance(p, int)]
        options = [parsers.poll_option(part) for part in
                   fanout.on_pool(_pool, [(lambda p=p: fetch.fetch_item(p)) for p in parts]) if part]
    return {**story, "poll_options": options}


def story_details(**data):
    return cached_call("hacker_news.story_details", data, _story_details, ttl.ITEM_CACHE)


def _flat_comments(story_id):
    """Every comment row of a story from the index, newest first, read in
    windows of 1,000 by date (the index serves one query 1,000 rows deep)."""
    rows, seen, before = [], set(), None
    for _ in range(FLAT_MAX_PAGES):
        numeric = [f"created_at_i<={before}"] if before is not None else []
        body = fetch.search_body("", tags=[f"story_{story_id}"], any_tags=("comment",), numeric=numeric, page=1,
                                 per_page=FLAT_PAGE, by_date=True, stories=False)
        hits = fetch.fetch_search(body, by_date=True)["hits"]
        fresh = [h for h in hits if h.get("objectID") not in seen]
        rows += fresh
        seen.update(h.get("objectID") for h in fresh)
        stamps = [h["created_at_i"] for h in hits if isinstance(h.get("created_at_i"), int)]
        if len(hits) < FLAT_PAGE or not fresh or not stamps:
            break
        before = min(stamps)
    return rows


def _tree_nodes(item_id):
    """The comment nodes under an item: the whole-tree call, or the index's
    flat rows put back into a tree when that call fails. An item the index
    does not hold (dead) has none."""
    try:
        return fetch.fetch_tree(item_id).get("children") or []
    except HackerNewsNotFound:
        return []
    except HackerNewsUpstreamError as e:
        print(f"hacker-news: comment tree {item_id} failed ({type(e).__name__}: {e}); reading the index instead")
        return parsers.tree_from_hits(_flat_comments(item_id), item_id)


def _story_comments(**data):
    story_id = data["story"]
    item, nodes = _side_by_side(lambda: _item_or_404(story_id, "story"), lambda: _tree_nodes(story_id))
    if item.get("type") == "comment":
        raise ValueError(f"story: item {story_id} is a comment; read it and its replies with "
                         "/hacker-news/comments/details.")
    if item.get("type") == "pollopt":
        raise ValueError(f"story: item {story_id} is an option of poll {item.get('poll')}; ask for the poll.")
    story = parsers.story_from_item(item)
    ref = parsers.story_ref(story["id"], story["title"], story["article_link"])
    # the official API's `kids` is the site's own ranking of the top level; the tree's is oldest first
    rank = {kid: n for n, kid in enumerate(item.get("kids") or [])}
    nodes = sorted((n for n in nodes if isinstance(n, dict)), key=lambda n: rank.get(n.get("id"), len(rank)))
    comments = [parsers.comment_from_tree(node, story=ref) for node in nodes]
    return {"story": story, "returned_comment_count": parsers.count_comments(comments), "comments": comments}


def story_comments(**data):
    return cached_call("hacker_news.story_comments", data, _story_comments, ttl.COMMENTS_CACHE)


def _comment_details(**data):
    comment_id = data["comment"]
    item = _item_or_404(comment_id, "comment")
    if item.get("type") != "comment":
        raise ValueError(f"comment: item {comment_id} is a {item.get('type')}; read it with "
                         "/hacker-news/stories/details.")
    tree, hits = _side_by_side(_optional(_missing(lambda: fetch.fetch_tree(comment_id))),
                               _optional(lambda: fetch.fetch_objects([comment_id]), [None]))
    hit = hits[0] if hits and isinstance(hits[0], dict) else {}
    story = parsers.story_ref(hit.get("story_id") or (tree or {}).get("story_id"), hit.get("story_title"),
                              hit.get("story_url"))
    if tree:
        comment = parsers.comment_from_tree(tree, story=story, order=item.get("kids"))
    else:
        comment = {**parsers.comment_from_item(item, story), "replies": []}
    comment["is_dead"], comment["is_deleted"] = bool(item.get("dead")), bool(item.get("deleted"))
    return comment


def comment_details(**data):
    return cached_call("hacker_news.comment_details", data, _comment_details, ttl.ITEM_CACHE)


def _stories_batch(**data):
    ids = data["ids"]
    hits = fetch.fetch_objects(ids)
    # the index keeps neither the text nor the poll of a poll option: those rows are read from the official API too
    hits = [None if hit is not None and parsers.hit_kind(hit) == "pollopt" else hit for hit in hits]
    lacking = [item_id for item_id, hit in zip(ids, hits) if hit is None]
    filled = dict(zip(lacking, fanout.on_pool(_pool, [(lambda i=i: fetch.fetch_item(i)) for i in lacking])))
    items, not_found = [], []
    for item_id, hit in zip(ids, hits):
        if hit is not None:
            items.append(parsers.record_from_hit(hit))
        elif filled.get(item_id):
            items.append(parsers.record_from_item(filled[item_id]))
        else:
            not_found.append(item_id)
    if not items:
        raise HackerNewsNotFound("none of these items exist" if len(ids) > 1 else f"item {ids[0]} not found")
    return {"count": len(items), "items": items, "not_found_ids": not_found}


def stories_batch(**data):
    return cached_call("hacker_news.stories_batch", data, _stories_batch, ttl.ITEM_CACHE)


# ---- users -----------------------------------------------------------------------------

def _user_details(**data):
    username = data["username"]
    user = fetch.fetch_user(username)
    if user is None:
        raise HackerNewsNotFound(f"user {username} not found (usernames are case-sensitive)")
    return parsers.parse_user(user)


def user_details(**data):
    return cached_call("hacker_news.user_details", data, _user_details, ttl.USER_CACHE)


def _user_rows(username, page, kind, parse_row, by_date):
    body = fetch.search_body("", tags=[f"author_{username}"], any_tags=(kind,), page=page, per_page=SEARCH_PER_PAGE,
                             by_date=by_date, stories=kind == "story")
    result = parsers.parse_search(fetch.fetch_search(body, by_date), parse_row, page, SEARCH_PER_PAGE)
    if _empty_first_page(result, page):
        _require_user(username)
    return result


def _user_stories(**data):
    result = _user_rows(data["username"], data["page"], "story", parsers.story_from_hit, data["sort"] == "date")
    return {"username": data["username"], "sort": data["sort"], **result}


def user_stories(**data):
    return cached_call("hacker_news.user_stories", data, _user_stories, ttl.SEARCH_CACHE)


def _user_comments(**data):
    result = _user_rows(data["username"], data["page"], "comment", parsers.comment_from_hit, True)
    return {"username": data["username"], **result}


def user_comments(**data):
    return cached_call("hacker_news.user_comments", data, _user_comments, ttl.SEARCH_CACHE)


# ---- "Who is hiring?" ------------------------------------------------------------------

def _job_thread(kind, month):
    """The monthly thread of `kind` for `month` (YYYY-MM), or the latest
    one when month is None. None when there is none."""
    numeric = []
    if month:
        start, end = refs.month_bounds(month)
        numeric = [f"created_at_i>={start}", f"created_at_i<{end}"]
    body = fetch.search_body("", tags=[f"author_{JOBS_AUTHOR}"], any_tags=("story",), numeric=numeric, page=1,
                             per_page=JOB_THREADS_READ, by_date=True)
    for hit in fetch.fetch_search(body, by_date=True)["hits"]:
        thread = parsers.thread_from_hit(hit)
        if thread and thread["kind"] == kind and (not month or thread["month"] == month):
            return thread
    return None


def _jobs_search(**data):
    kind, month, page = data["thread"], data.get("month"), data["page"]
    thread = cached_call("hacker_news.job_thread", {"kind": kind, "month": month}, _job_thread, ttl.JOB_THREAD_CACHE)
    if not thread:
        raise HackerNewsNotFound(f"no \"{refs.JOB_THREADS[kind].capitalize()}?\" thread for {month or 'this month'}")
    # a posting is a comment made on the thread itself; the replies under it are talk
    body = fetch.search_body(data.get("query") or "", tags=[f"story_{thread['id']}"], any_tags=("comment",),
                             numeric=[f"parent_id={thread['id']}"], page=page, per_page=SEARCH_PER_PAGE,
                             by_date=True, searchable=("comment_text",), stories=False)
    result = parsers.parse_search(fetch.fetch_search(body, by_date=True), parsers.job_post, page, SEARCH_PER_PAGE)
    return {"query": data.get("query"), "thread": thread, **result}


def jobs_search(**data):
    return cached_call("hacker_news.jobs_search", data, _jobs_search, ttl.JOBS_CACHE)


# ---- updates ---------------------------------------------------------------------------

def _items_updates(**data):
    updates, newest = _side_by_side(fetch.fetch_updates, fetch.fetch_max_item)
    return parsers.parse_updates(updates, newest)


def items_updates(**data):
    return cached_call("hacker_news.items_updates", data, _items_updates, ttl.UPDATES_CACHE)
