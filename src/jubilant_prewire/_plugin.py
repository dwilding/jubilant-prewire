"""pytest plugin hooks: install and restore the deploy interception."""

from __future__ import annotations

import logging

logger = logging.getLogger("prewire")


def pytest_configure(config):
    """Install the deploy interception."""
    prewire = logging.getLogger("prewire")
    # Default the prewire logger to INFO so its progress is visible without
    # any pytest logging configuration.
    if prewire.level == logging.NOTSET:
        prewire.setLevel(logging.INFO)
    # Emit straight to stdout only when pytest's live logging is not already
    # showing the records; otherwise they propagate to pytest's handlers
    # (live output and failure capture) and a second handler would print
    # every line twice.
    if not _live_logging_enabled(config) and not prewire.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        prewire.addHandler(handler)
    logger.debug("jubilant-prewire plugin configured")
    try:
        from . import _intercept
    except ImportError as exc:
        logger.warning("unable to load interception module: %s", exc)
        return
    try:
        _intercept.install()
    except ImportError:
        # jubilant is not installed; skip silently with a warning.
        logger.warning("jubilant not installed; skipping image pre-pulls")


def _live_logging_enabled(config) -> bool:
    """Return True if pytest's live logging will already show prewire records."""
    try:
        if config.getini("log_cli"):
            return True
        if config.getoption("log_cli_level", None):
            return True
        if config.getini("log_cli_level"):
            return True
    except ValueError:
        # Unregistered ini names (very old pytest); assume live logging is off.
        pass
    return False


def pytest_sessionfinish(session, exitstatus):
    """Restore the original deploy and print the summary."""
    logger.debug("session finished (exitstatus=%s)", exitstatus)
    try:
        from . import _intercept
    except ImportError:
        return
    _intercept.uninstall()
    _intercept._print_summary()
