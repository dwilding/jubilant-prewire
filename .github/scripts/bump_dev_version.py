"""Sets a new package dev version."""

import re
import subprocess
import sys
from pathlib import Path

VERSION_FILE = Path("src/jubilant_prewire/_version.py")


def main() -> None:
    new = sys.argv[1]
    subprocess.run(["uv", "version", new, "--no-sync"], check=True)
    VERSION_FILE.write_text(
        re.sub(r'(?m)^__version__ = ".*"$', f'__version__ = "{new}"', VERSION_FILE.read_text())
    )


if __name__ == "__main__":
    main()
