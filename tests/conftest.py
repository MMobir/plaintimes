import os
import sys
import textwrap

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

DEMO = os.path.join(ROOT, "src", "plaintimes", "templates", "demo")
EXAMPLES = os.path.join(ROOT, "examples")

HEAD = """
plaintimes: 1
name: t
currency: MUSD
discount_rate: 0.05
regions: [R1]
periods: {start: 2025, lengths: [1]}
units: {energy: PJ, capacity: GW}
"""


@pytest.fixture
def make_model(tmp_path):
    """Write YAML text (model.yaml plus optional extra files) and return the directory."""

    def _make(text, extra=None, head=HEAD):
        d = tmp_path / f"m{len(list(tmp_path.iterdir()))}"
        d.mkdir()
        (d / "model.yaml").write_text(textwrap.dedent(head) + textwrap.dedent(text))
        for name, body in (extra or {}).items():
            f = d / name
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(textwrap.dedent(body))
        return str(d)

    return _make


@pytest.fixture
def demo_copy(tmp_path):
    import shutil

    dst = tmp_path / "demo"
    shutil.copytree(DEMO, dst)
    return str(dst)
