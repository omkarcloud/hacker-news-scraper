"""Offline tests for the Hacker News transport and endpoint functions: a
fake session answers the fixtures, nothing reaches the network.

    python -m pytest hacker_news/test_fetch.py -q
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
import identity_pool  # noqa: E402
from hacker_news import api, fetch  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def text(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return f.read()


def load(name):
    return json.loads(text(name))


class Answer:
    def __init__(self, status=200, body="", headers=None):
        self.status_code, self.text, self.headers = status, body, headers or {}

    def json(self):
        return json.loads(self.text)


class Session:
    """Answers each request from a queue (the last answer repeats) and keeps what was sent."""

    def __init__(self, *answers):
        self.answers, self.sent = list(answers), []

    def request(self, method, url, **kw):
        self.sent.append((method, url, kw))
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        return answer

    def close(self):
        pass


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    """No backoff sleeps, no page pacing, a fresh profile pointer and key pool."""
    monkeypatch.setattr(config, "RETRY_BACKOFF", 0)
    monkeypatch.setattr(config, "HACKER_NEWS_PAGE_INTERVAL", 0)
    monkeypatch.setattr(fetch, "_page_profile_index", [0])
    monkeypatch.setattr(fetch, "_next_page_at", [0.0])
    monkeypatch.setattr(fetch, "_drop_session", lambda: None)
    monkeypatch.setattr(fetch, "_search_keys",
                        identity_pool.Pool("hacker-news-test-key", lambda key, hint: "freshkey", seed={"": "seedkey"}))


def use(monkeypatch, *answers):
    sess = Session(*answers)
    monkeypatch.setattr(fetch, "_session", lambda: sess)
    monkeypatch.setattr(fetch, "_page_session", lambda: sess)
    return sess


# ---- the official API ------------------------------------------------------------------

def test_item_and_the_null_of_an_unknown_one(monkeypatch):
    sess = use(monkeypatch, Answer(body=text("item_story_8863.json")))
    assert fetch.fetch_item(8863)["title"].startswith("My YC app: Dropbox")
    method, url, kw = sess.sent[0]
    assert (method, url) == ("GET", "https://hacker-news.firebaseio.com/v0/item/8863.json")
    assert kw["headers"] == {"accept": "application/json"}
    use(monkeypatch, Answer(body=text("item_not_found.null.txt")))
    assert fetch.fetch_item(999999999999) is None
    use(monkeypatch, Answer(body=text("user_not_found.null.txt")))
    assert fetch.fetch_user("nobody") is None


def test_lists_updates_and_maxitem(monkeypatch):
    use(monkeypatch, Answer(body=text("list_topstories.json")))
    assert len(fetch.fetch_story_list("topstories")) == 500
    use(monkeypatch, Answer(body=text("updates.json")))
    assert set(fetch.fetch_updates()) == {"items", "profiles"}
    use(monkeypatch, Answer(body=text("maxitem.json")))
    assert fetch.fetch_max_item() == 50588106
    use(monkeypatch, Answer(body="null"))
    with pytest.raises(fetch.HackerNewsUpstreamError):
        fetch.fetch_story_list("topstories")


def test_a_refusal_is_retried_and_then_raised(monkeypatch):
    sess = use(monkeypatch, Answer(status=429, body="slow down"))
    with pytest.raises(fetch.HackerNewsBlocked):
        fetch.fetch_item(8863)
    assert len(sess.sent) == config.MAX_RETRIES
    sess = use(monkeypatch, Answer(status=502), ConnectionError("reset"), Answer(body=text("item_story_8863.json")))
    assert fetch.fetch_item(8863)["id"] == 8863 and len(sess.sent) == 3
    use(monkeypatch, Answer(body="<html>oops</html>"))
    with pytest.raises(fetch.HackerNewsUpstreamError):
        fetch.fetch_item(8863)


# ---- the search index ------------------------------------------------------------------

def test_search_body_is_the_pages_own():
    assert fetch.search_body("rust", per_page=30) == {
        "query": "rust", "analyticsTags": ["web"], "page": 0, "hitsPerPage": 30, "minWordSizefor1Typo": 4,
        "minWordSizefor2Typos": 8, "advancedSyntax": True, "ignorePlurals": False, "clickAnalytics": True,
        "minProximity": 7, "numericFilters": [], "tagFilters": [["story"], []], "typoTolerance": True,
        "queryType": "prefixNone",
        "restrictSearchableAttributes": ["title", "comment_text", "url", "story_text", "author"],
        "getRankingInfo": True}
    by_date = fetch.search_body("", tags=["author_pg"], any_tags=("comment",), numeric=["created_at_i>=1"],
                                page=3, per_page=5000, by_date=True, stories=False)
    assert by_date["typoTolerance"] == "min" and by_date["minProximity"] == 1 and by_date["page"] == 2
    assert by_date["hitsPerPage"] == 1000 and by_date["tagFilters"] == [["comment"], ["author_pg"]]
    assert by_date["numericFilters"] == ["created_at_i>=1"]


def test_every_tag_of_a_search_must_match():
    # tags inside one group are "any of these" to the index: an author and a story in one group
    # would list the author's comments everywhere plus every comment of the story
    body = fetch.search_body("", tags=["author_pg", "story_8863"], any_tags=("comment",))
    assert body["tagFilters"] == [["comment"], ["author_pg"], ["story_8863"]]


def test_search_goes_out_as_the_page_sends_it(monkeypatch):
    sess = use(monkeypatch, Answer(body=text("search_stories_rust.json")))
    answer = fetch.fetch_search(fetch.search_body("rust"), by_date=True)
    assert len(answer["hits"]) == 30
    method, url, kw = sess.sent[0]
    assert method == "POST" and "/1/indexes/Item_dev_sort_date/query?" in url
    assert url.startswith("https://uj5wyc0l7x-dsn.algolia.net/") and "x-algolia-api-key=seedkey" in url
    assert "x-algolia-agent=Algolia%20for%20JavaScript%20(4.13.1)%3B%20Browser%20(lite)" in url
    assert kw["headers"]["content-type"] == "application/x-www-form-urlencoded"
    assert kw["headers"]["origin"] == "https://hn.algolia.com"
    assert json.loads(kw["data"])["query"] == "rust"
    assert "accept-language" not in {k.lower() for k in kw["headers"]}      # the session's own


def test_a_refused_key_is_read_again_once(monkeypatch):
    sess = use(monkeypatch, Answer(status=403, body='{"message":"Invalid Application-ID or API key"}'),
               Answer(body=text("search_no_result.json")))
    assert fetch.fetch_search(fetch.search_body("x"))["hits"] == []
    assert "x-algolia-api-key=seedkey" in sess.sent[0][1] and "x-algolia-api-key=freshkey" in sess.sent[1][1]
    sess = use(monkeypatch, Answer(status=403, body="{}"))
    with pytest.raises(fetch.HackerNewsBlocked):
        fetch.fetch_search(fetch.search_body("x"))


def test_objects(monkeypatch):
    sess = use(monkeypatch, Answer(body=text("objects_mixed.json")))
    rows = fetch.fetch_objects([8863, 2921983, 126809, 192327, 999999999999])
    assert [bool(r) for r in rows] == [True, True, True, True, False]
    assert json.loads(sess.sent[0][2]["data"])["requests"][0] == {"indexName": "Item_dev", "objectID": "8863"}
    assert "/1/indexes/*/objects?" in sess.sent[0][1]
    assert fetch.fetch_objects([]) == []
    with pytest.raises(fetch.HackerNewsBadRequest):
        fetch.fetch_objects(range(1001))
    use(monkeypatch, Answer(status=400, body=text("objects_too_many.json")))
    with pytest.raises(fetch.HackerNewsBadRequest) as e:
        fetch.fetch_objects([1, 2])
    assert "cannot fetch more than 1000" in str(e.value)


# ---- the comment tree ------------------------------------------------------------------

def test_tree_and_its_404(monkeypatch):
    sess = use(monkeypatch, Answer(body=text("tree_story_8863.json")))
    assert len(fetch.fetch_tree(8863)["children"]) == 32
    assert sess.sent[0][1] == "https://hn.algolia.com/api/v1/items/8863"
    sess = use(monkeypatch, Answer(status=404, body=text("tree_not_found.json")))
    with pytest.raises(fetch.HackerNewsNotFound):
        fetch.fetch_tree(999999999999)
    assert len(sess.sent) == 1                      # final, not retried


# ---- the site's pages ------------------------------------------------------------------

def test_front_page(monkeypatch):
    sess = use(monkeypatch, Answer(body=text("front_2026-11-20.html")))
    assert 'op="front"' in fetch.fetch_front_page("2026-11-20")[:300]
    method, url, kw = sess.sent[0]
    assert url == "https://news.ycombinator.com/front" and kw["params"] == {"day": "2026-11-20"}
    assert kw["headers"]["sec-fetch-mode"] == "navigate"
    fetch.fetch_front_page("2026-11-20", page=2)
    assert sess.sent[1][2]["params"] == {"day": "2026-11-20", "p": 2}


def test_the_rate_limit_page_moves_to_the_next_profile(monkeypatch):
    sess = use(monkeypatch, Answer(status=503, body=text("rate_limited_503.html")),
               Answer(body=text("front_2026-11-20.html")))
    fetch._page_local.profile = fetch._page_profile()       # the profile the refused session was built as
    assert fetch._page_profile() == config.HACKER_NEWS_PAGE_PROFILES[0]
    assert fetch.fetch_front_page("2026-11-20")
    assert len(sess.sent) == 2 and fetch._page_profile() == config.HACKER_NEWS_PAGE_PROFILES[1]
    # a thread whose session was built as an older profile does not push the pointer on
    fetch._page_local.profile = config.HACKER_NEWS_PAGE_PROFILES[0]
    fetch._drop_page_session(fetch.HackerNewsBlocked("503"))
    assert fetch._page_profile() == config.HACKER_NEWS_PAGE_PROFILES[1]


def test_a_day_without_data_is_a_bad_request(monkeypatch):
    for name in ("front_future.txt", "front_before_start.txt"):
        sess = use(monkeypatch, Answer(body=text(name)))
        with pytest.raises(fetch.HackerNewsBadRequest) as e:
            fetch.fetch_front_page("2031-01-01")
        assert text(name).strip() in str(e.value) and len(sess.sent) == 1


def test_page_requests_take_turns(monkeypatch):
    slept = []
    monkeypatch.setattr(config, "HACKER_NEWS_PAGE_INTERVAL", 1.0)
    monkeypatch.setattr(config, "HACKER_NEWS_PAGE_MAX_WAIT", 2.5)
    monkeypatch.setattr(fetch.time, "monotonic", lambda: 100.0)
    monkeypatch.setattr(fetch.time, "sleep", slept.append)
    for _ in range(3):
        fetch._wait_for_page_slot()
    assert slept == [1.0, 2.0]
    with pytest.raises(fetch.HackerNewsBlocked):
        fetch._wait_for_page_slot()                 # a fourth would wait 3 s: refused instead


def test_a_full_page_queue_is_not_a_refused_profile(monkeypatch):
    sess = use(monkeypatch, Answer(body=text("front_2026-11-20.html")))
    monkeypatch.setattr(config, "HACKER_NEWS_PAGE_INTERVAL", 1.0)
    monkeypatch.setattr(config, "HACKER_NEWS_PAGE_MAX_WAIT", 2.5)
    monkeypatch.setattr(fetch.time, "monotonic", lambda: 100.0)
    monkeypatch.setattr(fetch, "_next_page_at", [200.0])        # the queue is 100 s long
    fetch._page_local.profile = fetch._page_profile()
    with pytest.raises(fetch.HackerNewsBlocked):
        fetch.fetch_front_page("2026-11-20")
    assert sess.sent == []                                      # nothing was sent, so the site refused nothing
    assert fetch._page_profile() == config.HACKER_NEWS_PAGE_PROFILES[0]


# ---- the endpoint functions ------------------------------------------------------------

@pytest.fixture
def upstream(monkeypatch):
    """The fetch functions answered from the fixtures."""
    items = {8863: "item_story_8863.json", 121003: "item_ask_121003.json", 126809: "item_poll_126809.json",
             160705: "item_poll_option_160705.json", 2921983: "item_comment_2921983.json",
             50580016: "item_dead_comment.json", 50580029: "item_dead_story.json"}
    objects = {int(r["objectID"]): r for r in load("objects_mixed.json")["results"] if r}
    objects.update({int(r["objectID"]): r for r in load("objects_topstories_30.json")})
    monkeypatch.setattr(fetch, "fetch_item", lambda i: load(items[i]) if i in items else
                        ({"id": i, "type": "pollopt", "poll": 126809, "text": f"option {i}", "score": 1}
                         if i in (126810, 126811, 126812) else None))
    monkeypatch.setattr(fetch, "fetch_objects", lambda ids: [objects.get(i) for i in ids])
    monkeypatch.setattr(fetch, "fetch_user", lambda name: load("user_pg.json") if name == "pg" else None)
    monkeypatch.setattr(fetch, "fetch_story_list", lambda name: load("list_topstories.json"))
    monkeypatch.setattr(api, "cached_call", lambda namespace, data, fn, ttl, **kw: fn(**data))
    return objects


def test_stories_list_pages_the_ids(upstream):
    ids = load("list_topstories.json")
    held = [i for i in ids[:30] if i in upstream]
    result = api.stories_list(list="top", page=1)
    assert result["pagination"] == {"page": 1, "items_per_page": 30, "total_pages": 17, "total_count": 500}
    assert [s["id"] for s in result["items"]] == held
    assert [s["rank"] for s in result["items"]] == [ids.index(i) + 1 for i in held]
    assert list(result["items"][0])[:3] == ["rank", "id", "type"]
    assert api.stories_list(list="top", page=18)["items"] == []


def test_story_details(upstream):
    story = api.story_details(story=8863)
    assert story["id"] == 8863 and story["poll_options"] is None and story["is_on_front_page"] is False
    poll = api.story_details(story=126809)
    assert [o["id"] for o in poll["poll_options"]] == [126810, 126811, 126812]
    assert api.story_details(story=50580029)["is_dead"] is True
    with pytest.raises(fetch.HackerNewsNotFound):
        api.story_details(story=999999999999)
    with pytest.raises(ValueError) as e:
        api.story_details(story=2921983)
    assert str(e.value).startswith("story: item 2921983 is a comment")
    with pytest.raises(ValueError):
        api.story_details(story=160705)


def test_story_comments_take_the_sites_order(upstream, monkeypatch):
    monkeypatch.setattr(fetch, "fetch_tree", lambda i: load("tree_story_8863.json"))
    result = api.story_comments(story=8863)
    kids = load("item_story_8863.json")["kids"]
    top = [c["id"] for c in result["comments"]]
    assert top == [k for k in kids if k in top] and len(top) == 32
    assert result["returned_comment_count"] == 71 and result["story"]["comment_count"] == 71
    assert result["comments"][0]["story"]["title"] == "My YC app: Dropbox - Throw away your USB drive"
    with pytest.raises(ValueError) as e:
        api.story_comments(story=160705)            # a poll option is not a story: refused as stories/details does
    assert "option of poll 160704" in str(e.value)


def test_story_comments_fall_back_to_the_index(upstream, monkeypatch):
    def failing(item_id):
        raise fetch.HackerNewsUpstreamError("HTTP 500")
    monkeypatch.setattr(fetch, "fetch_tree", failing)
    monkeypatch.setattr(fetch, "fetch_search", lambda body, by_date=False: load("search_story_comments_8863.json"))
    result = api.story_comments(story=8863)
    assert result["returned_comment_count"] == 71 and len(result["comments"]) == 32


def test_comment_details(upstream, monkeypatch):
    monkeypatch.setattr(fetch, "fetch_tree", lambda i: load("tree_comment_2921983.json"))
    comment = api.comment_details(comment=2921983)
    assert comment["author"]["username"] == "norvig" and comment["story"]["title"].startswith("Peter Norvig")
    assert comment["replies"] and comment["is_dead"] is False

    def missing(item_id):
        raise fetch.HackerNewsNotFound("gone")
    monkeypatch.setattr(fetch, "fetch_tree", missing)
    dead = api.comment_details(comment=50580016)
    assert dead["is_dead"] is True and dead["replies"] == [] and dead["text"] is None
    with pytest.raises(ValueError) as e:
        api.comment_details(comment=8863)
    assert str(e.value).startswith("comment: item 8863 is a story")


def test_stories_batch(upstream):
    result = api.stories_batch(ids=[8863, 2921983, 160705, 50580016, 999999999999])
    assert [i["type"] for i in result["items"]] == ["story", "comment", "poll_option", "comment"]
    assert result["count"] == 4 and result["not_found_ids"] == [999999999999]
    assert result["items"][2]["poll_id"] == 160704 and result["items"][2]["text"]
    assert result["items"][3]["is_dead"] is True
    with pytest.raises(fetch.HackerNewsNotFound):
        api.stories_batch(ids=[999999999999])


def test_user_rows_tell_no_rows_from_no_user(upstream, monkeypatch):
    monkeypatch.setattr(fetch, "fetch_search", lambda body, by_date=False: load("search_no_result.json"))
    assert api.user_stories(username="pg", sort="date", page=1)["items"] == []
    with pytest.raises(fetch.HackerNewsNotFound) as e:
        api.user_comments(username="PG", page=1)
    assert "case-sensitive" in str(e.value)
    with pytest.raises(fetch.HackerNewsNotFound):
        api.user_details(username="nobody")
    assert api.user_details(username="pg")["karma"] == 157316


def test_search_filters_reach_the_index(upstream, monkeypatch):
    sent = []

    def search(body, by_date=False):
        sent.append((body, by_date))
        return load("search_stories_rust.json")
    monkeypatch.setattr(fetch, "fetch_search", search)
    base = {"query": "rust", "type": "story", "sort": None, "date_from": None, "date_to": None, "min_points": None,
            "min_comments": None, "author": None, "site": None, "page": 1}
    api.stories_search(**{**base, "sort": "date", "date_from": "2020-01-01", "date_to": "2020-12-31",
                          "min_points": 100, "min_comments": 10, "author": "pg", "page": 2})
    body, by_date = sent[-1]
    assert by_date is True and body["page"] == 1 and body["hitsPerPage"] == 100
    assert body["numericFilters"] == ["created_at_i>=1577836800", "created_at_i<1609459200", "points>=100",
                                      "num_comments>=10"]
    assert body["tagFilters"] == [["story"], ["author_pg"]]
    result = api.stories_search(**{**base, "query": None, "site": "github.com"})
    body, by_date = sent[-1]
    assert body["query"] == '"github.com"' and body["restrictSearchableAttributes"] == ["url"] and by_date is False
    assert (result["site"], result["sort"], result["type"]) == ("github.com", "popularity", "story")
    api.stories_search(**{**base, "query": None, "type": "job"})
    assert sent[-1][1] is True and sent[-1][0]["tagFilters"] == [["job"], []]


def test_comments_search_by_author_and_story_asks_for_both(upstream, monkeypatch):
    sent = []

    def search(body, by_date=False):
        sent.append(body)
        return load("search_comments_rust.json")
    monkeypatch.setattr(fetch, "fetch_search", search)
    api.comments_search(query=None, author="pg", story=8863, sort="date", date_from=None, date_to=None, page=1)
    assert sent[-1]["tagFilters"] == [["comment"], ["author_pg"], ["story_8863"]]


def test_jobs_search_reads_the_months_thread(upstream, monkeypatch):
    sent = []

    def search(body, by_date=False):
        sent.append(body)
        return load("hiring_threads.json") if body["tagFilters"][1] == ["author_whoishiring"] else \
            load("hiring_posts_remote_rust.json")
    monkeypatch.setattr(fetch, "fetch_search", search)
    result = api.jobs_search(query="remote rust", month=None, thread="hiring", page=1)
    assert result["thread"]["id"] == 50338728 and result["thread"]["month"] == "2026-11"
    assert len(result["items"]) == 21
    assert sent[-1]["tagFilters"] == [["comment"], ["story_50338728"]]
    assert sent[-1]["numericFilters"] == ["parent_id=50338728"]
    assert sent[-1]["restrictSearchableAttributes"] == ["comment_text"]
    wanted = api.jobs_search(query=None, month="2026-10", thread="wants_to_be_hired", page=1)
    assert wanted["thread"]["id"] == 49922568
    with pytest.raises(fetch.HackerNewsNotFound):
        api.jobs_search(query=None, month="2026-05", thread="hiring", page=1)


def test_stories_past_adds_the_index_rows_to_the_page(upstream, monkeypatch):
    monkeypatch.setattr(fetch, "fetch_front_page", lambda day, page=1: text("front_2026-11-20.html"))
    result = api.stories_past(date="2026-11-20", page=1)
    assert result["date"] == "2026-11-20" and len(result["items"]) == 30
    assert [s["rank"] for s in result["items"]] == list(range(1, 31))
    assert result["pagination"] == {"page": 1, "items_per_page": 30, "total_pages": None, "total_count": None,
                                    "has_next": True}
    assert all(list(s)[:2] == ["rank", "id"] and s["title"] for s in result["items"])

    def failing(ids):
        raise fetch.HackerNewsUpstreamError("index down")
    monkeypatch.setattr(fetch, "fetch_objects", failing)
    assert len(api.stories_past(date="2026-11-20", page=1)["items"]) == 30      # the page alone still answers
