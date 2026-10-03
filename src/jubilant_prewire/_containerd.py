"""containerd discovery and image pulls via ``ctr``."""

from __future__ import annotations

import logging
import shutil
import subprocess
import time
from pathlib import Path

logger = logging.getLogger("prewire")

# Namespace that the Kubernetes CRI plugin uses for images pulled via kubelet.
NAMESPACE = "k8s.io"

# Candidate ctr binaries, in order of preference. The k8s snap's ctr is
# preferred because concierge-provisioned environments use the k8s snap.
_CTR_CANDIDATES = [
    "/snap/k8s/current/bin/ctr",
]

# Candidate containerd sockets, in order of preference. A socket that exists
# but does not respond is worse than no socket at all: ctr hangs until its
# context deadline instead of failing fast, so each candidate is probed with
# `ctr version` before use.
_SOCKET_CANDIDATES = [
    "/var/snap/k8s/common/run/containerd.sock",
    "/var/snap/k8s/current/run/containerd.sock",
    "/run/containerd/containerd.sock",
]

# Pull retry policy: up to 5 attempts, 15 seconds apart. A full pull of a
# large image took 198s for postgresql-k8s (314 MiB) on a dev VM, so retries
# must tolerate long-running attempts.
_PULL_ATTEMPTS = 5
_PULL_RETRY_DELAY = 15

# Timeout for the socket probe. A healthy containerd answers `ctr version`
# quickly; a dead socket burns the whole timeout.
_PROBE_TIMEOUT = 10

# Per-attempt cap on a single pull, as a backstop against a stalled pull
# (network hang rather than failure). Real pulls took up to 462s in CI, so
# 20 minutes never cuts off a legitimate slow pull — but without a cap, a
# stalled pull would block the deploy call forever, which is worse than
# the 180s juju.wait() timeout jubilant-prewire exists to avoid.
_PULL_TIMEOUT = 20 * 60


def find_ctr() -> str | None:
    """Find the ctr binary: k8s snap path first, then PATH."""
    for candidate in _CTR_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    return shutil.which("ctr")


def find_socket(ctr: str) -> str | None:
    """Probe candidate sockets via sudo and return the first that responds.

    The containerd socket is root-only in every supported environment
    (GitHub Actions runners, Multipass, and LXD VMs all run tests as a
    non-root user), so all probes and pulls go through ``sudo -n``.
    """
    if shutil.which("sudo") is None:
        logger.warning("socket: sudo not found; skipping image pre-pulls")
        return None
    for candidate in _SOCKET_CANDIDATES:
        if not Path(candidate).exists():
            logger.debug("socket: %s does not exist", candidate)
            continue
        logger.debug("socket: probing %s (sudo)", candidate)
        if _probe(ctr, candidate):
            logger.info("socket: %s responds via sudo; using sudo for ctr", candidate)
            return candidate
        logger.debug("socket: %s did not respond (sudo)", candidate)
    logger.warning("socket: no candidate responded via sudo")
    return None


def _probe(ctr: str, socket: str) -> bool:
    """Run a `ctr version` probe against *socket* via sudo; True if it responds."""
    command = ["sudo", "-n", "--", ctr, "--address", socket, "-n", NAMESPACE, "version"]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            timeout=_PROBE_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        # A socket that exists but does not respond is worse than no
        # socket at all: ctr hangs until its context deadline instead of
        # failing fast. Treat it as absent and try the next candidate.
        logger.debug("socket: probe of %s raised %s", socket, type(exc).__name__)
        return False
    if result.returncode != 0:
        logger.debug(
            "socket: probe of %s failed (rc=%d): %s",
            socket,
            result.returncode,
            _stderr_summary(result.stderr),
        )
    return result.returncode == 0


def pull_image(image: str, username: str, password: str, ctr: str, socket: str) -> bool:
    """Pull an image into containerd, retrying on failure.

    Returns True if the pull succeeded. The k8s snap's ctr does not support
    ``--auth-file`` ("flag provided but not defined"), so credentials are
    passed with ``--user``. The pull runs via ``sudo -n`` because the
    containerd socket is root-only in every supported environment.
    """
    command = [
        "sudo",
        "-n",
        "--",
        ctr,
        "--address",
        socket,
        "-n",
        NAMESPACE,
        "image",
        "pull",
        "--user",
        f"{username}:{password}",
        image,
    ]
    # Log the command with the credentials redacted: the password is a
    # short-lived Charmhub macaroon, but it still shouldn't appear in logs.
    redacted = list(command)
    if "--user" in redacted:
        redacted[redacted.index("--user") + 1] = "<credentials>"
    logger.debug("ctr: running %s", " ".join(redacted))
    for attempt in range(1, _PULL_ATTEMPTS + 1):
        try:
            result = subprocess.run(
                command, capture_output=True, check=False, timeout=_PULL_TIMEOUT
            )
        except subprocess.TimeoutExpired:
            logger.warning(
                "ctr: pull attempt %d/%d for %s timed out after %ds",
                attempt,
                _PULL_ATTEMPTS,
                image,
                _PULL_TIMEOUT,
            )
            if attempt < _PULL_ATTEMPTS:
                time.sleep(_PULL_RETRY_DELAY)
            continue
        except OSError as exc:
            # Transient spawn failures (e.g. fork under load) are retried
            # like any other pull failure.
            logger.warning(
                "ctr: pull attempt %d/%d for %s raised %s",
                attempt,
                _PULL_ATTEMPTS,
                image,
                type(exc).__name__,
            )
            if attempt < _PULL_ATTEMPTS:
                time.sleep(_PULL_RETRY_DELAY)
            continue
        if result.returncode == 0:
            return True
        logger.warning(
            "ctr: pull attempt %d/%d failed for %s: %s",
            attempt,
            _PULL_ATTEMPTS,
            image,
            _stderr_summary(result.stderr),
        )
        if attempt < _PULL_ATTEMPTS:
            time.sleep(_PULL_RETRY_DELAY)
    return False


def _stderr_summary(stderr: bytes) -> str:
    """Return a one-line summary of ctr's stderr for a failed pull."""
    text = stderr.decode("utf-8", errors="replace").strip()
    return text.splitlines()[-1] if text else "unknown error"
