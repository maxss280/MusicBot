import logging
import os
import sys

from discord import opus

log = logging.getLogger(__name__)


def load_opus_lib() -> None:
    """
    Take steps needed to load opus library through discord.py
    """
    if opus.is_loaded():
        log.info("Opus library already loaded")
        return

    log.info("Attempting to load opus library...")

    # Try default loading first
    try:
        opus._load_default()  # pylint: disable=protected-access
        if opus.is_loaded():
            lib_info = getattr(opus, "_lib", "UNKNOWN")
            log.info("Opus library loaded successfully: %s", lib_info)
            return
    except OSError as e:
        log.warning("Default opus load failed: %s", e)

    # Alpine Linux fix: ctypes.util.find_library doesn't work with musl libc
    # Try common library paths directly
    alpine_paths = [
        "libopus.so.0",  # Try loading by name (LD_LIBRARY_PATH)
        "/usr/lib/libopus.so.0",
        "/usr/lib/libopus.so",
        "/usr/local/lib/libopus.so.0",
        "/usr/local/lib/libopus.so",
    ]

    for lib_path in alpine_paths:
        try:
            log.info("Trying to load opus from: %s", lib_path)
            opus.load_opus(lib_path)
            if opus.is_loaded():
                log.info("Opus library loaded successfully from: %s", lib_path)
                return
        except OSError as e:
            log.debug("Failed to load opus from %s: %s", lib_path, e)
            continue

    raise RuntimeError("Could not load an opus lib.")
