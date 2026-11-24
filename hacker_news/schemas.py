"""Marshmallow request schemas for every /hacker-news/* route.

ONE param per input (never a sibling `url` / `id` pair):

    story      an item id (8863) OR a news.ycombinator.com item link
    comment    the same, for a comment
    ids        up to 1,000 of those, comma-separated
    username   a username (pg; case-sensitive) OR a profile link
    site       a domain (github.com) OR any link on it

A result page of the search index holds 100 rows, and a query is served
1,000 rows deep (10 pages): narrow it with the dates to read further. A
page past the end is an empty page.
"""
from datetime import date, datetime, timezone

from marshmallow import ValidationError, pre_load, validates_schema

from schema_fields import BaseSchema as _SharedBaseSchema
from schema_fields import ChoiceField, DateField, PageField, PositiveInt, RefField, StrippedString, YearMonthField
from hacker_news import refs
from scraper_utils.schema import max_length as _max_length

MAX_QUERY_LENGTH = 200
MAX_PAGE = 100          # the index reads 10 pages deep and a list has at most 17: a page past the end is empty


class BaseSchema(_SharedBaseSchema):
    """The base of every /hacker-news/* schema."""

    @pre_load
    def empty_is_absent(self, data, **kwargs):
        """An optional param sent empty (`sort=`, `page=`, `date_from=`: a
        form or an automation that sends every field) is the absent param.
        A required param sent empty is still refused."""
        required = {field.data_key or name for name, field in self.fields.items() if field.required}
        return {key: value for key, value in data.items()
                if key in required or not (isinstance(value, str) and not value.strip())}


class ItemRefField(RefField):
    resolver = staticmethod(refs.resolve_item)


class ItemsRefField(RefField):
    """'8863, 121003, <link>' -> the ids, in order, repeats dropped."""
    resolver = staticmethod(refs.resolve_items)


class UserRefField(RefField):
    resolver = staticmethod(refs.resolve_user)


class SiteRefField(RefField):
    resolver = staticmethod(refs.resolve_site)


def _utc_today():
    return datetime.now(timezone.utc).date()


class PastDateField(DateField):
    """YYYY-MM-DD, from the first day the site keeps up to today (UTC)."""

    def _deserialize(self, value, attr, data, **kwargs):
        value = super()._deserialize(value, attr, data, **kwargs)
        day = date.fromisoformat(value)
        if day < refs.FIRST_DAY:
            raise ValidationError(f"Hacker News starts on {refs.FIRST_DAY.isoformat()}.")
        if day > _utc_today():
            raise ValidationError("Must be today (UTC) or an earlier day.")
        return value


def _query(description):
    return StrippedString(required=False, load_default=None, validate=_max_length(MAX_QUERY_LENGTH),
                          metadata={"description": description})


def _page():
    return PageField(max_page=MAX_PAGE, metadata={"description": "page number, from 1"})


def _date(description):
    return PastDateField(metadata={"description": description})


def _check_dates(data):
    if data.get("date_from") and data.get("date_to") and data["date_from"] > data["date_to"]:
        raise ValidationError("date_from must not be after date_to.", "date_from")


class StoriesSearchSchema(BaseSchema):
    query = _query("search words, matched in the title, the link, the text and the author; leave it out to "
                   "list everything the filters select")
    type = ChoiceField(list(refs.STORY_TYPES), load_default="story",
                       metadata={"description": "story (default), ask_hn, show_hn, launch_hn, job, poll or "
                                                "front_page (stories on the front page right now)"})
    sort = ChoiceField(list(refs.STORY_SORTS),
                       metadata={"description": "popularity (default) or date (newest first); jobs are "
                                                "listed by date only"})
    date_from = _date("only stories posted on or after this day (YYYY-MM-DD, UTC)")
    date_to = _date("only stories posted on or before this day (YYYY-MM-DD, UTC)")
    min_points = PositiveInt(metadata={"description": "only stories with at least this many points"})
    min_comments = PositiveInt(metadata={"description": "only stories with at least this many comments"})
    author = UserRefField(required=False, load_default=None,
                          metadata={"description": "only stories posted by this user: a username "
                                                   "(case-sensitive) or a profile link"})
    site = SiteRefField(required=False, load_default=None,
                        metadata={"description": "only stories that link to this site: a domain (github.com) "
                                                 "or a link on it; it takes the place of `query`"})
    page = _page()

    @validates_schema
    def _check(self, data, **kwargs):
        _check_dates(data)
        if data.get("site") and data.get("query"):
            raise ValidationError("`site` lists the stories of one site and cannot be combined with `query`.",
                                  "site")
        if data.get("type") == "job" and data.get("sort") == "popularity":
            raise ValidationError("Jobs have no points: they are listed by date only.", "sort")
        if data.get("type") == "job" and (data.get("min_points") or data.get("min_comments")):
            raise ValidationError("Jobs have neither points nor comments.",
                                  "min_points" if data.get("min_points") else "min_comments")


class CommentsSearchSchema(BaseSchema):
    query = _query("search words, matched in the comment's text and its author; leave it out to list every "
                   "comment the filters select")
    author = UserRefField(required=False, load_default=None,
                          metadata={"description": "only comments written by this user: a username "
                                                   "(case-sensitive) or a profile link"})
    story = ItemRefField(required=False, load_default=None,
                         metadata={"description": "only comments under this story: an item id or an item link"})
    sort = ChoiceField(list(refs.COMMENT_SORTS), load_default="date",
                       metadata={"description": "date (default, newest first) or relevance"})
    date_from = _date("only comments written on or after this day (YYYY-MM-DD, UTC)")
    date_to = _date("only comments written on or before this day (YYYY-MM-DD, UTC)")
    page = _page()

    @validates_schema
    def _check(self, data, **kwargs):
        _check_dates(data)


class StoriesListSchema(BaseSchema):
    list = ChoiceField(list(refs.STORY_LISTS), load_default="top",
                       metadata={"description": "top (the front page, default), new, best, ask, show or jobs"})
    page = _page()


class StoriesPastSchema(BaseSchema):
    date = _date("the day whose front page to read (YYYY-MM-DD, UTC); yesterday when left out")
    page = _page()


class StorySchema(BaseSchema):
    story = ItemRefField(metadata={"description": "item id (8863) or a news.ycombinator.com item link of a "
                                                  "story, a job or a poll"})


class CommentSchema(BaseSchema):
    comment = ItemRefField(metadata={"description": "item id (2921983) or a news.ycombinator.com item link "
                                                    "of a comment"})


class StoriesBatchSchema(BaseSchema):
    ids = ItemsRefField(metadata={"description": "up to 1,000 item ids or item links, comma-separated; "
                                                 "stories, jobs, polls and comments alike"})


class UserSchema(BaseSchema):
    username = UserRefField(metadata={"description": "username (pg; case-sensitive) or a profile link"})


class UserStoriesSchema(UserSchema):
    sort = ChoiceField(list(reversed(list(refs.STORY_SORTS))), load_default="date",
                       metadata={"description": "date (default, newest first) or popularity"})
    page = _page()


class UserCommentsSchema(UserSchema):
    page = _page()


class JobsSearchSchema(BaseSchema):
    query = _query("words the posting must contain (remote rust); leave it out to list every posting")
    month = YearMonthField(bound="past",
                           metadata={"description": "the month of the thread (YYYY-MM); the latest when left out"})
    thread = ChoiceField(list(refs.JOB_THREADS), load_default="hiring",
                         metadata={"description": "hiring (\"Who is hiring?\", default) or wants_to_be_hired "
                                                  "(\"Who wants to be hired?\")"})
    page = _page()


class UpdatesSchema(BaseSchema):
    """No params."""
