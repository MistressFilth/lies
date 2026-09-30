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

from lies.mcp.prompts_impl import (
    _parse_question_filters,
    _split_leading_flags,
    _split_tail,
)


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

    def test_equals_form_on_a_boolean_sets_the_flag_and_reports_the_value(self) -> None:
        """``--all=true`` must turn ``all`` on.

        Routing the ``=`` form on a boolean into ``values`` meant no
        prompt body ever saw the flag -- they all read ``booleans``.
        ``reindex --all=true`` silently ran the non-destructive path
        with no note anywhere.
        """
        parsed = _split_tail("--force=yes", value_flags=frozenset({"jobs"}))
        assert parsed.booleans == frozenset({"force"})
        assert parsed.values == {}
        assert parsed.flag_on("force")
        assert parsed.ignored_values == (("force", "yes"),)
        note = parsed.note()
        assert "--force='yes'" in note
        assert "takes no value" in note

    def test_flag_on_covers_both_collections(self) -> None:
        parsed = _split_tail(
            "--jobs 8 --force --tag=x",
            value_flags=frozenset({"jobs", "tag"}),
        )
        assert parsed.flag_on("jobs"), "a value flag that got a value is on"
        assert parsed.flag_on("force"), "a bare boolean is on"
        assert parsed.flag_on("tag"), "a =value flag is on"
        assert not parsed.flag_on("dry-run"), "an absent flag is off"

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
        """A prompt that declares no vocabulary reports nothing.

        Every prompt now declares one, so this pins the parser's
        behavior for a caller that genuinely has no flags.
        """
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

    @pytest.mark.parametrize(
        ("question", "expected_text"),
        [
            # The regression: a library of command-line tooling is full
            # of questions *about* option flags. The sigil-letter rule
            # was not enough -- ``-e``, ``+r`` and ``-v`` are ordinary
            # English in this domain.
            (
                "Explain the -e flag of grep and the +r modifier",
                "Explain the -e flag of grep and the +r modifier",
            ),
            (
                "compare -temperature control with +pressure drop",
                "compare -temperature control with +pressure drop",
            ),
            ("what does -v do", "what does -v do"),
            ("is -p or -q faster", "is -p or -q faster"),
        ],
    )
    def test_a_question_about_option_flags_keeps_its_words(
        self, question: str, expected_text: str
    ) -> None:
        text, tag_expr, excludes = _parse_question_filters(question)
        assert text == expected_text
        assert tag_expr is None
        assert excludes is None

    def test_a_filter_after_the_question_starts_is_question_text(self) -> None:
        """Position is the guard: the filter run is the leading run.

        ``+tag -tag <question>`` is the documented shape. A ``+atom``
        mid-sentence is far more likely to be a word the user wrote.
        """
        text, tag_expr, excludes = _parse_question_filters("why does +c:opencode matter")
        assert text == "why does +c:opencode matter"
        assert tag_expr is None
        assert excludes is None

    def test_leading_run_still_collects_several_filters(self) -> None:
        text, tag_expr, excludes = _parse_question_filters(
            "+c:alpha -t:draft -v2:beta what changed"
        )
        assert text == "what changed"
        assert tag_expr == "c:alpha"
        assert excludes == ["t:draft", "v2:beta"]


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


class TestRepeats:
    """Every occurrence of a value flag survives, in order.

    ``lies library modify --tag a --tag b`` is a real shape: the option
    is declared "Tag to add (repeatable)". ``values`` is last-wins,
    which is right for a scalar and a silent loss for a repeatable one
    — the first tag vanished with no word about it.
    """

    def test_two_occurrences_are_both_kept_in_order(self) -> None:
        parsed = _split_tail("--tag a --tag b", value_flags=frozenset({"tag"}))
        assert parsed.repeats_of("tag") == ("a", "b")
        # Last-wins stays the scalar answer, and keeps the tail's order.
        assert parsed.values["tag"] == "b"

    def test_a_single_occurrence_is_still_reported(self) -> None:
        parsed = _split_tail("--tag a", value_flags=frozenset({"tag"}))
        assert parsed.repeats_of("tag") == ("a",)

    def test_an_unused_flag_has_no_repeats(self) -> None:
        parsed = _split_tail("plain", value_flags=frozenset({"tag"}))
        assert parsed.repeats_of("tag") == ()

    def test_the_equals_form_repeats_too(self) -> None:
        parsed = _split_tail("--tag=a --tag=b", value_flags=frozenset({"tag"}))
        assert parsed.repeats_of("tag") == ("a", "b")


class TestLeadingFlagSplit:
    """``_split_leading_flags`` — flags before the question, text after.

    A tail that is mostly a question must keep its words. Running the
    full grammar over the whole string turned "what is the --only flag"
    into a search for "what is the flag".
    """

    def test_a_leading_flag_is_consumed(self) -> None:
        parsed, question = _split_leading_flags(
            "--top_k 5 what changed",
            value_flags=frozenset({"top_k"}),
            known_flags=frozenset({"top_k"}),
        )
        assert parsed.values["top_k"] == "5"
        assert question == "what changed"

    def test_a_flag_inside_the_question_is_text(self) -> None:
        parsed, question = _split_leading_flags(
            "what is the --only flag",
            value_flags=frozenset({"top_k"}),
            known_flags=frozenset({"top_k"}),
        )
        assert parsed.values == {}
        assert question == "what is the --only flag"

    def test_the_equals_form_in_the_leading_run(self) -> None:
        parsed, question = _split_leading_flags(
            "--top_k=5 what changed",
            value_flags=frozenset({"top_k"}),
            known_flags=frozenset({"top_k"}),
        )
        assert parsed.values["top_k"] == "5"
        assert question == "what changed"

    def test_a_leading_value_flag_with_no_value_is_reported(self) -> None:
        parsed, question = _split_leading_flags(
            "--top_k", value_flags=frozenset({"top_k"}), known_flags=frozenset({"top_k"})
        )
        assert "top_k" in parsed.missing_values
        assert question == ""

    def test_double_dash_says_the_flags_stop(self) -> None:
        parsed, question = _split_leading_flags(
            "-- --top_k 5 what changed",
            value_flags=frozenset({"top_k"}),
            known_flags=frozenset({"top_k"}),
        )
        assert parsed.values == {}
        assert question == "--top_k 5 what changed"

    def test_a_flag_with_no_value_does_not_eat_the_next_flag(self) -> None:
        """``--top_k --force`` reports the missing value and keeps ``--force``.

        Swallowing the neighbouring flag as the missing value would run
        neither flag as asked; the whole leading run stays flags here,
        so the question is empty and the body asks for one.
        """
        parsed, question = _split_leading_flags(
            "--top_k --force",
            value_flags=frozenset({"top_k"}),
            known_flags=frozenset({"top_k", "force"}),
        )
        assert "top_k" in parsed.missing_values
        assert "force" in parsed.booleans
        assert question == ""
