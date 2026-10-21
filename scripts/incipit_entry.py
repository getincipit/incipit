"""The packaged program's entry point: the same CLI, with `tray` as what a double-click runs."""

from __future__ import annotations

import sys

from incipit.cli import main

if __name__ == "__main__":
    # Started from the Start menu with no arguments: the tray, which runs the site.
    sys.exit(main(sys.argv[1:] or ["tray"]))
