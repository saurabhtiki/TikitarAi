"""Checking an upload against the cleaning template it is about to be cleaned by.

Pure: no Streamlit, no database, no pandas. `cleaner/session.py` reads the working set and
hands the plain (file name, sheet) pairs in; this module says which of the template's
expected files are there, which are missing, and which uploads the template says nothing
about.

`chat_types/matching.py` is deliberately **not** reused. That module imports
`engine.loading` and reports how a DuckDB column converted, which has no meaning here — a
cleaning step runs against a pandas frame, and a template is applied *to* the cleaner
rather than gating a load. The two modules answer different questions about different
things and happen to share a name.

Two rules shape everything here.

**Matching is table-level only.** A template does not check columns, because it does not
have to: `pipeline.apply_steps_with_report` already skips a step whose column has gone and
reports it in the table's own cleaning log, which is where a user is looking when they
wonder why a step didn't run. Duplicating that check here would report the same problem
twice, once in a place the user cannot act on it.

**An extra file is left alone, never discarded.** This is the one place this module parts
company with `chat_types.matching`, which drops what its setup doesn't expect. A chat type
gates a load; a template is applied to files the user chose to upload, and throwing one
away because a saved recipe doesn't mention it would destroy work. So an extra file simply
gets no steps, and is mentioned.
"""

import logging
from dataclasses import dataclass, field

from cleaner.template import CleaningTemplate, normalise, source_key

logger = logging.getLogger(__name__)


@dataclass
class TemplateMatch:
    """Which of a template's expected files the current upload actually has.

    Attributes:
        matched: the template's table name -> the `table_id` it was found as. The whole
            point of the match: `session.apply_template` walks this to know where each
            recipe goes.
        missing: expected file names with nothing uploaded under them. Blocking for those
            files only — the ones that did match are still cleaned.
        extra: loaded tables the template says nothing about. Never discarded.
    """

    matched: dict[str, str] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)
    extra: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """Whether every expected file is here.

        Not "whether anything can be applied": a partial match still cleans what it found,
        which is deliberate. This is what the status word and the warning read.
        """
        return not self.missing

    @property
    def has_notes(self) -> bool:
        return bool(self.extra)

    def status_word(self) -> str:
        """Two words for the bar, which is all there is room for beside the picker."""
        return "matched" if self.ok else "needs attention"

    def problems(self) -> list[str]:
        """The expected files that aren't here, as sentences naming each one."""
        return [
            f"**{name}** — nothing uploaded matches this file, so its saved steps weren't applied."
            for name in self.missing
        ]

    def notes(self) -> list[str]:
        """The differences worth mentioning that stopped nothing."""
        if not self.extra:
            return []
        listed = ", ".join(f"**{name}**" for name in self.extra)
        return [
            f"{len(self.extra)} uploaded file(s) aren't part of this template and were left "
            f"as they are: {listed}."
        ]

    def summary(self) -> str:
        """The one line the bar puts beside the picker."""
        if self.missing:
            return (
                f"{len(self.matched)} of {len(self.matched) + len(self.missing)} expected "
                f"file(s) matched — {len(self.missing)} missing."
            )
        matched = len(self.matched)
        if self.extra:
            return f"{matched} expected file(s) matched, plus {len(self.extra)} not in this template."
        return f"{matched} expected file(s) matched this template exactly."


def check_upload(
    template: CleaningTemplate, loaded: dict[str, tuple[str, str | None]]
) -> TemplateMatch:
    """Builds the whole-upload report.

    Args:
        template: the saved working set the upload is being checked against.
        loaded: every *source* table currently in the cleaner, as
            `table_id -> (file_name, sheet_name)`. Derived tables are the caller's to leave
            out — they have no file of their own, and the template rebuilds them itself.

    Never raises. An upload that doesn't match is a report, not an exception: a user with
    three missing files should see three, not discover them one refusal at a time.
    """
    by_key: dict[str, str] = {}
    duplicates: list[str] = []
    for table_id, (file_name, sheet_name) in loaded.items():
        name = source_key(file_name, sheet_name)
        key = normalise(name)
        if key in by_key:
            # First upload wins. Two files reduce to the same key only when the user has
            # uploaded the same name twice, and silently preferring the later one would move
            # a recipe between two tables that look identical on screen. The loser is still
            # listed as extra rather than passed over in silence — it is a file on screen
            # that nothing is being done to, which is exactly what `extra` means.
            duplicates.append(name)
            continue
        by_key[key] = table_id

    match = TemplateMatch()
    for table in template.tables:
        table_id = by_key.get(table.key())
        if table_id is None:
            match.missing.append(table.name)
        else:
            match.matched[table.name] = table_id

    expected_keys = {table.key() for table in template.tables}
    match.extra = [
        source_key(*loaded[table_id]) for key, table_id in by_key.items() if key not in expected_keys
    ] + duplicates

    if not match.ok:
        logger.info(
            "Upload didn't match cleaning template '%s': %d of %d expected file(s) missing.",
            template.display_name(),
            len(match.missing),
            len(template.tables),
        )
    return match
