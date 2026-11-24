"""Cache TTL per /hacker-news/* surface (cache.py, keyed on the validated
params). Points and comment counts move by the minute on a fresh story and
hardly at all on an old one, so the lists are kept short and the past long.
"""
from datetime import date, datetime, timedelta, timezone

# stories/search, comments/search, users/stories, users/comments: a search
# gains rows as people post, and its rows' points move with every vote
SEARCH_CACHE = timedelta(minutes=5)
# the ids of a list (top, new, best, ask, show, jobs): the front page
# reorders every few minutes
LIST_IDS_CACHE = timedelta(minutes=2)
# stories/list pages: the rows of those ids, with their points
LIST_CACHE = timedelta(minutes=2)
# stories/details, comments/details, stories/batch: points and the comment count of one item
ITEM_CACHE = timedelta(minutes=5)
# stories/comments: a live thread gains comments by the minute; the answer can be 1 MB
COMMENTS_CACHE = timedelta(minutes=5)
# users/details: karma moves slowly
USER_CACHE = timedelta(minutes=30)
# jobs/search: a monthly thread gains postings through its first days
JOBS_CACHE = timedelta(minutes=30)
# which thread is this month's: a new one appears once a month
JOB_THREAD_CACHE = timedelta(hours=6)
# items/updates: the official API renews it about every 30 seconds
UPDATES_CACHE = timedelta(seconds=30)

# stories/past: a day that is over keeps its page, but its points still
# move for a day or two; the page is the one upstream the site rate-limits
PAST_RECENT_CACHE = timedelta(minutes=30)
PAST_SETTLED_CACHE = timedelta(days=7)
PAST_SETTLED_AFTER_DAYS = 3


def past_cache(data, result):
    """The TTL of one stories/past page, by the age of its day."""
    try:
        day = date.fromisoformat(str(data.get("date")))
    except ValueError:
        return PAST_RECENT_CACHE
    age = (datetime.now(timezone.utc).date() - day).days
    return PAST_SETTLED_CACHE if age >= PAST_SETTLED_AFTER_DAYS else PAST_RECENT_CACHE
