"""Config-directory location and credential-file writing: one owner.

This module exists so that the *only* things which know where SchemaPilot's config lives, and
how a file holding a secret must be written, are here. ``USER_CONFIG_DIR`` was previously
defined independently in three modules, and ``llm.py`` reached into ``db.py`` for
``ensure_config_dir``/``write_credential_json`` -- so the LLM layer imported the database layer
purely for filesystem plumbing. Both files store secrets (DB passwords in ``connections.json``,
API keys in ``models.json``), so the permission and atomicity rules must not be able to drift
apart between them.
"""

import json
import logging
import os
import stat
import tempfile
from typing import Any

logger = logging.getLogger("schemapilot.paths")

# Store configuration files in the user's home configuration directory (standard global CLI practice)
USER_CONFIG_DIR = os.path.expanduser("~/.config/schemapilot")

# connections.json and models.json hold plaintext database passwords and LLM API keys, so the
# directory is owner-only and the files are written 0600 instead of inheriting the process umask
# (which typically yields world-readable 0644). OS keyring storage is future work.
CONFIG_DIR_MODE = 0o700
CREDENTIAL_FILE_MODE = 0o600


def ensure_config_dir(directory: str = None) -> str:
    """Creates the config directory 0700, tightening the mode if it already exists laxer."""
    # Resolved at call time, not snapshotted as a default argument, so that tests (and anything
    # else) can repoint USER_CONFIG_DIR on this module and have it actually take effect.
    if directory is None:
        directory = USER_CONFIG_DIR
    os.makedirs(directory, mode=CONFIG_DIR_MODE, exist_ok=True)
    try:
        if stat.S_IMODE(os.stat(directory).st_mode) != CONFIG_DIR_MODE:
            os.chmod(directory, CONFIG_DIR_MODE)
    except OSError as e:  # pragma: no cover - platform/permission dependent
        logger.debug(f"Could not tighten permissions on {directory}: {e}")
    return directory


def write_credential_json(path: str, payload: Any):
    """Writes JSON credentials atomically with 0600 permissions.

    Atomic because a half-written connections.json is unrecoverable: the temp file lives in the
    same directory (so os.replace never crosses a filesystem boundary) and only replaces the
    original once it is completely flushed, leaving the previous file intact if we die midway.
    """
    directory = os.path.dirname(path) or "."
    ensure_config_dir(directory)

    fd, tmp_path = tempfile.mkstemp(prefix=".{}.".format(os.path.basename(path)), dir=directory)
    try:
        os.fchmod(fd, CREDENTIAL_FILE_MODE)
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f, indent=4)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        # Never leave the temp file behind; the original stays untouched.
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise
