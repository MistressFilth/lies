from lies.etl.query.pre_translate import StructuredIntent
from lies.etl.query.qmd_syntax import translate


def test_translate_collection_or() -> None:
    intent = StructuredIntent(
        collection_filter=["htmx", "jinja"],
        tag_filter=[],
        exclude_terms=[],
        body="hx syntax",
    )
    assert translate(intent) == "+htmx|jinja hx syntax"


def test_translate_excludes() -> None:
    intent = StructuredIntent(
        collection_filter=[],
        tag_filter=["html"],
        exclude_terms=["javascript"],
        body="forms",
    )
    assert translate(intent) == "+tag:html -javascript forms"


def test_translate_noop_passes_body_through() -> None:
    intent = StructuredIntent(
        collection_filter=[], tag_filter=[], exclude_terms=[], body="hello world"
    )
    assert translate(intent) == "hello world"
