"""Configuration for the Hacker News Scraper. Everything can be set with an environment
variable; the defaults work out of the box.

    PORT                port the API listens on (default 8000)
    HACKER_NEWS_PROXY   proxy URL for every request, e.g. http://user:pass@host:port
                        (default: none, requests go out from your own connection).
                        You do not need a proxy: every data source answers a normal
                        connection. Set one only if your IP gets blocked.

Everything else below is a plain constant with a working default.
"""
import os

PORT = int(os.environ.get("PORT", "8000"))

HACKER_NEWS_PROXY = os.environ.get("HACKER_NEWS_PROXY") or None


# ---- what the code reads off config: defaults for a direct run ----
HACKER_NEWS_EXIT = HACKER_NEWS_PROXY      # the exit every session leaves through: your proxy, or direct

# The site's own public search credentials, the ones its search page at hn.algolia.com sends with
# every query. They are in that page's script for anyone to read. When the key changes, the
# scraper reads the new one from the same script by itself.
HACKER_NEWS_SEARCH_APP_ID = 'UJ5WYC0L7X'
HACKER_NEWS_SEARCH_KEY = '28f0e1ec37a5e792e6845e67da5f20dd'

# news.ycombinator.com limits how fast one address reads its pages. Only /stories/past reads a page:
# one request a second, and a refused request tries the next browser profile.
HACKER_NEWS_PAGE_INTERVAL = 1.0           # seconds between two page requests
HACKER_NEWS_PAGE_MAX_WAIT = 20            # seconds a page request may wait for its turn
HACKER_NEWS_PAGE_PROFILES = ('chrome', 'safari17_0', 'edge101')

# Retry policy for transport errors and refusals (every request).
MAX_RETRIES = 3
RETRY_BACKOFF = 2                         # seconds, multiplied by the attempt number
