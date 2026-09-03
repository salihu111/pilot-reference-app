"""
Aviation news feed. Pulls from a handful of public aviation-news RSS feeds
(no API key needed) and caches the merged result in memory for
CACHE_SECONDS, so opening the tab repeatedly doesn't hammer those sites on
every request.

Feed list is deliberately short and editable -- add/remove (source, url)
tuples below. A feed that's down, renamed, or blocks the request is skipped
silently (logged, not raised) so one bad URL never breaks the others.

Ethiopian Airlines doesn't publish a public RSS/API feed of its social
posts, and scraping X/Instagram/Facebook without their paid APIs violates
their terms and breaks constantly -- so rather than a fake or fragile
feed, the frontend embeds X's own official public timeline widget (no key
required, see index.html) and links out to the airline's other official
pages instead.
"""
import time
import feedparser

FEEDS = [
    ("Simple Flying", "https://simpleflying.com/feed/"),
    ("AeroTime", "https://www.aerotime.aero/feed"),
    ("The Aviation Herald", "https://avherald.com/rss.php"),
    ("Aviation (Neowin)", "https://www.neowin.net/news/rss/aviation/"),
]

CACHE_SECONDS = 900  # 15 min
_cache = {"at": 0, "articles": []}


def _parse_published(entry):
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            return time.mktime(t)
    return 0


def get_news(limit: int = 30):
    now = time.time()
    if _cache["articles"] and (now - _cache["at"]) < CACHE_SECONDS:
        return _cache["articles"][:limit]

    articles = []
    for source, url in FEEDS:
        try:
            parsed = feedparser.parse(url)
            for entry in parsed.entries[:15]:
                articles.append({
                    "source": source,
                    "title": entry.get("title", "").strip(),
                    "link": entry.get("link"),
                    "published": entry.get("published") or entry.get("updated"),
                    "_ts": _parse_published(entry),
                })
        except Exception as e:
            print(f"news feed fetch failed for {source} ({url}): {e}")

    articles.sort(key=lambda a: a["_ts"], reverse=True)
    for a in articles:
        a.pop("_ts", None)

    _cache["at"] = now
    _cache["articles"] = articles
    return articles[:limit]
