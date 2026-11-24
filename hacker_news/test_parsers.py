"""Offline tests for the Hacker News parsers against the fixtures.

    python -m pytest hacker_news/test_parsers.py -q
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hacker_news import parsers  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")

STORY_KEYS = ["id", "type", "title", "link", "article_link", "domain", "text", "links", "author", "points",
              "comment_count", "is_ask_hn", "is_show_hn", "is_launch_hn", "is_on_front_page", "is_dead",
              "is_deleted", "created_at"]
COMMENT_KEYS = ["id", "link", "text", "links", "author", "parent_id", "story", "reply_count", "is_dead",
                "is_deleted", "created_at"]


def text(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return f.read()


def load(name):
    return json.loads(text(name))


def search(name, parse_row):
    return parsers.parse_search(load(name), parse_row, 1, 100)


# ---- text ------------------------------------------------------------------------------

def test_text_and_links():
    html = ('Hi &amp; welcome<p>See <a href="https:&#x2F;&#x2F;example.com&#x2F;a&#x2F;very&#x2F;long&#x2F;path" '
            'rel="nofollow">https:&#x2F;&#x2F;example.com&#x2F;a&#x2F;very...</a> and <i>this</i>.<p>'
            '<pre><code>x &lt; y</code></pre>')
    plain, links = parsers.text_and_links(html)
    assert links == ["https://example.com/a/very/long/path"]
    assert plain.startswith("Hi & welcome\nSee https://example.com/a/very/long/path and this.")
    assert "x < y" in plain and "..." not in plain and "<" not in plain.replace("x < y", "")
    assert parsers.text_and_links(None) == (None, [])
    assert parsers.text_and_links("  ") == (None, [])
    assert parsers.text_and_links("plain words") == ("plain words", [])


def test_links_written_as_plain_text_count():
    about = "Creator of X.<p>https:&#x2F;&#x2F;simonwillison.net&#x2F; and (https:&#x2F;&#x2F;til.simonwillison.net&#x2F;a_(b))."
    plain, links = parsers.text_and_links(about)
    assert links == ["https://simonwillison.net/", "https://til.simonwillison.net/a_(b)"]
    assert plain == "Creator of X.\nhttps://simonwillison.net/ and (https://til.simonwillison.net/a_(b))."
    anchored = 'See <a href="https://a.example/x">https://a.example/x</a>. Also https://b.example/y, ok'
    assert parsers.text_and_links(anchored)[1] == ["https://a.example/x", "https://b.example/y"]


def test_text_keeps_each_link_once():
    html = '<a href="https://a.example/x">a</a> <a href="https://a.example/x">again</a> <a href="https://b.example">b</a>'
    assert parsers.text_and_links(html)[1] == ["https://a.example/x", "https://b.example"]


@pytest.mark.parametrize("link, expected", [
    ("https://www.github.com/a/b", "github.com"), ("http://blog.Example.co.uk/x?y=1", "blog.example.co.uk"),
    (None, None), ("", None), ("not a link", None),
])
def test_domain_of(link, expected):
    assert parsers.domain_of(link) == expected


# ---- stories ---------------------------------------------------------------------------

def test_search_stories():
    result = search("search_stories_rust.json", parsers.story_from_hit)
    assert result["pagination"] == {"page": 1, "items_per_page": 100, "total_pages": 34, "total_count": 36164}
    items = result["items"]
    assert len(items) == 30 and all(list(item) == STORY_KEYS for item in items)
    first = items[0]
    assert first["id"] == 22238335 and first["type"] == "story"
    assert first["title"] == "Why Discord is switching from Go to Rust"
    assert first["link"] == "https://news.ycombinator.com/item?id=22238335"
    assert first["article_link"].startswith("https://blog.discordapp.com/")
    assert first["domain"] == "blog.discordapp.com"
    assert first["author"] == {"username": "Sikul", "link": "https://news.ycombinator.com/user?id=Sikul"}
    assert (first["points"], first["comment_count"]) == (1582, 642)
    assert first["created_at"] == "2020-02-04T17:30:40Z"
    assert first["text"] is None and first["links"] == []
    assert first["is_on_front_page"] is False and first["is_dead"] is False
    with_text = [item for item in items if item["text"]]
    assert len(with_text) == 1 and "<p>" not in with_text[0]["text"] and "&#x" not in with_text[0]["text"]


def test_search_stories_flags_follow_the_tags():
    items = search("search_stories_by_date.json", parsers.story_from_hit)["items"]
    assert [i["created_at"] for i in items] == sorted((i["created_at"] for i in items), reverse=True)
    show = [i for i in items if i["is_show_hn"]]
    ask = [i for i in items if i["is_ask_hn"]]
    assert show and all(i["title"].startswith("Show HN") for i in show)
    assert ask and all(i["article_link"] is None for i in ask)


def test_search_past_end_and_no_result():
    for name in ("search_past_end.json", "search_no_result.json"):
        result = search(name, parsers.story_from_hit)
        assert result["items"] == []
    assert search("search_no_result.json", parsers.story_from_hit)["pagination"]["total_count"] == 0
    assert parsers.parse_search({}, parsers.story_from_hit, 3, 100) == {
        "items": [], "pagination": {"page": 3, "items_per_page": 100, "total_pages": 0, "total_count": 0}}


def test_search_site_rows_point_to_the_site():
    items = search("search_site_github.json", parsers.story_from_hit)["items"]
    assert len(items) == 30 and all(i["domain"].endswith("github.com") for i in items)


def test_story_from_item():
    story = parsers.story_from_item(load("item_story_8863.json"))
    assert list(story) == STORY_KEYS
    assert story["id"] == 8863 and story["type"] == "story"
    assert story["title"] == "My YC app: Dropbox - Throw away your USB drive"
    assert story["article_link"] == "http://www.getdropbox.com/u/2/screencast.html"
    assert story["domain"] == "getdropbox.com"
    assert (story["points"], story["comment_count"]) == (104, 71)
    assert story["created_at"] == "2007-04-04T19:16:40Z"
    assert story["is_on_front_page"] is None        # the official API does not say
    assert parsers.story_from_item(load("item_story_8863.json"), tags=["story", "front_page"])["is_on_front_page"] is True


def test_ask_hn_item_has_text_and_no_article():
    story = parsers.story_from_item(load("item_ask_121003.json"))
    assert story["is_ask_hn"] is True and story["is_show_hn"] is False
    assert story["article_link"] is None and story["domain"] is None
    assert story["text"].startswith("or HN: the Next Iteration\nI get the impression")
    assert "<" not in story["text"] and "&#x27;" not in story["text"]


def test_job_items():
    job = parsers.story_from_item(load("item_job_text_192327.json"))
    assert job["type"] == "job" and job["article_link"] is None     # the API sends url: ""
    assert job["text"].startswith("Justin.tv is the biggest live video site online.")
    assert job["comment_count"] is None and job["is_ask_hn"] is False
    linked = parsers.story_from_item(load("item_job_link_50545238.json"))
    assert linked["type"] == "job" and linked["domain"] == "ycombinator.com" and linked["text"] is None


def test_poll_item_and_option():
    poll = parsers.story_from_item(load("item_poll_126809.json"))
    assert poll["type"] == "poll" and poll["is_ask_hn"] is False and poll["comment_count"] == 54
    option = parsers.poll_option(load("item_poll_option_160705.json"))
    assert option == {"id": 160705, "points": 335,
                      "text": "Yes, ban them; I'm tired of seeing Valleywag stories on News.YC."}
    record = parsers.record_from_item(load("item_poll_option_160705.json"))
    assert record["type"] == "poll_option" and record["poll_id"] == 160704
    assert record["author"]["username"] == "pg"


def test_dead_and_deleted_items():
    dead_story = parsers.record_from_item(load("item_dead_story.json"))
    assert dead_story["is_dead"] is True and dead_story["title"] is None and dead_story["author"]["username"]
    dead = parsers.record_from_item(load("item_dead_comment.json"))
    assert dead["type"] == "comment" and dead["is_dead"] is True
    assert dead["text"] is None                     # the API's "[dead]" placeholder is not a text
    assert dead["author"]["username"] == "clinexaadmin"
    deleted = parsers.record_from_item(load("item_deleted_comment.json"))
    assert deleted["is_deleted"] is True and deleted["author"] is None and deleted["text"] is None
    assert deleted["parent_id"] == 50579715 and deleted["created_at"] == "2026-11-20T19:02:47Z"


def test_partial_payloads_do_not_raise():
    for parse in (parsers.story_from_hit, parsers.story_from_item, parsers.story_from_tree, parsers.comment_from_hit,
                  parsers.comment_from_item, parsers.comment_from_tree, parsers.record_from_hit,
                  parsers.record_from_item, parsers.parse_user, parsers.job_post, parsers.poll_option):
        for junk in ({}, None, {"id": None, "type": None}, []):
            parse(junk)
    assert parsers.thread_from_hit({}) is None
    assert parsers.parse_front_page("")["stories"] == []
    assert parsers.parse_updates(None, None) == {"max_item_id": None, "item_ids": [], "usernames": []}


# ---- the index's "get objects" ---------------------------------------------------------

def test_objects_mixed_kinds():
    rows = load("objects_mixed.json")["results"]
    assert rows[4] is None                          # an id the index does not hold
    story, comment, poll, job = (parsers.record_from_hit(row) for row in rows[:4])
    assert (story["type"], comment["type"], poll["type"], job["type"]) == ("story", "comment", "poll", "job")
    assert list(comment)[:2] == ["id", "type"]
    assert comment["story"] == {
        "id": 2921506, "title": "Peter Norvig on a 45-year-old article about a checkers-playing program",
        "link": "https://news.ycombinator.com/item?id=2921506",
        "article_link": comment["story"]["article_link"]}
    assert comment["parent_id"] == 2921506 and comment["reply_count"] == 7
    assert job["text"].startswith("Justin.tv is the biggest") and job["points"] is None


def test_objects_front_page_and_jobs():
    top = [parsers.story_from_hit(row) for row in load("objects_topstories_30.json")]
    assert len(top) == 30 and all(s["id"] and s["title"] and s["created_at"] for s in top)
    assert any(s["is_on_front_page"] for s in top)
    jobs = [parsers.story_from_hit(row) for row in load("objects_jobstories.json")["results"]]
    assert jobs and all(j["type"] == "job" and j["comment_count"] is None for j in jobs)


# ---- comments --------------------------------------------------------------------------

def test_search_comments():
    items = search("search_comments_rust.json", parsers.comment_from_hit)["items"]
    assert len(items) == 30 and all(list(item) == COMMENT_KEYS for item in items)
    first = items[0]
    assert first["id"] == 50587925 and first["parent_id"] == 50581277
    assert first["story"]["id"] == 50527444 and first["story"]["title"]
    assert first["author"]["username"] == "mike_hearn"
    assert first["text"].startswith("Are you assuming here that AI will never, ever code a memory leak?")
    assert all("<p>" not in (i["text"] or "") and "&#x27;" not in (i["text"] or "") for i in items)
    assert "points" not in first                    # the site shows a comment's score to no one


def test_comment_from_item():
    comment = parsers.comment_from_item(load("item_comment_2921983.json"))
    assert list(comment) == COMMENT_KEYS
    assert comment["author"]["username"] == "norvig" and comment["parent_id"] == 2921506
    assert comment["reply_count"] == 7 and comment["story"] is None
    assert comment["text"].startswith("Aw shucks, guys ... you make me blush with your compliments.\nTell you what")


def test_comment_tree():
    tree = load("tree_story_8863.json")
    story = parsers.story_from_tree(tree)
    assert story["id"] == 8863 and story["points"] == 104 and story["comment_count"] is None
    comments = [parsers.comment_from_tree(node) for node in tree["children"]]
    assert len(comments) == 32 and parsers.count_comments(comments) == 71
    first = comments[0]
    assert list(first) == COMMENT_KEYS + ["replies"]
    assert first["id"] == 8865 and first["text"] == "oh, and a mac port is coming :)"
    assert first["reply_count"] == 1 and first["replies"][0]["parent_id"] == 8865
    assert first["replies"][0]["story"]["id"] == 8863


def test_comment_tree_takes_the_sites_order():
    tree = load("tree_comment_2921983.json")
    kids = load("item_comment_2921983.json")["kids"]
    comment = parsers.comment_from_tree(tree, order=kids)
    held = [k for k in kids if k in {r["id"] for r in comment["replies"]}]
    assert [r["id"] for r in comment["replies"]][:len(held)] == held
    assert comment["author"]["username"] == "norvig" and comment["story"]["id"] == 2921506


def test_tree_from_flat_rows_matches_the_tree_call():
    hits = load("search_story_comments_8863.json")["hits"]
    nodes = parsers.tree_from_hits(hits, 8863)
    rebuilt = [parsers.comment_from_tree(node) for node in nodes]
    direct = [parsers.comment_from_tree(node) for node in load("tree_story_8863.json")["children"]]
    assert parsers.count_comments(rebuilt) == parsers.count_comments(direct) == 71
    assert sorted(c["id"] for c in rebuilt) == sorted(c["id"] for c in direct)
    by_id = {c["id"]: c for c in direct}
    for comment in rebuilt:
        assert comment["text"] == by_id[comment["id"]]["text"]
        assert comment["reply_count"] == by_id[comment["id"]]["reply_count"]


def test_tree_not_found_fixture_is_the_404_body():
    assert load("tree_not_found.json") == {"error": "Not Found", "status": 404}
    assert text("item_not_found.null.txt").strip() == "null"
    assert text("user_not_found.null.txt").strip() == "null"


# ---- users -----------------------------------------------------------------------------

def test_user():
    user = parsers.parse_user(load("user_pg.json"))
    assert user == {"username": "pg", "link": "https://news.ycombinator.com/user?id=pg", "about": "Bug fixer.",
                    "about_links": [], "karma": 157316, "submission_count": 15565,
                    "created_at": "2006-10-09T18:21:32Z"}


def test_user_without_about():
    user = parsers.parse_user(load("user_no_about.json"))
    assert user["username"] == "ranguna" and user["about"] is None and user["about_links"] == []
    assert user["karma"] == 1322 and user["submission_count"] == 1217


# ---- the front page of a past day ------------------------------------------------------

def test_front_page():
    page = parsers.parse_front_page(text("front_2026-11-20.html"))
    assert page["date"] == "2026-11-20" and page["has_next"] is True
    stories = page["stories"]
    assert [s["rank"] for s in stories] == list(range(1, 31))
    first = stories[0]
    assert list(first) == ["rank"] + STORY_KEYS
    assert first["id"] == 50574968 and first["article_link"] == "https://alpo.ge/remark.pdf"
    assert first["domain"] == "alpo.ge" and first["author"]["username"] == "iamronaldo"
    assert first["points"] > 2000 and first["comment_count"] > 1000
    assert first["created_at"] == "2026-11-20T14:16:21Z"


def test_front_page_second_page_and_past_the_end():
    second = parsers.parse_front_page(text("front_2026-11-20_page_2.html"))
    assert [s["rank"] for s in second["stories"]] == list(range(31, 61))
    first_ids = {s["id"] for s in parsers.parse_front_page(text("front_2026-11-20.html"))["stories"]}
    assert not first_ids & {s["id"] for s in second["stories"]}
    end = parsers.parse_front_page(text("front_past_end.html"))
    assert end == {"date": "2026-11-20", "stories": [], "has_next": False}


def test_front_page_text_post_and_first_day():
    page = parsers.parse_front_page(text("front_with_ask.html"))
    ask = next(s for s in page["stories"] if s["id"] == 50583761)
    assert ask["article_link"] is None and ask["is_ask_hn"] is True and ask["rank"] == 7
    assert (ask["points"], ask["comment_count"]) == (273, 241)
    first_day = parsers.parse_front_page(text("front_first_day.html"))
    assert first_day["date"] == "2007-02-19" and len(first_day["stories"]) == 30
    assert first_day["stories"][0]["id"] == 110


def test_refusal_pages_are_not_front_pages():
    for name in ("rate_limited_503.html", "front_future.txt", "front_before_start.txt"):
        assert parsers.parse_front_page(text(name))["stories"] == []
    assert "not able to serve your requests this quickly" in text("rate_limited_503.html")
    assert text("front_future.txt").startswith("We don't have that data yet")
    assert text("front_before_start.txt").startswith("HN didn't exist yet")


# ---- "Who is hiring?" ------------------------------------------------------------------

def test_hiring_threads():
    threads = [parsers.thread_from_hit(hit) for hit in load("hiring_threads.json")["hits"]]
    assert all(threads) and len(threads) == 12
    assert threads[0] == {"id": 50338728, "kind": "hiring", "month": "2026-11",
                          "title": "Ask HN: Who is hiring? (November 2026)",
                          "link": "https://news.ycombinator.com/item?id=50338728", "comment_count": 380,
                          "created_at": "2026-11-02T16:01:37Z"}
    assert threads[1]["kind"] == "wants_to_be_hired" and threads[1]["month"] == "2026-11"
    assert {t["month"] for t in threads} == {"2026-06", "2026-07", "2026-08", "2026-09", "2026-10", "2026-11"}
    assert parsers.thread_from_hit({"objectID": "1", "title": "Ask HN: Who is using Rust?"}) is None


def test_job_posts():
    posts = search("hiring_posts_remote_rust.json", parsers.job_post)["items"]
    assert len(posts) == 21
    first = posts[0]
    assert list(first) == ["id", "link", "headline", "headline_parts", "text", "emails", "links", "author",
                           "reply_count", "created_at"]
    assert first["headline_parts"] == ["Common Prefix", "Senior Software Engineer", "Athens, Greece / REMOTE",
                                       "Full-time", "$80k to $160k + equity + bonus"]
    assert first["headline"] == " | ".join(first["headline_parts"])
    assert first["text"].startswith(first["headline"]) and first["author"]["username"] == "commonprefix"
    assert all(p["headline"] and "<" not in p["headline"] for p in posts)
    emails = [e for p in posts for e in p["emails"]]
    assert emails and all("@" in e and not e.endswith(".") for e in emails)
    assert any(p["links"] for p in posts)
    free_text = [p for p in posts if not p["headline_parts"]]
    assert all("|" not in p["headline"] for p in free_text)


# ---- updates ---------------------------------------------------------------------------

def test_updates():
    updates = parsers.parse_updates(load("updates.json"), load("maxitem.json"))
    assert updates["max_item_id"] == 50588106
    assert len(updates["item_ids"]) == 36 and len(updates["usernames"]) == 17
    assert all(isinstance(i, int) for i in updates["item_ids"])


# ---- a user's rows, a poll's tree ------------------------------------------------------

def test_user_stories_and_comments_rows():
    stories = search("search_user_stories_pg.json", parsers.story_from_hit)
    assert stories["pagination"]["total_count"] == 699
    assert stories["items"] and all(s["author"]["username"] == "pg" and s["type"] == "story" for s in stories["items"])
    comments = search("search_user_comments_pg.json", parsers.comment_from_hit)
    assert comments["pagination"]["total_count"] == 9985
    assert all(c["author"]["username"] == "pg" and c["story"]["id"] and c["parent_id"] for c in comments["items"])
    assert comments["items"][0]["story"]["title"] == "The Best Essay"


def test_poll_tree():
    tree = load("tree_poll_126809.json")
    poll = parsers.story_from_tree(tree)
    assert poll["type"] == "poll" and poll["title"].startswith("Poll: What would happen")
    comments = [parsers.comment_from_tree(node) for node in tree["children"]]
    assert len(comments) == 24 and all(c["parent_id"] == 126809 for c in comments)
