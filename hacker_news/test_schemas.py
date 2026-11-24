"""Offline tests for the /hacker-news/* request schemas and the reference
resolvers.

    python -m pytest hacker_news/test_schemas.py -q
"""
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hacker_news import refs, schemas  # noqa: E402
from schema_fields import load_query  # noqa: E402

SITE = "https://news.ycombinator.com"
TOMORROW = (datetime.now(timezone.utc).date() + timedelta(days=1)).isoformat()


def ok(schema, query):
    data, error = load_query(schema, query)
    assert error is None, error
    return data


def bad(schema, query, field):
    data, error = load_query(schema, query)
    assert data is None and field in error["errors"], error
    return " ".join(error["errors"][field])


# ---- refs --------------------------------------------------------------------------

@pytest.mark.parametrize("value, expected", [
    ("8863", 8863), (" 8863 ", 8863), ("#8863", 8863),
    (f"{SITE}/item?id=8863", 8863), (f"{SITE}/item?id=8863&p=2#9224", 8863),
    ("news.ycombinator.com/item?id=121003", 121003), ("//news.ycombinator.com/item?id=1", 1),
    ("http://news.ycombinator.com/item?id=50588106", 50588106),
])
def test_resolve_item(value, expected):
    assert refs.resolve_item(value) == expected


@pytest.mark.parametrize("value", [
    "", "abc", "0", "-5", "88 63", "8863.5", "1" * 13, f"{SITE}/item", f"{SITE}/item?id=abc", f"{SITE}/user?id=pg",
    "https://example.com/item?id=8863", "https://hn.algolia.com/?q=8863",
])
def test_resolve_item_rejects(value):
    with pytest.raises(ValueError) as e:
        refs.resolve_item(value)
    assert "item id" in str(e.value)


def test_resolve_items():
    assert refs.resolve_items(f"8863, 121003 ,{SITE}/item?id=8863,,") == [8863, 121003]
    with pytest.raises(ValueError):
        refs.resolve_items(" , ")
    with pytest.raises(ValueError):
        refs.resolve_items("8863,abc")
    assert len(refs.resolve_items(",".join(str(n) for n in range(1, refs.MAX_BATCH_ITEMS + 1)))) == 1000
    with pytest.raises(ValueError) as e:
        refs.resolve_items(",".join(str(n) for n in range(1, refs.MAX_BATCH_ITEMS + 2)))
    assert "1000" in str(e.value)


@pytest.mark.parametrize("value, expected", [
    ("pg", "pg"), ("@pg", "pg"), ("PG", "PG"), ("mike_hearn", "mike_hearn"), ("a-b_c1", "a-b_c1"),
    (f"{SITE}/user?id=pg", "pg"), (f"{SITE}/submitted?id=pg", "pg"), (f"{SITE}/threads?id=pg&next=1", "pg"),
    (f"{SITE}/favorites?id=dang", "dang"), ("news.ycombinator.com/user?id=tptacek", "tptacek"),
])
def test_resolve_user(value, expected):
    assert refs.resolve_user(value) == expected


@pytest.mark.parametrize("value", [
    "", "x", "a b", "name!", "x" * 16, f"{SITE}/user", f"{SITE}/item?id=8863", "https://example.com/user?id=pg",
])
def test_resolve_user_rejects(value):
    with pytest.raises(ValueError) as e:
        refs.resolve_user(value)
    assert "username" in str(e.value)


@pytest.mark.parametrize("value, expected", [
    ("github.com", "github.com"), ("GitHub.com", "github.com"), ("www.github.com", "github.com"),
    ("https://github.com/torvalds/linux", "github.com"), ("blog.rust-lang.org/2026/", "blog.rust-lang.org"),
    ("http://www.bbc.co.uk/news?x=1", "bbc.co.uk"),
])
def test_resolve_site(value, expected):
    assert refs.resolve_site(value) == expected


@pytest.mark.parametrize("value", ["", "github", "not a domain", "http://", "a..b", "-x.com", "x.c"])
def test_resolve_site_rejects(value):
    with pytest.raises(ValueError) as e:
        refs.resolve_site(value)
    assert "domain" in str(e.value)


def test_bounds():
    assert refs.day_bounds("2020-01-01") == (1577836800, 1577923200)
    assert refs.month_bounds("2026-11") == (1793491200, 1796083200)
    start, end = refs.month_bounds("2026-12")
    assert end - start == 31 * 86400


def test_links():
    assert refs.item_link(8863) == f"{SITE}/item?id=8863" and refs.item_link(None) is None
    assert refs.user_link("pg") == f"{SITE}/user?id=pg" and refs.user_link(None) is None


# ---- stories/search ------------------------------------------------------------------

def test_stories_search_defaults():
    data = ok(schemas.StoriesSearchSchema, {"query": "  rust   lang "})
    assert data == {"query": "rust lang", "type": "story", "sort": None, "date_from": None, "date_to": None,
                    "min_points": None, "min_comments": None, "author": None, "site": None, "page": 1}
    assert ok(schemas.StoriesSearchSchema, {})["query"] is None


def test_stories_search_accepts():
    data = ok(schemas.StoriesSearchSchema, {
        "query": "rust", "type": "Show_HN", "sort": "DATE", "date_from": "2020-01-01", "date_to": "2020-12-31",
        "min_points": "100", "min_comments": "10", "author": f"{SITE}/user?id=pg", "page": "3"})
    assert (data["type"], data["sort"], data["author"], data["page"]) == ("show_hn", "date", "pg", 3)
    assert (data["min_points"], data["min_comments"]) == (100, 10)
    assert ok(schemas.StoriesSearchSchema, {"site": "https://github.com/a/b"})["site"] == "github.com"
    for kind in refs.STORY_TYPES:
        assert ok(schemas.StoriesSearchSchema, {"type": kind})["type"] == kind


def test_an_empty_param_is_the_absent_param():
    data = ok(schemas.StoriesSearchSchema, {"query": "rust", "sort": "", "page": "", "date_from": " ",
                                            "min_points": "", "author": "", "type": ""})
    assert data == ok(schemas.StoriesSearchSchema, {"query": "rust"})
    assert bad(schemas.StorySchema, {"story": ""}, "story")     # a required one is still refused


@pytest.mark.parametrize("query, field, words", [
    ({"query": "rust", "sort": "newest"}, "sort", "popularity, date"),
    ({"type": "comment"}, "type", "story, ask_hn"),
    ({"type": "job", "sort": "popularity"}, "sort", "by date only"),
    ({"type": "job", "min_points": "5"}, "min_points", "neither points"),
    ({"site": "github.com", "query": "rust"}, "site", "cannot be combined"),
    ({"query": "rust", "min_points": "0"}, "min_points", "greater than or equal to 1"),
    ({"query": "rust", "min_comments": "many"}, "min_comments", "integer"),
    ({"date_from": "2021-01-01", "date_to": "2020-01-01"}, "date_from", "must not be after"),
    ({"date_from": "01/02/2020"}, "date_from", "YYYY-MM-DD"),
    ({"date_from": "2006-01-01"}, "date_from", "starts on 2007-02-19"),
    ({"date_to": TOMORROW}, "date_to", "earlier day"),
    ({"author": "a b"}, "author", "username"),
    ({"site": "not a domain"}, "site", "domain"),
    ({"page": "0"}, "page", "less than or equal to 100"),
    ({"page": "101"}, "page", "less than or equal to 100"),
    ({"query": "x" * 201}, "query", "200"),
    ({"limit": "5"}, "limit", "Unknown field"),
])
def test_stories_search_rejects(query, field, words):
    assert words in bad(schemas.StoriesSearchSchema, query, field)


# ---- comments/search -----------------------------------------------------------------

def test_comments_search():
    assert ok(schemas.CommentsSearchSchema, {}) == {
        "query": None, "author": None, "story": None, "sort": "date", "date_from": None, "date_to": None, "page": 1}
    data = ok(schemas.CommentsSearchSchema, {"query": "usb", "story": f"{SITE}/item?id=8863", "author": "pg",
                                             "sort": "relevance", "page": "2"})
    assert (data["story"], data["author"], data["sort"], data["page"]) == (8863, "pg", "relevance", 2)
    assert "date, relevance" in bad(schemas.CommentsSearchSchema, {"sort": "points"}, "sort")
    assert "item id" in bad(schemas.CommentsSearchSchema, {"story": "abc"}, "story")
    assert "starts on" in bad(schemas.CommentsSearchSchema, {"date_to": "2006-01-01"}, "date_to")


# ---- lists ---------------------------------------------------------------------------

def test_stories_list():
    assert ok(schemas.StoriesListSchema, {}) == {"list": "top", "page": 1}
    for name in refs.STORY_LISTS:
        assert ok(schemas.StoriesListSchema, {"list": name.upper()})["list"] == name
    assert "top, new, best, ask, show, jobs" in bad(schemas.StoriesListSchema, {"list": "hot"}, "list")
    assert bad(schemas.StoriesListSchema, {"page": "0"}, "page")


def test_stories_past():
    assert ok(schemas.StoriesPastSchema, {}) == {"date": None, "page": 1}
    assert ok(schemas.StoriesPastSchema, {"date": "2026-11-20", "page": "2"}) == {"date": "2026-11-20", "page": 2}
    assert ok(schemas.StoriesPastSchema, {"date": "2007-02-19"})["date"] == "2007-02-19"
    assert "starts on 2007-02-19" in bad(schemas.StoriesPastSchema, {"date": "2007-02-18"}, "date")
    assert "YYYY-MM-DD" in bad(schemas.StoriesPastSchema, {"date": "notadate"}, "date")
    assert "Invalid date" in bad(schemas.StoriesPastSchema, {"date": "2026-02-30"}, "date")
    assert "earlier day" in bad(schemas.StoriesPastSchema, {"date": TOMORROW}, "date")


# ---- one item, many items ------------------------------------------------------------

def test_story_and_comment():
    assert ok(schemas.StorySchema, {"story": "8863"}) == {"story": 8863}
    assert ok(schemas.StorySchema, {"story": f"{SITE}/item?id=8863"}) == {"story": 8863}
    assert ok(schemas.CommentSchema, {"comment": "2921983"}) == {"comment": 2921983}
    assert "Missing data" in bad(schemas.StorySchema, {}, "story")
    assert "item id" in bad(schemas.StorySchema, {"story": "https://example.com/item?id=8863"}, "story")
    assert "item id" in bad(schemas.CommentSchema, {"comment": "abc"}, "comment")
    assert "Unknown field" in bad(schemas.StorySchema, {"story": "8863", "id": "8863"}, "id")


def test_stories_batch():
    assert ok(schemas.StoriesBatchSchema, {"ids": f"8863, {SITE}/item?id=121003,8863"}) == {"ids": [8863, 121003]}
    assert "item id" in bad(schemas.StoriesBatchSchema, {"ids": "8863,x"}, "ids")
    assert "At most 1000" in bad(schemas.StoriesBatchSchema, {"ids": ",".join(str(n) for n in range(1, 1002))}, "ids")
    assert "Missing data" in bad(schemas.StoriesBatchSchema, {}, "ids")


# ---- users ---------------------------------------------------------------------------

def test_users():
    assert ok(schemas.UserSchema, {"username": f"{SITE}/user?id=pg"}) == {"username": "pg"}
    assert ok(schemas.UserStoriesSchema, {"username": "pg"}) == {"username": "pg", "sort": "date", "page": 1}
    assert ok(schemas.UserStoriesSchema, {"username": "pg", "sort": "popularity", "page": "2"})["sort"] == "popularity"
    assert ok(schemas.UserCommentsSchema, {"username": "PG"}) == {"username": "PG", "page": 1}
    assert "username" in bad(schemas.UserSchema, {"username": "a b"}, "username")
    assert "date, popularity" in bad(schemas.UserStoriesSchema, {"username": "pg", "sort": "x"}, "sort")
    assert "Unknown field" in bad(schemas.UserCommentsSchema, {"username": "pg", "sort": "date"}, "sort")


# ---- jobs, updates -------------------------------------------------------------------

def test_jobs_search():
    assert ok(schemas.JobsSearchSchema, {}) == {"query": None, "month": None, "thread": "hiring", "page": 1}
    data = ok(schemas.JobsSearchSchema, {"query": "remote rust", "month": "2026-10", "thread": "wants_to_be_hired"})
    assert (data["month"], data["thread"]) == ("2026-10", "wants_to_be_hired")
    assert "hiring, wants_to_be_hired" in bad(schemas.JobsSearchSchema, {"thread": "freelancer"}, "thread")
    assert bad(schemas.JobsSearchSchema, {"month": "2026-13"}, "month")
    assert "current month or earlier" in bad(schemas.JobsSearchSchema, {"month": "2099-01"}, "month")


def test_updates_takes_no_params():
    assert ok(schemas.UpdatesSchema, {}) == {}
    assert "Unknown field" in bad(schemas.UpdatesSchema, {"since": "1"}, "since")


def test_every_field_is_described():
    for name in dir(schemas):
        schema = getattr(schemas, name)
        if isinstance(schema, type) and issubclass(schema, schemas.BaseSchema) and schema is not schemas.BaseSchema:
            for field_name, field in schema().fields.items():
                assert field.metadata.get("description"), f"{name}.{field_name} has no description"
