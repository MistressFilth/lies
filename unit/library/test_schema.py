import yaml

from lies.library.schema import ConfigYAML, dump_config_yaml


def _sample() -> dict:
    return {
        "name": "claude_code",
        "source": "https://code.claude.com/llms.txt",
        "tags": ["skills"],
        "scraper_cmd": None,
        "doc_path": None,
        "mapper_model": None,
        "language": None,
        "version": "1",
        "created_at": "2026-09-13T22:17:29.504044+00:00",
        "updated_at": "2026-09-13T22:17:29.504044+00:00",
        "config": {},
        # Legacy field — must be dropped at parse time.
        "path": "/home/divinefilth/.local/share/lies/default/raw/claude_code",
    }


def test_schema_drops_legacy_path_field() -> None:
    parsed = ConfigYAML.model_validate(_sample())
    assert not hasattr(parsed, "path")
    assert parsed.name == "claude_code"


def test_schema_drops_empty_config() -> None:
    payload = _sample()
    payload["config"] = {}
    parsed = ConfigYAML.model_validate(payload)
    assert parsed.config == {}


def test_schema_preserves_non_empty_config() -> None:
    payload = _sample()
    payload["config"] = {"k": "v"}
    parsed = ConfigYAML.model_validate(payload)
    assert parsed.config == {"k": "v"}


def test_dump_round_trips() -> None:
    parsed = ConfigYAML.model_validate(_sample())
    dumped = yaml.safe_load(dump_config_yaml(parsed))
    assert dumped["name"] == "claude_code"
    assert "path" not in dumped
