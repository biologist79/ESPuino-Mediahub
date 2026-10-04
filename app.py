"""ESPuino MediaHub — lightweight local hub for centrally managing the
RFID assignments of multiple ESPuinos.

See ../mediahub-konzept.md for the full specification.
"""

import json
import os
import re

from flask import (
    Flask,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)
from flask_babel import Babel, get_locale, gettext as _, ngettext
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

import manifest as manifest_lib
import media_library
import podcast_cache
import podcast_feed
import podcast_sync
import store as store_lib
from espuino_client import delete_rfid_on_device

DATA_DIR = os.environ.get("MEDIAHUB_DATA", "/data")
MEDIA_DIR = os.environ.get("MEDIAHUB_MEDIA", "/media")
LANGUAGES = ["de", "en", "fr"]

app = Flask(__name__)
app.config["BABEL_DEFAULT_LOCALE"] = "de"
# gunicorn itself only ever speaks plain HTTP (see Dockerfile) — https only
# reaches the ESP32 at all via a reverse proxy terminating TLS in front of
# this container. Without ProxyFix, Flask builds url_for(_external=True)
# URLs (filesBaseUrl in the manifest) from the proxy's internal plain-HTTP
# request and silently downgrades them to http://, even though the ESPuino
# reached the manifest endpoint itself over https.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)
store = store_lib.Store(DATA_DIR)

# Resolving and downloading podcast episodes happens off the request path
# entirely (concept §7.3) — one elected worker per container does it, no
# matter how many gunicorn workers there are (see podcast_sync).
podcast_worker = podcast_sync.start(store, DATA_DIR)


def _secret_key():
    """A one-time, persisted secret is enough for flash messages and the language cookie."""
    path = os.path.join(DATA_DIR, "secret.key")
    if not os.path.exists(path):
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(path, "wb") as f:
            f.write(os.urandom(32))
    with open(path, "rb") as f:
        return f.read()


app.secret_key = _secret_key()


def _select_locale():
    lang = session.get("lang")
    if lang in LANGUAGES:
        return lang
    return request.accept_languages.best_match(LANGUAGES, "de")


babel = Babel(app, locale_selector=_select_locale)

# Flask-Babel only registers gettext/ngettext as Jinja globals; `_` (used
# throughout the templates) and `get_locale` (for <html lang> and the
# language switcher) need to be added explicitly.
app.jinja_env.globals["_"] = app.jinja_env.globals["gettext"]
app.jinja_env.globals["get_locale"] = get_locale


def _short_datetime(value):
    """"2026-07-15T13:56:58+00:00" -> "2026-07-15 13:56" — timestamps are
    always UTC (now_iso()), so the seconds/offset just add width without
    adding information for an at-a-glance table."""
    return value[:16].replace("T", " ") if value else value


app.jinja_env.filters["shortdt"] = _short_datetime


@app.route("/lang/<lang_code>")
def set_language(lang_code):
    if lang_code in LANGUAGES:
        session["lang"] = lang_code
    return redirect(request.args.get("next") or url_for("index"))


# --------------------------------------------------------------------------
# Optional hub password (admin UI only — the ESPuino-facing API stays open,
# see PUBLIC_ENDPOINTS below; concept §2/§19 originally ruled out auth
# entirely, revised to make it optional since the hub may be reachable
# beyond a single trusted household).
# --------------------------------------------------------------------------
PUBLIC_ENDPOINTS = {
    "login",
    "set_language",
    "health",
    "card_manifest",
    "serve_media",
    # A podcast card's episodes are fetched by the ESPuino exactly like
    # library files are, so this endpoint has to stay open for the same
    # reason (§5.4: devices can't log in).
    "serve_podcast_media",
    "static",
}


@app.before_request
def _require_login():
    if request.endpoint in PUBLIC_ENDPOINTS or request.endpoint is None:
        return
    if store.get_settings().get("password_hash") and not session.get("authenticated"):
        return redirect(url_for("login", next=request.path))


@app.context_processor
def _inject_auth_state():
    return {"hub_password_set": bool(store.get_settings().get("password_hash"))}


@app.route("/login", methods=["GET", "POST"])
def login():
    password_hash = store.get_settings().get("password_hash")
    if not password_hash:
        return redirect(url_for("index"))
    next_url = request.values.get("next") or url_for("index")
    if request.method == "POST":
        if check_password_hash(password_hash, request.form.get("password", "")):
            session["authenticated"] = True
            return redirect(next_url)
        flash(_("Incorrect password."), "error")
    return render_template("login.html", next=next_url)


@app.route("/logout", methods=["POST"])
def logout():
    session.pop("authenticated", None)
    return redirect(url_for("index"))


# --------------------------------------------------------------------------
# Web UI: dashboard
# --------------------------------------------------------------------------
@app.route("/")
def index():
    cards = store.list_cards()
    devices = store.list_devices()
    pending_count = sum(1 for c in cards.values() if c["status"] == "pending")
    assigned_count = sum(1 for c in cards.values() if c["status"] == "assigned")
    total_bytes = sum(
        f["size"]
        for c in cards.values()
        if c["kind"] in ("files", "podcast")
        for f in c.get("files", [])
    )
    device_label = ngettext("%(num)s device", "%(num)s devices", len(devices)) % {"num": len(devices)}
    return render_template(
        "index.html",
        device_label=device_label,
        pending_count=pending_count,
        assigned_count=assigned_count,
        total_bytes=total_bytes,
    )


@app.route("/health")
def health():
    return jsonify(status="ok", service="espuino-mediahub")


# --------------------------------------------------------------------------
# Web UI: devices
# --------------------------------------------------------------------------
def _delete_device_confirm(esp_id, card_count):
    if not card_count:
        return _("Delete ESPuino %(esp_id)s?", esp_id=esp_id)
    return ngettext(
        "There is still %(num)s card assignment for this ESPuino, which must be "
        "deleted first. Delete it automatically? Note: cards are only removed "
        "locally in MediaHub.",
        "There are still %(num)s card assignments for this ESPuino, which must "
        "be deleted first. Delete them automatically? Note: cards are only "
        "removed locally in MediaHub.",
        card_count,
    ) % {"num": card_count}


@app.route("/devices")
def devices():
    devices_by_id = store.list_devices()
    rows = []
    for esp_id, dev in devices_by_id.items():
        card_count = store.count_cards_for_device(esp_id)
        rows.append((esp_id, dev, card_count, _delete_device_confirm(esp_id, card_count)))
    rows.sort(key=lambda row: row[1]["last_seen"], reverse=True)
    return render_template("devices.html", devices=rows)


@app.route("/devices/<esp_id>/alias", methods=["POST"])
def set_device_alias(esp_id):
    if esp_id not in store.list_devices():
        abort(404)
    store.set_device_alias(esp_id, request.form.get("alias", "").strip())
    flash(_("Alias saved."), "success")
    return redirect(url_for("devices"))


@app.route("/devices/<esp_id>/delete", methods=["POST"])
def delete_device(esp_id):
    if esp_id not in store.list_devices():
        abort(404)
    n = store.delete_device(esp_id)
    if n:
        # The cascade may have removed the last card referencing a cached
        # podcast episode.
        podcast_sync.prune_cache(store, DATA_DIR)
        flash(_("Device %(esp_id)s and its %(num)s card assignment(s) deleted (locally in MediaHub only).", esp_id=esp_id, num=n), "success")
    else:
        flash(_("Device %(esp_id)s deleted.", esp_id=esp_id), "success")
    return redirect(url_for("devices"))


# --------------------------------------------------------------------------
# Web UI: cards & assignments
# --------------------------------------------------------------------------
def _content_label(card):
    if card["kind"] == "webradio":
        return "📻 " + _("Webradio")
    files = card.get("files", [])
    n = len(files)
    if card["kind"] == "podcast":
        label = "🎙️ " + ngettext("%(num)s episode", "%(num)s episodes", n) % {"num": n}
    else:
        label = "📁 " + ngettext("%(num)s file", "%(num)s files", n) % {"num": n}
    if n:
        size_mb = round(sum(f["size"] for f in files) / 1048576, 1)
        label += f" ({size_mb} MB)"
    return label


def _podcast_detail(card):
    """"<show> · newest episode" resp. the episode titles actually synced —
    what a podcast card shows in the Content column."""
    podcast = card.get("podcast") or {}
    show = podcast.get("show_title") or _("Podcast")
    if podcast.get("selection") == "latest":
        count = podcast.get("episode_count") or 1
        which = ngettext("newest episode", "%(num)s newest episodes", count) % {"num": count}
    else:
        episodes = podcast.get("episodes") or []
        which = ", ".join(e.get("title") or e["id"] for e in episodes)
    return f"{show} · {which}" if which else show


def _podcast_status(card):
    """Sync state of a podcast card, ready to render: (state, text)."""
    state = podcast_sync.sync_state(card)
    kind = state["state"]
    if kind == podcast_sync.STATE_READY:
        return (kind, _("up to date"))
    if kind == podcast_sync.STATE_SYNCING:
        if state["total"]:
            return (
                kind,
                _(
                    "downloading %(done)s/%(total)s",
                    done=state["done"],
                    total=state["total"],
                ),
            )
        return (kind, _("checking the feed…"))
    if kind == podcast_sync.STATE_ERROR:
        # Storage refusals are the errors an admin can actually act on, so
        # they get a translated sentence built here rather than the English
        # one the background worker had to log without a request locale.
        if state["reason"] == "cache_full":
            return (
                kind,
                _(
                    "Cache limit of %(limit)s MB reached — no new episodes are "
                    "downloaded. Raise it in Settings or keep fewer episodes.",
                    limit=store.get_settings().get("podcast_cache_limit_mb"),
                ),
            )
        if state["reason"] == "disk_full":
            return (
                kind,
                _(
                    "Not enough free disk space on the hub — MediaHub stopped "
                    "before filling it up."
                ),
            )
        return (kind, state["message"] or _("sync failed"))
    return (kind, _("waiting for download"))


def _content_detail(card):
    """What's actually behind the label — one file's path, a shared folder
    if every file sits in the same one, or the full file list as a last
    resort. Shown truncated in the table with this as the hover tooltip."""
    if card["kind"] == "webradio":
        return card.get("stream_url") or ""
    if card["kind"] == "podcast":
        return _podcast_detail(card)
    files = card.get("files", [])
    if not files:
        return ""
    if len(files) == 1:
        return files[0]["path"]
    dirs = {os.path.dirname(f["path"]) for f in files}
    if len(dirs) == 1:
        return next(iter(dirs)) or "/"
    return ", ".join(f["path"] for f in files)


def _assignment_targets(esp_id):
    """The devices one save writes to: the card being edited, plus every
    other ESPuino ticked in the form. Unticking a box never deletes an
    existing assignment — that stays the explicit Delete button in the card
    list, because a delete can call DELETE /rfid on the device (secure
    delete) and that is no business of a Save button."""
    known = store.list_devices()
    targets = [esp_id]
    for target_esp_id in request.form.getlist("target_esp_ids"):
        if target_esp_id != esp_id and target_esp_id in known:
            targets.append(target_esp_id)
    return targets


def _assignment_target_choices(esp_id, card_id):
    """The other known ESPuinos offered as additional targets, each with the
    label to show and whether it already carries this card. Already-assigned
    ones are pre-ticked so an edit doesn't silently let sibling assignments
    drift apart; everything else follows the default_assign_scope setting."""
    scope = store.get_settings().get("default_assign_scope", "reporting")
    choices = []
    for other_esp_id, dev in store.list_devices().items():
        if other_esp_id == esp_id:
            continue
        other = store.get_card(other_esp_id, card_id)
        assigned = other is not None and other["status"] == "assigned"
        choices.append(
            {
                "esp_id": other_esp_id,
                "label": dev.get("alias") or other_esp_id,
                "assigned": assigned,
                "checked": assigned or scope == "all",
            }
        )
    choices.sort(key=lambda choice: choice["label"].lower())
    return choices


def _cards_revision():
    """Short fingerprint of everything the card list and the dashboard's
    "new cards" badge show: how many cards exist, how many are still
    pending, and the newest tap/change timestamp."""
    cards_by_key = store.list_cards()
    pending = sum(1 for c in cards_by_key.values() if c["status"] == "pending")
    last_seen = max((c.get("last_seen") or "" for c in cards_by_key.values()), default="")
    updated = max((c.get("updated_at") or "" for c in cards_by_key.values()), default="")
    return f"{len(cards_by_key)}:{pending}:{last_seen}:{updated}"


@app.route("/cards/state")
def cards_state():
    """Lets an open admin page notice a card that was just tapped without
    the admin reloading. Deliberately polled by the browser instead of
    pushed from here: gunicorn runs two sync workers (see Dockerfile), so a
    held-open SSE stream would occupy one of them for as long as its tab
    stays open — two tabs and the ESPuino-facing API starves. A poll is an
    ordinary short request and gives its worker straight back."""
    return jsonify(revision=_cards_revision())


@app.context_processor
def _inject_live_cards():
    """Only the pages showing card counts get a revision, which is what
    switches on the auto-refresh markup in base.html."""
    if request.endpoint in ("index", "cards"):
        return {"live_cards_revision": _cards_revision()}
    return {}


@app.route("/cards")
def cards():
    only_pending = request.args.get("pending") == "1"
    esp_id_filter = request.args.get("esp_id") or ""
    cards_by_key = store.list_cards()
    devices_by_id = store.list_devices()
    rows = [
        (
            c,
            _content_label(c),
            _content_detail(c),
            devices_by_id.get(c["esp_id"], {}).get("alias") or c["esp_id"],
            _podcast_status(c) if c["kind"] == "podcast" else None,
        )
        for c in cards_by_key.values()
        if (not only_pending or c["status"] == "pending")
        and (not esp_id_filter or c["esp_id"] == esp_id_filter)
    ]
    rows.sort(key=lambda row: row[0]["last_seen"], reverse=True)
    return render_template(
        "cards.html",
        cards=rows,
        # A lazy delete leaves the downloaded episodes on the ESPuino's SD
        # card; for a podcast card that is worth saying out loud at the
        # moment of deletion, not just in the docs.
        delete_mode=store.get_settings()["delete_mode"],
        only_pending=only_pending,
        devices=devices_by_id,
        esp_id_filter=esp_id_filter,
        esp_id_filter_label=(devices_by_id.get(esp_id_filter, {}).get("alias") or esp_id_filter),
    )


@app.route("/cards/add", methods=["POST"])
def add_card():
    esp_id = request.form.get("esp_id", "").strip()
    card_id = request.form.get("card_id", "").strip()

    if not manifest_lib.is_valid_card_id(card_id):
        flash(_("Card ID must be exactly 12 digits."), "error")
        return redirect(url_for("cards"))

    if esp_id not in store.list_devices():
        flash(_("Please choose a known ESPuino."), "error")
        return redirect(url_for("cards"))

    existing = store.get_card(esp_id, card_id)
    if existing and existing["status"] == "assigned":
        # Not an error — "Add" doubles as "jump to this assignment's edit
        # form" — but flag it so a card ID typo doesn't silently overwrite
        # an already-configured assignment without the admin noticing.
        flash(
            _("Card %(id)s is already assigned on this ESPuino — opening it for editing.", id=card_id),
            "info",
        )
    return redirect(url_for("assign_card", esp_id=esp_id, card_id=card_id))


@app.route("/cards/<esp_id>/<card_id>/duplicate", methods=["POST"])
def duplicate_card(esp_id, card_id):
    source = store.get_card(esp_id, card_id)
    if source is None or source["status"] != "assigned":
        abort(404)

    target_esp_id = request.form.get("target_esp_id", "").strip()
    if target_esp_id == esp_id or target_esp_id not in store.list_devices():
        flash(_("Please choose a different, known ESPuino."), "error")
        return redirect(url_for("cards"))

    existing = store.get_card(target_esp_id, card_id)
    if existing and existing["status"] == "assigned":
        flash(
            _("Card %(id)s is already assigned on this ESPuino — opening it for editing.", id=card_id),
            "info",
        )
        return redirect(url_for("assign_card", esp_id=target_esp_id, card_id=card_id))

    if source["kind"] == "podcast":
        # Copy the *intent*, not the resolved file list: the duplicate re-syncs
        # on its own (hitting the same shared episode cache, so it usually
        # needs no download at all).
        store.save_podcast_assignment(
            target_esp_id, card_id, source["name"], source["play_mode"], source.get("podcast") or {}
        )
        podcast_worker.nudge()
    else:
        store.save_assignment(
            target_esp_id,
            card_id,
            source["name"],
            source["kind"],
            source["play_mode"],
            source["stream_url"],
            source["files"],
        )
    flash(_("Card %(id)s duplicated to %(esp_id)s.", id=card_id, esp_id=target_esp_id), "success")
    return redirect(url_for("cards"))


_RSS_EPISODE_ID = re.compile(r"^rss-[0-9a-f]{16}$")


def _is_valid_episode_id(episode_id):
    """Feed episode ids are the hash podcast_feed derives from a GUID."""
    return bool(_RSS_EPISODE_ID.match(episode_id))


def _parse_podcast_form(form):
    """Validates the podcast picker's hidden JSON field.

    Returns `(podcast, None)` — the intent to store, with the play mode
    included under "play_mode" — or `(None, message)`.
    """
    try:
        raw = json.loads(form.get("podcast_json") or "{}")
    except ValueError:
        raw = {}
    if not isinstance(raw, dict):
        raw = {}

    feed_url = str(raw.get("feed_url") or "").strip()
    if not feed_url.startswith(("http://", "https://")):
        return (None, _("Please load a podcast feed first."))

    selection = raw.get("selection")
    if selection not in ("latest", "episodes"):
        selection = "latest"

    episodes = []
    if selection == "episodes":
        for entry in raw.get("episodes") or []:
            # The payload is JSON from a hidden form field, so nothing about
            # its shape is guaranteed — a wrong type here has to come back as
            # "please pick an episode", not as a 500.
            if not isinstance(entry, dict):
                continue
            episode_id = str(entry.get("id") or "").strip()
            if not _is_valid_episode_id(episode_id):
                continue
            try:
                duration = int(entry.get("duration") or 0)
            except (TypeError, ValueError):
                duration = 0
            episodes.append(
                {
                    "id": episode_id,
                    "title": str(entry.get("title") or "")[:300],
                    "publish_date": str(entry.get("publish_date") or "")[:64] or None,
                    "duration": duration,
                }
            )
        episodes = episodes[: podcast_sync.MAX_EPISODES]
        if not episodes:
            return (None, _("Please select at least one episode."))

    try:
        episode_count = int(raw.get("episode_count") or 1)
    except (TypeError, ValueError):
        episode_count = 1
    episode_count = max(1, min(episode_count, podcast_sync.MAX_EPISODES))

    try:
        play_mode = int(form.get("podcast_play_mode", ""))
    except ValueError:
        play_mode = -1
    if not manifest_lib.is_valid_podcast_play_mode(play_mode):
        return (None, _("Please choose a valid play mode."))

    effective_count = episode_count if selection == "latest" else len(episodes)
    if play_mode in manifest_lib.SINGLE_FILE_PLAY_MODES and effective_count > 1:
        return (
            None,
            _("This play mode only supports a single episode — please select just one."),
        )

    return (
        {
            "feed_url": feed_url,
            "show_title": str(raw.get("show_title") or "")[:300],
            "show_image": str(raw.get("show_image") or "") or None,
            "selection": selection,
            "episode_count": episode_count,
            "episodes": episodes,
            "play_mode": play_mode,
        },
        None,
    )


@app.route("/cards/<esp_id>/<card_id>/assign", methods=["GET", "POST"])
def assign_card(esp_id, card_id):
    card = store.get_card(esp_id, card_id) or {
        "esp_id": esp_id,
        "card_id": card_id,
        "status": "pending",
        "name": "",
        "kind": "files",
        "play_mode": None,
        "stream_url": "",
        "files": [],
    }

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        kind = request.form.get("kind", "files")

        if kind == "webradio":
            stream_url = request.form.get("stream_url", "").strip()
            if not stream_url:
                flash(_("A stream URL is required for webradio."), "error")
                return redirect(url_for("assign_card", esp_id=esp_id, card_id=card_id))
            payload = (name, "webradio", None, stream_url, [])
        elif kind == "podcast":
            podcast, error = _parse_podcast_form(request.form)
            if error:
                flash(error, "error")
                return redirect(url_for("assign_card", esp_id=esp_id, card_id=card_id))
            play_mode = podcast.pop("play_mode")
            # One intent per device, like any other assignment. The episodes
            # are cached once and shared (podcast_cache), so a second ESPuino
            # costs the hub no second download.
            targets = _assignment_targets(esp_id)
            for target_esp_id in targets:
                store.save_podcast_assignment(target_esp_id, card_id, name, play_mode, podcast)
            # The episodes themselves are fetched in the background; the card
            # only becomes playable once that finished (see the sync column
            # in the card list).
            podcast_worker.nudge()
            if len(targets) == 1:
                flash(
                    _("Card %(id)s assigned — downloading the episodes now.", id=card_id),
                    "success",
                )
            else:
                flash(
                    _(
                        "Card %(id)s assigned on %(num)s ESPuinos — downloading the episodes now.",
                        id=card_id,
                        num=len(targets),
                    ),
                    "success",
                )
            return redirect(url_for("cards"))
        else:
            try:
                play_mode = int(request.form.get("play_mode", ""))
            except ValueError:
                play_mode = -1
            if not manifest_lib.is_valid_file_play_mode(play_mode):
                flash(_("Please choose a valid play mode."), "error")
                return redirect(url_for("assign_card", esp_id=esp_id, card_id=card_id))

            try:
                selected_paths = json.loads(request.form.get("selected_paths_json", "[]"))
            except ValueError:
                selected_paths = []

            files, missing = [], []
            for relpath in selected_paths:
                info = media_library.stat_and_hash(MEDIA_DIR, relpath)
                if info is None:
                    missing.append(relpath)
                else:
                    files.append(info)

            if missing:
                flash(
                    _(
                        "Some selected files could not be used (missing, or not "
                        "readable by the container's user) and were skipped: "
                        "%(paths)s",
                        paths=", ".join(missing),
                    ),
                    "error",
                )

            if not files:
                flash(_("At least one audio file is required."), "error")
                return redirect(url_for("assign_card", esp_id=esp_id, card_id=card_id))

            if play_mode in manifest_lib.SINGLE_FILE_PLAY_MODES and len(files) > 1:
                flash(_("This play mode only supports a single file — please select just one."), "error")
                return redirect(url_for("assign_card", esp_id=esp_id, card_id=card_id))

            files.sort(key=lambda f: f["path"])
            payload = (name, "files", play_mode, None, files)

        # Same content, one independent assignment per device — the ESPuinos
        # keep their own play positions and caches.
        targets = _assignment_targets(esp_id)
        for target_esp_id in targets:
            store.save_assignment(target_esp_id, card_id, *payload)

        if len(targets) == 1:
            flash(_("Card %(id)s assigned.", id=card_id), "success")
        else:
            flash(
                _("Card %(id)s assigned on %(num)s ESPuinos.", id=card_id, num=len(targets)),
                "success",
            )
        return redirect(url_for("cards"))

    return render_template(
        "card_form.html",
        esp_id=esp_id,
        card_id=card_id,
        card=card,
        device=store.list_devices().get(esp_id),
        target_choices=_assignment_target_choices(esp_id, card_id),
        play_modes=manifest_lib.FILE_PLAY_MODES,
        single_file_play_modes=list(manifest_lib.SINGLE_FILE_PLAY_MODES),
        recursive_play_modes=list(manifest_lib.RECURSIVE_PLAY_MODES),
        podcast_play_modes=manifest_lib.PODCAST_PLAY_MODES,
        default_podcast_play_mode=manifest_lib.DEFAULT_PODCAST_PLAY_MODE,
        max_podcast_episodes=podcast_sync.MAX_EPISODES,
    )


# --------------------------------------------------------------------------
# Web UI: podcasts (concept §7.3)
#
# The feed is fetched through the hub rather than from the browser: it keeps
# the frontend CDN-free and offline-capable like the rest of the UI (no
# third-party script, no CORS dance), lets one shared in-process cache serve
# every admin, and means only the hub ever talks to the outside.
# --------------------------------------------------------------------------
@app.route("/podcast/feed")
def podcast_feed_preview():
    """Title and episodes of a podcast feed the admin pasted.

    The URL comes from a form field — the hub is being asked to read a feed
    of the admin's choosing. It is an authenticated action (the assignment UI
    sits behind the optional hub password), the scheme is restricted to
    HTTP(S) and the response is size-capped; beyond that the hub does not
    second-guess which feed its own admin may subscribe to, since a
    self-hosted feed on the same LAN is a perfectly ordinary thing to want.
    """
    feed_url = (request.args.get("url") or "").strip()
    try:
        feed = podcast_feed.fetch_feed(feed_url)
    except podcast_feed.PodcastFeedError as exc:
        return jsonify(error=str(exc)), 502
    return jsonify(
        feed_url=feed["feed_url"],
        title=feed["title"],
        description=feed["description"],
        image_url=feed["image_url"],
        total=len(feed["episodes"]),
        episodes=feed["episodes"],
    )


@app.route("/podcast/status")
def podcast_status():
    """Sync state of every podcast card — polled by the card list so a
    running download shows its progress without a page reload."""
    states = {}
    for card in store.list_cards().values():
        if card["kind"] != "podcast":
            continue
        state, text = _podcast_status(card)
        states[f"{card['esp_id']}/{card['card_id']}"] = {
            "state": state,
            "text": text,
            "label": _content_label(card),
        }
    return jsonify(cards=states)


@app.route("/cards/<esp_id>/<card_id>/podcast-refresh", methods=["POST"])
def podcast_refresh_card(esp_id, card_id):
    """"Check for new episodes now" — the manual counterpart to the
    configurable refresh interval (Settings)."""
    if store.mark_podcast_pending(esp_id, card_id) is None:
        abort(404)
    podcast_worker.nudge()
    flash(_("Checking the feed for new episodes of card %(id)s.", id=card_id), "success")
    return redirect(url_for("cards"))


@app.route("/podcast-media/<path:filename>")
def serve_podcast_media(filename):
    """Serves a cached podcast episode to an ESPuino — the podcast-cache
    counterpart of serve_media(), and the `filesBaseUrl` of a podcast card's
    manifest."""
    return send_from_directory(podcast_cache.cache_root(DATA_DIR), filename)


@app.route("/cards/<esp_id>/<card_id>/force-refresh", methods=["POST"])
def force_refresh_card(esp_id, card_id):
    if store.bump_force_epoch(esp_id, card_id) is None:
        abort(404)
    flash(_("Force refresh triggered for card %(id)s.", id=card_id), "success")
    return redirect(url_for("cards"))


@app.route("/cards/force-refresh-all", methods=["POST"])
def force_refresh_all():
    n = store.bump_force_epoch_all()
    flash(_("Force refresh triggered for all %(num)s cards.", num=n), "success")
    return redirect(url_for("cards"))


@app.route("/cards/<esp_id>/<card_id>/delete", methods=["POST"])
def delete_card(esp_id, card_id):
    card = store.get_card(esp_id, card_id)
    if card is None:
        abort(404)

    delete_mode = store.get_settings()["delete_mode"]
    if delete_mode == "secure" and card["status"] == "assigned":
        # The assignment already names its one ESPuino — no more guessing
        # from whichever device last happened to tap the card.
        device = store.list_devices().get(esp_id)
        ip = device.get("ip") if device else None
        if not delete_rfid_on_device(ip, card_id):
            flash(
                _(
                    "Secure delete: ESPuino (%(esp_id)s) did not confirm the deletion. "
                    "Card was kept, please retry.",
                    esp_id=esp_id,
                ),
                "error",
            )
            return redirect(url_for("cards"))

    # Only the assignment (NVS-equivalent entry) is removed — the underlying
    # files belong to the admin's own media library, not to MediaHub, and
    # are never touched here. Cached podcast episodes are the one
    # exception: those *are* hub-owned, so they get cleaned up once no card
    # references them any more.
    store.delete_card(esp_id, card_id)
    podcast_sync.prune_cache(store, DATA_DIR)

    flash(_("Card %(id)s deleted (%(mode)s).", id=card_id, mode=delete_mode), "success")
    return redirect(url_for("cards"))


def _files_base_url(card):
    """`filesBaseUrl` for a card: the media library for a normal assignment,
    the hub's podcast cache for a podcast one. Both are a plain HTTP
    prefix to the ESPuino — it never learns which is which (§7.3)."""
    endpoint = "serve_podcast_media" if card["kind"] == "podcast" else "serve_media"
    return url_for(endpoint, filename="", _external=True)


@app.route("/cards/<esp_id>/<card_id>/manifest-preview")
def manifest_preview(esp_id, card_id):
    """Admin-only preview of the manifest an ESPuino would receive for this
    card — same builder as card_manifest(), but without its side effects
    (no device/card "last seen" tracking, no pending-registration)."""
    card = store.get_card(esp_id, card_id)
    if card is None or card["status"] != "assigned":
        abort(404)
    return jsonify(manifest_lib.build_manifest(card_id, card, _files_base_url(card)))


# --------------------------------------------------------------------------
# Web UI: media (per-card storage usage)
# --------------------------------------------------------------------------
@app.route("/media")
def media_overview():
    cards_by_key = store.list_cards()
    devices_by_id = store.list_devices()
    rows = [
        (
            c,
            sum(f["size"] for f in c.get("files", [])),
            devices_by_id.get(c["esp_id"], {}).get("alias") or c["esp_id"],
        )
        for c in cards_by_key.values()
        if c["kind"] in ("files", "podcast") and c.get("files")
    ]
    rows.sort(key=lambda row: row[1], reverse=True)
    return render_template("media.html", rows=rows)


def _podcast_cache_panel():
    """What the Settings page shows next to the cache limit.

    Deliberately more than a number: the cache is the only storage MediaHub
    owns, it fills itself in the background, and it empties itself again —
    so the admin setting the limit has to see both halves of that, otherwise
    the only honest thing they could do is go and look in the data volume.
    """
    settings = store.get_settings()
    state = store.get_podcast_cache_state()
    used = podcast_cache.total_bytes(DATA_DIR)
    limit_mb = settings.get("podcast_cache_limit_mb") or 0
    limit = int(limit_mb) * 1048576

    episodes = 0
    for card in store.list_cards().values():
        if card["kind"] == "podcast":
            episodes += len(card.get("files", []))

    return {
        "used": used,
        "limit": limit,
        "percent": round(used * 100 / limit) if limit else 0,
        "near_limit": bool(limit) and used * 100 / limit >= 90,
        "free": podcast_cache.free_bytes(DATA_DIR),
        "episode_slots": episodes,
        "last_cleanup_at": state.get("last_cleanup_at"),
        "last_removal_at": state.get("last_removal_at"),
        "last_removed_files": state.get("last_removed_files") or 0,
        "last_removed_bytes": state.get("last_removed_bytes") or 0,
    }


@app.route("/media/browse")
def media_browse():
    """JSON directory listing under MEDIA_DIR, consumed by the card
    assignment form's inline file tree (static/js/media-browser.js)."""
    relpath = request.args.get("path", "")
    listing = media_library.list_directory(MEDIA_DIR, relpath)
    if listing is None:
        abort(404)
    return jsonify(path=relpath.strip("/"), dirs=listing["dirs"], files=listing["files"])


@app.route("/media/browse-recursive")
def media_browse_recursive():
    """Like /media/browse, but gathers audio files from subfolders too (up
    to the configured recursion depth) — used by "use folder" when the
    card's play mode is one of the recursive ones."""
    relpath = request.args.get("path", "")
    if media_library.list_directory(MEDIA_DIR, relpath) is None:
        abort(404)
    depth = store.get_settings().get("recursion_depth", store_lib.DEFAULT_RECURSION_DEPTH)
    files = media_library.list_audio_files_recursive(MEDIA_DIR, relpath, depth)
    return jsonify(path=relpath.strip("/"), files=files)


# --------------------------------------------------------------------------
# Web UI: settings
# --------------------------------------------------------------------------
@app.route("/settings", methods=["GET", "POST"])
def settings():
    if request.method == "POST":
        mode = request.form.get("delete_mode")
        if mode in ("lazy", "secure"):
            store.set_delete_mode(mode)

        scope = request.form.get("default_assign_scope")
        if scope in ("reporting", "all"):
            store.set_default_assign_scope(scope)

        try:
            depth = int(request.form.get("recursion_depth", ""))
        except ValueError:
            depth = -1
        if 0 <= depth <= 20:
            store.set_recursion_depth(depth)
        else:
            flash(_("Recursion depth must be between 0 and 20."), "error")
            return redirect(url_for("settings"))

        try:
            refresh_minutes = int(request.form.get("podcast_refresh_minutes", ""))
        except ValueError:
            refresh_minutes = -1
        if 0 <= refresh_minutes <= 10080:
            store.set_podcast_refresh_minutes(refresh_minutes)
        else:
            flash(
                _("The episode check interval must be between 0 and 10080 minutes (one week)."),
                "error",
            )
            return redirect(url_for("settings"))

        try:
            cache_limit = int(request.form.get("podcast_cache_limit_mb", ""))
        except ValueError:
            cache_limit = -1
        if 0 <= cache_limit <= 1024 * 1024:
            store.set_podcast_cache_limit_mb(cache_limit)
        else:
            flash(_("The cache limit must be between 0 and 1048576 MB."), "error")
            return redirect(url_for("settings"))

        flash(_("Settings saved."), "success")
        return redirect(url_for("settings"))
    return render_template(
        "settings.html", settings=store.get_settings(), cache=_podcast_cache_panel()
    )


@app.route("/settings/password", methods=["POST"])
def set_password():
    new_password = request.form.get("new_password", "")
    confirm_password = request.form.get("confirm_password", "")
    if not new_password:
        flash(_("Please enter a password."), "error")
    elif new_password != confirm_password:
        flash(_("Passwords do not match."), "error")
    else:
        store.set_password_hash(generate_password_hash(new_password))
        flash(_("Hub password set."), "success")
    return redirect(url_for("settings"))


@app.route("/settings/password/remove", methods=["POST"])
def remove_password():
    store.set_password_hash(None)
    session.pop("authenticated", None)
    flash(_("Hub password removed."), "success")
    return redirect(url_for("settings"))


# --------------------------------------------------------------------------
# MediaHub API (see mediahub-konzept.md §6/§7)
# --------------------------------------------------------------------------
@app.route("/<esp_id>/card/<card_id>/manifest.json")
def card_manifest(esp_id, card_id):
    ts = store_lib.now_iso()
    store.touch_device(esp_id, request.remote_addr, card_id, ts)

    card = store.get_card(esp_id, card_id)
    if card is None:
        store.register_pending(esp_id, card_id, ts)
        return jsonify(error="unknown_card", status="pending"), 404

    store.touch_card_seen(esp_id, card_id, ts)
    if card["status"] != "assigned":
        return jsonify(error="not_assigned", status="pending"), 404

    if card["kind"] == "podcast" and not card.get("files"):
        # Assigned, but the hub hasn't finished fetching the episode yet
        # (§7.3). Answering "not ready" rather than an empty file list keeps
        # the ESPuino from caching a manifest with nothing to play; it simply
        # shows the same short error as for an unknown card and the admin
        # taps again once the download completed.
        return jsonify(error="not_ready", status="preparing"), 404

    return jsonify(manifest_lib.build_manifest(card_id, card, _files_base_url(card)))


@app.route("/media/<path:filename>")
def serve_media(filename):
    return send_from_directory(MEDIA_DIR, filename)


if __name__ == "__main__":
    # Development only; the container runs Gunicorn instead (see Dockerfile).
    app.run(host="0.0.0.0", port=8080, debug=True)
