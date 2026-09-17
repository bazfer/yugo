"""Controls for the shared in-tree fleet-bus schema."""

import json
from pathlib import Path


SCHEMA_PATH = Path(__file__).resolve().parents[2] / "schema" / "envelope.v1.schema.json"


def test_contract_schema_exists_and_is_a_json_object() -> None:
    assert SCHEMA_PATH.is_file(), f"contract schema is missing: {SCHEMA_PATH}"
    data = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    assert isinstance(data, dict), "contract schema must be a JSON object"
