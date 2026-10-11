"""The map page's files: complete, self-contained (the Pi has no internet) and safe with names that come from the air."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "src" / "beacon_base" / "static"
OWN = [STATIC / "index.html", STATIC / "app.js", STATIC / "app.css"]


def test_the_page_names_files_that_exist():
    html = (STATIC / "index.html").read_text()
    refs = re.findall(r'(?:src|href)="(static/[^"]+)"', html)
    assert "static/app.js" in refs and "static/vendor/leaflet/leaflet.js" in refs
    for ref in refs:
        assert (STATIC.parent / ref).is_file(), ref


@pytest.mark.parametrize("path", OWN, ids=lambda p: p.name)
def test_nothing_is_loaded_from_another_host(path):
    text = path.read_text()
    for url in re.findall(r"https?://[^\s'\"<>)]+", text):
        assert "www.w3.org/2000/svg" in url, f"{path.name} refers to {url}"  # the xmlns of the inline favicon
    assert "@import" not in text


def test_the_script_never_puts_data_into_markup():
    """Names are untrusted text from the air: the page builds elements and sets textContent, never markup from data."""
    js = (STATIC / "app.js").read_text()
    for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
        assert banned not in js, banned
    # Leaflet turns a string given to a tooltip into markup, so every tooltip is given an element
    assert re.findall(r"(?<!un)bindTooltip\((?!tipEl\()", js) == []
    # the one place markup is made, the repeater's icon, uses a class chosen by the page itself
    assert js.count("html:") == 1 and "'<div class=\"' + cls + '\"></div>'" in js


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_script_parses():
    res = subprocess.run(["node", "--check", str(STATIC / "app.js")], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr


def test_leaflet_is_vendored_with_its_licence_and_provenance():
    leaflet = STATIC / "vendor" / "leaflet"
    for name in ("leaflet.js", "leaflet.css", "LICENSE", "README.md"):
        assert (leaflet / name).is_file(), name
    assert "Leaflet 1.9" in (leaflet / "leaflet.js").read_text()[:200]
    import hashlib

    readme = (leaflet / "README.md").read_text()
    for name in ("leaflet.js", "leaflet.css"):
        assert hashlib.sha256((leaflet / name).read_bytes()).hexdigest() in readme, f"{name} differs from the recorded copy"
