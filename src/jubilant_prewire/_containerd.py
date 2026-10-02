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

# Set to True during socket discovery when the responsive socket is only
# reachable via sudo (root:root sockets on dev VMs; CI runners run as root
# and never hit this). All subsequent ctr invocations then go through sudo.
_use_sudo = False


def find_ctr() -> str | None:
    """Find the ctr binary: k8s snap path first, then PATH."""
    for candidate in _CTR_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    return shutil.which("ctr")


def find_socket(ctr: str) -> str | None:
    """Probe candidate sockets and return the first that responds.

    If no socket responds directly (common on dev VMs where the socket is
    root-only), the probe is retried via ``sudo -n``; when that works, all
    subsequent ctr invocations use sudo too.
    """
    global _use_sudo
    for candidate in _SOCKET_CANDIDATES:
        if not Path(candidate).exists():
            logger.info("socket: %s does not exist", candidate)
            continue
        logger.info("socket: probing %s (direct)", candidate)
        if _probe(ctr, candidate):
            logger.info("socket: %s responds (direct)", candidate)
            return candidate
        logger.info("socket: %s did not respond (direct)", candidate)
    # Direct access failed everywhere. If passwordless sudo is available,
    # retry the probe with it — CI runners run as root and never get here,
    # but dev VMs typically have root-only sockets and passwordless sudo.
    if shutil.which("sudo") is None:
        logger.warning("socket: no candidate responded and sudo is not available")
        return None
    for candidate in _SOCKET_CANDIDATES:
        if not Path(candidate).exists():
            continue
        logger.info("socket: probing %s (sudo)", candidate)
        if _probe(ctr, candidate, sudo=True):
            _use_sudo = True
            logger.info("socket: %s responds via sudo; using sudo for ctr", candidate)
            return candidate
        logger.info("socket: %s did not respond (sudo)", candidate)
    logger.warning("socket: no candidate responded directly or via sudo")
    return None


def _probe(ctr: str, socket: str, sudo: bool = False) -> bool:
    """Run a `ctr version` probe against *socket*; True if it responds."""
    command = [ctr, "--address", socket, "-n", NAMESPACE, "version"]
    if sudo:
        command = ["sudo", "-n", "--", *command]
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
        logger.info("socket: probe of %s raised %s", socket, type(exc).__name__)
        return False
    if result.returncode != 0:
        logger.info(
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
    passed with ``--user``. When socket discovery needed sudo, the pull runs
    via sudo too.
    """
    command = [
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
    if _use_sudo:
        command = ["sudo", "-n", "--", *command]
    # Log the command with the credentials redacted: the password is a
    # short-lived Charmhub macaroon, but it still shouldn't appear in logs.
    redacted = list(command)
    if "--user" in redacted:
        redacted[redacted.index("--user") + 1] = "<credentials>"
    logger.info("ctr: running %s", " ".join(redacted))
    for attempt in range(1, _PULL_ATTEMPTS + 1):
        try:
            result = subprocess.run(command, capture_output=True, check=False)
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
