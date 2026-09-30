"""Table-driven coverage for the prompt tail parser.

``_split_tail`` is the load-bearing piece of the single-string prompt
contract and, until this file, had no direct tests: every other test
reached it through a prompt body that asserted on a different
substring, so a parser change could pass the suite while producing a
wrong dispatch. These cases pin the parser itself.

The split is whitespace-only, deliberately. Question tails are prose
and shell flags, not shell command lines; an apostrophe in English
(``what are Claude Code's differences?``) and an unbalanced quote are
ordinary text that must survive to the prompt body rather than raising
``ValueError: No closing quotation`` from a POSIX lexer.
"""

from __future__ import annotations

import pytest

from lies.mcp.prompts_impl import _parse_question_filters, _split_tail


class TestWhitespaceSplitting:
    @pytest.mark.parametrize(
        ("tail", "expected"),
        [
            ("", []),
            ("   ", []),
            ("\n\t ", []),
            ("one", ["one"]),
            ("one two three", ["one", "two", "three"]),
            ("  leading and trailing  ", ["leading", "and", "trailing"]),
            # Runs of internal whitespace collapse to single tokens.
            ("one   two", ["one", "two"]),
        ],
    )
    def test_positionals(self, tail: str, expected: list[str]) -> None:
        parsed = _split_tail(tail)
        assert list(parsed.positionals) == expected

    @pytest.mark.parametrize(
        "tail",
        [
            "what are Claude Code's plugin differences?",
            "why isn't the tool calling the librarian",
            'an unbalanced " quote',
            "a `backtick` and a $dollar",
            "100% of the corpus",
        ],
    )
    def test_ordinary_prose_never_raises(self, tail: str) -> None:
        """Shell metacharacters in prose are text, not syntax.

        A ``shlex``-based splitter raises ``ValueError: No closing
        quotation`` on the first two of these, which FastMCP surfaces
        as a ``PromptError`` -- the prompt fails for the exact English a
        user is most likely to type.
        """
        parsed = _split_tail(tail)
        assert list(parsed.positionals) == tail.split()
        assert parsed.booleans == frozenset()
        assert parsed.missing_values == ()


class TestFlagValues:
    def test_equals_form_binds_a_value(self) -> None:
        parsed = _split_tail("--top_k=7 the question", value_flags=frozenset({"top_k"}))
        assert parsed.values == {"top_k": "7"}
        assert list(parsed.positionals) == ["the", "question"]

    def test_space_form_binds_a_value(self) -> None:
        parsed = _split_tail("--top_k 7 the question", value_flags=frozenset({"top_k"}))
        assert parsed.values == {"top_k": "7"}
        assert list(parsed.positionals) == ["the", "question"]

    def test_value_form_beats_space_form_within_a_tail(self) -> None:
        parsed = _split_tail(
            "--jobs=8 --scraper-timeout 600",
            value_flags=frozenset({"jobs", "scraper-timeout"}),
        )
        assert parsed.values == {"jobs": "8", "scraper-timeout": "600"}
        assert parsed.positionals == ()

    def test_equals_form_binds_even_for_a_boolean_flag(self) -> None:
        """``--flag=value`` is unambiguous regardless of the vocabulary."""
        parsed = _split_tail("--force=yes", value_flags=frozenset({"jobs"}))
        assert parsed.values == {"force": "yes"}
        assert parsed.booleans == frozenset()

    def test_empty_equals_value_is_preserved(self) -> None:
        parsed = _split_tail("--title=", value_flags=frozenset({"title"}))
        assert parsed.values == {"title": ""}
        assert "title" not in parsed.booleans

    def test_a_value_flag_swallows_a_position_that_follows_it(self) -> None:
        parsed = _split_tail("--delete entity-x docs/a.html", value_flags=frozenset({"delete"}))
        assert parsed.values == {"delete": "entity-x"}
        assert list(parsed.positionals) == ["docs/a.html"]


class TestMissingValues:
    def test_value_flag_at_end_of_tail_is_reported(self) -> None:
        parsed = _split_tail("--jobs", value_flags=frozenset({"jobs"}))
        assert "jobs" not in parsed.values
        assert parsed.missing_values == ("jobs",)
        assert "jobs" in parsed.booleans, "the flag was present even though its value was not"

    def test_value_flag_followed_by_a_flag_does_not_eat_it(self) -> None:
        """The regression: ``--jobs --force`` used to set ``jobs='--force'``."""
        parsed = _split_tail(
            "--jobs --force",
            value_flags=frozenset({"jobs"}),
            known_flags=frozenset({"jobs", "force"}),
        )
        assert "jobs" not in parsed.values
        assert parsed.missing_values == ("jobs",)
        assert "force" in parsed.booleans
        assert parsed.unknown == frozenset()

    def test_several_missing_values_are_all_reported(self) -> None:
        parsed = _split_tail(
            "--jobs --scraper-timeout",
            value_flags=frozenset({"jobs", "scraper-timeout"}),
        )
        assert set(parsed.missing_values) == {"jobs", "scraper-timeout"}


class TestBooleanFlags:
    def test_bare_flag_is_boolean(self) -> None:
        parsed = _split_tail("--fix", value_flags=frozenset({"check"}))
        assert parsed.booleans == frozenset({"fix"})
        assert parsed.values == {}

    def test_repeated_flag_keeps_the_last_value(self) -> None:
        parsed = _split_tail("--top_k=3 --top_k=9", value_flags=frozenset({"top_k"}))
        assert parsed.values == {"top_k": "9"}

    def test_single_dash_token_is_positional(self) -> None:
        parsed = _split_tail("-1 -- -x")
        assert list(parsed.positionals) == ["-1", "--", "-x"]


class TestUnknownFlags:
    def test_flag_outside_the_vocabulary_is_recorded(self) -> None:
        parsed = _split_tail(
            "--top_k 3 --cleaup",
            value_flags=frozenset({"top_k"}),
            known_flags=frozenset({"top_k"}),
        )
        assert parsed.unknown == frozenset({"cleaup"})

    def test_unknown_flags_are_not_reported_without_a_vocabulary(self) -> None:
        """``collections`` has no flags, so nothing is ever unknown there."""
        parsed = _split_tail("--anything list")
        assert parsed.unknown == frozenset()

    def test_note_names_both_problem_kinds(self) -> None:
        parsed = _split_tail(
            "--jobs --bogus",
            value_flags=frozenset({"jobs"}),
            known_flags=frozenset({"jobs"}),
        )
        note = parsed.note()
        assert "--jobs needs a value" in note
        assert "--bogus" in note

    def test_note_is_empty_when_the_parse_is_clean(self) -> None:
        parsed = _split_tail("--force", known_flags=frozenset({"force"}))
        assert parsed.note() == ""


class TestQuestionFilterParsing:
    def test_include_and_exclude_are_separated(self) -> None:
        assert _parse_question_filters("+c:alpha -t:draft my question") == (
            "my question",
            "c:alpha",
            ["t:draft"],
        )

    def test_multiple_includes_or_join(self) -> None:
        text, tag_expr, excludes = _parse_question_filters("+c:alpha +c:beta q")
        assert (text, tag_expr, excludes) == ("q", "c:alpha|c:beta", None)

    def test_an_or_expression_within_one_token_survives(self) -> None:
        text, tag_expr, _ = _parse_question_filters("+c:claude_code|c:opencode why")
        assert (text, tag_expr) == ("why", "c:claude_code|c:opencode")

    def test_no_filters_passes_the_question_through(self) -> None:
        assert _parse_question_filters("plain question text") == ("plain question text", None, None)

    @pytest.mark.parametrize(
        ("question", "expected_text"),
        [
            ("why is -1 broken here", "why is -1 broken here"),
            ("what is a -- b", "what is a -- b"),
            ("compare - and + operators", "compare - and + operators"),
            ("a lone -", "a lone -"),
            ("the -3 to -1 range", "the -3 to -1 range"),
        ],
    )
    def test_prose_that_looks_like_a_flag_stays_in_the_text(
        self, question: str, expected_text: str
    ) -> None:
        """A token is a filter only when a letter follows the sigil.

        ``-1`` and a bare ``--`` are far likelier in prose than a real
        exclude tag; treating them as tags silently deleted the text.
        """
        text, tag_expr, excludes = _parse_question_filters(question)
        assert text == expected_text
        assert tag_expr is None
        assert excludes is None

    def test_apostrophes_survive_filter_parsing(self) -> None:
        text, tag_expr, excludes = _parse_question_filters("+c:opencode what isn't the difference?")
        assert text == "what isn't the difference?"
        assert tag_expr == "c:opencode"
        assert excludes is None


class TestMultiWordFlags:
    def test_multi_word_flag_consumes_up_to_the_next_flag(self) -> None:
        parsed = _split_tail(
            "--title Pydantic basics --type concept",
            value_flags=frozenset({"type"}),
            multi_word_flags=frozenset({"title"}),
        )
        assert parsed.values["title"] == "Pydantic basics"
        assert parsed.values["type"] == "concept"
        assert parsed.positionals == ()

    def test_multi_word_flag_at_end_of_tail_consumes_what_is_there(self) -> None:
        """Regression: ``--title two words`` bound ``title='two'`` and
        dropped ``words`` entirely, with nothing in the body saying so."""
        parsed = _split_tail(
            "--title two words",
            value_flags=frozenset({"type"}),
            multi_word_flags=frozenset({"title"}),
        )
        assert parsed.values["title"] == "two words"
        assert parsed.positionals == ()

    def test_scalar_flag_still_takes_exactly_one_token(self) -> None:
        """The multi-word rule is opt-in. ``--jobs 8 pydantic`` means
        ``jobs=8`` and a positional ``pydantic``."""
        parsed = _split_tail(
            "--jobs 8 pydantic",
            value_flags=frozenset({"jobs"}),
            multi_word_flags=frozenset({"title"}),
        )
        assert parsed.values == {"jobs": "8"}
        assert list(parsed.positionals) == ["pydantic"]

    def test_multi_word_flag_with_no_value_is_reported(self) -> None:
        parsed = _split_tail(
            "--title --type concept",
            value_flags=frozenset({"type"}),
            multi_word_flags=frozenset({"title"}),
        )
        assert parsed.missing_values == ("title",)
        assert parsed.values == {"type": "concept"}
