"""Reference resolvers, link builders and the choice tables for
/hacker-news/*.

ONE param per input: `story` / `comment` accept an item id OR a
news.ycombinator.com item link; `username` a username OR a profile link;
`site` a domain OR any link on it. Resolvers raise ValueError with a
user-facing message (RefField turns it into a 400).

Identifier shapes (news.ycombinator.com, 2026-11-21):
  * item      one counter for every kind of record: a story, a job, a
              poll, a poll option and a comment are all items
              (item?id=8863). The newest id that day was 50,588,xxx.
  * user      the username, case-sensitive: `pg` exists, `PG` does not
              (user?id=pg). 2 to 15 characters: letters, digits, dash and
              underscore.
  * site      the host a story points to, as the site's own "from" pages
              take it (from?site=github.com).
"""
import re
from datetime import date
from urllib.parse import parse_qs, urlparse

SITE = "https://news.ycombinator.com"
HOSTS = ("news.ycombinator.com",)

ITEM_ID_RE = re.compile(r"^[0-9]{1,12}$")
USERNAME_RE = re.compile(r"^[A-Za-z0-9_-]{2,15}$")
_DOMAIN_RE = re.compile(r"^(?=.{4,100}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24}$")

MAX_BATCH_ITEMS = 1000          # the search index's own cap: "objects cannot fetch more than 1000 hits" (2026-11-21)
FIRST_DAY = date(2007, 2, 19)   # the oldest front page the site keeps; a day before it answers "HN didn't exist yet."

# the pages of a user that name them in ?id=
_USER_PAGES = ("/user", "/submitted", "/threads", "/favorites")

# public value -> the official API's list name
STORY_LISTS = {
    "top": "topstories",
    "new": "newstories",
    "best": "beststories",
    "ask": "askstories",
    "show": "showstories",
    "jobs": "jobstories",
}

# public value -> the search index's tag (the search page's own "type" control, plus its /hot page)
STORY_TYPES = {
    "story": "story",
    "ask_hn": "ask_hn",
    "show_hn": "show_hn",
    "launch_hn": "launch_hn",
    "job": "job",
    "poll": "poll",
    "front_page": "front_page",
}

# public value -> the index that holds that order
STORY_SORTS = {"popularity": "Item_dev", "date": "Item_dev_sort_date"}
COMMENT_SORTS = {"date": "Item_dev_sort_date", "relevance": "Item_dev"}

# public value -> the words the monthly thread's title carries
JOB_THREADS = {
    "hiring": "who is hiring",
    "wants_to_be_hired": "who wants to be hired",
}

_ITEM_HELP = "Must be a Hacker News item id (8863) or an item link (https://news.ycombinator.com/item?id=8863)."
_USER_HELP = ("Must be a Hacker News username (pg; case-sensitive) or a profile link "
              "(https://news.ycombinator.com/user?id=pg).")
_SITE_HELP = "Must be a domain (github.com) or a link on it (https://github.com/torvalds/linux)."


def item_link(item_id):
    return f"{SITE}/item?id={item_id}" if item_id else None


def user_link(username):
    return f"{SITE}/user?id={username}" if username else None


def _site_url(value):
    """A parsed news.ycombinator.com link (None when the value is not a
    link); a link to another host raises ValueError."""
    if not value.startswith(("http://", "https://", "//", "news.ycombinator.com/")):
        return None
    url = value
    if url.startswith("//"):
        url = "https:" + url
    elif not url.startswith("http"):
        url = "https://" + url
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host not in HOSTS:
        raise ValueError("Must be a news.ycombinator.com link.")
    return parsed


def _query_id(parsed):
    values = parse_qs(parsed.query).get("id") or []
    return values[0].strip() if values else ""


def resolve_item(value):
    """Item id or item link -> the id, as an int."""
    value = (value or "").strip()
    try:
        parsed = _site_url(value)
    except ValueError:
        raise ValueError(_ITEM_HELP)
    if parsed is not None:
        item_id = _query_id(parsed) if parsed.path.rstrip("/") == "/item" else ""
        if ITEM_ID_RE.match(item_id) and int(item_id) > 0:
            return int(item_id)
        raise ValueError(_ITEM_HELP)
    if value.startswith("#"):
        value = value[1:]
    if ITEM_ID_RE.match(value) and int(value) > 0:
        return int(value)
    raise ValueError(_ITEM_HELP)


def resolve_items(value):
    """'8863, 121003, <link>' -> the ids, in order, repeats dropped."""
    ids = []
    for raw in (value or "").split(","):
        raw = raw.strip()
        if not raw:
            continue
        item_id = resolve_item(raw)
        if item_id not in ids:
            ids.append(item_id)
    if not ids:
        raise ValueError(_ITEM_HELP)
    if len(ids) > MAX_BATCH_ITEMS:
        raise ValueError(f"At most {MAX_BATCH_ITEMS} items in one call.")
    return ids


def resolve_user(value):
    """Username or profile link -> the username, in the case it was given
    (the site tells `pg` from `PG`)."""
    value = (value or "").strip()
    try:
        parsed = _site_url(value)
    except ValueError:
        raise ValueError(_USER_HELP)
    if parsed is not None:
        username = _query_id(parsed) if parsed.path.rstrip("/") in _USER_PAGES else ""
        if USERNAME_RE.match(username):
            return username
        raise ValueError(_USER_HELP)
    if value.startswith("@"):
        value = value[1:]
    if USERNAME_RE.match(value):
        return value
    raise ValueError(_USER_HELP)


def resolve_site(value):
    """Domain or link -> the host, lower case, without a leading www."""
    value = (value or "").strip().lower()
    if not value or " " in value:
        raise ValueError(_SITE_HELP)
    url = value if "://" in value else "https://" + value.lstrip("/")
    try:
        host = (urlparse(url).hostname or "").strip(".")
    except ValueError:
        raise ValueError(_SITE_HELP)
    if host.startswith("www."):
        host = host[4:]
    if not _DOMAIN_RE.match(host):
        raise ValueError(_SITE_HELP)
    return host


def month_bounds(month):
    """'2026-11' -> (epoch of its first second, epoch of the next month's
    first second), UTC."""
    from calendar import timegm
    year, mon = int(month[:4]), int(month[5:7])
    start = timegm((year, mon, 1, 0, 0, 0))
    end = timegm((year + (mon == 12), mon % 12 + 1, 1, 0, 0, 0))
    return start, end


def day_bounds(day):
    """'2026-11-20' -> (epoch of its first second, epoch of the next day's
    first second), UTC."""
    from calendar import timegm
    year, mon, dom = int(day[:4]), int(day[5:7]), int(day[8:10])
    start = timegm((year, mon, dom, 0, 0, 0))
    return start, start + 86400
