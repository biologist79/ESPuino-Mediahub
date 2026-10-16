#!/bin/sh
#
# Hands the data directory to the service user, then drops to it.
#
# Why this exists: /data is a bind mount, and Docker creates a missing host
# path for one *as root* - so a first start with no ./data on the host leaves
# the service, which runs as an ordinary uid, with a directory it cannot write.
# The container therefore starts as root, fixes the ownership and steps down
# with gosu. Setting `user:` in Compose still works: we are then not root,
# cannot chown anything, and only check that the directory is usable.
set -e

DATA_DIR="${MEDIAHUB_DATA:-/data}"
WANT_UID="${MEDIAHUB_UID:-33}"
WANT_GID="${MEDIAHUB_GID:-33}"

usage_error() {
	echo "ERROR: MediaHub cannot write to its data directory ($DATA_DIR, uid $1)." >&2
	echo "       The host directory behind it belongs to someone else, and this" >&2
	echo "       container may not change that - an NFS share with root_squash, a" >&2
	echo "       CIFS mount or a 'user:' override in your Compose file all do that." >&2
	echo "       Fix it on the host, e.g.:  sudo chown -R $WANT_UID:$WANT_GID ./data" >&2
	exit 1
}

if [ "$(id -u)" = "0" ]; then
	mkdir -p "$DATA_DIR"
	# Covers a db.json that an earlier start already created as root. A failure
	# is not fatal by itself - the check below decides, since the directory may
	# well be writable for the target user anyway.
	chown -R "$WANT_UID:$WANT_GID" "$DATA_DIR" 2>/dev/null || true
	gosu "$WANT_UID:$WANT_GID" test -w "$DATA_DIR" || usage_error "$WANT_UID"
	exec gosu "$WANT_UID:$WANT_GID" "$@"
fi

# Already running as an unprivileged user: nothing to hand over, just report
# clearly if it won't work.
mkdir -p "$DATA_DIR" 2>/dev/null || true
test -w "$DATA_DIR" || usage_error "$(id -u)"
exec "$@"
