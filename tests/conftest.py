import json
from pathlib import Path

import pytest

DATA_DIR = Path(__file__).parent / "data"


@pytest.fixture
def sample_item_dict() -> dict:
    return json.loads((DATA_DIR / "sample_item.json").read_text())
