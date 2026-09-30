"""Reads a podcast RSS feed (concept §7.3).

The source behind a "Podcast" card: the admin pastes a podcast's feed URL
— the same address a podcast app would subscribe to — and the hub takes
the episode list from there.

The feed is what a podcast client reads, and fetching an episode from its
`<enclosure>` is what the format exists for.

Parsing is stdlib-only (`xml.etree`), in keeping with the project's
no-dependencies stance, and the fetch is size-capped: the URL comes from an
(authenticated) admin, but a parser that will happily chew through an
unbounded download is a bad idea regardless.
"""

import email.utils
import hashlib
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ElementTree

from ttl_cache import TtlCache

REQUEST_TIMEOUT = 15
FEED_TTL = 600

# Comfortably more than any real podcast feed (a few dozen KB);
# small enough that a runaway response can't exhaust memory.
MAX_FEED_BYTES = 8 * 1024 * 1024

ITUNES_NS = "http://www.itunes.com/dtds/podcast-1.0.dtd"

_cache = TtlCache()


class PodcastFeedError(Exception):
    """A feed that could not be fetched or parsed."""


def _fetch(feed_url):
    request = urllib.request.Request(
        feed_url,
        headers={
            "User-Agent": "ESPuino-MediaHub (+https://github.com/albert0815/ESPuino-Mediahub)",
            "Accept": "application/rss+xml, application/xml, text/xml, */*",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            # read(n+1) so an oversized body is detected rather than silently
            # truncated into invalid XML.
            body = response.read(MAX_FEED_BYTES + 1)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise PodcastFeedError(f"Podcast feed unreachable ({exc}).") from exc
    if len(body) > MAX_FEED_BYTES:
        raise PodcastFeedError("Podcast feed is implausibly large — ignoring it.")
    return body


def _parse_date(value):
    """RFC 822 `pubDate` -> ISO 8601, or None."""
    if not value:
        return None
    try:
        return email.utils.parsedate_to_datetime(value).isoformat()
    except (TypeError, ValueError):
        return None


def _parse_duration(value):
    """iTunes durations come as seconds, `MM:SS` or `HH:MM:SS`."""
    if not value:
        return 0
    parts = value.strip().split(":")
    try:
        numbers = [int(part) for part in parts]
    except ValueError:
        return 0
    seconds = 0
    for number in numbers:
        seconds = seconds * 60 + number
    return seconds


def feed_key(feed_url):
    """Stable, filesystem-safe folder name for a feed's cached episodes."""
    return "rss-" + hashlib.sha256((feed_url or "").encode("utf-8")).hexdigest()[:12]


def _episode_id(guid, audio_url):
    """A short, stable, filesystem-safe id for a feed episode.

    Feed GUIDs are free-form (UUIDs, URLs, sentences), so they are hashed
    rather than sanitized — that keeps cache filenames bounded and
    collision-free.
    """
    seed = (guid or audio_url or "").strip()
    return "rss-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


def _item_to_episode(item):
    enclosure = item.find("enclosure")
    if enclosure is None:
        return None
    audio_url = (enclosure.get("url") or "").strip()
    if not audio_url:
        return None

    # `length` is the enclosure's size in bytes. Feeds are not careful with
    # it (missing, empty, or a float), and it is only ever shown to the
    # admin, so anything unusable becomes 0 and is left out of the UI.
    try:
        size = int(enclosure.get("length") or 0)
    except ValueError:
        size = 0

    guid = (item.findtext("guid") or "").strip()
    return {
        "id": _episode_id(guid, audio_url),
        "urn": guid or None,
        "title": (item.findtext("title") or "").strip(),
        "publish_date": _parse_date(item.findtext("pubDate")),
        "duration": _parse_duration(item.findtext(f"{{{ITUNES_NS}}}duration")),
        "audio_url": audio_url,
        "mime_type": enclosure.get("type"),
        "size": max(0, size),
    }


def fetch_feed(feed_url):
    """`{"title", "description", "image_url", "episodes"}` for a feed,
    episodes newest first. Raises PodcastFeedError for anything unusable."""
    if not (feed_url or "").startswith(("http://", "https://")):
        raise PodcastFeedError("A podcast feed URL has to start with http:// or https://.")

    cache_key = f"feed:{feed_url}"
    cached = _cache.get(cache_key)
    if cached is None:
        body = _fetch(feed_url)
        try:
            root = ElementTree.fromstring(body)
        except ElementTree.ParseError as exc:
            raise PodcastFeedError(f"Podcast feed is not valid XML ({exc}).") from exc

        channel = root.find("channel")
        if channel is None:
            raise PodcastFeedError("Podcast feed has no <channel> element.")

        episodes = [
            episode
            for episode in (_item_to_episode(item) for item in channel.findall("item"))
            if episode is not None
        ]
        # Most feeds are already newest-first, but say so explicitly — the
        # cache filenames and playback order depend on it.
        episodes.sort(key=lambda episode: episode["publish_date"] or "", reverse=True)
        if not episodes:
            raise PodcastFeedError("This feed lists no downloadable episode.")

        image = channel.find(f"{{{ITUNES_NS}}}image")
        cached = {
            "feed_url": feed_url,
            "title": (channel.findtext("title") or "").strip() or feed_url,
            "description": (channel.findtext("description") or "").strip()[:500],
            "image_url": (image.get("href") if image is not None else None)
            or (channel.findtext("image/url") or "").strip()
            or None,
            "episodes": episodes,
        }
        _cache.set(cache_key, cached, FEED_TTL)

    return cached
