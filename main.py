"""Use the scraper straight from Python, no server needed.

    python main.py

Each call validates its params with the same schema the API uses (so the
defaults apply) and returns the same JSON the API does, except that a list
keeps its `pagination` block instead of next / previous links. The results
are written to output/*.json.
"""
import json
import os

from hacker_news import api, schemas
from schema_fields import load_query

os.makedirs("output", exist_ok=True)


def run(schema_cls, impl, **params):
    data, error = load_query(schema_cls, {k: str(v) for k, v in params.items()})
    if error:
        raise ValueError(error)
    return impl(**data)


def save(name, data):
    path = os.path.join("output", name)
    with open(path, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"saved {path}")


if __name__ == "__main__":
    # one story in full: the GPT-4 launch post, with its link, author, points and comment count
    save("stories_details.json", run(schemas.StorySchema, api.story_details, story="35154527"))

    # stories about Rust, the most popular first (100 per page)
    save("stories_search.json", run(schemas.StoriesSearchSchema, api.stories_search, query="rust"))

    # the front page right now, in the site's own order (30 per page)
    save("stories_list.json", run(schemas.StoriesListSchema, api.stories_list))

    # the whole comment thread of Dropbox's 2007 launch post, replies nested
    save("stories_comments.json", run(schemas.StorySchema, api.story_comments, story="8863"))

    # Paul Graham's profile: about text, karma, submission count, the date he joined
    save("users_details.json", run(schemas.UserSchema, api.user_details, username="pg"))

    # this month's "Who is hiring?" posts that mention remote and Rust
    save("jobs_search.json", run(schemas.JobsSearchSchema, api.jobs_search, query="remote rust"))
