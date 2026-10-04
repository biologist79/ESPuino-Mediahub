"""Background sync of podcast cards: resolve → download → manifest.

A podcast card stores *intent* ("the newest episode of this show", "these
three episodes"), not a file list. Turning that into the concrete
`files[]` the manifest needs takes a feed fetch and one download per
episode — far too slow to do inside a request, and completely off-limits
inside a manifest request (concept §3.2: an ESPuino must never wait on the
hub doing slow work).

So all of it happens here, in a background worker, and the ESPuino-facing
endpoint only ever serves what is already cached. A card that just gained a
newer episode keeps serving the previous one until the download finished —
the same "content changes take effect on the next tap" behaviour the
`version`/stale mechanism already has (§9).

**Exactly one worker across all gunicorn workers.** The container runs
several processes (see Dockerfile), and two of them downloading the same
episode would be wasteful at best. An `flock` on a file in DATA_DIR elects
one of them; if that process dies, the OS drops the lock and another takes
over on its next poll — no heartbeat bookkeeping, no stale-lock cleanup.
"""

import fcntl
import os
import threading
import time
from datetime import datetime, timedelta, timezone

import podcast_cache
import podcast_feed
import store as store_lib

# Episodes per card. "Latest N" plus a fixed selection both cap here: every
# episode is a full download onto the hub's data volume, and a card whose
# manifest lists 200 files is not a plausible ESPuino assignment either.
MAX_EPISODES = 20

STATE_PENDING = "pending"    # intent saved, nothing resolved/downloaded yet
STATE_SYNCING = "syncing"    # this worker is on it right now
STATE_READY = "ready"        # files are cached, manifest servable
STATE_ERROR = "error"        # last attempt failed, message says why

LOCK_FILENAME = "podcast-sync.lock"
POLL_SECONDS = 5
ERROR_RETRY_SECONDS = 600

# Cleanup normally rides along with a sync, which covers every case where
# something *became* unreferenced. This interval is the safety net for a hub
# where nothing syncs for a long time (automatic checks switched off, fixed
# episode selections only) — the admin should never have to tidy the cache
# by hand, so it also happens on its own.
CLEANUP_INTERVAL_SECONDS = 3600


def _now():
    return datetime.now(timezone.utc)


def _parse_ts(value):
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def is_podcast(card):
    return card.get("kind") == "podcast"


def sync_state(card):
    """The card's sync bookkeeping, with defaults for a card that has none
    yet (freshly switched to a podcast, or written by an older version)."""
    state = dict(card.get("podcast_sync") or {})
    state.setdefault("state", STATE_PENDING)
    state.setdefault("message", "")
    state.setdefault("done", 0)
    state.setdefault("total", 0)
    state.setdefault("last_checked", None)
    state.setdefault("last_synced", None)
    state.setdefault("episodes", [])
    state.setdefault("reason", None)
    state.setdefault("pending_paths", [])
    return state


def is_due(card, refresh_minutes, now=None):
    """Whether the worker should (re-)sync this card right now."""
    if not is_podcast(card) or card.get("status") != "assigned":
        return False

    now = now or _now()
    state = sync_state(card)
    if state["state"] in (STATE_PENDING, STATE_SYNCING):
        return True

    last_checked = _parse_ts(state["last_checked"])
    if last_checked is None:
        return True

    if state["state"] == STATE_ERROR:
        return now - last_checked >= timedelta(seconds=ERROR_RETRY_SECONDS)

    # A fixed episode selection cannot change behind our back; only "latest"
    # needs polling, and only if the admin left the interval enabled. (A fixed
    # pick *can* scroll out of the feed, but re-checking would not bring it
    # back — that surfaces on the next sync either way.)
    if (card.get("podcast") or {}).get("selection") != "latest":
        return False
    if not refresh_minutes:
        return False
    return now - last_checked >= timedelta(minutes=refresh_minutes)


# --------------------------------------------------------------------------
# One card
# --------------------------------------------------------------------------
def cache_folder(podcast):
    """Which cache subfolder a card's episodes live in — a hash of the feed
    URL, so every feed keeps its episodes to itself."""
    return podcast_feed.feed_key(podcast.get("feed_url"))


def _resolve_episodes(podcast):
    """The episodes a card's intent currently points at.

    In playback order (oldest first, matching the cache's date-sorted
    filenames).
    """
    feed = podcast_feed.fetch_feed(podcast.get("feed_url"))
    available = feed["episodes"]

    if podcast.get("selection") == "latest":
        count = max(1, min(int(podcast.get("episode_count") or 1), MAX_EPISODES))
        return list(reversed(available[:count]))

    wanted = [str(entry["id"]) for entry in (podcast.get("episodes") or [])[:MAX_EPISODES]]
    by_id = {episode["id"]: episode for episode in available}
    episodes = [by_id[episode_id] for episode_id in wanted if episode_id in by_id]
    if not episodes:
        # A feed is a rolling window: an episode that scrolled out of it cannot
        # be resolved at all, and saying so beats a download error per missing
        # episode.
        raise podcast_feed.PodcastFeedError(
            "None of this card's episodes are in the feed any more — feeds only "
            "list the most recent ones. Pick from the current episodes instead."
        )
    episodes.sort(key=lambda ep: (ep.get("publish_date") or "", ep["id"]))
    return episodes


def sync_card(store, data_dir, esp_id, card_id):
    """Resolves and downloads one podcast card. Returns its final state.

    Never raises: every failure ends up as STATE_ERROR with a message the
    admin sees in the card list.
    """
    card = store.get_card(esp_id, card_id)
    if card is None or not is_podcast(card):
        return None

    podcast = card.get("podcast") or {}
    # Snapshot of the intent we are about to realise. If the admin edits the
    # card while we download, the result we produce describes the old
    # selection and must not overwrite the new one (see save_podcast_result).
    intent_token = card.get("updated_at")
    store.update_podcast_sync(
        esp_id, card_id, state=STATE_SYNCING, message="", done=0, total=0
    )

    def fail(message, reason=None):
        return store.update_podcast_sync(
            esp_id,
            card_id,
            state=STATE_ERROR,
            message=message,
            reason=reason,
            last_checked=store_lib.now_iso(),
        )

    try:
        episodes = _resolve_episodes(podcast)
    except podcast_feed.PodcastFeedError as exc:
        return fail(str(exc))
    except Exception as exc:  # noqa: BLE001
        # A card must never be left stuck on "syncing" by something we didn't
        # anticipate: an error state is retried, a stuck one is not.
        return fail(f"Unexpected error while resolving episodes: {exc}")

    store.update_podcast_sync(esp_id, card_id, total=len(episodes))

    limit_mb = store.get_settings().get("podcast_cache_limit_mb") or 0
    budget_bytes = int(limit_mb) * 1048576

    files, resolved = [], []
    for index, episode in enumerate(episodes):
        relpath = podcast_cache.episode_relpath(cache_folder(podcast), episode)
        try:
            info = podcast_cache.ensure_episode(
                data_dir, relpath, episode.get("audio_url"), budget_bytes
            )
        except podcast_cache.PodcastCacheFullError as exc:
            # Not the episode's fault, so don't name it — the admin needs the
            # storage sentence, not a track title.
            return fail(str(exc), exc.reason)
        except Exception as exc:  # noqa: BLE001
            # Anything at all, not just PodcastDownloadError/OSError: a
            # truncated transfer raises http.client.IncompleteRead, a bad
            # enclosure URL raises ValueError, and either one escaping here
            # would leave the card on "syncing" — which is_due treats as due,
            # so the worker would retry it every few seconds forever.
            return fail(f"{episode.get('title') or relpath}: {exc}")
        files.append(info)
        resolved.append(
            {
                "id": episode["id"],
                "title": episode.get("title") or "",
                "publish_date": episode.get("publish_date"),
                "duration": episode.get("duration") or 0,
                "path": info["path"],
                "size": info["size"],
            }
        )
        # Publishing the paths as we go keeps a concurrent prune (triggered by
        # deleting some other card) from collecting episodes this sync has
        # already downloaded but not yet committed to the card's file list.
        store.update_podcast_sync(
            esp_id,
            card_id,
            done=index + 1,
            pending_paths=[entry["path"] for entry in files],
        )

    result = store.save_podcast_result(esp_id, card_id, files, resolved, intent_token)
    try:
        prune_cache(store, data_dir)
    except OSError:
        # Housekeeping only — a card that just synced successfully must not be
        # reported as failed because the cleanup pass tripped over the disk.
        pass
    return result


def prune_cache(store, data_dir):
    """Drops cached episodes no card references any more, and records that it
    ran so the Media page can show the housekeeping instead of just asserting
    it happens. Returns (files removed, bytes freed)."""
    keep = set()
    for card in store.list_cards().values():
        if not is_podcast(card):
            continue
        keep.update(entry["path"] for entry in card.get("files", []))
        keep.update(sync_state(card)["pending_paths"])
    removed, freed = podcast_cache.prune(data_dir, keep)
    store.record_podcast_cleanup(removed, freed)
    return (removed, freed)


# --------------------------------------------------------------------------
# Worker
# --------------------------------------------------------------------------
class SyncWorker:
    """Single background thread doing the podcast work for the whole hub."""

    def __init__(self, store, data_dir):
        self.store = store
        self.data_dir = data_dir
        self._lock_path = os.path.join(data_dir, LOCK_FILENAME)
        self._lock_file = None
        self._wakeup = threading.Event()
        self._thread = None
        self._next_cleanup = 0.0

    def start(self):
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="podcast-sync", daemon=True)
        self._thread.start()

    def nudge(self):
        """Ask the worker to look for work now instead of after the poll
        interval. Only effective in the process that holds the lock — in any
        other one the change is picked up within POLL_SECONDS anyway, which
        is why this is a nicety and not the mechanism."""
        self._wakeup.set()

    # -- internals ---------------------------------------------------------
    def _acquire_lock(self):
        """True if this process is the elected sync worker."""
        if self._lock_file is not None:
            return True
        try:
            handle = open(self._lock_path, "a+b")
        except OSError:
            return False
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        self._lock_file = handle
        # A download interrupted by a restart left its card on "syncing"
        # forever; as the (single) fresh worker we can safely reset those.
        self._reset_stuck()
        return True

    def _reset_stuck(self):
        for card in list(self.store.list_cards().values()):
            if is_podcast(card) and sync_state(card)["state"] == STATE_SYNCING:
                self.store.update_podcast_sync(
                    card["esp_id"], card["card_id"], state=STATE_PENDING, done=0
                )

    def _run(self):
        while True:
            try:
                if self._acquire_lock():
                    self._tick()
            except Exception:  # noqa: BLE001 — a worker that dies stops all syncing
                pass
            self._wakeup.wait(POLL_SECONDS)
            self._wakeup.clear()

    def _tick(self):
        refresh_minutes = self.store.get_settings().get("podcast_refresh_minutes")
        now = _now()
        for card in list(self.store.list_cards().values()):
            if not is_due(card, refresh_minutes, now):
                continue
            try:
                sync_card(self.store, self.data_dir, card["esp_id"], card["card_id"])
            except Exception:  # noqa: BLE001
                # sync_card reports its own failures; this is the backstop for
                # one that could not even do that, so the remaining cards in
                # this tick still get their turn.
                pass

        # Runs on the first tick after startup and hourly after that, whether
        # or not anything synced.
        if time.monotonic() >= self._next_cleanup:
            self._next_cleanup = time.monotonic() + CLEANUP_INTERVAL_SECONDS
            try:
                prune_cache(self.store, self.data_dir)
            except OSError:
                pass


def start(store, data_dir):
    """Starts the hub's sync worker and returns it."""
    worker = SyncWorker(store, data_dir)
    worker.start()
    return worker
