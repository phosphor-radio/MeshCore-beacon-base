"""The operator-facing documents must not point at things that are not there."""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
# docs/plan links were written for the firmware repository's layout (see CLAUDE.md), so only the documents meant to be read here are checked
DOCS = [ROOT / "README.md", ROOT / "CLAUDE.md", ROOT / "docs" / "deployment.md", ROOT / "docs" / "operations.md", ROOT / "docs" / "wire-format.md", ROOT / "docs" / "web.md", ROOT / "docs" / "map-tiles.md"]
LINK = re.compile(r"\]\(([^)\s]+)\)")


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: p.name)
def test_relative_links_resolve(doc):
    broken = []
    for target in LINK.findall(doc.read_text()):
        if target.startswith(("http://", "https://", "mailto:", "#")):
            continue
        path = (doc.parent / target.split("#", 1)[0]).resolve()
        if not path.exists():
            broken.append(target)
    assert not broken, f"{doc.name}: {broken}"


def test_the_guide_names_files_the_bundle_and_repo_have():
    guide = (ROOT / "docs" / "deployment.md").read_text()
    docker = ROOT / "deploy" / "docker"
    for name in ("load-image.sh", "smoke-test.sh", "beaconctl", "build-bundle.sh", ".env.example", "compose.yaml"):
        assert name in guide and (docker / name).exists(), name
    assert (ROOT / "deploy" / "99-beacon-companion.rules.example").exists() and "99-beacon-companion.rules.example" in guide
    bundle_script = (docker / "build-bundle.sh").read_text()
    for name in ("load-image.sh", "smoke-test.sh", "beaconctl", "compose.yaml", "deployment.md", "99-beacon-companion.rules.example"):
        assert name in bundle_script, f"build-bundle.sh does not copy {name}"


def test_the_guide_uses_only_commands_and_paths_that_exist():
    guide = (ROOT / "docs" / "deployment.md").read_text()
    from beacon_base.cli import build_parser

    parser = build_parser()
    for command in ("channel generate", "channel show", "channel set", "autoadd", "status", "check", "health", "rejects", "time", "repeater adverts", "beacon reset"):
        assert f"beaconctl {command}" in guide or f"`{command}`" in guide or f"beaconctl {command.split()[0]}" in guide, command
    # every beaconctl example, in a code block or inline, parses
    examples = re.findall(r"^\s*\./beaconctl ([^#\n]+?)\s*(?:#.*)?$", guide, flags=re.M) + re.findall(r"`\./beaconctl ([^`]+)`", guide)
    assert len(examples) >= 8  # the examples were found
    checked = 0
    for line in examples:
        if line.startswith("<") or "..." in line or line.startswith("repeater remote") or line == "time set":
            continue  # shown with placeholders, or named without its argument
        parser.parse_args(re.sub(r"<[^>]*>", "x", line).split())
        checked += 1
    assert checked >= 6
