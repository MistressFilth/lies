import json
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone

from lies.mcp.daemon import PidRecord


def test_pid_record_is_dataclass() -> None:
    assert is_dataclass(PidRecord)


def test_pid_record_json_round_trip() -> None:
    rec = PidRecord(
        pid=4242,
        host="127.0.0.1",
        port=8765,
        transport="stdio",
        started_at=datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc),
        wiki_root="/tmp/wiki",
        version="0.21.0",
    )
    raw = json.dumps(asdict(rec), default=str)
    revived = PidRecord(**json.loads(raw))
    assert revived.pid == 4242
    assert revived.port == 8765
