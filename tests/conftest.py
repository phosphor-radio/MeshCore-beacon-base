import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def golden():
    """Golden vectors generated from the firmware repository (see fixtures/README.md)."""
    return json.loads((FIXTURES / "beacon_report_v1.json").read_text())
