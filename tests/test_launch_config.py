"""The checked-in desktop launcher must remain executable by the current CLI and safe for local data."""

import json
from pathlib import Path

from incipit.cli import build_parser


def test_desktop_launch_arguments_match_the_cli():
    launch = json.loads((Path(__file__).parents[1] / ".claude" / "launch.json").read_text())
    runtime_args = launch["configurations"][0]["runtimeArgs"]

    # The launcher invokes ``python -m incipit.cli`` before these application arguments.
    parsed = build_parser().parse_args(runtime_args[2:])

    assert parsed.command == "review"
    assert parsed.db == "data/incipit.db"
    assert parsed.bucket_uri == "file://./data/bucket"


def test_the_launcher_carries_no_admin_signup_phrase():
    """A developer's admin phrase belongs in the ignored .env, not every checkout."""
    launch = json.loads((Path(__file__).parents[1] / ".claude" / "launch.json").read_text())

    for configuration in launch["configurations"]:
        assert "INCIPIT_ADMIN_SIGNUP_SECRET" not in (configuration.get("env") or {})


def test_review_launches_keep_smoke_copies_isolated():
    configs = json.loads((Path(__file__).resolve().parents[1] / ".claude" / "launch.json").read_text())
    launches = configs["configurations"]

    def args(config):
        values = config["runtimeArgs"]
        return {value: values[i + 1] for i, value in enumerate(values[:-1]) if value.startswith("--")}

    primary = next(config for config in launches if config["name"] == "incipit")
    live = args(primary)
    for config in launches:
        if config is primary:
            continue
        options = args(config)
        if "--db" not in options or options["--db"] == live["--db"]:
            continue
        # A different database must not be able to delete the primary site's scans or overwrite
        # its deliverables. Explicit paths also prevent falling back to shared CLI defaults.
        assert options.get("--bucket-uri") != live["--bucket-uri"]
        assert "--bucket-uri" in options
        assert options.get("--export-dir") not in (None, live.get("--export-dir", "data/exports"))
        assert "--backup-dir" in options and "--backup-mirror" in options
