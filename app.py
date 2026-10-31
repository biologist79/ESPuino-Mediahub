"""ESPuino MediaHub — lightweight local hub for centrally managing the
RFID assignments of multiple ESPuinos.

See ../mediahub-konzept.md for the full specification.
"""

import json
import os
import posixpath

from flask import (
    Flask,
    Response,
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

import cover_art
import manifest as manifest_lib
import media_library
import print_back
import printer_client
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
PUBLIC_ENDPOINTS = {"login", "set_language", "health", "card_manifest", "serve_media", "static"}


@app.before_request
def _require_login():
    if request.endpoint in PUBLIC_ENDPOINTS or request.endpoint is None:
        return
    if store.get_settings().get("password_hash") and not session.get("authenticated"):
        return redirect(url_for("login", next=request.path))


@app.context_processor
def _inject_auth_state():
    return {"hub_password_set": bool(store.get_settings().get("password_hash"))}


@app.context_processor
def _inject_printer():
    """The print shop's URL, read at render time so there is one source for it
    (`printer_client.BASE_URL`, which is also where its calls go)."""
    return {"printer_url": printer_client.BASE_URL}


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
    print_project_count = len(store.list_print_projects())
    pending_count = sum(1 for c in cards.values() if c["status"] == "pending")
    assigned_count = sum(1 for c in cards.values() if c["status"] == "assigned")
    total_bytes = sum(
        f["size"] for c in cards.values() if c["kind"] == "files" for f in c.get("files", [])
    )
    device_label = ngettext("%(num)s device", "%(num)s devices", len(devices)) % {"num": len(devices)}
    print_project_label = ngettext("%(num)s project", "%(num)s projects", print_project_count) % {
        "num": print_project_count
    }
    return render_template(
        "index.html",
        device_label=device_label,
        pending_count=pending_count,
        assigned_count=assigned_count,
        total_bytes=total_bytes,
        print_project_label=print_project_label,
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
    label = "📁 " + ngettext("%(num)s file", "%(num)s files", n) % {"num": n}
    if n:
        size_mb = round(sum(f["size"] for f in files) / 1048576, 1)
        label += f" ({size_mb} MB)"
    return label


def _content_detail(card):
    """What's actually behind the label — one file's path, a shared folder
    if every file sits in the same one, or the full file list as a last
    resort. Shown truncated in the table with this as the hover tooltip."""
    if card["kind"] == "webradio":
        return card.get("stream_url") or ""
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
    # The row carries the card's *identity* (the store key's second half), not
    # `card.card_id`: a card ordered from the print shop has no UID yet, and every
    # link below has to address it the way the store and the URL space do.
    rows = [
        (
            store_lib.split_card_key(key, c),
            c,
            _content_label(c),
            _content_detail(c),
            devices_by_id.get(c["esp_id"], {}).get("alias") or c["esp_id"],
        )
        for key, c in cards_by_key.items()
        if (not only_pending or c["status"] == "pending")
        and (not esp_id_filter or c["esp_id"] == esp_id_filter)
    ]
    # row[0] is the identity, row[1] the record itself.
    rows.sort(key=lambda row: row[1]["last_seen"], reverse=True)
    return render_template(
        "cards.html",
        cards=rows,
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

    store.save_assignment(
        target_esp_id,
        card_id,
        source["name"],
        source["kind"],
        source["play_mode"],
        source["stream_url"],
        source["files"],
        source.get("play_root", ""),
        source.get("print"),
    )
    flash(_("Card %(id)s duplicated to %(esp_id)s.", id=card_id, esp_id=target_esp_id), "success")
    return redirect(url_for("cards"))


def _read_assignment(form):
    """The assignment a card form carries, validated: `(payload, error, warning)`.

    Both the ordinary assignment form and the print-card form end in the same
    `save_assignment` call, so both read their content through this function — a
    rule only one of them applied would be a bug in whichever form the admin
    happened to use.

    `payload` is the six fields `save_assignment` takes after the card's identity.
    `error` is fatal (nothing may be written) and `warning` is not (the assignment
    is usable, something was skipped); either, both or neither may be set, and a
    warning is reported alongside an error so a form does not lose it.
    """
    name = form.get("name", "").strip()
    kind = form.get("kind", "files")

    if kind == "webradio":
        stream_url = form.get("stream_url", "").strip()
        if not stream_url:
            return None, _("A stream URL is required for webradio."), None
        return (name, "webradio", None, stream_url, [], ""), None, None

    try:
        play_mode = int(form.get("play_mode", ""))
    except ValueError:
        play_mode = -1
    if not manifest_lib.is_valid_file_play_mode(play_mode):
        return None, _("Please choose a valid play mode."), None

    try:
        selected_paths = json.loads(form.get("selected_paths_json", "[]"))
    except ValueError:
        selected_paths = []

    files, missing = [], []
    for relpath in selected_paths:
        info = media_library.stat_and_hash(MEDIA_DIR, relpath)
        if info is None:
            missing.append(relpath)
        else:
            files.append(info)

    warning = None
    if missing:
        warning = _(
            "Some selected files could not be used (missing, or not "
            "readable by the container's user) and were skipped: "
            "%(paths)s",
            paths=", ".join(missing),
        )

    if not files:
        return None, _("At least one audio file is required."), warning

    if play_mode in manifest_lib.SINGLE_FILE_PLAY_MODES and len(files) > 1:
        return (
            None,
            _("This play mode only supports a single file — please select just one."),
            warning,
        )

    # The ESPuino is handed a folder, not a file list (see
    # SINGLE_FOLDER_PLAY_MODES): with files from several folders these modes find
    # nothing to play at all. The browser already refuses the combination, but a
    # form can be submitted without it.
    folders = {posixpath.dirname(f["path"]) for f in files}
    if play_mode in manifest_lib.SINGLE_FOLDER_PLAY_MODES and len(folders) > 1:
        return (
            None,
            _(
                "This play mode plays a single folder, but the selected files "
                "lie in %(num)s different ones. Choose a recursive play mode, "
                "or pick files from one folder only.",
                num=len(folders),
            ),
            warning,
        )

    files.sort(key=lambda f: f["path"])
    # The folder "use folder" was applied to, if that is how the selection came
    # about. Validated against the library rather than trusted: it is a hidden form
    # field, and it ends up in the manifest as the folder an ESPuino will play.
    play_root = (form.get("play_root") or "").strip().strip("/").strip()
    if play_root and media_library.list_directory(MEDIA_DIR, play_root) is None:
        play_root = ""
    return (name, "files", play_mode, None, files, play_root), None, warning


@app.route("/cards/<esp_id>/<card_id>/assign", methods=["GET", "POST"])
def assign_card(esp_id, card_id):
    card = store.get_card(esp_id, card_id) or {
        "esp_id": esp_id,
        # A card ordered from the print shop has no UID yet and is addressed by a
        # hub token instead — which is not a card id and must not be shown as one.
        "card_id": card_id if manifest_lib.is_valid_card_id(card_id) else None,
        "status": "pending",
        "name": "",
        "kind": "files",
        "play_mode": None,
        "stream_url": "",
        "files": [],
    }

    if request.method == "POST":
        payload, error, warning = _read_assignment(request.form)
        if warning:
            # Not fatal: the rest of the selection is usable, and the admin is told
            # what was dropped rather than losing the whole form over it.
            flash(warning, "error")
        if error:
            flash(error, "error")
            return redirect(url_for("assign_card", esp_id=esp_id, card_id=card_id))

        # Same content, one independent assignment per device — the ESPuinos
        # keep their own play positions and caches.
        targets = _assignment_targets(esp_id)
        for target_esp_id in targets:
            # A card the hub ordered printed carries its print order to every device
            # it is written to: that reference is what `promote_print_card` finds the
            # card by once the printer reports the UID. A card a device reported has
            # none, and then nothing is passed.
            store.save_assignment(target_esp_id, card_id, *payload, print_ref=card.get("print"))

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
        single_folder_play_modes=list(manifest_lib.SINGLE_FOLDER_PLAY_MODES),
        gather_recursive_play_modes=list(manifest_lib.GATHER_RECURSIVE_PLAY_MODES),
    )


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
    # Secure delete calls the ESPuino with this id. A card that has no UID yet is
    # keyed by a hub token, which means nothing to a device — and the card is not
    # on any device in the first place.
    if (
        delete_mode == "secure"
        and card["status"] == "assigned"
        and manifest_lib.is_valid_card_id(card_id)
    ):
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
    # are never touched here.
    store.delete_card(esp_id, card_id)

    flash(_("Card %(id)s deleted (%(mode)s).", id=card_id, mode=delete_mode), "success")
    return redirect(url_for("cards"))


@app.route("/cards/<esp_id>/<card_id>/manifest-preview")
def manifest_preview(esp_id, card_id):
    """Admin-only preview of the manifest an ESPuino would receive for this
    card — same builder as card_manifest(), but without its side effects
    (no device/card "last seen" tracking, no pending-registration)."""
    card = store.get_card(esp_id, card_id)
    # A card without a UID has no manifest to preview either: the manifest is built
    # from a card id, and there is none until the card is printed.
    if card is None or card["status"] != "assigned" or not manifest_lib.is_valid_card_id(card_id):
        abort(404)
    files_base_url = url_for("serve_media", filename="", _external=True)
    return jsonify(manifest_lib.build_manifest(card_id, card, files_base_url))


# --------------------------------------------------------------------------
# Web UI: print projects (orders at the card-printing service)
# --------------------------------------------------------------------------
@app.route("/print")
def print_projects():
    projects = store.list_print_projects()
    rows = [
        (token, project, store.count_cards_for_print_project(token))
        for token, project in projects.items()
    ]
    rows.sort(key=lambda row: row[1]["created_at"], reverse=True)
    return render_template("print_projects.html", projects=rows)


def _read_order_into(token, project):
    """Asks the print shop about this project's order and stores what it says.

    Returns `(error, promoted)`: the error to show the admin — None when the shop
    answered — and how many hub cards the answer turned into real ones. The status is
    the shop's own vocabulary and is never interpreted here.

    A card's `serial_number` is the UID the print process read off the chip, so a
    card that has one is the card a device now holds, and the assignment made for it
    before printing moves onto that number here. This is the only place that happens:
    a tap cannot do it, because the hub would have to ask the shop on a device's
    request, and the button on the project page is what does the asking.

    `token` is passed alongside the record because the record does not carry its own
    key — the store keys projects by it, and a second copy could disagree with it.
    """
    try:
        order = printer_client.get_order(project["prefix"])
        cards = printer_client.list_cards(project["prefix"])
    except printer_client.PrinterError as exc:
        return str(exc), 0
    store.set_print_project_status(token, order.get("status"))

    ts = store_lib.now_iso()
    promoted = 0
    for card in cards:
        uid = str(card.get("serial_number") or "").strip()
        if not manifest_lib.is_valid_card_id(uid):
            # Not something this hub keys a card by. The field is written by the
            # print process alone, so this is not distrust: it is that the whole
            # record is keyed by the card id, and a value that is not one would
            # produce a card the ESPuino could never report.
            continue
        promoted += store.promote_print_card({"token": token, "id": card.get("id")}, uid, ts)
    return None, promoted


@app.route("/print/new", methods=["POST"])
def create_print_project():
    name = request.form.get("name", "").strip()
    if not name:
        flash(_("Please give the print project a name."), "error")
        return redirect(url_for("print_projects"))

    project = store.create_print_project(name)
    flash(_("Print project %(name)s created.", name=name), "success")
    # The order is placed right away, so the customer has something to open and pay
    # for. A shop that is down does not lose the project: it stays here, unlinked,
    # and "Check order" places it later — the uuid is already minted either way.
    try:
        order = printer_client.ensure_order(project["prefix"])
    except printer_client.PrinterError as exc:
        flash(
            _("The print shop did not take the order: %(error)s", error=str(exc)),
            "error",
        )
    else:
        store.set_print_project_status(project["token"], order.get("status"))
    return redirect(url_for("print_project", token=project["token"]))


@app.route("/print/<token>/sync", methods=["POST"])
def sync_print_project(token):
    """Re-reads the order at the print shop: its status, and — once cards exist —
    the UIDs the print process has filled in. The (only) trigger is the button on the
    project page: nothing polls the shop, and nothing here runs on a device's path."""
    project = store.get_print_project(token)
    if project is None:
        abort(404)
    error, promoted = _read_order_into(token, project)
    if error:
        flash(_("The print shop could not be read: %(error)s", error=error), "error")
        return redirect(url_for("print_project", token=token))
    status = (store.get_print_project(token) or {}).get("status") or ""
    flash(_("Order at the print shop: %(status)s", status=status), "success")
    if promoted:
        # The cards themselves are on the page below, under Printed — this only says
        # that something changed, since a check that promotes nothing looks the same
        # as one that was never run.
        flash(
            ngettext(
                "%(num)s card is printed and works here now.",
                "%(num)s cards are printed and work here now.",
                promoted,
            )
            % {"num": promoted},
            "success",
        )
    return redirect(url_for("print_project", token=token))


@app.route("/print/<token>")
def print_project(token):
    project = store.get_print_project(token)
    if project is None:
        abort(404)

    devices_by_id = store.list_devices()
    # One printed card is one hub card per device, so they are grouped by the card
    # they become at the shop — that grouping is also what the print sync works
    # per card, and what tells the admin whether a card is printed yet.
    groups = {}
    for key, card in store.list_cards().items():
        ref = card.get("print") or {}
        if ref.get("token") != token:
            continue
        group = groups.setdefault(
            ref.get("id"),
            {"id": ref.get("id"), "promoted_at": ref.get("promoted_at"), "cards": []},
        )
        group["cards"].append(
            (
                store_lib.split_card_key(key, card),
                card,
                devices_by_id.get(card["esp_id"], {}).get("alias") or card["esp_id"],
            )
        )
    for group in groups.values():
        group["cards"].sort(key=lambda row: row[2].lower())
    ordered = [groups[shop_id] for shop_id in sorted(groups, key=lambda i: (i is None, i))]
    return render_template(
        "print_project.html",
        token=token,
        project=project,
        groups=ordered,
        card_count=store.count_cards_for_print_project(token),
    )


@app.route("/print/<token>/delete", methods=["POST"])
def delete_print_project(token):
    if store.get_print_project(token) is None:
        abort(404)
    # Deliberately no cascade: these are live cards at the shop.
    count = store.count_cards_for_print_project(token)
    if count:
        flash(
            ngettext(
                "This print project still has %(num)s card — delete it first.",
                "This print project still has %(num)s cards — delete them first.",
                count,
            )
            % {"num": count},
            "error",
        )
        return redirect(url_for("print_project", token=token))
    store.delete_print_project(token)
    flash(_("Print project deleted."), "success")
    return redirect(url_for("print_projects"))


def _uploaded_cover():
    """`(cover, error)` from the cover part of a card form.

    `cover` is `(bytes, content_type)` the way `cover_art.resolve` wants it. An
    upload the print shop cannot print is an *error*, not a silent fall back to the
    media: the admin picked that file on purpose, and a card printed with the wrong
    picture is worse than a form that says no.
    """
    upload = request.files.get("cover")
    if upload is None or not upload.filename:
        return None, None
    content_type = (upload.mimetype or "").lower()
    if content_type not in cover_art.CONTENT_TYPES.values():
        return None, _("The cover must be a PNG, JPEG, WebP or GIF image.")
    try:
        data = upload.read()
    except OSError:
        return None, _("The uploaded cover could not be read.")
    if not data:
        return None, _("The uploaded cover is empty.")
    if len(data) > cover_art.MAX_ART_BYTES:
        return None, _("The uploaded cover is too large (limit is 10 MB).")
    return (data, content_type), None


def _selected_devices(known):
    """The devices a print card is written to: every ticked box that is known.

    No primary device here, unlike the assignment form: there is no card yet, so
    all of them are equal targets — the card exists once, and each ticked ESPuino
    gets its own record of it (own play position, own cache).
    """
    return [esp_id for esp_id in request.form.getlist("esp_ids") if esp_id in known]


def _card_name(payload, shop_card_id):
    """The card's name: the admin's, or one derived from the media.

    The service requires a non-empty name, while a hub card's name is optional
    ("display/logs only" here), so an unnamed card is named after the folder or the
    file it plays — and only then after its number. The same name is then used on
    this side too: one card, one name, rather than the same card appearing unnamed
    in the hub and named at the shop. Not translated: this is a card's name, not hub
    UI text.
    """
    name, _kind, _play_mode, _stream_url, files, play_root = payload
    if name:
        return name
    if play_root:
        return os.path.basename(play_root.rstrip("/")) or f"Card {shop_card_id}"
    if files:
        stem = os.path.splitext(os.path.basename(files[0]["path"]))[0]
        if stem:
            return stem
    return f"Card {shop_card_id}"


@app.route("/print/<token>/card/new", methods=["GET", "POST"])
def new_print_card(token):
    """One card of this project: an ordinary assignment plus the image it is printed
    with, written to both systems in one submit.

    The cover is the reason the two happen together: the shop refuses a card without
    a front image, and the only thing that says what the cover *is* is the media the
    card will play. So the assignment is read and validated first, then the cover is
    resolved from it, then the shop card is created at the project's next card id —
    and only then does a hub record exist, holding the print order it belongs to.
    """
    project = store.get_print_project(token)
    if project is None:
        abort(404)

    devices = store.list_devices()
    if request.method == "POST":
        payload, error, warning = _read_assignment(request.form)
        upload, upload_error = _uploaded_cover()
        if warning:
            flash(warning, "error")
        if error or upload_error:
            flash(error or upload_error, "error")
            return _print_card_form(token, project, devices, request.form)

        targets = _selected_devices(devices)
        if not targets:
            # Every failure below re-renders the form with what was submitted
            # instead of redirecting: a folder-at-a-time selection is a lot of work
            # to lose to a missing cover, and there is no stored record to prefill
            # from, the way there is when editing an existing card.
            flash(_("Please tick the ESPuinos this card should work on."), "error")
            return _print_card_form(token, project, devices, request.form)

        # An uploaded cover wins; otherwise it comes from the media (a sidecar image
        # beside it, or the artwork inside a track).
        cover = cover_art.resolve(MEDIA_DIR, [f["path"] for f in payload[4]], upload)
        if cover is None:
            flash(
                _(
                    "No cover found for this card — neither beside the media nor inside "
                    "it. Upload an image to print it with."
                ),
                "error",
            )
            return _print_card_form(token, project, devices, request.form)

        shop_card_id = store.allocate_shop_card_id(token)
        name = _card_name(payload, shop_card_id)
        payload = (name,) + payload[1:]
        try:
            # The track list rides along as the card's back text, and no back side
            # with it: a back spends a ribbon panel, so which one the card gets
            # stays the customer's choice at the shop — the list is simply there
            # when he picks a markdown back.
            printer_client.put_card(
                project["prefix"],
                shop_card_id,
                name,
                cover,
                back_text=print_back.markdown_for(payload),
            )
        except printer_client.PrinterError as exc:
            # Nothing was written on this side either, so the card can be submitted
            # again: the shop id was consumed, but the ids are the hub's to pick and
            # the retry lands on the next one — a gap in them is already possible
            # (a card deleted at the shop).
            flash(
                _("The card was not created at the print shop: %(error)s", error=str(exc)),
                "error",
            )
            return _print_card_form(token, project, devices, request.form)

        # The hub's record of the same card. It has no UID yet — it is identified by
        # a hub token until the print process reports one — and it carries the print
        # order, which is what `promote_print_card` finds it by afterwards.
        identity = store_lib.print_card_identity()
        print_ref = {"token": token, "prefix": project["prefix"], "id": shop_card_id}
        for esp_id in targets:
            store.save_assignment(esp_id, identity, *payload, print_ref=print_ref)

        flash(
            _("Card %(num)s created: it is at the print shop, and ready here.", num=shop_card_id),
            "success",
        )
        return redirect(url_for("print_project", token=token))

    return _print_card_form(token, project, devices)


def _print_card_form(token, project, devices, form=None):
    """Renders the print-card form, prefilled from `form` when a submit failed."""
    form = form or {}
    try:
        submitted = json.loads(form.get("selected_paths_json", "[]"))
    except ValueError:
        submitted = []
    try:
        play_mode = int(form.get("play_mode", ""))
    except (TypeError, ValueError):
        play_mode = None
    return render_template(
        "print_card_form.html",
        token=token,
        project=project,
        devices=devices,
        play_modes=manifest_lib.FILE_PLAY_MODES,
        single_file_play_modes=list(manifest_lib.SINGLE_FILE_PLAY_MODES),
        recursive_play_modes=list(manifest_lib.RECURSIVE_PLAY_MODES),
        single_folder_play_modes=list(manifest_lib.SINGLE_FOLDER_PLAY_MODES),
        gather_recursive_play_modes=list(manifest_lib.GATHER_RECURSIVE_PLAY_MODES),
        initial_name=form.get("name", ""),
        initial_kind=form.get("kind", "files"),
        initial_stream_url=form.get("stream_url", ""),
        selected_play_mode=play_mode,
        selected_play_root=form.get("play_root", ""),
        initial_selection=submitted if isinstance(submitted, list) else [],
        # A failed submit re-renders this form rather than redirecting, so what was
        # ticked has to come back with it.
        selected_esp_ids=form.getlist("esp_ids") if hasattr(form, "getlist") else [],
    )


@app.route("/print/<token>/card/cover", methods=["POST"])
def print_card_cover(token):
    """The cover a selection would be printed with, for the form's own preview.

    POST, not GET: a folder-at-a-time selection is hundreds of paths and does not
    belong in a URL. Resolved through the same `cover_art.resolve` the create uses,
    so the preview cannot promise a card that creating it would then refuse.
    """
    if store.get_print_project(token) is None:
        abort(404)
    try:
        paths = json.loads(request.form.get("selected_paths_json", "[]"))
    except ValueError:
        paths = []
    if not isinstance(paths, list) or not paths:
        abort(404)
    cover = cover_art.resolve(MEDIA_DIR, [str(path) for path in paths])
    if cover is None:
        abort(404)
    data, content_type = cover
    return Response(data, mimetype=content_type)


# --------------------------------------------------------------------------
# Web UI: media (per-card storage usage)
# --------------------------------------------------------------------------
@app.route("/media")
def media_overview():
    cards_by_key = store.list_cards()
    devices_by_id = store.list_devices()
    rows = [
        (
            store_lib.split_card_key(key, c),
            c,
            sum(f["size"] for f in c.get("files", [])),
            devices_by_id.get(c["esp_id"], {}).get("alias") or c["esp_id"],
        )
        for key, c in cards_by_key.items()
        if c["kind"] == "files" and c.get("files")
    ]
    # row[0] is the identity, row[1] the record, row[2] the total size.
    rows.sort(key=lambda row: row[2], reverse=True)
    return render_template("media.html", rows=rows)


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

        flash(_("Settings saved."), "success")
        return redirect(url_for("settings"))
    return render_template("settings.html", settings=store.get_settings())


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
    # A card the hub ordered printed has no UID yet and is keyed by a hub token
    # instead, which no device ever sends. Answering such a request would hand out
    # a manifest whose cardId is that token, and registering it would invent a
    # device/card pair nothing can ever tap.
    if not manifest_lib.is_valid_card_id(card_id):
        return jsonify(error="unknown_card", status="pending"), 404

    ts = store_lib.now_iso()
    store.touch_device(esp_id, request.remote_addr, card_id, ts)

    card = store.get_card(esp_id, card_id)
    if card is None:
        store.register_pending(esp_id, card_id, ts)
        return jsonify(error="unknown_card", status="pending"), 404

    store.touch_card_seen(esp_id, card_id, ts)
    if card["status"] != "assigned":
        return jsonify(error="not_assigned", status="pending"), 404

    files_base_url = url_for("serve_media", filename="", _external=True)
    return jsonify(manifest_lib.build_manifest(card_id, card, files_base_url))


@app.route("/media/<path:filename>")
def serve_media(filename):
    return send_from_directory(MEDIA_DIR, filename)


if __name__ == "__main__":
    # Development only; the container runs Gunicorn instead (see Dockerfile).
    app.run(host="0.0.0.0", port=8080, debug=True)
