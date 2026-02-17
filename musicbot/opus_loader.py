import logging

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
    try:
        opus._load_default()  # pylint: disable=protected-access
        lib_info = getattr(opus, '_lib', 'UNKNOWN')
        log.info("Opus library loaded successfully: %s", lib_info)
        return
    except OSError as e:
        log.error("Failed to load opus library: %s", e)
        # Try to find the library manually
        import ctypes.util
        lib_path = ctypes.util.find_library('opus')
        log.error("ctypes.util.find_library('opus') returned: %s", lib_path)
        pass

    raise RuntimeError("Could not load an opus lib.")
