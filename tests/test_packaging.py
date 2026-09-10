"""The build has to contain the files the app reads at run time.

This exists because it already went wrong. The first packaged build
omitted ``echoact/db/schema.sql``, because the spec declared only the
fonts as data, and the symptom was a launch that opened its window,
answered DB_UNAVAILABLE, and wrote nothing else. A packaging mistake is
invisible in every test that runs from a checkout, where the file is
simply there.

A full build takes minutes, so this checks the rule the spec collects by
rather than the output: every suffix the package actually contains must
be one the spec gathers.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "packaging" / "echoact.spec"
PACKAGE = ROOT / "echoact"


def _spec_suffixes() -> set[str]:
    """The suffix set the spec's collector uses."""
    text = SPEC.read_text(encoding="utf-8")
    match = re.search(r"path\.suffix\.lower\(\) in \{([^}]*)\}", text)
    assert match, "the spec no longer collects package data by suffix"
    return set(re.findall(r'"(\.[a-z0-9]+)"', match.group(1)))


def _runtime_suffixes() -> set[str]:
    return {
        path.suffix.lower()
        for path in PACKAGE.rglob("*")
        if path.is_file() and path.suffix != ".py" and "__pycache__" not in path.parts
    }


def test_every_non_python_file_in_the_package_is_collected() -> None:
    missing = _runtime_suffixes() - _spec_suffixes()
    assert not missing, (
        f"the package contains {sorted(missing)} files that packaging/echoact.spec "
        "would not put in a build; add the suffix to its collector"
    )


def test_the_schema_is_one_of_them() -> None:
    """The specific file that was missing, named, so the regression has a
    test rather than only a rule."""
    schema = PACKAGE / "db" / "schema.sql"
    assert schema.is_file()
    assert ".sql" in _spec_suffixes()


def test_the_spec_still_builds_a_directory_not_a_single_file() -> None:
    """N-10 describes a folder, and A.2 rules out the one-file build twice:
    it is not that layout, and it would statically bundle Qt, which is
    LGPLv3 and has to stay relinkable."""
    text = SPEC.read_text(encoding="utf-8")
    assert "COLLECT(" in text
    assert "exclude_binaries=True" in text, "a one-file build sets this False"


def test_the_worker_and_mcp_entry_points_are_reachable_in_a_build() -> None:
    """A bundle cannot run `python -m echoact.engine.worker`, so the
    executable re-invokes itself. Both halves have to exist."""
    entry = (ROOT / "packaging" / "entry.py").read_text(encoding="utf-8")
    assert "--worker" in entry
    assert "--mcp" in entry

    from echoact.engine.supervisor import default_worker_command

    command = default_worker_command()
    assert command[-1] in {"--worker", "echoact.engine.worker"}


def test_the_weights_are_not_bundled() -> None:
    """F-09 makes preparation an explicit user action, N-11 says running a
    model locally is not a right to redistribute it, and F-84 resolves
    every download against the manifest at run time."""
    text = SPEC.read_text(encoding="utf-8")
    assert ".onnx" not in _spec_suffixes()
    assert "weights are NOT bundled" in text or "NOT bundled" in text
