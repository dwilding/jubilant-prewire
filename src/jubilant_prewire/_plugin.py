"""pytest plugin hooks: install and restore the deploy interception."""

from __future__ import annotations

import logging

logger = logging.getLogger("prewire")


def pytest_configure(config):
    """Install the deploy interception."""
    # Make the prewire logger emit at INFO by default so the experimentation
    # logging is visible without any pytest configuration. pytest's root
    # logger defaults to WARNING.
    if logging.getLogger("prewire").level == logging.NOTSET:
        logging.getLogger("prewire").setLevel(logging.INFO)
    # Also install a handler that prints to stdout, so prewire logs appear
    # even when pytest's log capture is disabled or misconfigured.
    if not logging.getLogger("prewire").handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        logging.getLogger("prewire").addHandler(handler)
    logger.info("jubilant-prewire plugin configured (installing deploy interception)")
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


def pytest_sessionfinish(session, exitstatus):
    """Restore the original deploy and print the summary."""
    logger.info(
        "session finished (exitstatus=%s); restoring deploy and printing summary", exitstatus
    )
    try:
        from . import _intercept
    except ImportError:
        return
    _intercept.uninstall()
    _intercept._print_summary()
