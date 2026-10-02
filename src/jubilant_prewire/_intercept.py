"""Deploy interception and charm classification."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from . import _charmhub, _containerd

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
        return None
    if not isinstance(charm, str) or not charm:
        return None
    if charm.startswith(("./", "/")):
        return None
    if charm.startswith("ch:"):
        return charm[3:]
    return charm


def _patched_deploy(self, charm, app=None, **kwargs):
    """Pre-pull OCI images for *charm* before the real deploy runs."""
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
        return

    start = time.monotonic()
    try:
        if not _discover():
            return
        info = _charmhub.get_charm_info(name, channel)
        if info.type == "bundle":
            _warm_bundle(name, info)
        else:
            pulled = _pull_charm_images(info, _ctr, _socket)
            if pulled:
                _pulls.append((info.name, channel, pulled))
    except Exception as exc:  # noqa: BLE001 -- best-effort: never break the deploy
        print(f"prewire: failed to pre-pull {name}: {exc}")
    finally:
        _elapsed += time.monotonic() - start
        _warmed.add(key)


def _discover() -> bool:
    """Find ctr and a responsive containerd socket; True if both are found.

    Discovery runs once per session; afterwards the cached result is returned.
    """
    global _ctr, _socket, _discovered
    if _discovered:
        return _ctr is not None and _socket is not None
    _discovered = True
    _ctr = _containerd.find_ctr()
    if _ctr is None:
        print("prewire: ctr not found; skipping image pre-pulls")
        return False
    _socket = _containerd.find_socket(_ctr)
    if _socket is None:
        print("prewire: no responsive containerd socket; skipping image pre-pulls")
        return False
    return True


def _warm_bundle(bundle: str, info: _charmhub.CharmInfo) -> None:
    """Pre-pull the OCI images for every charm in a bundle."""
    if not info.bundle_yaml:
        print(f"prewire: bundle {bundle} has no bundle-yaml; skipping")
        return
    charms = _charmhub.get_bundle_charms(info.bundle_yaml)
    for ref in charms:
        key = (ref.name, ref.channel)
        if key in _warmed:
            continue
        try:
            charm_info = _charmhub.get_charm_info(ref.name, ref.channel)
            pulled = _pull_charm_images(charm_info, _ctr, _socket)
            if pulled:
                _bundle_pulls.append((bundle, charm_info.name, ref.channel, pulled))
        except Exception as exc:  # noqa: BLE001 -- best-effort: never break the deploy
            print(f"prewire: failed to pre-pull {ref.name}: {exc}")
        finally:
            _warmed.add(key)


def _pull_charm_images(info: _charmhub.CharmInfo, ctr: str | None, socket: str | None) -> int:
    """Pull the OCI image resources for *info*; return the number pulled."""
    if ctr is None or socket is None:
        return 0
    pulled = 0
    for resource in info.resources:
        if resource.get("type") != "oci-image":
            continue
        resource_name = resource.get("name")
        revision = resource.get("revision")
        if not isinstance(resource_name, str) or not isinstance(revision, int):
            continue
        try:
            manifest = _charmhub.get_oci_image_manifest(info.id, resource_name, revision)
        except Exception as exc:  # noqa: BLE001 -- best-effort: skip this resource
            print(f"prewire: failed to get image manifest for {info.name}/{resource_name}: {exc}")
            continue
        if _containerd.pull_image(
            manifest.image_name, manifest.username, manifest.password, ctr, socket
        ):
            pulled += 1
        else:
            print(f"prewire: failed to pull image for {info.name}/{resource_name}")
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
            f"pulled {images} images in {_elapsed:.0f}s"
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
