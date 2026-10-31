"""Acceptance check for the MediaHub hub itself.

Boots the real Flask app against a scratch DATA_DIR/MEDIA_DIR through Flask's test
client (no server of our own, deterministic) and asserts:

  1. every pre-existing flow behaves as before — tap-to-register, add card, assign
     files, manifest (+version/force-refresh), duplicate, delete;
  2. a card that has no UID yet (one the print shop is making) can exist, be listed,
     be assigned, be refused by everything that assumes a UID, and be promoted to the
     real card once the print process reports the UID — including the merge with a
     record the device already registered by tapping it;
  3. a print project: created here *and* placed as an order at the print shop, with
     its status re-read on demand and a shop that is down degrading gracefully;
  4. a db.json from before print projects still works.

The print shop is a throwaway stub HTTP server (stdlib), so this pins the requests
the hub makes — method, path, and that it sends no credential — without needing the
real service. Run from anywhere:

    nix develop . -c python3 scripts/check_mediahub.py

Exits non-zero on the first failure; prints every check either way.
"""

import http.server
import io
import json
import os
import re
import shutil
import struct
import sys
import tempfile
import threading
import uuid
import zlib

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT = tempfile.mkdtemp(prefix="mediahub-check-")
os.environ["MEDIAHUB_DATA"] = os.path.join(ROOT, "data")
os.environ["MEDIAHUB_MEDIA"] = os.path.join(ROOT, "media")
os.makedirs(os.path.join(ROOT, "media", "Bobo"), exist_ok=True)
for name in ("01.mp3", "02.mp3"):
    with open(os.path.join(ROOT, "media", "Bobo", name), "wb") as f:
        f.write(b"\xff\xfb" + b"\x00" * 64)
with open(os.path.join(ROOT, "media", "top.mp3"), "wb") as f:
    f.write(b"\xff\xfb" + b"\x00" * 32)

# --------------------------------------------------------------- print shop stub
class ShopStub(http.server.BaseHTTPRequestHandler):
    """The two public endpoints the hub uses, plus a record of every call."""

    calls = []
    status = "draft"
    cards = []
    last_body = b""
    put_code = 201

    def _reply(self, code, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _target(self):
        path = self.path.split("?")[0]
        parts = [part for part in path.split("/") if part]
        return path, (parts[2] if len(parts) > 2 else "")

    def do_PUT(self):
        path, prefix = self._target()
        length = int(self.headers.get("content-length") or 0)
        ShopStub.last_body = self.rfile.read(length) if length else b""
        ShopStub.calls.append(("PUT", path, self.headers.get("authorization")))
        if ShopStub.put_code != 201:
            self._reply(ShopStub.put_code, {"error": "prefix is 'paid' and can no longer be modified"})
            return
        self._reply(201, self._order(prefix))

    def do_GET(self):
        path, prefix = self._target()
        ShopStub.calls.append(("GET", path, self.headers.get("authorization")))
        if path.endswith("/cards"):
            self._reply(200, {"prefix": prefix, "cards": ShopStub.cards})
        else:
            self._reply(200, self._order(prefix))

    def _order(self, prefix):
        return {
            "uuid": prefix,
            "status": ShopStub.status,
            "ribbon": "ymckoko",
            "card_count": len(ShopStub.cards),
        }

    def log_message(self, *args):
        pass  # keep the check's own output readable


shop = http.server.ThreadingHTTPServer(("127.0.0.1", 0), ShopStub)
SHOP_URL = f"http://127.0.0.1:{shop.server_port}"
threading.Thread(target=shop.serve_forever, daemon=True).start()
os.environ["MEDIAHUB_PRINTER_URL"] = SHOP_URL

sys.path.insert(0, REPO)
import app as mediahub  # noqa: E402  (env must be set first)
import store as store_lib  # noqa: E402

store = mediahub.store
client = mediahub.app.test_client()
# The hub renders in the browser's language (German by default), so pin English
# before asserting on any text — including the "to print" badge below.
client.get("/lang/en")

FAILED = []


def tiny_png(colour=b"\xd0\x30\x30"):
    """A real 2x2 PNG of one colour.

    Real because the *shop* decodes the cover it is handed — a handful of junk
    bytes would come back as a card whose render failed, not as a cover. Two
    colours, so a test can tell one image from another.
    """

    def chunk(kind, data):
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data))
        )

    raw = b"".join(b"\x00" + colour * 2 for _ in range(2))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


# A real MPEG-1 Layer III frame at 128 kbps / 44.1 kHz: 144 * 128000 / 44100 = 417
# bytes. mutagen refuses to sync to anything shorter, and a file it cannot sync to
# is not an mp3 as far as it is concerned — the tag would never be read.
AUDIO_FRAME = b"\xff\xfb\x90\x00" + b"\x00" * 413


def id3_mp3(path, image, mime="image/png"):
    """An mp3 with one APIC picture in it, built here and now.

    Hand-built so the check needs no binary fixture in the repository, and no ID3
    writer: an ID3v2.3 header, one APIC frame, and enough audio frames for the
    file to be recognised as an mp3.
    """
    frame = (
        b"APIC"
        + len(b"\x00" + mime.encode() + b"\x00" + b"\x03" + b"\x00" + image).to_bytes(4, "big")
        + b"\x00\x00"
        + b"\x00"
        + mime.encode()
        + b"\x00"
        + b"\x03"
        + b"\x00"
        + image
    )
    size = len(frame)
    header = b"ID3\x03\x00\x00" + bytes(
        [(size >> 21) & 0x7F, (size >> 14) & 0x7F, (size >> 7) & 0x7F, size & 0x7F]
    )
    with open(path, "wb") as handle:
        handle.write(header + frame + AUDIO_FRAME * 4)


def labels_json(html):
    """The media browser's label block, parsed.

    Its contract with the browser is a JSON object in a script tag, and a broken
    one breaks the page silently — no server-side test would notice otherwise.
    """
    match = re.search(rb'id="media-tree-labels"[^>]*>(.*?)</script>', html, re.S)
    if not match:
        return None
    try:
        return json.loads(match.group(1).decode("utf-8"))
    except ValueError:
        return None


def check(label, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + label + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        FAILED.append(label)


def cards_raw():
    with open(store.db_path, encoding="utf-8") as f:
        return json.load(f)["cards"]


def manifest(esp_id, card_id):
    return client.get(f"/{esp_id}/card/{card_id}/manifest.json")


def assign(esp_id, identity, name, play_mode=5, paths=("Bobo/01.mp3", "Bobo/02.mp3")):
    return client.post(
        f"/cards/{esp_id}/{identity}/assign",
        data={
            "name": name,
            "kind": "files",
            "play_mode": str(play_mode),
            "selected_paths_json": json.dumps(list(paths)),
            "play_root": "Bobo",
        },
        follow_redirects=False,
    )


print("\n== 1. existing flows ==")
r = client.get("/health")
check("health", r.status_code == 200 and r.get_json()["status"] == "ok")

UID_A = "111111111111"
PRINTED_UID = "555555555555"  # the UID the print process reports in section 10
r = manifest("esp-a", UID_A)
check("unknown tap registers pending + 404", r.status_code == 404 and r.get_json()["error"] == "unknown_card")
check("pending record exists", cards_raw().get(f"esp-a/{UID_A}", {}).get("status") == "pending")
check("pending record has no print ref", "print" not in cards_raw()[f"esp-a/{UID_A}"])

r = client.get("/cards")
check("card list renders the tapped id", r.status_code == 200 and UID_A.encode() in r.data)
check("no literal 'None' in the id column", b"<code>None</code>" not in r.data)

r = client.post("/cards/add", data={"esp_id": "esp-a", "card_id": UID_A}, follow_redirects=False)
check("add existing card redirects to the assignment", r.status_code == 302 and "/assign" in r.headers["Location"])
r = client.get(f"/cards/esp-a/{UID_A}/assign")
check("assignment form renders", r.status_code == 200)

r = assign("esp-a", UID_A, "Bobo")
check("assign files redirects", r.status_code == 302)
rec = cards_raw()[f"esp-a/{UID_A}"]
check("card is assigned with 2 files", rec["status"] == "assigned" and len(rec["files"]) == 2)
check("card_id is the UID", rec["card_id"] == UID_A)

r = manifest("esp-a", UID_A)
body = r.get_json()
check("manifest served", r.status_code == 200 and body["cardId"] == UID_A and len(body["files"]) == 2)
version_before = body["version"]

r = client.post(f"/cards/esp-a/{UID_A}/force-refresh", follow_redirects=False)
check("force refresh redirects", r.status_code == 302)
body2 = manifest("esp-a", UID_A).get_json()
check("force refresh changed the version", body2["version"] != version_before and body2["forceEpoch"] == 1)

r = manifest("esp-b", UID_A)  # a second device learns the card the same way
check("second device sees the same card as pending", r.status_code == 404)
r = client.post(
    f"/cards/esp-a/{UID_A}/duplicate", data={"target_esp_id": "esp-b"}, follow_redirects=False
)
check("duplicate redirects", r.status_code == 302)
check("duplicate assigned the content on esp-b", cards_raw()[f"esp-b/{UID_A}"]["status"] == "assigned")

print("\n== 2. a card with no UID yet (print order) ==")
TOKEN = "pending-1f3a9c2b"
PRINT_REF = {"token": "1f3a9c2b", "prefix": "9c0a-prefix-uuid", "id": 1, "ribbon": "ymckoko"}
ts = store_lib.now_iso()
for esp in ("esp-a", "esp-b"):
    store.register_print_card(esp, TOKEN, PRINT_REF, ts)
raw = cards_raw()
check("virtual record on both devices", f"esp-a/{TOKEN}" in raw and f"esp-b/{TOKEN}" in raw)
check("virtual record has no card_id", raw[f"esp-a/{TOKEN}"]["card_id"] is None)
check("virtual record carries the print ref", raw[f"esp-a/{TOKEN}"]["print"]["prefix"] == PRINT_REF["prefix"])

r = client.get("/cards")
check("card list renders the virtual card", r.status_code == 200 and b"to print" in r.data)
check("the virtual row links by identity",
      f'/cards/esp-a/{TOKEN}/assign'.encode() in r.data)
check("no manifest button on a virtual card",
      f'/cards/esp-a/{TOKEN}/manifest-preview'.encode() not in r.data)
check("identity is not shown in the id column", f"<code>{TOKEN}</code>".encode() not in r.data)

r = client.get(f"/cards/esp-a/{TOKEN}/assign")
check("assignment form renders for a virtual card", r.status_code == 200)
r = assign("esp-a", TOKEN, "Bobo (print)")
check("assign to a virtual card redirects", r.status_code == 302)
raw = cards_raw()
check("virtual card is assigned, still no card_id",
      raw[f"esp-a/{TOKEN}"]["status"] == "assigned" and raw[f"esp-a/{TOKEN}"]["card_id"] is None)

# A third device checks in, so it can be ticked as a target at all (only known
# devices are accepted), then the card is written to it in the same save.
manifest("esp-c", "999999999991")
r = client.post(
    f"/cards/esp-a/{TOKEN}/assign",
    data={
        "name": "Bobo (print)",
        "kind": "files",
        "play_mode": "5",
        "selected_paths_json": json.dumps(["Bobo/01.mp3", "Bobo/02.mp3"]),
        "play_root": "Bobo",
        "target_esp_ids": ["esp-c"],
    },
    follow_redirects=False,
)
check("multi-device assign redirects", r.status_code == 302)
rec_c = cards_raw().get("esp-c/" + TOKEN)
check("a ticked device with no record gets a virtual one", rec_c is not None and rec_c["card_id"] is None)
check("...carrying the print order with it", (rec_c or {}).get("print", {}).get("id") == PRINT_REF["id"])

before = cards_raw()
r = manifest("esp-a", TOKEN)
check("manifest route refuses a token identity", r.status_code == 404 and r.get_json()["error"] == "unknown_card")
check("and registers nothing for it", cards_raw() == before)
r = client.get(f"/cards/esp-a/{TOKEN}/manifest-preview")
check("manifest preview refuses a token identity", r.status_code == 404)

print("\n== 3. secure delete does not call a device with a token ==")
store.set_delete_mode("secure")
store.touch_device("esp-a", "127.0.0.1:9", UID_A, ts)  # nothing listens there
r = client.post(f"/cards/esp-b/{TOKEN}/delete", follow_redirects=False)
check("virtual card deleted", r.status_code == 302 and f"esp-b/{TOKEN}" not in cards_raw())
r = client.post(f"/cards/esp-a/{UID_A}/delete", follow_redirects=True)
check("a real card with an unreachable device is kept", f"esp-a/{UID_A}" in cards_raw())
store.set_delete_mode("lazy")

print("\n== 4. promotion ==")
UID_PRINTED = "123456789012"
moved = store.promote_print_card({"token": PRINT_REF["token"], "id": PRINT_REF["id"]}, UID_PRINTED, ts)
raw = cards_raw()
check("both virtual records were promoted", moved == 2, f"moved={moved}")
check("keys are the UID now", f"esp-a/{UID_PRINTED}" in raw and f"esp-c/{UID_PRINTED}" in raw)
check("virtual keys are gone", f"esp-a/{TOKEN}" not in raw and f"esp-c/{TOKEN}" not in raw)
rec = raw[f"esp-a/{UID_PRINTED}"]
check("card_id is the printed UID", rec["card_id"] == UID_PRINTED)
check("the assignment survived the promotion", rec["status"] == "assigned" and len(rec["files"]) == 2)
check("the print ref is kept as provenance", rec["print"]["promoted_at"] == ts)
r = manifest("esp-a", UID_PRINTED)
check("the printed card now serves the manifest", r.status_code == 200 and r.get_json()["cardId"] == UID_PRINTED)
check("promotion is idempotent",
      store.promote_print_card({"token": PRINT_REF["token"], "id": PRINT_REF["id"]}, UID_PRINTED, ts) == 0)

print("\n== 5. promotion merges with an early tap ==")
REF2 = {"token": "aa11bb22", "prefix": "9c0a-prefix-uuid", "id": 2, "ribbon": "ymckoko"}
TOKEN2 = "pending-aa11bb22"
UID2 = "222222222222"
store.register_print_card("esp-d", TOKEN2, REF2, ts)
assign("esp-d", TOKEN2, "Tapped first")
store.set_default_assign_scope("reporting")
manifest("esp-d", UID2)  # the device taps the card before the sync runs
check("early tap left a pending record at the UID", cards_raw()[f"esp-d/{UID2}"]["status"] == "pending")
store.promote_print_card({"token": REF2["token"], "id": REF2["id"]}, UID2, ts)
raw = cards_raw()
check("the tapped record absorbed the virtual one", f"esp-d/{TOKEN2}" not in raw and f"esp-d/{UID2}" in raw)
rec = raw[f"esp-d/{UID2}"]
check("it took the virtual assignment", rec["status"] == "assigned" and rec["name"] == "Tapped first")
check("and the print ref", rec["print"]["id"] == REF2["id"])

REF3 = {"token": "cc33dd44", "prefix": "9c0a-prefix-uuid", "id": 3, "ribbon": "ymckoko"}
TOKEN3 = "pending-cc33dd44"
UID3 = "333333333333"
store.register_print_card("esp-e", TOKEN3, REF3, ts)
store.save_assignment("esp-e", TOKEN3, "Virtual content", "files", 5, None, [{"path": "x.mp3", "size": 1, "sha256": "x"}], "Bobo", REF3)
manifest("esp-e", UID3)
store.save_assignment("esp-e", UID3, "Device content", "webradio", None, "http://stream", [], "")
store.promote_print_card({"token": REF3["token"], "id": REF3["id"]}, UID3, ts)
raw = cards_raw()
rec = raw[f"esp-e/{UID3}"]
check("an assigned record keeps its own content", rec["name"] == "Device content" and rec["kind"] == "webradio")
check("but still gains the print ref", rec["print"]["id"] == REF3["id"])

print("\n== 6. the print ref survives a plain re-assignment ==")
store.save_assignment("esp-e", UID3, "Renamed", "webradio", None, "http://stream", [], "")
rec = cards_raw()[f"esp-e/{UID3}"]
check("card_id untouched by an edit", rec["card_id"] == UID3)
check("print ref untouched by an edit", rec["print"]["id"] == REF3["id"])

print("\n== 7. print projects ==")
r = client.get("/print")
check("project list renders empty", r.status_code == 200 and b"No print project yet." in r.data)
r = client.get("/")
check("dashboard tile is there", b"Print projects" in r.data and b"0 projects" in r.data)

r = client.post("/print/new", data={"name": "   "}, follow_redirects=False)
check("a nameless project is refused", r.status_code == 302 and not store.list_print_projects())

r = client.post("/print/new", data={"name": "Kindergarten 2026"}, follow_redirects=False)
check("creating redirects to the project", r.status_code == 302 and "/print/" in r.headers["Location"])
token = r.headers["Location"].rstrip("/").rsplit("/", 1)[-1]
project = store.get_print_project(token)
check("the token is a hub token", len(token) == 8 and all(c in "0123456789abcdef" for c in token))
uuid.UUID(project["prefix"])
check("the order uuid is minted here (so creating it at the shop is idempotent)", True)
check("the shop's answer is stored", project["status"] == "draft" and project["status_synced_at"])
check("the shop card counter starts at 1", project["next_shop_card_id"] == 1)
check("the project is in the file", token in json.load(open(store.db_path, encoding="utf-8"))["print_projects"])

r = client.get("/print")
check("the project is listed", r.status_code == 200 and b"Kindergarten 2026" in r.data)
r = client.get(f"/print/{token}")
check("the project page renders", r.status_code == 200 and b"Kindergarten 2026" in r.data)
check("...with the order's status and a link to it",
      b"<code>draft</code>" in r.data and f"/p/{project['prefix']}".encode() in r.data)
check("...and that it has no card", b"No card in this project yet." in r.data)
check("an unknown project is a 404", client.get("/print/nosuchtok").status_code == 404)

r = client.post("/print/new", data={"name": "Second"}, follow_redirects=False)
second = r.headers["Location"].rstrip("/").rsplit("/", 1)[-1]
check("a second project gets its own token and order", second != token
      and store.get_print_project(second)["prefix"] != project["prefix"])

# a card of this order: registered by the hub, still without a UID
REF4 = {"token": token, "prefix": project["prefix"], "id": 1, "ribbon": None}
store.register_print_card("esp-a", "pending-1234abcd", REF4, ts)
r = client.get(f"/print/{token}")
check("the project page lists its card", b"#1" in r.data and b"to print" in r.data)
check("...with the device it is on", b"esp-a" in r.data)
check("...as well-formed rows", r.data.count(b"<tr") == r.data.count(b"</tr>"))
r = client.get("/print")
check("the project count follows", b"<td>1</td>" in r.data or b">1<" in r.data)

r = client.post(f"/print/{token}/delete", follow_redirects=True)
check("a project with cards is not deletable", store.get_print_project(token) is not None)
check("...and says why", b"still has 1 card" in r.data)

store.delete_card("esp-a", "pending-1234abcd")
r = client.post(f"/print/{token}/delete", follow_redirects=False)
check("an empty project deletes", r.status_code == 302 and store.get_print_project(token) is None)
check("and its page is gone", client.get(f"/print/{token}").status_code == 404)

print("\n== 8. the print shop (stub) ==")
check("a shop is configured", mediahub.printer_client.configured())

r = client.post("/print/new", data={"name": "Stub order"}, follow_redirects=False)
shop_token = r.headers["Location"].rstrip("/").rsplit("/", 1)[-1]
shop_project = store.get_print_project(shop_token)
check("creating a project placed the order: PUT, uuid in the path, no credential",
      ("PUT", f"/api/prefixes/{shop_project['prefix']}", None) in ShopStub.calls)
check("...and stored the status the shop answered",
      shop_project["status"] == "draft" and shop_project["status_synced_at"])

ShopStub.status = "paid"
r = client.post(f"/print/{shop_token}/sync", follow_redirects=True)
check("sync re-reads the order",
      ("GET", f"/api/prefixes/{shop_project['prefix']}", None) in ShopStub.calls)
check("...stores what the shop says", store.get_print_project(shop_token)["status"] == "paid")
check("...and shows it",
      b"<code>paid</code>" in r.data and b"Order at the print shop: paid" in r.data)

ShopStub.cards = [{"id": 1, "name": "Card 1", "serial_number": None, "back": "none"}]
cards = mediahub.printer_client.list_cards(shop_project["prefix"])
check("the order's cards are readable",
      ("GET", f"/api/prefixes/{shop_project['prefix']}/cards", None) in ShopStub.calls
      and cards[0]["id"] == 1 and cards[0]["serial_number"] is None)

# A dead port rather than a stopped server: the stub is needed again in a moment,
# and rebinding the same port would be a race. The hub has one setting for where
# the shop is, so this is exactly what an unreachable shop looks like to it.
live_url = mediahub.printer_client.BASE_URL
mediahub.printer_client.BASE_URL = "http://127.0.0.1:9"
r = client.post("/print/new", data={"name": "No shop"}, follow_redirects=True)
no_shop_token = [t for t, p in store.list_print_projects().items() if p["name"] == "No shop"][0]
check("a shop that is down does not lose the project",
      store.get_print_project(no_shop_token)["status"] is None)
check("...and says so", b"did not take the order" in r.data)
check("...leaving the order unlinked", b"not registered yet" in r.data)
before = store.get_print_project(shop_token)["status"]
r = client.post(f"/print/{shop_token}/sync", follow_redirects=True)
check("a sync against a dead shop degrades", b"could not be read" in r.data)
check("...without touching the stored status", store.get_print_project(shop_token)["status"] == before)
mediahub.printer_client.BASE_URL = live_url

print("\n== 9. a card at the print shop ==")
png = tiny_png()
uploaded_png = tiny_png(b"\x30\x60\xd0")
os.makedirs(os.path.join(ROOT, "media", "Printed"), exist_ok=True)
with open(os.path.join(ROOT, "media", "Printed", "cover.png"), "wb") as handle:
    handle.write(png)
for name in ("01.mp3", "02.mp3"):
    with open(os.path.join(ROOT, "media", "Printed", name), "wb") as handle:
        handle.write(b"\xff\xfb" + b"\x00" * 64)
# A second folder whose artwork is *inside* the track, with nothing beside it.
os.makedirs(os.path.join(ROOT, "media", "Tagged"), exist_ok=True)
id3_mp3(os.path.join(ROOT, "media", "Tagged", "solo.mp3"), png)

r = client.get(f"/print/{shop_token}/card/new")
check("the new-card form renders", r.status_code == 200 and b"cover-preview" in r.data)
check("...with exactly one media picker",
      r.data.count(b'id="media-tree"') == 1 and r.data.count(b'id="selected-paths-json"') == 1)
check("...whose browser labels are valid JSON", labels_json(r.data) is not None)
check("...and it asks for the cover", b'name="cover"' in r.data)
check("...with the preview script", b"js/print-card.js" in r.data)
check("...and a preview box the script can style per state", b'data-state="empty"' in r.data)

r = client.post(
    f"/print/{shop_token}/card/cover", data={"selected_paths_json": json.dumps(["Printed/01.mp3"])}
)
check("the preview finds a sidecar cover",
      r.status_code == 200 and r.data == png and r.mimetype == "image/png")
r = client.post(
    f"/print/{shop_token}/card/cover", data={"selected_paths_json": json.dumps(["Tagged/solo.mp3"])}
)
check("and the artwork inside a track", r.status_code == 200 and r.data == png)
r = client.post(
    f"/print/{shop_token}/card/cover", data={"selected_paths_json": json.dumps(["top.mp3"])}
)
check("a selection with no cover anywhere is a 404", r.status_code == 404)

calls_before = len(ShopStub.calls)
r = client.post(
    f"/print/{shop_token}/card/new",
    data={
        # No name: the card at the shop has to be named after something.
        "name": "",
        "kind": "files",
        "play_mode": "5",
        "selected_paths_json": json.dumps(["Printed/01.mp3", "Printed/02.mp3"]),
        "play_root": "Printed",
        "esp_ids": ["esp-a", "esp-b"],
    },
    follow_redirects=True,
)
puts = [call for call in ShopStub.calls[calls_before:] if call[0] == "PUT"]
check("the card was written to the shop once, at the project's next id",
      len(puts) == 1 and puts[0][1] == f"/api/prefixes/{shop_project['prefix']}/cards/1")
check("...with no credential", puts[0][2] is None)
body = ShopStub.last_body
check("...as multipart carrying a name derived from the media",
      b'name="name"\r\n\r\nPrinted\r\n' in body)
check("...and the cover as the image part",
      b'name="image"; filename="cover.png"' in body and b"image/png" in body and png in body)
check("...and no back side, which is the customer's choice at the shop", b'name="back"' not in body)
check("...and the track list as the card's back text",
      b'name="back_text"' in body and b"**All tracks of a folder (sorted)**\n\n- 01\n- 02" in body)

# The text itself, independent of the scratch library's two by-two files: the play
# mode as its own line and one bullet per track, named as a listener reads it —
# no folder prefix, no extension.
check(
    "the back text is the play mode and the tracks",
    mediahub.print_back.markdown_for(
        (
            "Bobo",
            "files",
            5,
            None,
            [{"path": "Bobo/01 - Intro.mp3"}, {"path": "Bobo/02 - Kapitel 1.mp3"}],
            "Bobo",
        )
    )
    == "**All tracks of a folder (sorted)**\n\n- 01 - Intro\n- 02 - Kapitel 1",
)
check(
    "a webradio card's back text is the stream",
    mediahub.print_back.markdown_for(
        ("Radio", "webradio", None, "http://radio.example/stream.mp3", [], "")
    )
    == "**Webradio**\n\n- http://radio.example/stream.mp3",
)

raw = cards_raw()
card_keys = [
    key
    for key, card in raw.items()
    if (card.get("print") or {}).get("id") == 1 and (card.get("print") or {}).get("token") == shop_token
]
identities = {store_lib.split_card_key(key, raw[key]) for key in card_keys}
check("the hub holds one card per ticked device, under one identity",
      len(card_keys) == 2 and len(identities) == 1 and list(identities)[0].startswith("pending-"))
record = raw[card_keys[0]]
check("...assigned, with no UID yet", record["status"] == "assigned" and record["card_id"] is None)
check("...under the same name the card has at the shop", record["name"] == "Printed")
check("...carrying the print order", record["print"]["prefix"] == shop_project["prefix"])
check("the project counts both records", store.count_cards_for_print_project(shop_token) == 2)
check("and its card counter moved on", store.get_print_project(shop_token)["next_shop_card_id"] == 2)
r = client.get(f"/print/{shop_token}")
check("the project page lists the card as to print", b"#1" in r.data and b"to print" in r.data)

r = client.post(
    f"/print/{shop_token}/card/new",
    data={
        "kind": "files",
        "play_mode": "5",
        "selected_paths_json": json.dumps(["top.mp3"]),
        "esp_ids": ["esp-a"],
    },
    follow_redirects=False,
)
check("a card with no cover anywhere is refused", r.status_code == 200 and b"No cover found" in r.data)
check("...and the form comes back with the selection, not an empty one", b"top.mp3" in r.data)
check("...and nothing was written", store.get_print_project(shop_token)["next_shop_card_id"] == 2)

r = client.post(
    f"/print/{shop_token}/card/new",
    data={
        "kind": "files",
        "play_mode": "5",
        "selected_paths_json": json.dumps(["Printed/01.mp3"]),
        "play_root": "Printed",
    },
    follow_redirects=False,
)
check("a card for nobody is refused", r.status_code == 200 and b"tick the ESPuinos" in r.data)
check("...and nothing was written", store.get_print_project(shop_token)["next_shop_card_id"] == 2)

# An upload of one's own wins over whatever the media has.
calls_before = len(ShopStub.calls)
r = client.post(
    f"/print/{shop_token}/card/new",
    data={
        "kind": "files",
        "play_mode": "5",
        "selected_paths_json": json.dumps(["Printed/01.mp3", "Printed/02.mp3"]),
        "play_root": "Printed",
        "esp_ids": ["esp-a"],
        "cover": (io.BytesIO(uploaded_png), "mine.png"),
    },
    content_type="multipart/form-data",
    follow_redirects=True,
)
puts = [call for call in ShopStub.calls[calls_before:] if call[0] == "PUT"]
check("an uploaded cover is used, not the one beside the media",
      len(puts) == 1 and uploaded_png in ShopStub.last_body and png not in ShopStub.last_body)

# A shop that refuses (a paid order, say) must leave the hub alone.
ShopStub.put_code = 409
counter = store.get_print_project(shop_token)["next_shop_card_id"]
calls_before = len(ShopStub.calls)
r = client.post(
    f"/print/{shop_token}/card/new",
    data={
        "kind": "files",
        "play_mode": "5",
        "selected_paths_json": json.dumps(["Printed/01.mp3"]),
        "play_root": "Printed",
        "esp_ids": ["esp-a"],
    },
    follow_redirects=False,
)
check("a shop that refuses is reported with its own message",
      r.status_code == 200 and b"no longer be modified" in r.data)
check("...and no card is recorded here", store.count_cards_for_print_project(shop_token) == 3)
check("...and the id it consumed is simply skipped, not reused",
      store.get_print_project(shop_token)["next_shop_card_id"] == counter + 1)
ShopStub.put_code = 201

# A webradio card has no media to take a cover from: the upload is the only source.
calls_before = len(ShopStub.calls)
r = client.post(
    f"/print/{shop_token}/card/new",
    data={
        "name": "Radio",
        "kind": "webradio",
        "stream_url": "http://radio.example/stream.mp3",
        "esp_ids": ["esp-a"],
        "cover": (io.BytesIO(png), "radio.png"),
    },
    content_type="multipart/form-data",
    follow_redirects=True,
)
puts = [call for call in ShopStub.calls[calls_before:] if call[0] == "PUT"]
check("a webradio card is created with an uploaded cover",
      len(puts) == 1 and b'name="name"' in ShopStub.last_body and b"Radio" in ShopStub.last_body)
check("...with the stream as its back text, and still no back side",
      b"- http://radio.example/stream.mp3" in ShopStub.last_body
      and b'name="back"' not in ShopStub.last_body)
check("...and the project refuses to be deleted while it holds cards",
      client.post(f"/print/{shop_token}/delete", follow_redirects=True).status_code == 200
      and store.get_print_project(shop_token) is not None)

print("\n== 10. the UID arrives ==")

# The card the hub created in section 9 is card 1 at the shop, and no UID is known
# for it anywhere yet.
raw = cards_raw()
virtual = {
    key: card
    for key, card in raw.items()
    if (card.get("print") or {}).get("id") == 1 and card["card_id"] is None
}
identities = {store_lib.split_card_key(key, card) for key, card in virtual.items()}
check("the hub holds the order's card under a virtual id for both devices",
      len(virtual) == 2 and len(identities) == 1 and list(identities)[0].startswith("pending-"))
check("...on the two devices the card was made for",
      sorted(key.split("/")[0] for key in virtual) == ["esp-a", "esp-b"])

# One of those devices already has the printed card in hand and taps it before
# anybody has synced.
manifest("esp-b", PRINTED_UID)
check("a tap on the printed card registers itself at the UID",
      cards_raw()[f"esp-b/{PRINTED_UID}"]["status"] == "pending")

# The print process reports the UID it read off the chip.
ShopStub.cards[0]["serial_number"] = PRINTED_UID
r = client.post(f"/print/{shop_token}/sync", follow_redirects=True)
check("the sync says the cards are printed now",
      b"cards are printed and work here now" in r.data)
raw = cards_raw()
check("every record of that card is re-keyed onto the UID",
      sorted(key for key, card in raw.items() if card["card_id"] == PRINTED_UID)
      == [f"esp-a/{PRINTED_UID}", f"esp-b/{PRINTED_UID}"])
_left = [
    key
    for key, card in raw.items()
    if card["card_id"] is None and (card.get("print") or {}).get("id") == 1
]
check("nothing is left of that card under a virtual id", not _left, f"left={_left}")
check("the tapped record took the assignment over",
      raw[f"esp-b/{PRINTED_UID}"]["status"] == "assigned"
      and len(raw[f"esp-b/{PRINTED_UID}"]["files"]) == 2)
_other = raw[f"esp-a/{PRINTED_UID}"]
check("the other device's record kept the same assignment",
      _other["name"] == "Printed" and len(_other["files"]) == 2,
      f"name={_other['name']!r} files={len(_other['files'])}")
check("and both stay traceable to the order they came from",
      all(raw[key]["print"]["token"] == shop_token and raw[key]["print"]["promoted_at"]
          for key in (f"esp-a/{PRINTED_UID}", f"esp-b/{PRINTED_UID}")))
r = manifest("esp-a", PRINTED_UID)
check("the printed card serves the device right away",
      r.status_code == 200 and r.get_json()["cardId"] == PRINTED_UID)
r = client.get(f"/print/{shop_token}")
check("the project page shows it as printed, with its UID",
      PRINTED_UID.encode() in r.data)
check("...and it is on the card list", PRINTED_UID.encode() in client.get("/cards").data)

# A serial number the hub would not key a card by is left where it is: nothing is
# gained by putting a value there that a device could never report.
ShopStub.cards.append({"id": 2, "name": "Second", "serial_number": "12345", "back": "none"})
r = client.post(f"/print/{shop_token}/sync", follow_redirects=True)
check("a serial number that is not a card id promotes nothing",
      b"works here now" not in r.data and b"Order at the print shop: paid" in r.data)
check("...and nothing was keyed by it", not [key for key in cards_raw() if key.endswith("12345")])

keys_before = sorted(cards_raw())
r = client.post(f"/print/{shop_token}/sync", follow_redirects=True)
check("a sync with nothing new to promote reports only the order",
      b"works here now" not in r.data and b"Order at the print shop: paid" in r.data)
check("...and changes nothing", sorted(cards_raw()) == keys_before)

print("\n== 11. an existing db.json without the new key ==")
old_install = os.path.join(ROOT, "old-install")
os.makedirs(old_install, exist_ok=True)
with open(os.path.join(old_install, "db.json"), "w", encoding="utf-8") as f:
    json.dump({"settings": {}, "devices": {}, "cards": {}}, f)
legacy = store_lib.Store(old_install)
check("reading projects from an old db is empty, not a crash", legacy.list_print_projects() == {})
check("and a project can still be created", legacy.create_print_project("Neu")["name"] == "Neu")
check("...landing in the file", list(legacy.list_print_projects().values())[0]["prefix"])

print("\n== 12. an unusable data directory says why ==")
blocker = os.path.join(ROOT, "not-a-directory")
with open(blocker, "w", encoding="utf-8") as f:
    f.write("x")
try:
    store_lib.Store(os.path.join(blocker, "data"))
    check("a data directory that cannot be created names the fix", False, "no error raised")
except RuntimeError as exc:
    check("a data directory that cannot be created names the fix",
          "cannot write to the data directory" in str(exc), str(exc))

shutil.rmtree(ROOT, ignore_errors=True)
print("\n" + ("ALL CHECKS PASSED" if not FAILED else f"{len(FAILED)} FAILED: {FAILED}"))
sys.exit(1 if FAILED else 0)
