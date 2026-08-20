"""What a Task run produced, step by step (requirement 8.2 step 5).

No Streamlit, no database, no DuckDB: `runner/replay.py` fills these in and
`runner/session.py` holds one in session state, and keeping both out is what makes the whole
shape testable without `AppTest`.

Requirement 8.2 step 5 asks for a preview screen "summarizing what succeeded, what needed the
LLM fallback, and what failed". Those three are not degrees of the same thing, they are three
different facts about a step, and the difference is what the user acts on:

- **ok** — the recorded SQL ran. Nothing was asked of a model, so the answer is the same
  question the Task's author saved, and it cost nothing.
- **fallback** — the recorded SQL failed and a regenerated one worked. The report has an
  answer, but it is the model's reading of the request rather than the statement that was
  reviewed and saved, so it is worth a second look before the report goes out.
- **failed** — neither worked. The report has a gap, and the detail says why.
- **skipped** — there was nothing to run. An item saved with no SQL at all, most often
  because it was never generated.

`detail` is always written for the person reading the screen, never a stack trace: this is
the only account of the run they get.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime

from dashboard.model import Report

logger = logging.getLogger(__name__)

# What a step did. See the module docstring — these are four different facts, not a scale.
STATUS_OK = "ok"
STATUS_FALLBACK = "fallback"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"
STATUSES = (STATUS_OK, STATUS_FALLBACK, STATUS_FAILED, STATUS_SKIPPED)

# What kind of thing the step was. Used to group the summary, so a failed column step reads
# as the different sort of problem it is — everything below one was written against it.
KIND_SETUP = "setup"
KIND_COLUMN = "column"
KIND_ITEM = "item"
KIND_CHECK = "check"
KIND_SUMMARY = "summary"

_STATUS_WORDS = {
    STATUS_OK: "Ran from the saved SQL",
    STATUS_FALLBACK: "Needed the AI to rewrite its SQL",
    STATUS_FAILED: "Failed",
    STATUS_SKIPPED: "Skipped",
}


@dataclass
class StepResult:
    """One thing the run tried to do, and what came of it.

    Attributes:
        kind: one of the `KIND_*` constants.
        label: what to call this step on screen — a report item's heading, a criteria's
            name. Taken from the recipe rather than composed here, so the summary names
            things the way the rest of the app does.
        status: one of the `STATUS_*` constants.
        detail: a sentence for the user. Why it failed, what the fallback had to change, or
            what a successful step produced ("124 row(s)"). Never empty for anything that
            didn't simply work.
        notes: anything raised on the way that cost nothing — a comment that couldn't be
            rewritten, a chart that wouldn't draw. Kept apart from `detail` because a step
            with notes still succeeded, and folding them together would make it read as if
            it hadn't.
    """

    kind: str
    label: str
    status: str = STATUS_OK
    detail: str = ""
    notes: list[str] = field(default_factory=list)

    def status_word(self) -> str:
        """The status as a phrase, for a table cell."""
        return _STATUS_WORDS.get(self.status, self.status)

    def needs_attention(self) -> bool:
        """Whether a person should look at this before the report goes out.

        A fallback counts. The report has an answer, but it is one a model wrote just now
        rather than the statement the Task's author reviewed and saved.
        """
        return self.status in (STATUS_FALLBACK, STATUS_FAILED)


@dataclass
class RunResult:
    """Everything one press of Run produced.

    Attributes:
        steps: every step attempted, in the order it was attempted. The order is the
            content: a column step changes what every item below it sees, so a run read out
            of order would not describe what happened.
        report: the Task's report skeleton with this run's results in it. Held here rather
            than only in session state so a test can assert on a run without Streamlit.
        started_at / finished_at: stamped so a summary left on screen can say how old it is.
            A run against a big file takes minutes, and "which upload was this?" is the
            first question anyone asks of a report they didn't just make.
        fatal: set when the run stopped early rather than finishing with failures in it.
            None on any completed run, including one where every step failed — those are two
            different outcomes and the screen words them differently.
    """

    steps: list[StepResult] = field(default_factory=list)
    report: Report = field(default_factory=Report)
    started_at: str = ""
    finished_at: str = ""
    fatal: str | None = None

    def record(self, step: StepResult) -> StepResult:
        """Appends a step and returns it, so a caller can keep adding notes to it."""
        self.steps.append(step)
        return step

    def counts(self) -> dict[str, int]:
        """How many steps landed in each status. Every status is present, including the
        zeroes — a summary reading "0 failed" says something a missing row doesn't."""
        return {status: sum(1 for step in self.steps if step.status == status) for status in STATUSES}

    def failures(self) -> list[StepResult]:
        return [step for step in self.steps if step.status == STATUS_FAILED]

    def fallbacks(self) -> list[StepResult]:
        return [step for step in self.steps if step.status == STATUS_FALLBACK]

    def clean(self) -> bool:
        """Whether the whole run went as recorded — nothing failed and nothing was rewritten.

        The one condition under which the report can be downloaded without reading the
        summary first, which is why it is a single question rather than two.
        """
        return self.fatal is None and not self.failures() and not self.fallbacks()

    def headline(self) -> str:
        """One sentence for the top of the summary screen."""
        if self.fatal:
            return f"The run stopped: {self.fatal}"
        counts = self.counts()
        if self.clean():
            return f"All {len(self.steps)} step(s) ran from the saved recipe."
        parts = []
        if counts[STATUS_OK]:
            parts.append(f"{counts[STATUS_OK]} ran as saved")
        if counts[STATUS_FALLBACK]:
            parts.append(f"{counts[STATUS_FALLBACK]} needed the AI to rewrite its SQL")
        if counts[STATUS_FAILED]:
            parts.append(f"{counts[STATUS_FAILED]} failed")
        if counts[STATUS_SKIPPED]:
            parts.append(f"{counts[STATUS_SKIPPED]} skipped")
        return ", ".join(parts) + "."


def now() -> str:
    """A sortable timestamp for a run. Seconds are enough — this labels a run in the UI."""
    return datetime.now().isoformat(timespec="seconds")
