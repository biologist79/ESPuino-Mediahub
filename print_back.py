"""The markdown a printed card's back is pre-filled with at the print shop.

A card the hub has printed carries a track list as its `back_text`: what the card
plays — its play mode, or `Webradio` — and what is on it, one bullet per track.
The hub sends it *without* a back side, because a back side spends a ribbon panel
and which one a card gets is the customer's choice on the shop's page. The shop
stores a `back_text` whatever its `back` is and uses it only once the back is
markdown, so the list is simply there the moment the customer picks that, to keep,
shorten or throw away.

Not a manifest concern: nothing here reaches an ESPuino, and none of it is hashed
into a card's `version`.
"""

import posixpath

from flask_babel import gettext as _

import manifest as manifest_lib


def markdown_for(payload):
    """The back text for the card `payload` describes.

    `payload` is the assignment tuple the card forms carry (see
    `app._read_assignment`): name, kind, play_mode, stream_url, files, play_root.
    The name is left out — the card has it as its own at the shop — and so is the
    chosen folder, which is device-side detail no listener needs on a card.
    """
    _name, kind, play_mode, stream_url, files, _play_root = payload
    if kind == "webradio":
        heading, entries = _("Webradio"), [stream_url]
    else:
        heading = _(_play_mode_label(play_mode))
        entries = [_track_title(info["path"]) for info in files]
    return "\n".join([f"**{heading}**", ""] + [f"- {entry}" for entry in entries])


def _play_mode_label(play_mode):
    """One play mode's label, read off `manifest` rather than restated here."""
    for value, label in manifest_lib.FILE_PLAY_MODES:
        if value == play_mode:
            return label
    return f"Play mode {play_mode}"


def _track_title(path):
    """A track as it reads on a card: the file's own name, without its extension.

    Not the path a device is handed: a card has no room for a folder prefix that
    repeats on every line, and a listener reads a title.
    """
    name = posixpath.basename(path)
    return posixpath.splitext(name)[0] or name
