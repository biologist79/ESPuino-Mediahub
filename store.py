"""Persistent storage for devices, cards and settings.

A single JSON file (``db.json``) under ``DATA_DIR`` is plenty for the
expected scale (a handful of devices, a few dozen cards) — a database
server would be pure overhead here. Writes are serialized by a
process-local lock *and* an ``flock`` on a sibling file, then applied
atomically (tmp file + rename), mirroring the download pattern used on the
ESPuino itself (concept §13). The cross-process lock matters because the
container runs several gunicorn workers: without it, a read-modify-write in
one of them could silently drop a change another made in the meantime.
A mutation that leaves the data unchanged writes nothing at all — the legacy
migration runs in every worker on every boot, and re-saving Settings posts
values that already stand there.

Cards are keyed by (esp_id, card_id), not card_id alone — the same
physical card can be enrolled on several ESPuinos (that's the point of
the feature: kids swap cards between devices), and each device gets its
own independent assignment. This also makes secure delete unambiguous:
an assignment already knows exactly which one ESPuino to call, no more
guessing from whichever device last happened to tap the card.

The second half of that key is a card's *identity*, and it is not always a card
id: a card the hub has ordered printed has no 12-digit UID yet, so it is keyed by
a hub-generated ``pending-<token>`` string instead, and its ``card_id`` stays empty
until the print process reports the UID (``register_print_card`` raises one,
``promote_print_card`` turns it into the real card once it is printed). Anything
that assumes a UID — the ESPuino-facing manifest route, the secure-delete callback
— has to refuse every other identity; ``manifest.is_valid_card_id()`` is that
definition.
"""

import fcntl
import json
import os
import threading
import uuid
from datetime import datetime, timezone

# The one definition of "this is a real card id" — the firmware's 12-digit UID
# form. A card's identity (see the module docstring) is not always one, and the
# difference decides whether it belongs in a record's `card_id`.
from manifest import is_valid_card_id

_lock = threading.Lock()

DEFAULT_RECURSION_DEPTH = 3

_DEFAULT_DB = {
    "settings": {
        "delete_mode": "lazy",  # "lazy" | "secure" — see concept §13.1
        "password_hash": None,  # None = hub web UI has no login requirement
        "recursion_depth": DEFAULT_RECURSION_DEPTH,  # subfolder levels "use folder" descends into for recursive play modes
        # Which devices the assignment form pre-selects: "reporting" = only
        # the one being edited, "all" = every known ESPuino. Only the
        # pre-selection, never what is actually written — that is whatever
        # boxes are ticked when saving.
        "default_assign_scope": "reporting",  # "reporting" | "all"
    },
    "devices": {},
    "cards": {},
    # Print projects: one order at the card-printing service, which the hub creates
    # the cards for. Keyed by the hub's own token — the same token its cards carry
    # — because the shop's prefix uuid is that order's only credential and must
    # never turn into a hub URL or a store key.
    "print_projects": {},
}


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _card_key(esp_id, identity):
    """The store key of one card: its device plus its *identity*.

    The identity is the card's 12-digit UID once a device has reported it, and a
    hub-generated ``pending-<token>`` while the card is still being printed (see
    the module docstring).
    """
    return f"{esp_id}/{identity}"


def print_card_identity():
    """A fresh identity for a card that has no UID yet (see the module docstring).

    Minted outside the store's lock: two admins submitting at the same moment get
    different identities by construction, and a collision would need the same four
    random bytes twice.
    """
    return f"pending-{os.urandom(4).hex()}"


def split_card_key(key, card):
    """The identity half of a store key, for a record read out of ``cards``.

    Derived from the key rather than stored on the record: two places holding the
    same fact could disagree, and the key is what the URL space and the
    ESPuino-facing routes actually use.
    """
    prefix = f"{card['esp_id']}/"
    return key[len(prefix):] if key.startswith(prefix) else key


class Store:
    def __init__(self, data_dir):
        self.data_dir = data_dir
        self.db_path = os.path.join(data_dir, "db.json")
        self.lock_path = self.db_path + ".lock"
        try:
            os.makedirs(data_dir, exist_ok=True)
            self._ensure_db()
        except OSError as exc:
            # The two setup mistakes that cost people the most time, and both look
            # the same from inside a worker: Docker creates a missing bind-mount
            # path as root while the service runs as another uid, and a *relative*
            # path is resolved against the working directory — which for the
            # packaged app is the read-only store path gunicorn chdir'd into.
            # Without this, every worker dies inside _ensure_db() with a bare
            # traceback and the container restart-loops, with nothing in the log
            # that names the cause. entrypoint.sh catches it before Python even
            # starts; this covers a run without the entrypoint, and a directory
            # that stops being writable later on.
            raise RuntimeError(
                f"cannot write to the data directory {data_dir!r} as uid {os.geteuid()} "
                f"({exc.strerror}) - it has to be an absolute path this user owns "
                f"(on the host: chown -R {os.geteuid()}:{os.getegid()} <that directory>)"
            ) from exc

    def _ensure_db(self):
        """Creates db.json on first start, or migrates an existing one.

        Under the same cross-process lock as every other write: the container
        boots several gunicorn workers at once and each one constructs a
        Store, so on a fresh data volume they would otherwise all take the
        "file is missing" branch, write the temp file and rename it — the
        first one to finish leaves the others renaming a file that is no
        longer there, and those workers die on boot with ENOENT.
        """
        created = False
        with _lock, open(self.lock_path, "a+b") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                if not os.path.exists(self.db_path):
                    self._write(_DEFAULT_DB)
                    created = True
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)
        if not created:
            # Takes the lock itself, so it must run once this one is released.
            self._migrate_legacy_cards()

    # -- low-level ---------------------------------------------------
    def _read(self):
        with open(self.db_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _read_raw(self):
        """The database plus the exact text it came from — `_mutate` compares
        against that text to decide whether anything actually changed."""
        with open(self.db_path, "r", encoding="utf-8") as f:
            text = f.read()
        return text, json.loads(text)

    def _serialize(self, data):
        return json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False)

    def _write_text(self, text):
        # Process-unique temp name, so two writers can never rename each
        # other's file out from under themselves (see _ensure_db).
        tmp_path = f"{self.db_path}.{os.getpid()}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp_path, self.db_path)

    def _write(self, data):
        self._write_text(self._serialize(data))

    def _mutate(self, fn):
        """Read, let fn mutate the data in place, write back atomically.

        The whole read-modify-write runs under both locks, so it is atomic
        against the other threads of this process *and* against the other
        gunicorn workers.
        """
        with _lock, open(self.lock_path, "a+b") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                before, data = self._read_raw()
                result = fn(data)
                # Mutations that change nothing are routine: every worker runs
                # the legacy migration on boot, and saving Settings posts values
                # that already stand there. Rewriting the whole database for
                # that is wasted I/O — and one more window to be interrupted in.
                after = self._serialize(data)
                if after != before:
                    self._write_text(after)
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)
            return result

    def _migrate_legacy_cards(self):
        """One-time upgrade from the pre-esp_id schema, where "cards" was
        keyed by card_id alone and carried a "last_seen_esp_id" field.
        Cards that never saw a device (no last_seen_esp_id) can't be
        migrated — there's nothing to attach them to — and are dropped.
        """

        def mutate(data):
            legacy = {
                key: card
                for key, card in data["cards"].items()
                if "esp_id" not in card
            }
            if not legacy:
                return 0
            for key, card in legacy.items():
                del data["cards"][key]
                esp_id = card.pop("last_seen_esp_id", None)
                if not esp_id:
                    continue
                card["esp_id"] = esp_id
                card["card_id"] = key
                data["cards"][_card_key(esp_id, key)] = card
            return len(legacy)

        self._mutate(mutate)

    # -- devices -------------------------------------------------------
    def list_devices(self):
        return self._read()["devices"]

    def touch_device(self, esp_id, ip, card_id, ts):
        def mutate(data):
            dev = data["devices"].setdefault(
                esp_id,
                {"first_seen": ts, "last_seen": ts, "ip": ip, "last_card_id": card_id, "alias": None},
            )
            dev["last_seen"] = ts
            dev["ip"] = ip
            dev["last_card_id"] = card_id
            return dev

        return self._mutate(mutate)

    def set_device_alias(self, esp_id, alias):
        def mutate(data):
            dev = data["devices"].get(esp_id)
            if dev is not None:
                dev["alias"] = alias or None
            return dev

        return self._mutate(mutate)

    def count_cards_for_device(self, esp_id):
        return sum(1 for c in self._read()["cards"].values() if c["esp_id"] == esp_id)

    def delete_device(self, esp_id):
        """Removes the device bookkeeping entry AND all of its card
        assignments (cascade) — the admin already confirmed the count
        client-side before this is called. Returns the number of card
        assignments removed."""

        def mutate(data):
            keys = [key for key, c in data["cards"].items() if c["esp_id"] == esp_id]
            for key in keys:
                del data["cards"][key]
            data["devices"].pop(esp_id, None)
            return len(keys)

        return self._mutate(mutate)

    # -- cards -----------------------------------------------------------
    def list_cards(self):
        return self._read()["cards"]

    def get_card(self, esp_id, card_id):
        return self._read()["cards"].get(_card_key(esp_id, card_id))

    def register_pending(self, esp_id, card_id, ts):
        """Registers a not-yet-known (esp_id, card_id) pair as 'pending'
        (concept §5.3)."""

        def mutate(data):
            key = _card_key(esp_id, card_id)
            if key in data["cards"]:
                card = data["cards"][key]
                card["last_seen"] = ts
                return card
            card = {
                "esp_id": esp_id,
                "card_id": card_id,
                "status": "pending",
                "name": "",
                "kind": "files",
                "play_mode": None,
                "stream_url": None,
                "files": [],
                "force_epoch": 0,
                "first_seen": ts,
                "last_seen": ts,
                "created_at": ts,
                "updated_at": ts,
            }
            data["cards"][key] = card
            return card

        return self._mutate(mutate)

    def touch_card_seen(self, esp_id, card_id, ts):
        def mutate(data):
            card = data["cards"].get(_card_key(esp_id, card_id))
            if card is not None:
                card["last_seen"] = ts
            return card

        return self._mutate(mutate)

    def save_assignment(
        self,
        esp_id,
        card_id,
        name,
        kind,
        play_mode,
        stream_url,
        files,
        play_root="",
        print_ref=None,
    ):
        """`card_id` is the card's *identity*: its 12-digit UID for a card a
        device has reported, or the hub token of a card that has none yet. The
        caller passes back whatever it was handed — a device-reported id, or the
        key the admin UI was working on.

        `print_ref` is the print order the card belongs to, when the hub ordered it
        printed; `promote_print_card` later finds the card by it. It is carried to
        every device the card is written to, and kept after promotion as
        provenance.

        `play_root` is the folder the admin based the selection on, if they
        used "use folder" rather than ticking files by hand.

        The ESPuino is handed a folder, not a file list, and without this it
        has to guess that folder back from the common prefix of the paths -
        which is lossy. Pick a folder holding a single subfolder and the guess
        lands on the subfolder, one level below what was chosen; "random
        subfolder" then looks underneath it and finds nothing. Empty when the
        files were picked individually, and the device falls back to guessing,
        which is right for a selection that never had one folder behind it.
        """

        def mutate(data):
            ts = now_iso()
            key = _card_key(esp_id, card_id)
            card = data["cards"].setdefault(
                key,
                {
                    "esp_id": esp_id,
                    # The identity (the key suffix) is the 12-digit UID of a card a
                    # device reported, and a hub token for one that has no UID yet.
                    # Only the former belongs in `card_id`.
                    "card_id": card_id if is_valid_card_id(card_id) else None,
                    "status": "pending",
                    "force_epoch": 0,
                    "first_seen": ts,
                    "last_seen": ts,
                },
            )
            if print_ref:
                card.setdefault("print", dict(print_ref))
            card["status"] = "assigned"
            card["name"] = name
            card["kind"] = kind
            card["play_mode"] = play_mode
            card["stream_url"] = stream_url
            card["files"] = files
            card["play_root"] = play_root or ""
            card["updated_at"] = ts
            return card

        return self._mutate(mutate)

    def bump_force_epoch(self, esp_id, card_id):
        def mutate(data):
            card = data["cards"].get(_card_key(esp_id, card_id))
            if card is not None:
                card["force_epoch"] = card.get("force_epoch", 0) + 1
                card["updated_at"] = now_iso()
            return card

        return self._mutate(mutate)

    def bump_force_epoch_all(self):
        def mutate(data):
            ts = now_iso()
            for card in data["cards"].values():
                card["force_epoch"] = card.get("force_epoch", 0) + 1
                card["updated_at"] = ts
            return len(data["cards"])

        return self._mutate(mutate)

    def delete_card(self, esp_id, card_id):
        def mutate(data):
            return data["cards"].pop(_card_key(esp_id, card_id), None)

        return self._mutate(mutate)

    # -- cards ordered from the print shop (no UID yet) ------------------
    def register_print_card(self, esp_id, identity, print_ref, ts):
        """Registers a card the hub is having printed, on one device.

        It has no UID yet, so it is keyed by `identity` — always a hub-generated
        `pending-<token>`, never a 12-digit number — and its `card_id` stays empty
        until the print process reports the UID. Call it once per device that
        should carry the card; the card is then 'pending' and assignable exactly
        like a tapped one.

        `print_ref` names the shop card it will become — {"token", "prefix", "id",
        "ribbon"}. The prefix uuid is the shop's own credential for that order, so
        it is stored data only: never a URL, never a store key.
        """

        def mutate(data):
            key = _card_key(esp_id, identity)
            card = data["cards"].get(key)
            if card is not None:
                card["last_seen"] = ts
                return card
            card = {
                "esp_id": esp_id,
                "card_id": None,
                "status": "pending",
                "name": "",
                "kind": "files",
                "play_mode": None,
                "stream_url": None,
                "files": [],
                "force_epoch": 0,
                "print": dict(print_ref),
                "first_seen": ts,
                "last_seen": ts,
                "created_at": ts,
                "updated_at": ts,
            }
            data["cards"][key] = card
            return card

        return self._mutate(mutate)

    def promote_print_card(self, print_ref, card_id, ts):
        """Turns the virtual cards of one printed shop card into the real card.

        Called once the print process reports the 12-digit UID `card_id` for the
        shop card `print_ref` names {"token", "id"}: every record carrying that
        reference is re-keyed from `esp/<pending-token>` to `esp/<card_id>` with
        its `card_id` filled in, so the assignment made before printing is live the
        moment the card is tapped. Returns how many records changed — 0 for a job
        whose cards are already promoted, which is what makes a retried sync
        harmless.

        A device may already carry a record under the UID, because the card was
        tapped before this ran and a tap registers a pending record of its own.
        That record wins: it is what the device itself reported and the device may
        already be playing it. The virtual card's assignment is taken over only
        while the device's own record has none, and the print reference stays on
        whichever record survives, so a finished order remains traceable.
        """

        def mutate(data):
            token = print_ref.get("token")
            shop_card_id = print_ref.get("id")
            if not token or shop_card_id is None:
                return 0
            cards = data["cards"]
            virtual = [
                key
                for key, card in cards.items()
                if card.get("card_id") is None
                and (card.get("print") or {}).get("token") == token
                and (card.get("print") or {}).get("id") == shop_card_id
            ]
            for key in virtual:
                card = cards[key]
                target_key = _card_key(card["esp_id"], card_id)
                if target_key != key:
                    existing = cards.get(target_key)
                    if existing is None:
                        del cards[key]
                        cards[target_key] = card
                    else:
                        if existing.get("status") != "assigned" and card.get("status") == "assigned":
                            for field in ("name", "kind", "play_mode", "stream_url", "files", "play_root", "status"):
                                existing[field] = card.get(field)
                        existing["print"] = card.get("print")
                        if card.get("first_seen"):
                            first_seen = existing.get("first_seen")
                            existing["first_seen"] = (
                                min(first_seen, card["first_seen"]) if first_seen else card["first_seen"]
                            )
                        del cards[key]
                card["card_id"] = card_id
                card.setdefault("print", dict(print_ref))["promoted_at"] = ts
                card["updated_at"] = ts
            return len(virtual)

        return self._mutate(mutate)

    # -- print projects --------------------------------------------------
    def list_print_projects(self):
        return self._read().get("print_projects", {})

    def get_print_project(self, token):
        return self._read().get("print_projects", {}).get(token)

    def create_print_project(self, name):
        """Creates a print project: one order at the card-printing service.

        `name` is the admin's own label — an order has no name at the shop. Returns
        the record with its `token`, which is what everything outside the store
        addresses the project by.

        The shop's prefix uuid is minted here rather than read back from the shop,
        so that creating the order there is idempotent (`PUT /api/prefixes/<uuid>`
        answers "already exists" instead of making a second order on a retry). Until
        the shop has been asked about it, `status` is null and the project is not
        known to be an order yet.
        """

        def mutate(data):
            projects = data.setdefault("print_projects", {})
            token = os.urandom(4).hex()
            while token in projects:
                token = os.urandom(4).hex()
            project = {
                "name": name,
                "prefix": str(uuid.uuid4()),
                "status": None,  # the shop's own status, once synced
                "status_synced_at": None,
                # The shop card id this order's next card gets. The hub picks it so
                # that creating a card is idempotent too: it PUTs at a known id, and
                # a retry lands on the same card instead of a second one.
                "next_shop_card_id": 1,
                "created_at": now_iso(),
            }
            projects[token] = project
            return {"token": token, **project}

        return self._mutate(mutate)

    def delete_print_project(self, token):
        """Removes a print project. Never cascades: the project's cards are live
        cards at the shop — the customer may already have paid for them — so they
        are the admin's to delete one by one, in the UI that shows them.

        Returns the record, or None when it was not there.
        """

        def mutate(data):
            return data.setdefault("print_projects", {}).pop(token, None)

        return self._mutate(mutate)

    def allocate_shop_card_id(self, token):
        """The shop card id the next card of this project gets, counting it up.

        The hub picks these ids because the service's PUT is idempotent: a card
        whose write failed halfway can be written again at the same id, instead of
        leaving a second card behind for the same hub card. Returns None when the
        project is gone.
        """

        def mutate(data):
            project = data.setdefault("print_projects", {}).get(token)
            if project is None:
                return None
            shop_card_id = project.get("next_shop_card_id", 1)
            project["next_shop_card_id"] = shop_card_id + 1
            return shop_card_id

        return self._mutate(mutate)

    def set_print_project_status(self, token, status):
        """Remembers the shop's own status for this project's order, as of now.

        `status` is the service's vocabulary (draft, payment_pending, paid, queued,
        printing, printed, cancelled) and is passed through as-is: it is that
        service's state, not a hub state, so the hub never maps or interprets it.
        """

        def mutate(data):
            project = data.setdefault("print_projects", {}).get(token)
            if project is not None:
                project["status"] = status
                project["status_synced_at"] = now_iso()
            return project

        return self._mutate(mutate)

    def count_cards_for_print_project(self, token):
        """How many cards (per device) this project has raised."""

        return sum(
            1
            for card in self._read()["cards"].values()
            if (card.get("print") or {}).get("token") == token
        )

    # -- settings ----------------------------------------------------
    def get_settings(self):
        # Merge over defaults so a db.json predating a newly-added setting
        # (e.g. recursion_depth) still returns a usable value instead of
        # requiring an explicit migration step for every new setting.
        return {**_DEFAULT_DB["settings"], **self._read()["settings"]}

    def set_delete_mode(self, mode):
        def mutate(data):
            data["settings"]["delete_mode"] = mode
            return mode

        return self._mutate(mutate)

    def set_default_assign_scope(self, scope):
        def mutate(data):
            data["settings"]["default_assign_scope"] = scope
            return scope

        return self._mutate(mutate)

    def set_recursion_depth(self, depth):
        def mutate(data):
            data["settings"]["recursion_depth"] = depth
            return depth

        return self._mutate(mutate)

    def set_password_hash(self, password_hash):
        """password_hash is None to disable the login requirement entirely."""

        def mutate(data):
            data["settings"]["password_hash"] = password_hash
            return password_hash

        return self._mutate(mutate)
