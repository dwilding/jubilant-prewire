"""Charmhub API client for charm info and OCI image resource manifests."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

import yaml

CHARMHUB_API = "https://api.charmhub.io"

# Timeout for Charmhub HTTP requests. The info endpoint is fast; the resource
# manifest endpoint redirects to a CDN that can occasionally be slow.
_HTTP_TIMEOUT = 30

# The resource manifest CDN intermittently resets connections, so HTTP
# requests are retried a few times before giving up.
_HTTP_ATTEMPTS = 5
_HTTP_RETRY_DELAY = 3


@dataclass(frozen=True)
class CharmInfo:
    """Charm info and default-release resources for one charm or bundle."""

    id: str
    name: str
    type: str
    channel: str | None
    resources: list[dict[str, object]]
    bundle_yaml: str | None


@dataclass(frozen=True)
class CharmRef:
    """A charm referenced by a bundle, with its channel if specified."""

    name: str
    channel: str | None


@dataclass(frozen=True)
class ImageManifest:
    """OCI image resource manifest: image reference and pull credentials."""

    image_name: str
    username: str
    password: str


def get_charm_info(charm: str, channel: str | None = None) -> CharmInfo:
    """Query Charmhub for charm info and default-release resources.

    Uses the ``default-release`` fields so the resources returned match what a
    deploy on *channel* (or the default channel) would actually use.
    """
    params = {"fields": "default-release.resources,default-release.revision"}
    if channel:
        params["channel"] = channel
    url = f"{CHARMHUB_API}/v2/charms/info/{urllib.parse.quote(charm)}?{urllib.parse.urlencode(params)}"
    data = _get_json(url)

    default_release = data.get("default-release") or {}
    resources = default_release.get("resources") or []
    revision = default_release.get("revision") or {}
    bundle_yaml = revision.get("bundle-yaml")
    return CharmInfo(
        id=data["id"],
        name=data.get("name", charm),
        type=data.get("type", "charm"),
        channel=(default_release.get("channel") or {}).get("name"),
        resources=[r for r in resources if isinstance(r, dict)],
        bundle_yaml=bundle_yaml if isinstance(bundle_yaml, str) else None,
    )


def get_bundle_charms(bundle_yaml: str) -> list[CharmRef]:
    """Parse a bundle's YAML manifest to extract charm names and channels."""
    data = yaml.safe_load(bundle_yaml)
    if not isinstance(data, dict):
        return []
    applications = data.get("applications")
    if not isinstance(applications, dict):
        return []
    charms: list[CharmRef] = []
    for app in applications.values():
        if not isinstance(app, dict):
            continue
        name = app.get("charm")
        if not isinstance(name, str) or not name:
            continue
        channel = app.get("channel")
        charms.append(CharmRef(name=name, channel=channel if isinstance(channel, str) else None))
    return charms


def get_oci_image_manifest(charm_id: str, resource_name: str, revision: int) -> ImageManifest:
    """Download the OCI image resource manifest for one resource.

    The endpoint 302-redirects to a CDN; ``urlopen`` follows redirects and the
    final payload is the manifest JSON.
    """
    url = f"{CHARMHUB_API}/api/v1/resources/download/charm_{charm_id}.{resource_name}_{revision}"
    data = _get_json(url)
    return ImageManifest(
        image_name=data["ImageName"],
        username=data["Username"],
        password=data["Password"],
    )


def _get_json(url: str) -> dict:
    """GET *url* and return the JSON response, retrying transient failures."""
    last_error: Exception | None = None
    for attempt in range(1, _HTTP_ATTEMPTS + 1):
        request = urllib.request.Request(url, headers={"Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT) as response:
                return json.loads(response.read().decode("utf-8"))
        except (OSError, urllib.error.HTTPError, ValueError) as exc:
            # OSError covers URLError, socket errors, and connection resets;
            # HTTPError covers bad HTTP status lines; ValueError covers a
            # truncated body that fails JSON parsing.
            last_error = exc
            if attempt < _HTTP_ATTEMPTS:
                time.sleep(_HTTP_RETRY_DELAY)
    assert last_error is not None
    raise last_error
