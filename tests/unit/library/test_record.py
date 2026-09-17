from datetime import UTC, datetime

import pytest

from lies.library.record import LibraryCollectionConfig


def test_record_carries_all_kept_fields() -> None:
    cfg = LibraryCollectionConfig(
        name="claude_code",
        source="https://code.claude.com/llms.txt",
        tags=("skills",),
        scraper_cmd=None,
        doc_path=None,
        mapper_model=None,
        language="en",
        version="1",
        created_at=datetime(2026, 9, 13, 22, 17, 29, tzinfo=UTC),
        updated_at=datetime(2026, 9, 13, 22, 17, 29, tzinfo=UTC),
        config={"k": "v"},
    )
    assert cfg.name == "claude_code"
    assert cfg.tags == ("skills",)
    assert cfg.config == {"k": "v"}
    # No path field
    assert not hasattr(cfg, "path")


def test_record_rejects_invalid_name() -> None:
    from lies.library.paths import CollectionNameError

    with pytest.raises(CollectionNameError):
        LibraryCollectionConfig(
            name="bad+name",
            source="x",
            scraper_cmd=None,
            doc_path=None,
            mapper_model=None,
            language=None,
            version="1",
            created_at=datetime.now(tz=UTC),
            updated_at=datetime.now(tz=UTC),
            config={},
        )
