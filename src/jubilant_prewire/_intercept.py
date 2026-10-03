"""Deploy interception and charm classification."""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from . import _charmhub, _containerd

logger = logging.getLogger("prewire")

# Worker threads for bundle pulls. Pulls are subprocess- and network-bound, so
# threads release the GIL while waiting. Four workers balance speed against
# saturating the runner's bandwidth; CI runners have ~4 vCPUs.
_PULL_WORKERS = 4

# Charms already pre-pulled this session, keyed by (charm, channel). The
# channel matters: a deploy on a different channel may use a different image.
_warmed: set[tuple[str, str | None]] = set()

# Direct pulls: (charm, channel, image_count) per pre-pulled charm, in order.
# The channel is the one specified in the deploy call (None if not specified),
# matching the summary format in DESIGN.md.
_pulls: list[tuple[str, str | None, int]] = []

# Bundle pulls: (bundle, charm, channel, image_count) per pre-pulled charm.
_bundle_pulls: list[tuple[str, str, str | None, int]] = []

# Cumulative time spent pre-pulling, for the session summary.
_elapsed = 0.0

# Resolved ctr binary and containerd socket, discovered lazily on first use.
_ctr: str | None = None
_socket: str | None = None
_discovered = False

_original_deploy: Any = None


def classify(charm: object) -> str | None:
    """Classify a deploy argument as a Charmhub charm name, or None to skip.

    - ``pathlib.Path`` → local charm, skip
    - String starting with ``./`` or ``/`` → local charm, skip
    - String starting with ``ch:`` → Charmhub charm (strip the prefix)
    - Any other string → Charmhub charm

    This matches how jubilant itself distinguishes local charms from
    Charmhub charms (``_deploy_tempdir`` treats ``Path`` objects and strings
    starting with ``.`` or ``/`` as local files).
    """
    if isinstance(charm, Path):
        logger.info("classify: %r is a pathlib.Path -> local charm, skipping", charm)
        return None
    if not isinstance(charm, str) or not charm:
        logger.info("classify: %r is not a non-empty string -> skipping", charm)
        return None
    if charm.startswith(("./", "/")):
        logger.info("classify: %r starts with ./ or / -> local charm, skipping", charm)
        return None
    if charm.startswith("ch:"):
        name = charm[3:]
        logger.info("classify: %r has ch: prefix -> Charmhub charm %r", charm, name)
        return name
    logger.info("classify: %r -> Charmhub charm", charm)
    return charm


def _patched_deploy(self, charm, app=None, **kwargs):
    """Pre-pull OCI images for *charm* before the real deploy runs."""
    logger.debug(
        "intercepted deploy(charm=%r, app=%r, kwargs=%s)",
        charm,
        app,
        ", ".join(f"{k}={v!r}" for k, v in kwargs.items()) or "{}",
    )
    try:
        _warm(charm, kwargs.get("channel"))
    except Exception as exc:  # noqa: BLE001 -- best-effort: never break the deploy
        # Best-effort: never let a prewire failure break the deploy.
        print(f"prewire: unexpected error during pre-pull: {exc!r}")
    return _original_deploy(self, charm, app=app, **kwargs)


def _warm(charm: object, channel: str | None) -> None:
    """Pre-pull the OCI images for *charm* on *channel*.

    Best-effort: any failure prints a warning and returns so the deploy
    proceeds regardless. The charm is recorded as warmed either way so
    subsequent deploys of the same charm skip the pull.
    """
    global _elapsed
    name = classify(charm)
    if name is None:
        return
    key = (name, channel)
    if key in _warmed:
        logger.debug("warm: %s (channel=%r) already warmed, skipping", name, channel)
        return

    logger.info("pre-pulling %s (channel=%r)", name, channel)
    start = time.monotonic()
    try:
        if not _discover():
            return
        info = _charmhub.get_charm_info(name, channel)
        logger.debug(
            "charm info for %s: id=%s type=%s channel=%r resources=%s",
            name,
            info.id,
            info.type,
            info.channel,
            [(r.get("name"), r.get("type"), r.get("revision")) for r in info.resources],
        )
        if info.type == "bundle":
            _warm_bundle(name, info)
        else:
            pulled = _pull_charm_images(info, _ctr, _socket)
            logger.info("pulled %d image(s) for %s", pulled, name)
            if pulled:
                _pulls.append((info.name, channel, pulled))
    except Exception as exc:  # noqa: BLE001 -- best-effort: never break the deploy
        logger.warning("failed to pre-pull %s: %s", name, exc)
    finally:
        elapsed = time.monotonic() - start
        _elapsed += elapsed
        _warmed.add(key)
        logger.info("pre-pull of %s done in %.1fs", name, elapsed)


def _discover() -> bool:
    """Find ctr and a responsive containerd socket; True if both are found.

    Discovery runs once per session; afterwards the cached result is returned.
    """
    global _ctr, _socket, _discovered
    if _discovered:
        return _ctr is not None and _socket is not None
    _discovered = True
    logger.debug("discover: finding ctr binary (candidates: %s)", _containerd._CTR_CANDIDATES)
    _ctr = _containerd.find_ctr()
    if _ctr is None:
        logger.warning("ctr not found; skipping image pre-pulls")
        return False
    logger.debug("discover: found ctr at %s", _ctr)
    logger.debug("discover: probing sockets (candidates: %s)", _containerd._SOCKET_CANDIDATES)
    _socket = _containerd.find_socket(_ctr)
    if _socket is None:
        logger.warning("no responsive containerd socket; skipping image pre-pulls")
        return False
    logger.info("using ctr %s, socket %s (via sudo)", _ctr, _socket)
    return True


def _warm_bundle(bundle: str, info: _charmhub.CharmInfo) -> None:
    """Pre-pull the OCI images for every charm in a bundle.

    The charms are pulled in parallel: a bundle's images are independent,
    and sequential pulls dominated job runtime in CI validation (the 8
    cos-lite images took 714s of a 1714s job).
    """
    if not info.bundle_yaml:
        logger.warning("bundle %s has no bundle-yaml; skipping", bundle)
        return
    charms = _charmhub.get_bundle_charms(info.bundle_yaml)
    logger.info(
        "bundle %s: %d charm(s): %s",
        bundle,
        len(charms),
        [(ref.name, ref.channel) for ref in charms],
    )
    # Dedup and mark as warmed in the main thread so the same charm is never
    # submitted twice; workers do not touch _warmed.
    pending = []
    for ref in charms:
        key = (ref.name, ref.channel)
        if key in _warmed:
            logger.debug(
                "bundle %s: %s (channel=%r) already warmed, skipping",
                bundle,
                ref.name,
                ref.channel,
            )
            continue
        _warmed.add(key)
        pending.append(ref)
    if not pending:
        return
    start = time.monotonic()
    with ThreadPoolExecutor(max_workers=_PULL_WORKERS) as pool:
        futures = {pool.submit(_pull_bundle_charm, ref): ref for ref in pending}
        for future in as_completed(futures):
            ref = futures[future]
            try:
                charm_name, pulled = future.result()
                if pulled:
                    _bundle_pulls.append((bundle, charm_name, ref.channel, pulled))
            except Exception as exc:  # noqa: BLE001 -- best-effort: never break the deploy
                logger.warning("bundle %s: failed to pre-pull %s: %s", bundle, ref.name, exc)
    # Note: the bundle's wall time is added to _elapsed by _warm's finally
    # block, which covers this whole call; adding it here too would
    # double-count.
    logger.info(
        "bundle %s: %d charm(s) pulled in %.1fs",
        bundle,
        len(pending),
        time.monotonic() - start,
    )


def _pull_bundle_charm(ref: _charmhub.CharmRef) -> tuple[str, int]:
    """Fetch info for and pull one bundle charm; return (name, pulled).

    Runs on a worker thread; must not touch module-level mutable state.
    """
    start = time.monotonic()
    charm_info = _charmhub.get_charm_info(ref.name, ref.channel)
    pulled = _pull_charm_images(charm_info, _ctr, _socket)
    logger.info(
        "bundle: pulled %d image(s) for %s in %.1fs", pulled, ref.name, time.monotonic() - start
    )
    return charm_info.name, pulled


def _pull_charm_images(info: _charmhub.CharmInfo, ctr: str | None, socket: str | None) -> int:
    """Pull the OCI image resources for *info*; return the number pulled."""
    if ctr is None or socket is None:
        return 0
    pulled = 0
    for resource in info.resources:
        if resource.get("type") != "oci-image":
            logger.info(
                "pull: skipping resource %s/%s (type=%r, not oci-image)",
                info.name,
                resource.get("name"),
                resource.get("type"),
            )
            continue
        resource_name = resource.get("name")
        revision = resource.get("revision")
        if not isinstance(resource_name, str) or not isinstance(revision, int):
            logger.warning(
                "pull: skipping malformed resource %s/%s (name=%r revision=%r)",
                info.name,
                resource_name,
                resource_name,
                revision,
            )
            continue
        try:
            manifest = _charmhub.get_oci_image_manifest(info.id, resource_name, revision)
        except Exception as exc:  # noqa: BLE001 -- best-effort: skip this resource
            logger.warning(
                "pull: failed to get image manifest for %s/%s: %s", info.name, resource_name, exc
            )
            continue
        logger.info(
            "pulling %s/%s (rev %d) -> %s",
            info.name,
            resource_name,
            revision,
            manifest.image_name,
        )
        start = time.monotonic()
        if _containerd.pull_image(
            manifest.image_name, manifest.username, manifest.password, ctr, socket
        ):
            pulled += 1
            logger.info("pulled %s in %.1fs", manifest.image_name, time.monotonic() - start)
        else:
            logger.warning("failed to pull image for %s/%s", info.name, resource_name)
    return pulled


def _print_summary() -> None:
    """Print the end-of-session summary of pre-pulled charms and images."""
    if _pulls:
        images = sum(count for _, _, count in _pulls)
        print(
            f"prewire: pre-pulled {len(_pulls)} charms, pulled {images} images in {_elapsed:.0f}s"
        )
        for name, channel, count in _pulls:
            suffix = f" ({channel})" if channel else ""
            print(f"  {name}{suffix} → {count} image{'s' if count != 1 else ''}")
    for bundle in dict.fromkeys(entry[0] for entry in _bundle_pulls):
        entries = [entry for entry in _bundle_pulls if entry[0] == bundle]
        images = sum(entry[3] for entry in entries)
        print(
            f"prewire: pre-pulled {len(entries)} charms from bundle {bundle}, "
            f"pulled {images} images (time included above)"
        )
        for _, name, channel, count in entries:
            suffix = f" ({channel})" if channel else ""
            print(f"  {name}{suffix} → {count} image{'s' if count != 1 else ''}")


def install() -> None:
    """Patch ``jubilant.Juju.deploy`` to pre-pull images before deploys."""
    global _original_deploy
    import jubilant

    _original_deploy = jubilant.Juju.deploy
    jubilant.Juju.deploy = _patched_deploy


def uninstall() -> None:
    """Restore the original ``jubilant.Juju.deploy``."""
    global _original_deploy
    if _original_deploy is not None:
        import jubilant

        jubilant.Juju.deploy = _original_deploy
        _original_deploy = None
