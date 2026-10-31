"""Client for the card-printing service an order is placed at.

The hub is a *customer* of that service, not an operator: it creates one order per
print project, adds cards to it while it is still a draft, and reads it back to show
its state here. It holds no credential for it — an order's uuid is the customer's own
credential, which is why the hub mints it (the service's PUT is idempotent, so the
same uuid twice is one order) and why it never appears in a hub URL.

Two settings in one: `BASE_URL` is both where these calls go and where a customer's
browser opens the order, because there is one service. A deployment that needs them
to differ can split them later.

Every call is short-timeout, and an unconfigured or unreachable service is an
ordinary outcome the caller reports — never a traceback, and never anything on the
ESPuino-facing side of the hub.
"""

import json
import os
import urllib.error
import urllib.parse
import urllib.request

# Trailing slash trimmed so paths can be appended directly. Empty = the integration
# is off, and every call raises PrinterUnavailable.
BASE_URL = os.environ.get("MEDIAHUB_PRINTER_URL", "").rstrip("/") or "https://kartenschmiede.home.tilman.baumann.name"

# Short on purpose: these calls happen while an admin waits, and the hub runs two
# gunicorn sync workers that the ESPuino-facing API also needs (see Dockerfile).
REQUEST_TIMEOUT = 3


class PrinterError(Exception):
    """Anything that kept a call from doing what was asked."""


class PrinterUnavailable(PrinterError):
    """No print shop configured, or it did not answer within the timeout."""


class PrinterRejected(PrinterError):
    """The shop answered and refused. `message` is its own explanation."""

    def __init__(self, status, message):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.message = message


def configured():
    return bool(BASE_URL)


def _call(method, path, body=None, content_type=None):
    if not BASE_URL:
        raise PrinterUnavailable("no print shop is configured (set MEDIAHUB_PRINTER_URL)")
    data = None
    headers = {}
    if body is not None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
        headers["content-type"] = content_type or "application/json"
    request = urllib.request.Request(f"{BASE_URL}{path}", data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        # The service's refusals carry a JSON {"error": "..."} — that is the message
        # an admin wants to read, not the status code on its own.
        payload = exc.read().decode("utf-8", "replace")
        message = payload
        try:
            message = json.loads(payload).get("error") or payload
        except (ValueError, AttributeError):
            pass
        raise PrinterRejected(exc.code, message) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise PrinterUnavailable(str(exc)) from exc


def _prefix_path(prefix, suffix=""):
    return f"/api/prefixes/{urllib.parse.quote(str(prefix))}{suffix}"


def ensure_order(prefix):
    """Creates this project's order at the shop, or returns the one already there.

    Idempotent by construction: the uuid is minted on this side (see
    `Store.create_print_project`), so a retried create lands on the same order
    instead of placing a second one. No body is sent — the shop's own default ribbon
    applies, and which ribbon goes in the printer is the customer's choice on the
    shop's page.
    """
    _, payload = _call("PUT", _prefix_path(prefix))
    return json.loads(payload)


def put_card(prefix, shop_card_id, name, image, back=None, back_text=None):
    """Creates or replaces one card of the order, at a client-chosen id.

    PUT rather than POST: the hub picks the id (the project counts them up), so a
    create that fails halfway can be written again at the same id instead of
    leaving a second card behind.

    `image` is `(bytes, content_type)` and is required by the service — it is the
    card's front, i.e. its cover. No back side is ever sent: a back spends a ribbon
    panel, so which one a card gets is the customer's choice on the shop's page,
    not something the hub picks on his behalf.

    `back_text` rides along without a back side, and the service stores it: it is
    the markdown a `markdown` back is rendered from, so the customer finds the
    track list already in the card when he picks that back — to keep, shorten or
    throw away. Until he does, nothing renders it.
    """
    fields = {"name": name}
    if back:
        fields["back"] = back
    if back_text:
        fields["back_text"] = back_text
    filename = "cover" + {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/webp": ".webp",
        "image/gif": ".gif",
    }.get(image[1], ".img")
    body, content_type = _multipart(fields, {"image": (filename, image[0], image[1])})
    _, payload = _call(
        "PUT",
        _prefix_path(prefix, f"/cards/{shop_card_id}"),
        body=body,
        content_type=content_type,
    )
    return json.loads(payload)


def _multipart(fields, files):
    """A multipart/form-data body: `(bytes, content_type)`.

    Hand-rolled because the hub carries no HTTP client library: one text field per
    entry in `fields`, one file part per `(filename, bytes, content_type)` in
    `files`. The boundary is random per call, which is what the format asks for.
    """
    boundary = f"----mediahub{os.urandom(12).hex()}"
    lines = []
    for name, value in fields.items():
        lines.append(f"--{boundary}\r\n".encode())
        lines.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        lines.append(str(value).encode("utf-8"))
        lines.append(b"\r\n")
    for name, (filename, data, content_type) in files.items():
        lines.append(f"--{boundary}\r\n".encode())
        lines.append(
            f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'.encode()
        )
        lines.append(f"Content-Type: {content_type}\r\n\r\n".encode())
        lines.append(data)
        lines.append(b"\r\n")
    lines.append(f"--{boundary}--\r\n".encode())
    return b"".join(lines), f"multipart/form-data; boundary={boundary}"


def get_order(prefix):
    """The order as the shop has it: `status`, `ribbon`, `card_count`, …"""
    _, payload = _call("GET", _prefix_path(prefix))
    return json.loads(payload)


def list_cards(prefix):
    """Every card of the order, in the shop's own order.

    Each card carries the physical UID in `serial_number` once the print process has
    read it — that value is what turns a hub card that has no UID yet into the real
    one.
    """
    _, payload = _call("GET", _prefix_path(prefix, "/cards"))
    return json.loads(payload).get("cards", [])
