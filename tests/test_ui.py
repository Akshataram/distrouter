"""The console is static files served by the router. Checks that every file the
page loads exists and parses, that the UI never writes server-supplied text with
innerHTML, and runs the Node unit tests for its pure logic when Node exists."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

UI = Path(__file__).resolve().parent.parent / "swiftserve" / "ui"
JS = sorted((UI / "js").glob("*.js"))


def test_every_module_the_page_and_modules_import_exists():
    html = (UI / "index.html").read_text()
    assert 'src="js/main.js"' in html and 'href="styles.css"' in html
    for f in JS:
        for target in re.findall(r'from "\./([\w.]+)"', f.read_text()):
            assert (UI / "js" / target).exists(), f"{f.name} imports missing ./{target}"


def test_the_ui_never_uses_innerhtml_or_eval():
    for f in JS:
        text = re.sub(r"//.*", "", f.read_text())          # comments may *mention* innerHTML; code must not use it
        assert "innerHTML" not in text and "outerHTML" not in text and "insertAdjacentHTML" not in text and "eval(" not in text, f.name


def test_the_ui_contains_no_baked_in_credentials_or_external_hosts():
    blob = "".join(f.read_text() for f in JS) + (UI / "index.html").read_text()
    assert not re.search(r"https?://(?!www\.w3\.org)", blob), "UI must only talk to its own origin"
    assert "Bearer sk-" not in blob and "SWIFTSERVE_API_TOKEN\"" not in blob.replace('placeholder="SWIFTSERVE_API_TOKEN"', "")


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js not installed")
def test_javascript_syntax_and_store_unit_tests():
    for f in JS:
        subprocess.run(["node", "--check", str(f)], check=True)
    out = subprocess.run(["node", str(Path(__file__).parent / "ui" / "store.test.mjs")], capture_output=True, text=True)
    assert out.returncode == 0, out.stdout + out.stderr
    assert "tests passed" in out.stdout


def test_views_never_call_replace_children_directly():
    """DOM replaceChildren(null) renders the text "null" (seen as a stray "null" in the load-test
    progress panel); views must go through util.fill, which skips null/false."""
    import pathlib

    for f in pathlib.Path("swiftserve/ui/js").glob("*.js"):
        if f.name != "util.js":
            assert "replaceChildren" not in f.read_text(), f.name
