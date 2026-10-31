"""Cover art for a card that is being printed.

The card-printing service wants a front image, and the hub has two sources for it:
the media the card plays, and an upload. The media path costs nothing when the
library already carries artwork, and it is the reason this module exists at all —
everything else about a print card is an ordinary assignment.

Lookup order, per the card's own media:

1. a *sidecar* image beside the selected files (``cover.jpg``, ``folder.png``,
   ``album.jpeg``, ``front.webp``, case-insensitive). One directory listing, no
   parsing, and it is what the library author put there on purpose;
2. the artwork *inside* the first selected file that carries any: ID3v2 ``APIC``
   for mp3, ``covr`` for m4a, ``PICTURE`` for flac, ``METADATA_BLOCK_PICTURE`` for
   ogg/opus.

An upload always wins over both, because it is what the admin explicitly chose.

Everything mutagen-shaped stays in this module: it is the hub's only dependency
beyond Flask, and nothing else needs to know which container format holds a
picture. A file that cannot be read, or that carries no picture, is simply not a
source — never an error, since a cover is optional until a card is ordered.
"""

import base64
import os
import posixpath

import mutagen
from mutagen.flac import Picture

import media_library

# Sidecar base names, in the order a library is likely to have them. A cover
# library file is named after nothing in particular, so all four are checked.
SIDECAR_STEMS = ("cover", "folder", "album", "front")
SIDECAR_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp", ".gif")

# Content types the shop accepts as an image, by suffix. Anything else is stored
# there with a `.bin` extension and never renders, so an unknown type is no source.
CONTENT_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
}

# Embedded artwork is copied whole into a card, so it is capped: a 40 MB scan in a
# tag is not a cover, and the shop's own fetch cap is the same order of size.
MAX_ART_BYTES = 10 * 1024 * 1024

# How many files of a selection are opened looking for embedded artwork.
MAX_TAG_PROBE = 10


def resolve(media_root, paths, upload=None):
    """The cover for a card playing `paths`: `(bytes, content_type)`, or None.

    `upload` is an already-read `(bytes, content_type)` pair and wins. `paths` are
    media-library-relative paths; the order decides only which of several equally
    valid covers is used.
    """
    if upload:
        return upload
    # One sidecar lookup per *directory*, not per file: a folder-at-a-time
    # selection is hundreds of files sharing one cover, and the first file of each
    # directory is the one whose own name is worth trying.
    seen_dirs = set()
    for relpath in paths:
        directory = posixpath.dirname(relpath)
        if directory in seen_dirs:
            continue
        seen_dirs.add(directory)
        found = _sidecar(media_root, relpath)
        if found:
            return found
    # Tag art has to be parsed out of the file, so only the first few are tried:
    # the artwork of a set lies in one of the first files, and a folder selection
    # of several hundred must not be opened one by one.
    for relpath in paths[:MAX_TAG_PROBE]:
        found = _embedded(media_root, relpath)
        if found:
            return found
    return None


def _read(path, suffix):
    """Reads an image file into `(bytes, content_type)`, or None."""
    content_type = CONTENT_TYPES.get(suffix.lower())
    if content_type is None:
        return None
    try:
        if os.path.getsize(path) > MAX_ART_BYTES:
            return None
        with open(path, "rb") as handle:
            data = handle.read()
    except OSError:
        return None
    return (data, content_type) if data else None


def _sidecar(media_root, relpath):
    """An image sitting beside this media file, or None."""
    absolute = media_library.resolve(media_root, relpath)
    if absolute is None:
        return None
    directory = os.path.dirname(absolute)
    stem = os.path.splitext(os.path.basename(absolute))[0].casefold()
    try:
        entries = {entry.name.casefold(): entry.name for entry in os.scandir(directory)}
    except OSError:
        return None
    # The file's own name first (an audiobook chapter's own artwork), then the
    # usual album-level names. `relpath` may name a folder rather than a file —
    # then only the album-level names apply, which is what the stem test below
    # takes care of.
    stems = [stem, *SIDECAR_STEMS] if os.path.isfile(absolute) else list(SIDECAR_STEMS)
    for candidate_stem in stems:
        for suffix in SIDECAR_SUFFIXES:
            name = entries.get(candidate_stem + suffix)
            if name:
                found = _read(os.path.join(directory, name), suffix)
                if found:
                    return found
    return None


def _embedded(media_root, relpath):
    """The artwork inside this media file, or None."""
    absolute = media_library.resolve(media_root, relpath)
    if absolute is None or not os.path.isfile(absolute):
        return None
    try:
        handle = mutagen.File(absolute, easy=False)
    except Exception:  # mutagen raises whatever the container's parser raises
        return None
    if handle is None:
        return None

    tags = getattr(handle, "tags", None)
    if tags is None:
        return None

    # mp3: one APIC frame per attached picture; take the first.
    getall = getattr(tags, "getall", None)
    if callable(getall):
        pictures = getall("APIC")
        if pictures:
            return _capped(pictures[0].data, pictures[0].mime)

    # flac: `PICTURE` blocks, exposed as Picture objects.
    pictures = getattr(handle, "pictures", None)
    if pictures:
        return _capped(pictures[0].data, pictures[0].mime)

    # m4a: a list of MP4Cover, whose imageformat says the type.
    covers = tags.get("covr") if hasattr(tags, "get") else None
    if covers:
        cover = covers[0]
        return _capped(bytes(cover), _mime_for_format(getattr(cover, "imageformat", None)))

    # ogg/opus: the picture as a base64 METADATA_BLOCK_PICTURE — a flac Picture.
    block = tags.get("metadata_block_picture") if hasattr(tags, "get") else None
    if block:
        try:
            picture = Picture(base64.b64decode(block[0]))
        except Exception:
            return None
        return _capped(picture.data, picture.mime)

    return None


def _mime_for_format(image_format):
    """The content type for an MP4Cover's image format constant."""
    cover_formats = getattr(mutagen, "MP4Cover", None)
    if cover_formats is None:  # pragma: no cover - mutagen always ships it
        return None
    if image_format == getattr(cover_formats, "FORMAT_PNG", None):
        return "image/png"
    return "image/jpeg"


def _capped(data, mime):
    """`(bytes, content_type)` for embedded art, or None when it is unusable."""
    if not data or len(data) > MAX_ART_BYTES:
        return None
    # mutagen reports the frame's own mime, which may be jpeg/png only in
    # practice; anything else would be stored as `.bin` at the shop and never
    # render, so it is treated as no cover.
    normalized = (mime or "").lower()
    if normalized in ("image/jpg", "image/pjpeg"):
        normalized = "image/jpeg"
    if normalized not in CONTENT_TYPES.values():
        return None
    return (data, normalized)
