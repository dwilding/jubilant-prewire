# jubilant-prewire

## Problem

When you deploy K8s charms with OCI image resources on CI runners, the container image pull from `registry.jujucharms.com` can take 30 seconds to 7+ minutes. Jubilant's default `juju.wait()` timeout is 180 seconds. When the pull exceeds that, tests fail. The charm isn't broken; the network was slow.

jubilant-prewire moves the image pull out of the test's critical path. It intercepts `juju.deploy()` calls, pulls the charm's OCI image into containerd before the deploy proceeds, then lets jubilant do its work as normal. The image is already cached when the pod starts, so the pull is instant.

## How it works

jubilant-prewire is a pytest plugin. Add it to your integration test dependencies and it auto-activates — no conftest changes, no CLI invocation, no configuration. It monkey-patches `jubilant.Juju.deploy` in `pytest_configure` and restores the original in `pytest_sessionfinish`.

### The interception

When a test calls `juju.deploy(charm, ...)`, jubilant-prewire classifies the first argument:

- `pathlib.Path` → local charm, skip
- String starting with `./` or `/` → local charm, skip
- String starting with `ch:` → Charmhub charm (strip the prefix)
- Any other string → Charmhub charm

This classification is 100% accurate. Unlike static AST analysis, the runtime has already resolved every variable, f-string, attribute access, and function call to its actual value. There are no unknowns.

### The pre-pull step

For each Charmhub charm, jubilant-prewire:

1. Queries Charmhub for the charm's default-release resources on the specified channel (or the default channel if none is specified).
2. Checks the `type` field. If it is `bundle`, jubilant-prewire parses the `bundle-yaml` from the response to extract the charm names and channels of every charm in the bundle, then pre-pulls each one. If it is `charm`, continues.
3. Filters for `type: oci-image` resources. Charms with no OCI image resources are skipped silently.
4. Downloads each OCI image resource manifest, which contains the image reference (`ImageName`) and temporary registry credentials (`Username`, `Password`).
5. Pulls the image into containerd using `ctr`:
   ```
   ctr --address {socket} -n k8s.io image pull --user {username}:{password} {image_name}
   ```
6. Records the charm as pre-pulled so subsequent deploys of the same charm skip the pull.

The pre-pull step blocks the deploy call until the pull completes. This is intentional: the test can't proceed until the image is cached, and the pull takes the same time whether it happens in jubilant-prewire or in the pod's init phase. The difference is that jubilant-prewire's pull happens before the 180-second jubilant timeout starts ticking.

If the pull fails, jubilant-prewire prints a warning and lets the deploy proceed. The test still runs — it just might time out on the image pull, same as without jubilant-prewire.

### Charmhub API

jubilant-prewire queries two Charmhub endpoints:

1. **Charm info**: gets the charm ID, type, and resource list.
   ```
   GET https://api.charmhub.io/v2/charms/info/{charm}?channel={channel}&fields=default-release.resources,default-release.revision
   ```
   Returns `id`, `type` (`charm` or `bundle`), and `default-release.resources` (a list of resources, each with a `name`, `type`, and `revision`). For bundles, the response also includes `default-release.revision.bundle-yaml`, which contains the YAML manifest of the bundle's applications.

2. **Resource manifest**: gets the OCI image reference and credentials.
   ```
   GET https://api.charmhub.io/api/v1/resources/download/charm_{charm_id}.{resource_name}_{revision}
   ```
   Returns `ImageName`, `Username`, `Password`.

Both calls use `urllib.request` from the standard library.

### ctr and containerd discovery

jubilant-prewire assumes concierge has set up the K8s environment.

**ctr binary**: try `/snap/k8s/current/bin/ctr`, then `which ctr`. If you find neither, print a warning and skip all pulls. The tests still run — jubilant-prewire is best-effort.

**containerd socket**: probe candidate sockets and verify each one responds before you use it. Do not assume a single path, and do not let `ctr` fall back to its default silently. The candidates, in order:

1. `/var/snap/k8s/common/run/containerd.sock` (k8s snap, older layout)
2. `/var/snap/k8s/current/run/containerd.sock` (k8s snap, older layout)
3. `/run/containerd/containerd.sock` (standard location; this is where the k8s snap's containerd listens as of v1.32 — validated on a dev VM)

For each candidate, check that the socket exists and that `ctr --address {candidate} -n k8s.io version` succeeds. A socket that exists but does not respond is worse than no socket at all: `ctr` hangs until its context deadline instead of failing fast.

All probes and pulls go through `sudo -n` (passwordless sudo). The containerd socket is `root:root` with mode `srw-rw----` in every supported environment — GitHub Actions runners, Multipass, and LXD VMs all run tests as a non-root user — so a direct probe gets permission denied everywhere that matters. Root environments (e.g. containers without sudo) are not supported. If sudo is unavailable or no candidate responds, print a warning and skip all pulls.

If no candidate responds even via sudo, print a warning and skip all pulls.

**Namespace**: hardcoded to `k8s.io`.

**Credentials**: pass with `--user {username}:{password}`. The `ctr` shipped in the k8s snap does not support `--auth-file` — it fails with "flag provided but not defined".

### Retry

Retry each pull up to 5 times, waiting 15 seconds between attempts. A full pull of a large image takes minutes: the postgresql-k8s image is ~286 MiB, and it took roughly 3.5 minutes on a GitHub Actions runner at ~890 KiB/s (314 MiB in 198s on a dev VM). Retries must tolerate long-running attempts, not just quick failures.

Charmhub HTTP requests are also retried (5 attempts, 3 seconds apart): the resource manifest endpoint redirects to a CDN that intermittently resets connections. Without HTTP retries, a full cos-lite pre-pull dropped charms at random.

## Installation

Add `jubilant-prewire` to your integration test dependencies in `pyproject.toml`:

```toml
[dependency-groups]
integration = [
    "jubilant>=1.8,<2",
    "jubilant-prewire",
    "pytest",
    "pytest-jubilant>=2.0.1,<3",
]
```

That's it. The next time you run `pytest`, jubilant-prewire auto-activates.

## CI usage

```yaml
- name: Run integration tests
  run: uv run --group integration pytest tests/integration/
```

No separate pre-pull step. jubilant-prewire runs as part of the test session — the first `juju.deploy()` call for each charm triggers the pull.

Run jubilant-prewire in every CI job that deploys charms, not just the job whose example is "about" that charm. In validation, two of three example jobs deployed postgresql-k8s indirectly and timed out at 180 seconds while the image was still being pulled. jubilant-prewire intercepts every deploy call, so the same zero-config setup works in every job.

## Report

At the end of the test session, jubilant-prewire prints a summary:

```
prewire: pre-pulled 4 charms, pulled 4 images in 47s
  postgresql-k8s (14/stable) → 1 image
  self-signed-certificates (edge) → 1 image
  traefik-k8s → 1 image
  any-charm (latest/edge) → 1 image
```

For bundles, jubilant-prewire lists each charm it pre-pulled from the bundle:

```
prewire: pre-pulled 6 charms from bundle cos-lite, pulled 8 images in 1296s
  alertmanager-k8s (1/stable) → 1 image
  catalogue-k8s (1/stable) → 1 image
  grafana-k8s (1/stable) → 2 images
  loki-k8s (1/stable) → 2 images
  prometheus-k8s (1/stable) → 1 image
  traefik-k8s (latest/stable) → 1 image
```

A charm can have more than one oci-image resource (grafana-k8s and loki-k8s each have two), so the image count can exceed the charm count.

## What jubilant-prewire does NOT do

- **Manual charm specification**: you can't tell jubilant-prewire which charms to pull. It intercepts deploy calls and pulls what it sees. If a charm is deployed through a code path that jubilant-prewire doesn't intercept, the image won't be pre-pulled.
- **Machine charm filtering**: not needed. You run jubilant-prewire in a K8s charm repo. Machine charms don't have OCI image resources, so Charmhub returns nothing and jubilant-prewire skips them.
- **Caching**: no cross-run caching. Each test session re-fetches image manifests and re-pulls. Containerd's own layer cache makes re-pulls fast if the image hasn't changed.
- **Concurrency with environment setup**: the pull needs a responsive containerd socket, which only exists once the K8s environment is up, so the first deploy call blocks until the pull completes. You could overlap the pull with environment setup by backgrounding it and polling for a socket, but that adds failure-attribution complexity that isn't worth it for the initial version.
- **pytest-operator support**: jubilant-prewire intercepts `jubilant.Juju.deploy` only. Tests that use pytest-operator (libjuju's `Model.deploy`) aren't intercepted. This could be added later if needed.

## Dependencies

- Python 3.10+
- `jubilant` (required — jubilant-prewire intercepts its `deploy` method)
- `PyYAML` (required for bundle resolution — already a dependency of jubilant)
- stdlib only for everything else: `urllib.request`, `json`, `subprocess`, `pathlib`, `time`
- `ctr` binary on the system (inside k8s snap or standalone containerd)

No other third-party Python packages. No API keys. No network dependencies beyond Charmhub.

## Implementation

### Package structure

```
jubilant-prewire/
  pyproject.toml
  src/jubilant_prewire/
    __init__.py
    _plugin.py        # pytest hooks: pytest_configure, pytest_sessionfinish
    _intercept.py     # deploy interception and charm classification
    _charmhub.py      # Charmhub API client
    _containerd.py    # ctr discovery, socket probing, image pull with retry
  tests/
    test_intercept.py
    test_charmhub.py
    test_containerd.py
```

### Entry point

```toml
[project.entry-points.pytest11]
prewire = "jubilant_prewire._plugin"
```

### Plugin hooks

```python
def pytest_configure(config):
    """Install the deploy interception."""
    import jubilant

    _original = jubilant.Juju.deploy
    jubilant.Juju.deploy = _patched_deploy
    # Store _original for restoration in pytest_sessionfinish


def pytest_sessionfinish(session, exitstatus):
    """Restore the original deploy and print the summary."""
    import jubilant

    jubilant.Juju.deploy = _original
    _print_summary()
```

### Interception

```python
def _patched_deploy(self, charm, app=None, **kwargs):
    _warm(charm, kwargs.get("channel"))
    return _original_deploy(self, charm, app=app, **kwargs)
```

The `_warm` function classifies the charm argument, queries Charmhub if it is a Charmhub charm, pulls the OCI image if one exists, and records the charm as pre-pulled. If the pull fails, it prints a warning and returns — the deploy proceeds regardless.

### Charmhub client

```python
def get_charm_info(charm: str, channel: str | None) -> CharmInfo:
    """Query Charmhub for charm info and resources."""
    # GET /v2/charms/info/{charm}?channel={channel}&fields=...


def get_bundle_charms(bundle_yaml: str) -> list[CharmRef]:
    """Parse a bundle's YAML manifest to extract charm names and channels."""
    # yaml.safe_load(bundle_yaml) -> applications -> charm, channel


def get_oci_image_manifest(charm_id: str, resource_name: str, revision: int) -> ImageManifest:
    """Download the OCI image resource manifest."""
    # GET /api/v1/resources/download/charm_{id}.{name}_{revision}
```

### containerd client

```python
def find_ctr() -> str | None:
    """Find the ctr binary."""


def find_socket(ctr: str) -> str | None:
    """Probe candidate sockets and return the first that responds."""


def pull_image(image: str, username: str, password: str, ctr: str, socket: str) -> bool:
    """Pull an image into containerd with retry."""
    # ctr --address {socket} -n k8s.io image pull --user {user}:{pass} {image}
```

### Testing

Unit tests for:
- Charm classification (feed in various argument types, check classification)
- Charmhub API response parsing (mocked HTTP responses)
- ctr discovery and socket probing (mocked filesystem and subprocess)

No integration tests are needed for the initial version. The real Charmhub API and containerd are tested by running jubilant-prewire in CI.

## Reference

### Charmhub API response shapes

**Charm info** (`GET /v2/charms/info/{charm}?channel={channel}&fields=default-release.resources,default-release.revision`):

```json
{
  "id": "y2vuZHGLElMmdfcL4q1zJayL9utaheIa",
  "name": "postgresql-k8s",
  "type": "charm",
  "default-release": {
    "channel": {"name": "14/stable", "track": "14", "risk": "stable"},
    "resources": [
      {
        "name": "postgresql-image",
        "type": "oci-image",
        "revision": 208,
        "download": {
          "url": "https://api.charmhub.io/api/v1/resources/download/charm_{id}.{name}_{revision}"
        }
      }
    ],
    "revision": {"revision": 925, "version": "14"}
  }
}
```

For bundles, `type` is `"bundle"` and the charm list is in `default-release.revision.bundle-yaml`:

```json
{
  "id": "66OyiQ94SPfNCh8uYMSsJqgntevNtDLo",
  "name": "cos-lite",
  "type": "bundle",
  "default-release": {
    "resources": [],
    "revision": {
      "bundle-yaml": "applications:\n  alertmanager:\n    charm: alertmanager-k8s\n    channel: 1/stable\n  ..."
    }
  }
}
```

**OCI image manifest** (`GET /api/v1/resources/download/charm_{charm_id}.{resource_name}_{revision}`):

```json
{
  "ImageName": "registry.jujucharms.com/charm/.../postgresql-image@sha256:...",
  "Username": "docker-registry",
  "Password": "MDAxOGxvY2F0aW9uIGNoYXJtc3RvcmUK..."
}
```

The `Username` and `Password` are passed directly to `ctr` as `--user {Username}:{Password}`. The password is a base64-encoded macaroon, not plaintext — pass it through as-is.

### URL construction

The `fields` parameter controls which fields the API returns. For charm info:

```
?channel={channel}&fields=default-release.resources,default-release.revision
```

If `channel` is not specified, omit it and Charmhub returns the default release. The `fields` parameter is always needed — without it, the response omits `resources` and `revision`.

### Plugin safety

The monkey-patch is installed in `pytest_configure` and restored in `pytest_sessionfinish`. If the test session crashes before `pytest_sessionfinish` runs, the patch persists in the process — but since pytest exits after the session, this is not a problem in practice.

If jubilant is not installed (the user added jubilant-prewire to dependencies but not jubilant), `pytest_configure` should catch `ImportError` and skip silently with a warning. This can't happen if the user follows the Installation instructions, but it's a reasonable defensive measure.

### Concurrency

jubilant-prewire patches `jubilant.Juju.deploy` at the class level, so all `Juju` instances share the same patched method. The `_warmed` set is module-level and not protected by a lock. This is safe because integration tests run sequentially by default — a scan of ~600 charm repos found none that use pytest-xdist for integration tests. If parallel integration tests become common later, a lock would be needed.

## Validated in CI (2026-09-24)

The pre-pull approach was validated end to end in the `canonical/operator` repository (workflow: `example-charm-integration-tests.yaml`, branch `try-cached-postgres`). Three jobs each pre-pulled the postgresql-k8s image before running integration tests. All three passed after previously failing with 180-second `juju.wait()` timeouts. With the image cached, the postgres deploy went from pod-scheduled to `active` in roughly 55 seconds, and the test suite completed in 178 seconds.

The debugging journey that produced the guidance above:

1. The k8s snap's `ctr` rejected `--auth-file`, so the pull uses `--user`.
2. `ctr` silently fell back to `/run/containerd/containerd.sock` and got permission denied, so jubilant-prewire always passes `--address` explicitly.
3. The snap socket path existed but timed out on dial, so jubilant-prewire probes candidates with `ctr version` and only uses one that responds.
4. Two of three jobs deployed the charm indirectly and timed out, so the guidance is to pre-pull in every job that deploys the charm.

## Validated in CI (2026-10-03)

The plugin (installed as a git dependency) ran in all five k8s example jobs of the operator repo's example-charm-integration-tests workflow, on real GitHub Actions runners. All five passed:

- k8s-1-minimal and k8s-2-configurable deploy only local charms; jubilant-prewire correctly did nothing.
- k8s-3-postgresql and k8s-4-action deploy postgresql-k8s from Charmhub; the image was pre-pulled (318s and 220s) before the deploy, and both suites passed well inside the 180-second `juju.wait()` timeout.
- k8s-5-observe deploys postgresql-k8s and the cos-lite bundle; all 8 images were pre-pulled (462s + 714s) before the bundle deploy, and the suite passed.

Key findings from the run logs:

1. **The sudo fallback is required on CI, not just dev VMs.** GitHub Actions runners run tests as the non-root `runner` user, so the direct socket probe gets permission denied on every pulling job; the sudo fallback engaged each time. Without it, jubilant-prewire would have been a silent no-op on CI.
2. **Bundle pulls are sequential and dominate runtime.** The 8 cos-lite images pulled one at a time took 714s of a 1714s job — 42% of the job's time. Parallelizing pulls is the main performance opportunity.
3. **The live socket on the runners was `/run/containerd/containerd.sock`** (the standard location); the snap-specific candidates did not exist, matching the dev VM finding.

## Validated on a dev VM (2026-10-03)

The plugin was validated end to end on a Multipass VM (Ubuntu 26.04, k8s snap v1.32.13, Juju 3.6.29, running as a non-root `ubuntu` user):

1. **Discovery**: `find_ctr()` found `/snap/k8s/current/bin/ctr`; the responsive socket was `/run/containerd/containerd.sock` (the standard location — the snap-specific candidates did not exist on this layout).
2. **Non-root access**: the socket is `root:root srw-rw----`, so direct probes got permission denied; the sudo fallback engaged and pulls worked as the `ubuntu` user.
3. **Real pull**: postgresql-k8s (314 MiB) pulled in 198s on first pull; a re-pull took 5s thanks to containerd's layer cache.
4. **Direct deploy e2e**: a pytest session deploying `postgresql-k8s` (14/stable) pre-pulled the image before the deploy, reached `active`, and passed in 206s.
5. **Bundle e2e**: a pytest session deploying `cos-lite` pre-pulled all 6 charms (8 images, 1296s) before the bundle deploy, all apps reached `active`, and the test passed in 1741s.
