"""The point-wise summary that goes under a criteria's result (requirement 6.5).

A sibling of `analyst/commentary.py` rather than a caller of it, for one concrete reason:
`commentary.INSTRUCTIONS` says "no bullet lists", and `docs/addtion.md` asks for exactly a
point-wise summary. The two want opposite things from the same plumbing, so they share
`llm.client.run_structured` — and `commentary.render_rows`, which already caps a result to
what a model should be sent.

What it is told to write about is different. Commentary describes a result; remarks
describe an **exception report** — how many rows breached the rule, which ones, and what
stands out about them. A summary that opened by restating the pass rate of a clean run
would be the wrong document. So the rows it is given are the failures only, and the cap is
spent entirely on those rather than shared with rows that passed.

The persona travels in as `knowledge_base`, the argument `analyst/commentary.py` has
carried unused since Stage 6 — requirement 7.2's domain-rules slot, filled a stage early.
"""

import logging
from pathlib import Path

from pydantic import BaseModel, Field

from analyst.commentary import render_rows
from checks.model import FILTER_FAILURES, Check, CheckSet, filter_rows
from llm.client import LLMConnectionError, run_structured

logger = logging.getLogger(__name__)

INSTRUCTIONS = (
    "You summarise the result of one compliance check for a business reader. "
    "Write three to six short bullet points, one fact each, starting with '- '. "
    "Lead with how many records breached the rule and out of how many. "
    "Name the specific records or groups that breached it, quoting the figures exactly as "
    "given — never round, rescale or recompute them, and never mention a value that is not "
    "in the rows provided. "
    "No preamble, no heading, no closing sentence, no recommendation unless the rule itself "
    "implies one."
)


SET_INSTRUCTIONS = (
    "You summarise a whole set of compliance checks for a business reader. "
    "Write three to six short bullet points, one fact each, starting with '- '. "
    "Lead with the overall picture — how many rules were checked and how many records "
    "breached them in total. "
    "Then name the rules that fared worst, quoting the figures exactly as given — never "
    "round, rescale or recompute them, and never mention a rule or a number that is not "
    "listed below. "
    "Do not repeat every rule's detail; this sits above the individual results, not "
    "instead of them. "
    "No preamble, no heading, no closing sentence."
)


class Remarks(BaseModel):
    """The structured shape the remarks call returns."""

    summary: str = Field(description="Three to six bullet points, each on its own line, starting with '- '.")


def render_failures(check: Check) -> str:
    """Renders the failing rows for the prompt, capped in rows and characters.

    The capping is `analyst.commentary.render_rows`; only the two empty states differ,
    because "no rows" and "nothing breached the rule" are different facts here and the
    second is the good outcome.
    """
    run = check.saved_run
    if run is None or run.frame is None:
        return "No rows."

    failures = filter_rows(run.frame, FILTER_FAILURES)
    if failures is None or failures.empty:
        return "No records breached this rule."
    return render_rows(failures)


def build_prompt(persona: str, check: Check) -> str:
    """Assembles the remarks prompt from a saved run."""
    run = check.saved_run
    parts = [f"The rule that was checked:\n{check.criteria_text.strip()}"]

    if run is not None:
        # Counts come from the saved run rather than being recounted from the rows the
        # model is shown, which are capped — otherwise a 500-failure check would be
        # summarised as "40 records breached the rule".
        parts.append(
            f"Outcome: {run.fail_count} of {run.row_count} record(s) breached the rule, "
            f"{run.pass_count} passed."
        )

    parts.append(f"The breaching records:\n{render_failures(check)}")

    if persona and persona.strip():
        parts.append(f"Write as this role, applying this context:\n{persona.strip()}")
    return "\n\n".join(parts)


def write_remarks(
    profile: dict,
    persona: str,
    check: Check,
    *,
    key_path: Path | str | None = None,
) -> tuple[str, list[str]]:
    """Writes the point-wise summary of a criteria's saved run.

    Returns:
        `(remarks, warnings)`. Never raises: the remarks are one part of a report item that
        also carries a table and a chart, and a provider hiccup must not cost the user the
        run they just saved. A failure comes back as an empty string plus a warning, and
        the user can write their own remarks or press the button again.
    """
    if check.saved_run is None:
        return "", ["Save this criteria's results before writing remarks."]

    try:
        result = run_structured(
            profile,
            build_prompt(persona, check),
            Remarks,
            instructions=INSTRUCTIONS,
            # The bullets are the whole answer, so a model that just writes them has still
            # written the remarks.
            text_field="summary",
            key_path=key_path,
        )
    except LLMConnectionError as error:
        logger.warning("Remarks call failed for '%s': %s", check.display_name(), error)
        return "", [f"Couldn't write the remarks: {error}"]

    summary = str(result.summary or "").strip()
    if not summary:
        return "", ["The model returned empty remarks."]
    return summary, []


def build_set_prompt(check_set: CheckSet) -> str:
    """Assembles the whole-set summary prompt from each criteria's saved run.

    Built from the **counts and the per-criteria remarks**, not from the rows. Every saved
    criteria already has a point-wise summary of its own failures written at Save time, so
    sending the rows again would re-send the entire report to summarise a summary — and with
    ten criteria it would blow any context window long before it improved the answer.
    """
    saved = check_set.saved_checks()
    parts = [f"{len(saved)} rule(s) were checked."]

    for check in saved:
        run = check.saved_run
        lines = [
            f"Rule: {check.display_name()}",
            f"Stated as: {check.criteria_text.strip() or 'not stated'}",
            f"Outcome: {run.fail_count} of {run.row_count} record(s) breached it, {run.pass_count} passed.",
        ]
        if check.remarks.strip():
            lines.append(f"Its own summary:\n{check.remarks.strip()}")
        parts.append("\n".join(lines))

    if check_set.persona and check_set.persona.strip():
        parts.append(f"Write as this role, applying this context:\n{check_set.persona.strip()}")
    return "\n\n".join(parts)


def write_set_summary(
    check_set: CheckSet,
    profile: dict,
    *,
    key_path: Path | str | None = None,
) -> tuple[str, list[str]]:
    """Writes the overview that sits under the summary chart.

    Never raises, for the same reason `write_remarks` doesn't: this is one optional
    paragraph beneath a chart that stands on its own, and a provider hiccup must not take
    the Design tab down with it. A failure comes back as an empty string plus a warning, and
    the box below the chart is editable — the user can always write their own.
    """
    if not check_set.saved_checks():
        return "", ["Save at least one criteria's results before writing the summary."]

    try:
        result = run_structured(
            profile,
            build_set_prompt(check_set),
            Remarks,
            instructions=SET_INSTRUCTIONS,
            text_field="summary",
            key_path=key_path,
        )
    except LLMConnectionError as error:
        logger.warning("Set summary call failed for '%s': %s", check_set.display_name(), error)
        return "", [f"Couldn't write the summary: {error}"]

    summary = str(result.summary or "").strip()
    if not summary:
        return "", ["The model returned an empty summary."]
    return summary, []
