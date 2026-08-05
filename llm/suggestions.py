"""Light-Model auto-suggestion of column descriptions (requirement 5.3).

The example from the spec is the whole design brief: `qty` with sample values `5, 12, 3`
should become "Number of units sold per transaction". The column name alone would never
get there — the sample values are what make the suggestion specific — so both go into
the prompt.

Three properties matter more than cleverness here:

- **Chunked.** A 200-column upload becomes eight small calls rather than one enormous
  prompt that a small local model would truncate.
- **Independently failing.** A chunk that fails is logged and skipped; the rest still
  return. Requirement 5.3 has the user reviewing and editing suggestions anyway, so a
  partial fill is genuinely useful while an all-or-nothing failure is not.
- **Never destructive.** This module only ever *returns* suggestions.
  `engine.dictionary.apply_suggestions` decides what to do with them, and by default it
  leaves anything the user has already written alone.
"""

import logging

from pydantic import BaseModel, Field

from llm.client import LLMConnectionError, run_structured

logger = logging.getLogger(__name__)

DEFAULT_CHUNK_SIZE = 25

_INSTRUCTIONS = """\
You write short data-dictionary entries for columns in a business dataset.

For every column you are given, return:
- description: one plain sentence saying what the column holds, in business terms.
  Use the sample values as evidence. Never restate the column name ("The qty column
  holds qty" is useless). Never invent units or currencies the samples don't support.
- synonyms: 2 to 4 other words a colleague might use for this column when asking a
  question about the data. For `qty` that would be quantity, units, units sold, count.

Return an entry for every column you were given, with the table and column names copied
back exactly as provided.\
"""


class ColumnSuggestion(BaseModel):
    """One column's suggested dictionary entry."""

    table: str = Field(..., description="The table name, copied exactly from the input")
    column: str = Field(..., description="The column name, copied exactly from the input")
    description: str = Field(..., description="One plain sentence describing what the column holds")
    synonyms: list[str] = Field(
        default_factory=list, description="2-4 alternative words a user might call this column"
    )


class ColumnSuggestions(BaseModel):
    """The batch a single call returns."""

    suggestions: list[ColumnSuggestion] = Field(default_factory=list)


def build_prompt(entries: list, samples: dict[tuple[str, str], list[str]]) -> str:
    """Renders one chunk of columns into the prompt body."""
    lines = ["Describe each of these columns:", ""]
    for entry in entries:
        sample_values = samples.get(entry.key, [])
        shown = ", ".join(sample_values) if sample_values else "(no sample values available)"
        semantic = f", detected as {entry.semantic_type}" if entry.semantic_type else ""
        lines.append(
            f"- table: {entry.table} | column: {entry.column} "
            f"| type: {entry.sql_type}{semantic} | sample values: {shown}"
        )
    return "\n".join(lines)


def chunk(entries: list, size: int = DEFAULT_CHUNK_SIZE) -> list[list]:
    """Splits the entry list into batches of at most `size`."""
    size = max(1, int(size))
    return [entries[start : start + size] for start in range(0, len(entries), size)]


def suggest_descriptions(
    profile: dict,
    entries: list,
    samples: dict[tuple[str, str], list[str]] | None = None,
    *,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    key_path=None,
) -> tuple[dict[tuple[str, str], ColumnSuggestion], list[str]]:
    """Asks the Light Model to describe every column in `entries`.

    Returns:
        `(suggestions_by_(table, column), warnings)`. The warnings name the chunks that
        failed, so the UI can say "18 of 25 columns described" instead of quietly
        returning less than was asked for.

    Never raises: a failed batch is a degraded result, not a broken screen. The user can
    always type the descriptions themselves, which is the fallback the spec assumes.
    """
    if not entries:
        return {}, []

    samples = samples or {}
    wanted = {entry.key for entry in entries}
    results: dict[tuple[str, str], ColumnSuggestion] = {}
    warnings: list[str] = []

    for index, batch in enumerate(chunk(entries, chunk_size), start=1):
        try:
            response = run_structured(
                profile,
                build_prompt(batch, samples),
                ColumnSuggestions,
                instructions=_INSTRUCTIONS,
                key_path=key_path,
            )
        except LLMConnectionError as error:
            logger.warning("Column-description batch %d failed: %s", index, error)
            warnings.append(f"{len(batch)} column(s) couldn't be described: {error}")
            continue

        for suggestion in response.suggestions:
            key = (suggestion.table, suggestion.column)
            # A model that invents or renames a column would otherwise write an entry
            # that matches nothing and silently go missing; dropping it here makes the
            # count reported back to the user honest.
            if key in wanted:
                results[key] = suggestion
            else:
                logger.info("Ignoring suggestion for unknown column %s.%s.", *key)

    return results, warnings
