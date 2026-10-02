"""pytest plugin hooks: install and restore the deploy interception."""

from __future__ import annotations


def pytest_configure(config):
    """Install the deploy interception."""
    try:
        from . import _intercept
    except ImportError as exc:
        print(f"prewire: unable to load interception module: {exc}")
        return
    try:
        _intercept.install()
    except ImportError:
        # jubilant is not installed; skip silently with a warning.
        print("prewire: jubilant not installed; skipping image pre-pulls")


def pytest_sessionfinish(session, exitstatus):
    """Restore the original deploy and print the summary."""
    try:
        from . import _intercept
    except ImportError:
        return
    _intercept.uninstall()
    _intercept._print_summary()
