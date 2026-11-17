"""Bumps pinned and locked dependency versions, to the latest releases older than seven days."""

import json
import re
import subprocess
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path


def requirements(dev: bool) -> list[str]:
    """Return the project's runtime or dev requirement strings."""
    section = "dev" if dev else "dependencies"
    match = re.search(rf"(?ms)^\s*{section} = \[(.*?)\]", Path("pyproject.toml").read_text())
    assert match is not None
    return re.findall(r'"([^"]*)"', match.group(1))


def parse_version(version: str) -> tuple[int, ...] | None:
    """Parse a simple numeric version string like '2.4.1' into a comparable tuple.

    Returns None for anything that isn't purely numeric: prereleases, post-releases, and so on.
    """
    parts = version.split(".")
    if not all(part.isdigit() for part in parts):
        return None
    return tuple(int(part) for part in parts)


def latest_version(package: str) -> str:
    """Return the latest stable version of *package* published more than seven days ago."""
    with urllib.request.urlopen(f"https://pypi.org/pypi/{package}/json") as resp:
        data = json.load(resp)
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    versions = []
    for version, files in data["releases"].items():
        if not files:
            continue
        parsed = parse_version(version)
        if parsed is None:
            continue  # Unstable or non-standard version.
        upload_time = max(f["upload_time_iso_8601"] for f in files)
        versions.append((parsed, version, upload_time))
    for parsed, version, upload_time in sorted(versions, reverse=True):
        if datetime.fromisoformat(upload_time) <= cutoff:
            return version
    sys.exit(f"Error: No stable release of {package} older than seven days")


def bump_build(uv_version: str) -> None:
    content = Path("pyproject.toml").read_text()
    content = re.sub(r'"uv_build==[^"]+"', f'"uv_build=={uv_version}"', content)
    Path("pyproject.toml").write_text(content)
    subprocess.run(["uv", "lock"], check=True)


def bump_precommit(just_version: str) -> None:
    path = Path(".pre-commit-config.yaml")
    path.write_text(
        re.sub(r"rust-just@[0-9][0-9.]*", f"rust-just@{just_version}", path.read_text())
    )


def bump_workflows(just_version: str) -> None:
    for path in Path(".github/workflows").glob("*.yaml"):
        path.write_text(
            re.sub(r"rust-just@[0-9][0-9.]*", f"rust-just@{just_version}", path.read_text())
        )


def main() -> None:
    for dev in (False, True):
        for entry in requirements(dev):
            pin = re.fullmatch(r"([A-Za-z0-9._-]+)==([^;]+)(?:;.*)?", entry)
            if pin is None:
                # Not an exact specifier. Keep the specifier and refresh the locked version.
                name = re.match(r"[A-Za-z0-9._-]+", entry)
                assert name is not None
                subprocess.run(["uv", "lock", "--upgrade-package", name.group()], check=True)
                continue
            name, current = pin.groups()
            version = latest_version(name)
            if version == current:
                print(f"{name}: {version} (up to date)")
                continue
            print(f"{name}: {current} -> {version}")
            cmd = ["uv", "add"]
            if dev:
                cmd.append("--dev")
            cmd.append(entry.replace(f"=={current}", f"=={version}", 1))
            subprocess.run(cmd, check=True)
    uv_version = latest_version("uv_build")
    print(f"uv_build: {uv_version}")
    bump_build(uv_version)
    just_version = latest_version("rust-just")
    print(f"rust-just: {just_version}")
    bump_precommit(just_version)
    bump_workflows(just_version)


if __name__ == "__main__":
    main()
