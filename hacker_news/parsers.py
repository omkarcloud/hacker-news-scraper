"""Raw search-index / official-API / page answers -> clean dicts. Pure
functions, no I/O; every parser tolerates a partial payload.

Fixtures (fixtures/, captured 2026-11-21, untrimmed unless the name says so):
  search_*.json          result pages of the search index (stories by
                         popularity and by date, comments, a user's
                         stories and comments, a domain, past the end, no
                         result)
  objects_*.json         "get objects" answers: one of each kind with an
                         unknown id, the front page's 30, the job list
  tree_*.json            /api/v1/items answers: a story with 71 comments,
                         a comment with its replies, a poll, not found
  item_*.json            official-API items: story, Ask HN, job (with a
                         link and with text), poll, poll option, comment,
                         dead and deleted ones; item_not_found.null.txt is
                         the literal `null` an unknown id answers
  user_*.json            official-API users (with and without "about");
                         user_not_found.null.txt
  list_topstories.json, updates.json, maxitem.json
  front_*.html           the front page of a past day: page 1, page 2, a
                         page past the end, the first day the site keeps;
                         front_future.txt / front_before_start.txt are the
                         site's one-line refusals, rate_limited_503.html
                         its "this quickly" page
  hiring_*.json          the "Who is hiring?" threads and one thread's postings

Shapes the endpoints share:
  * story     a story, a job or a poll (`type`), from an index row
              (story_from_hit) or an official-API item (story_from_item)
  * comment   from an index row, an official-API item or a tree node;
              `replies` only where a tree was read
  * text      HTML becomes plain text with paragraphs as blank lines. The
              site shortens a long link's visible text ("https://a.b/c..."),
              so every link is written out in full in the text and listed
              in `links`.
"""
import html as html_lib
import re
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from hacker_news import refs
from scraper_utils.dates import epoch_utc, iso_utc
from scraper_utils.text import clean, html_to_text
from scraper_utils.values import as_dict, as_list, first_int, to_int

_ANCHOR_RE = re.compile(r'<a\s[^>]*?href="([^"]*)"[^>]*>.*?</a>', re.S | re.I)
_BARE_LINK_RE = re.compile(r"https?://[^\s<>]+", re.I)
_LINK_TAIL = ".,;:!?)]}'\""
_EMAIL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._%+-]*@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,24}")
_THREAD_MONTH_RE = re.compile(r"\((January|February|March|April|May|June|July|August|September|October|November|"
                              r"December)\s+(\d{4})\)", re.I)
_MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
           "november", "december")
_PREFIX_SHOW = "show hn:"
_PREFIX_LAUNCH = "launch hn:"


# ---- text ------------------------------------------------------------------------------

def _trimmed_link(found):
    """A link found in running text, without the punctuation that follows
    it; a closing bracket stays when the link opened it (…/Rust_(language))."""
    link = found
    while link and link[-1] in _LINK_TAIL:
        if link[-1] == ")" and link.count("(") >= link.count(")"):
            break
        link = link[:-1]
    return link


def text_and_links(value):
    """An HTML text of the site -> (plain text, [links in it]). Each link
    is written out in full where it stood (the site shows a long one cut
    short); a link written as plain text counts too. (None, []) for nothing."""
    if not isinstance(value, str) or not value.strip():
        return None, []
    links = []

    def full_link(match):
        link = html_lib.unescape(match.group(1)).strip()
        if link and link not in links:
            links.append(link)
        return html_lib.escape(link, quote=False)

    text = html_to_text(_ANCHOR_RE.sub(full_link, value))
    # a link the site did not make clickable (a profile's "about" is plain text) is a link all the same
    for found in _BARE_LINK_RE.findall(text or ""):
        link = _trimmed_link(found)
        if found not in links and link not in links and len(link) > 10:
            links.append(link)
    return text, links


def domain_of(link):
    """The host a link points to, without a leading www; None for none."""
    if not link:
        return None
    try:
        host = (urlparse(link).hostname or "").lower()
    except ValueError:
        return None
    return (host[4:] if host.startswith("www.") else host) or None


def _author(username):
    username = clean(username)
    return {"username": username, "link": refs.user_link(username)} if username else None


def _article_link(value):
    """The page a story points to; '' (a text post) -> None."""
    link = clean(value)
    return link if link and link.lower().startswith(("http://", "https://")) else None


# ---- stories ---------------------------------------------------------------------------

def _story(item_id, kind, title, article_link, text_html, username, points, comments, created, tags=None,
           is_dead=False, is_deleted=False):
    """The story shape. The three "HN" kinds are the search index's own
    tags where a row has them; otherwise they follow from the title and the
    link the way the index tags them (2,264 rows compared on 2026-11-21:
    show_hn and launch_hn are the title's prefix, ask_hn is every other
    story without a link)."""
    text, links = text_and_links(text_html)
    title = clean(title)
    if tags is not None:
        is_show, is_launch, is_ask = "show_hn" in tags, "launch_hn" in tags, "ask_hn" in tags
        on_front_page = "front_page" in tags
    else:
        low = (title or "").lower()
        is_show, is_launch = low.startswith(_PREFIX_SHOW), low.startswith(_PREFIX_LAUNCH)
        is_ask = bool(kind == "story" and title and not article_link and not is_show and not is_launch)
        on_front_page = None
    return {
        "id": item_id,
        "type": kind,
        "title": title,
        "link": refs.item_link(item_id),
        "article_link": article_link,
        "domain": domain_of(article_link),
        "text": text,
        "links": links,
        "author": _author(username),
        "points": points,
        "comment_count": comments,
        "is_ask_hn": is_ask,
        "is_show_hn": is_show,
        "is_launch_hn": is_launch,
        "is_on_front_page": on_front_page,
        "is_dead": bool(is_dead),
        "is_deleted": bool(is_deleted),
        "created_at": created,
    }


def hit_kind(hit):
    """story / job / poll / pollopt / comment: the first tag of an index row."""
    tags = as_list(as_dict(hit).get("_tags"))
    return tags[0] if tags and isinstance(tags[0], str) else None


def story_from_hit(hit):
    """A story, job or poll row of the search index -> the story shape.
    The index holds no dead or deleted item."""
    hit = as_dict(hit)
    kind = hit_kind(hit) or "story"
    return _story(
        to_int(hit.get("objectID")), kind, hit.get("title"), _article_link(hit.get("url")),
        hit.get("story_text") or hit.get("job_text"), hit.get("author"), to_int(hit.get("points")),
        to_int(hit.get("num_comments")), epoch_utc(hit.get("created_at_i")) or iso_utc(hit.get("created_at")),
        tags=as_list(hit.get("_tags")))


def story_from_item(item, tags=None):
    """A story, job or poll of the official API -> the story shape. `tags`
    are the index row's, when the caller has it (they say whether the story
    is on the front page)."""
    item = as_dict(item)
    return _story(
        to_int(item.get("id")), item.get("type") or "story", item.get("title"), _article_link(item.get("url")),
        item.get("text"), item.get("by"), to_int(item.get("score")), to_int(item.get("descendants")),
        epoch_utc(item.get("time")), tags=tags, is_dead=item.get("dead"), is_deleted=item.get("deleted"))


def story_from_tree(node):
    """The root of a comment tree -> the story shape (it carries no comment
    count: the caller counts the tree)."""
    node = as_dict(node)
    return _story(
        to_int(node.get("id")), node.get("type") or "story", node.get("title"), _article_link(node.get("url")),
        node.get("text"), node.get("author"), to_int(node.get("points")), None,
        epoch_utc(node.get("created_at_i")) or iso_utc(node.get("created_at")))


def poll_option(item):
    """A poll option of the official API -> {id, text, points}."""
    item = as_dict(item)
    text, _ = text_and_links(item.get("text"))
    return {"id": to_int(item.get("id")), "text": text, "points": to_int(item.get("score"))}


# ---- comments --------------------------------------------------------------------------

def _comment(item_id, text_html, username, parent_id, story, reply_count, created, is_dead=False,
             is_deleted=False):
    """The comment shape. It has no points: the site shows a comment's
    score to its author alone, and no upstream carries one (the index's
    `points` is null on every comment row, 2026-11-21)."""
    text, links = text_and_links(text_html)
    if is_dead and text == "[dead]":
        text = None             # the official API's placeholder for a dead comment's text
    return {
        "id": item_id,
        "link": refs.item_link(item_id),
        "text": text,
        "links": links,
        "author": _author(username),
        "parent_id": parent_id,
        "story": story,
        "reply_count": reply_count,
        "is_dead": bool(is_dead),
        "is_deleted": bool(is_deleted),
        "created_at": created,
    }


def story_ref(story_id, title=None, article_link=None):
    """The short form of the story a comment belongs to; None without an id."""
    story_id = to_int(story_id)
    if not story_id:
        return None
    return {"id": story_id, "title": clean(title), "link": refs.item_link(story_id),
            "article_link": _article_link(article_link)}


def comment_from_hit(hit):
    """A comment row of the search index -> the comment shape."""
    hit = as_dict(hit)
    return _comment(
        to_int(hit.get("objectID")), hit.get("comment_text"), hit.get("author"), to_int(hit.get("parent_id")),
        story_ref(hit.get("story_id"), hit.get("story_title"), hit.get("story_url")),
        len(as_list(hit.get("children"))), epoch_utc(hit.get("created_at_i")) or iso_utc(hit.get("created_at")))


def comment_from_item(item, story=None):
    """A comment of the official API -> the comment shape. The API names
    only the comment's parent, so `story` is what the caller knows of it."""
    item = as_dict(item)
    return _comment(
        to_int(item.get("id")), item.get("text"), item.get("by"), to_int(item.get("parent")), story,
        len(as_list(item.get("kids"))), epoch_utc(item.get("time")), is_dead=item.get("dead"),
        is_deleted=item.get("deleted"))


def comment_from_tree(node, story=None, order=None):
    """A node of a comment tree -> the comment shape with its `replies`
    nested. `order` lists child ids in the site's own ranking (the official
    API's `kids`); children it does not name follow in the tree's order,
    which is oldest first."""
    node = as_dict(node)
    children = [c for c in as_list(node.get("children")) if isinstance(c, dict)]
    if order:
        rank = {item_id: n for n, item_id in enumerate(order)}
        children.sort(key=lambda c: rank.get(c.get("id"), len(rank)))
    out = _comment(
        to_int(node.get("id")), node.get("text"), node.get("author"), to_int(node.get("parent_id")), story or story_ref(node.get("story_id")), len(children),
        epoch_utc(node.get("created_at_i")) or iso_utc(node.get("created_at")))
    out["replies"] = [comment_from_tree(c, story=out["story"]) for c in children]
    return out


def tree_from_hits(hits, root_id):
    """Flat comment rows of one story -> the nested node list under
    `root_id`, in the shape /api/v1/items answers (id, author, text,
    parent_id, story_id, created_at_i, children), oldest first."""
    nodes = {}
    for hit in hits:
        hit = as_dict(hit)
        item_id = to_int(hit.get("objectID"))
        if item_id:
            nodes[item_id] = {"id": item_id, "author": hit.get("author"), "text": hit.get("comment_text"),
                              "parent_id": to_int(hit.get("parent_id")), "story_id": to_int(hit.get("story_id")),
                              "created_at_i": hit.get("created_at_i"), "children": []}
    top = []
    for item_id in sorted(nodes):
        node = nodes[item_id]
        parent = nodes.get(node["parent_id"])
        if parent is not None:
            parent["children"].append(node)
        elif node["parent_id"] == root_id:
            top.append(node)
    return top


def count_comments(comments):
    """How many comments a nested list holds, replies included."""
    return sum(1 + count_comments(c.get("replies") or []) for c in comments)


def record_from_hit(hit):
    """Any index row -> its shape with a `type`: a comment gets
    type "comment" in front, a story / job / poll is the story shape."""
    if hit_kind(hit) == "comment":
        comment = comment_from_hit(hit)
        return {"id": comment["id"], "type": "comment", **{k: v for k, v in comment.items() if k != "id"}}
    if hit_kind(hit) == "pollopt":
        # the index keeps neither an option's text nor its poll: a caller that needs them reads the official API
        return _poll_option_record(to_int(as_dict(hit).get("objectID")), None, None, as_dict(hit).get("author"),
                                   to_int(as_dict(hit).get("points")), epoch_utc(as_dict(hit).get("created_at_i")))
    return story_from_hit(hit)


def record_from_item(item):
    """Any official-API item -> its shape with a `type` (see record_from_hit)."""
    item = as_dict(item)
    kind = item.get("type")
    if kind == "comment":
        comment = comment_from_item(item)
        return {"id": comment["id"], "type": "comment", **{k: v for k, v in comment.items() if k != "id"}}
    if kind == "pollopt":
        return _poll_option_record(to_int(item.get("id")), item.get("text"), to_int(item.get("poll")),
                                   item.get("by"), to_int(item.get("score")), epoch_utc(item.get("time")),
                                   is_dead=item.get("dead"), is_deleted=item.get("deleted"))
    return story_from_item(item)


def _poll_option_record(item_id, text_html, poll_id, username, points, created, is_dead=False, is_deleted=False):
    text, _ = text_and_links(text_html)
    return {"id": item_id, "type": "poll_option", "link": refs.item_link(item_id), "text": text,
            "poll_id": poll_id, "author": _author(username), "points": points, "is_dead": bool(is_dead),
            "is_deleted": bool(is_deleted), "created_at": created}


# ---- result pages ----------------------------------------------------------------------

def parse_search(payload, parse_row, page, per_page):
    """A result page of the index -> {"items", "pagination"}. The index
    serves a query 1,000 rows deep: total_count is what the site says
    matched, total_pages what can be read."""
    payload = as_dict(payload)
    items = [parse_row(hit) for hit in as_list(payload.get("hits")) if isinstance(hit, dict)]
    return {
        "items": items,
        "pagination": {
            "page": page,
            "items_per_page": per_page,
            "total_pages": to_int(payload.get("nbPages")) or 0,
            "total_count": to_int(payload.get("nbHits")) or 0,
        },
    }


# ---- users -----------------------------------------------------------------------------

def parse_user(user):
    """An official-API user -> the profile a visitor sees: name, karma,
    the "about" text, when the account was made, and how many items
    (stories, comments, poll options) it has submitted."""
    user = as_dict(user)
    about, links = text_and_links(user.get("about"))
    username = clean(user.get("id"))
    return {
        "username": username,
        "link": refs.user_link(username),
        "about": about,
        "about_links": links,
        "karma": to_int(user.get("karma")),
        "submission_count": len(as_list(user.get("submitted"))),
        "created_at": epoch_utc(user.get("created")),
    }


# ---- the front page of a past day ------------------------------------------------------

def parse_front_page(page_html):
    """The HTML of /front?day=… -> {"date", "stories", "has_next"}. Each
    story is in the story shape with its `rank` on that day's page first;
    `text` is not on the page (the caller fills it from the index)."""
    soup = BeautifulSoup(page_html or "", "lxml")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    day = re.match(r"(\d{4}-\d{2}-\d{2}) front", title)
    stories = []
    for row in soup.select("tr.athing"):
        item_id = to_int(row.get("id"))
        anchor = row.select_one("span.titleline > a")
        if not item_id or anchor is None:
            continue
        sub = row.find_next_sibling("tr")
        sub = sub.select_one("td.subtext") if sub is not None else None
        score = sub.select_one("span.score") if sub is not None else None
        user = sub.select_one('a[href^="user?id="]') if sub is not None else None
        age = sub.select_one("span.age") if sub is not None else None
        comments = None
        if sub is not None:
            for link in sub.select(f'a[href="item?id={item_id}"]'):
                label = link.get_text(" ", strip=True).replace("\xa0", " ")
                if "comment" in label:
                    comments = first_int(label)
                elif label == "discuss":
                    comments = 0
        stamp = (age.get("title") or "").split(" ")[0] if age is not None else ""
        is_job = score is None and user is None
        story = _story(item_id, "job" if is_job else "story", anchor.get_text(" ", strip=True),
                       _article_link(anchor.get("href")), None, user.get_text(strip=True) if user is not None else None,
                       first_int(score.get_text()) if score is not None else None, comments, iso_utc(stamp) if stamp else None)
        rank = row.select_one("span.rank")
        stories.append({"rank": first_int(rank.get_text()) if rank is not None else None, **story})
    return {
        "date": day.group(1) if day else None,
        "stories": stories,
        "has_next": soup.select_one("a.morelink") is not None,
    }


# ---- "Who is hiring?" ------------------------------------------------------------------

def thread_from_hit(hit):
    """A monthly thread row -> {id, kind, month, title, link, comment_count,
    created_at}; None when the title is not one of the two monthly threads."""
    hit = as_dict(hit)
    title = clean(hit.get("title")) or ""
    low = title.lower()
    kind = next((name for name, words in refs.JOB_THREADS.items() if words in low), None)
    month = _THREAD_MONTH_RE.search(title)
    if not kind or not month:
        return None
    item_id = to_int(hit.get("objectID"))
    return {
        "id": item_id,
        "kind": kind,
        "month": f"{month.group(2)}-{_MONTHS.index(month.group(1).lower()) + 1:02d}",
        "title": title,
        "link": refs.item_link(item_id),
        "comment_count": to_int(hit.get("num_comments")),
        "created_at": epoch_utc(hit.get("created_at_i")),
    }


def job_post(hit):
    """A top-level comment of a monthly thread -> one posting: the text as
    written, its first line (most are "Company | Role | Location | …") and
    that line's parts, and the emails and links the text carries."""
    hit = as_dict(hit)
    text, links = text_and_links(hit.get("comment_text"))
    headline = (text or "").split("\n", 1)[0].strip() or None
    parts = [part.strip() for part in headline.split("|")] if headline and "|" in headline else []
    emails = []
    for email in _EMAIL_RE.findall(text or ""):
        email = email.rstrip(".")
        if email.lower() not in (e.lower() for e in emails):
            emails.append(email)
    item_id = to_int(hit.get("objectID"))
    return {
        "id": item_id,
        "link": refs.item_link(item_id),
        "headline": headline,
        "headline_parts": [part for part in parts if part],
        "text": text,
        "emails": emails,
        "links": links,
        "author": _author(hit.get("author")),
        "reply_count": len(as_list(hit.get("children"))),
        "created_at": epoch_utc(hit.get("created_at_i")) or iso_utc(hit.get("created_at")),
    }


# ---- updates ---------------------------------------------------------------------------

def parse_updates(updates, newest_id):
    """The official API's updates + maxitem -> the ids and usernames that
    changed in the last moments and the newest item id."""
    updates = as_dict(updates)
    return {
        "max_item_id": to_int(newest_id),
        "item_ids": [i for i in as_list(updates.get("items")) if isinstance(i, int)],
        "usernames": [u for u in as_list(updates.get("profiles")) if isinstance(u, str)],
    }
