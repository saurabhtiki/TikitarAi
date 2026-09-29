"""Question steps: reading their rules and checking answers against them (phase 50).

No Streamlit, no database and no model here. The model's only job on a question turn is to
*convert* what the invitee typed ("45k" -> "45000"); whether that passes the rule is decided
here, in plain Python, so "the bot moved on with a wrong answer" can't be a model's mistake.

A rule is a short phrase the organiser types in the agenda grid:

    Number   > 0 | >= 1000 | < 90 | <= 90 | between 20000 and 60000
    Date     future | past | after 01-10-2026 | before 31-12-2026 | within 30 days
    Text     at least 20 characters
    Choice   Morning, Evening, Night          (required: the options themselves)
    Yes/No   (no rule)

A blank rule means "any answer of that type".

A question can also say where to go next (phase 51), in its Go to cell:

    if > 60000 go to End; if No go to Notice period

The condition uses the same words as a rule (Yes/No takes `Yes` or `No`, Choice takes one or
more of its options). Jumps only go forward, so a path always ends.

Questions with a For each list (phase 52) are asked once per row of that list, so a path is
made of **steps**: a question, plus the row it is about. Inside a list, Go to can also say
`Next row`. A row's answer is stored under `answer_key(title, row)`.
"""

import datetime
import logging
import re
from dataclasses import dataclass, replace

from meetings.model import (
    ANSWER_CHOICE,
    ANSWER_DATE,
    ANSWER_NUMBER,
    ANSWER_TEXT,
    ANSWER_YES_NO,
    STEP_ANSWERED,
    STEP_NOT_ANSWERED,
    STEP_PENDING,
    AgendaItem,
    Meeting,
    StepAnswer,
)

logger = logging.getLogger(__name__)

ANSWER_TYPE_LABELS = {
    ANSWER_TEXT: "Text",
    ANSWER_NUMBER: "Number",
    ANSWER_DATE: "Date",
    ANSWER_YES_NO: "Yes/No",
    ANSWER_CHOICE: "Choice",
}
ANSWER_TYPE_BY_LABEL = {label: answer_type for answer_type, label in ANSWER_TYPE_LABELS.items()}

_DATE_FORMATS = ("%d-%m-%Y", "%Y-%m-%d", "%d/%m/%Y")
_NUMBER = r"(-?[\d,]*\.?\d+)"
_COMPARE_RULE = re.compile(rf"^(>=|<=|>|<)\s*{_NUMBER}$")
_BETWEEN_RULE = re.compile(rf"^between\s+{_NUMBER}\s+and\s+{_NUMBER}$")
_DATE_EDGE_RULE = re.compile(r"^(after|before)\s+(\S+)$")
_WITHIN_RULE = re.compile(r"^within\s+(\d+)\s+days?$")
_MIN_LENGTH_RULE = re.compile(r"^at least\s+(\d+)\s+characters?$")

_YES_WORDS = {"yes", "y", "true", "yeah", "yep", "haan", "ha"}
_NO_WORDS = {"no", "n", "false", "nope", "nahi"}

_RULE_EXAMPLES = {
    ANSWER_NUMBER: "For a number, write it like `> 5000` or `between 20000 and 60000`.",
    ANSWER_DATE: "For a date, write `future`, `past`, `after 01-10-2026`, `before 31-12-2026` or `within 30 days`.",
    ANSWER_TEXT: "For text, write `at least 20 characters`, or leave it blank.",
    ANSWER_CHOICE: "For a choice, list the options with commas, like `Morning, Evening, Night`.",
    ANSWER_YES_NO: "A Yes/No question needs no rule — leave it blank.",
}


END_TARGET = "End"
NEXT_ROW_TARGET = "Next row"
SKIPPED_CELL = "— skipped"
# Between a question's title and its row number in a stored answer key. A control character,
# so no title typed into a grid cell can collide with a row key.
ROW_KEY_SEPARATOR = "\x1f"
_BRANCH_LINE = re.compile(r"^if\s+(.+?)\s+go\s*to\s+(.+)$", re.IGNORECASE)
_BRANCH_EXAMPLE = "Write it like `if > 60000 go to End` or `if No go to Notice period`."


class RuleError(ValueError):
    """A rule the organiser typed that this module can't read."""


@dataclass(frozen=True)
class Branch:
    """One Go to line: when the answer passes `condition`, jump to `target` (a title or End)."""

    condition: str
    target: str


@dataclass
class Step:
    """One thing to ask: a question, and the list row it is about (None for an ordinary one).

    `order` is the question's position among the question items, which is what "forward"
    means for Go to.
    """

    question: AgendaItem
    row: int | None = None
    order: int = 0

    @property
    def item(self) -> str:
        return self.question.item

    @property
    def key(self) -> str:
        return answer_key(self.question.item, self.row)


@dataclass
class Walk:
    """An invitee's way through the questions so far: asked (the open one last) and jumped over."""

    path: list[Step]
    skipped: list[Step]
    finished: bool

    @property
    def current(self) -> Step | None:
        return None if self.finished or not self.path else self.path[-1]


@dataclass
class AnswerCheck:
    """The verdict on one converted answer: `value` is the cleaned form that gets stored."""

    ok: bool
    value: str = ""
    reason: str = ""


def answer_key(title: str, row: int | None = None) -> str:
    """Where one answer is stored: the title alone, or the title and the list row."""
    return title if row is None else f"{title}{ROW_KEY_SEPARATOR}{row}"


def row_key_prefix(title: str) -> str:
    """The start every per-row answer key of this question shares."""
    return f"{title}{ROW_KEY_SEPARATOR}"


def loop_key(name: str) -> str:
    """A For each name compared the way the organiser means it: case and spacing ignored."""
    return " ".join(str(name or "").split()).lower()


def loop_names(meeting: Meeting) -> list[str]:
    """The meeting's For each lists, in agenda order, spelt as first written."""
    names: dict[str, str] = {}
    for item in meeting.question_items():
        if item.loop.strip():
            names.setdefault(loop_key(item.loop), " ".join(item.loop.split()))
    return list(names.values())


def loop_questions(meeting: Meeting, name: str) -> list[AgendaItem]:
    """The questions asked for each row of one list, in order."""
    return [item for item in meeting.question_items() if item.loop and loop_key(item.loop) == loop_key(name)]


def tidy_loop_names(agenda: list[AgendaItem]) -> list[AgendaItem]:
    """The agenda with every For each spelt like its first mention, so "outstanding invoices"
    and "Outstanding  Invoices" are one list. Non-question rows lose their For each."""
    first: dict[str, str] = {}
    tidied = []
    for item in agenda:
        if not item.is_question() or not item.loop.strip():
            tidied.append(replace(item, loop=""))
            continue
        name = first.setdefault(loop_key(item.loop), " ".join(item.loop.split()))
        tidied.append(replace(item, loop=name))
    return tidied


def _to_number(text: str) -> float:
    return float(str(text).replace(",", "").replace(" ", ""))


def _format_number(number: float) -> str:
    return str(int(number)) if number == int(number) else f"{number:g}"


def _to_date(text: str) -> datetime.date:
    cleaned = str(text).strip()
    for date_format in _DATE_FORMATS:
        try:
            return datetime.datetime.strptime(cleaned, date_format).date()
        except ValueError:
            continue
    raise ValueError(f"'{text}' is not a date")


def _format_date(value: datetime.date) -> str:
    return value.strftime("%d-%m-%Y")


def choice_options(rule: str) -> list[str]:
    return [option.strip() for option in str(rule or "").split(",") if option.strip()]


def _parse_rule(item: AgendaItem) -> tuple | None:
    """The rule as `(kind, *arguments)`, or None for "any answer of this type".

    Raises:
        RuleError: naming the problem in a sentence the organiser can act on.
    """
    rule = " ".join(str(item.rule or "").split())
    lowered = rule.lower()
    answer_type = item.answer_type

    if answer_type == ANSWER_CHOICE:
        options = choice_options(rule)
        if len(options) < 2:
            raise RuleError("a Choice question needs at least two options, separated by commas.")
        return ("options", options)

    if not rule:
        return None

    try:
        if answer_type == ANSWER_NUMBER:
            if match := _COMPARE_RULE.match(lowered):
                return ("compare", match.group(1), _to_number(match.group(2)))
            if match := _BETWEEN_RULE.match(lowered):
                low, high = sorted((_to_number(match.group(1)), _to_number(match.group(2))))
                return ("between", low, high)
        elif answer_type == ANSWER_DATE:
            if lowered in ("future", "past"):
                return (lowered,)
            if match := _DATE_EDGE_RULE.match(lowered):
                return (match.group(1), _to_date(match.group(2)))
            if match := _WITHIN_RULE.match(lowered):
                return ("within", int(match.group(1)))
        elif answer_type == ANSWER_TEXT:
            if match := _MIN_LENGTH_RULE.match(lowered):
                return ("min_length", int(match.group(1)))
    except ValueError as error:
        logger.info("Rule '%s' on '%s' did not parse: %s", rule, item.item, error)

    raise RuleError(f"'{rule}' isn't a rule I understand. {_RULE_EXAMPLES[answer_type]}")


def rule_problem(item: AgendaItem) -> str:
    """A sentence saying why this question's rule can't be used, or "" when it is fine."""
    if not item.is_question():
        return ""
    try:
        _parse_rule(item)
    except RuleError as error:
        return f"{item.item}: {error}"
    return ""


def _yes_no_word(text: str) -> str:
    """ "Yes", "No", or "" for anything else."""
    word = str(text).lower().strip(".! ")
    if word in _YES_WORDS:
        return "Yes"
    if word in _NO_WORDS:
        return "No"
    return ""


def _check_condition(item: AgendaItem, condition: str) -> None:
    """Raises RuleError if `condition` can't be tested against this question's answers."""
    if item.answer_type == ANSWER_YES_NO:
        if not _yes_no_word(condition):
            raise RuleError(f"'{condition}' should be Yes or No.")
    elif item.answer_type == ANSWER_CHOICE:
        known = {option.lower() for option in choice_options(item.rule)}
        for option in choice_options(condition):
            if option.lower() not in known:
                raise RuleError(
                    f"'{option}' isn't one of this question's options ({', '.join(choice_options(item.rule))})."
                )
    else:
        _parse_rule(replace(item, rule=condition))


def parse_branches(item: AgendaItem, agenda: list[AgendaItem]) -> list[Branch]:
    """The question's Go to lines, with each target spelt as its agenda title.

    Raises:
        RuleError: naming the first line that can't be used.
    """
    raw = str(item.branch or "").strip()
    if not raw or not item.is_question():
        return []
    if item.answer_type == ANSWER_TEXT:
        raise RuleError("a Text question can't branch — use Yes/No, Choice, Number or Date.")

    questions = [question for question in agenda if question.is_question()]
    position = next((index for index, question in enumerate(questions) if question.item == item.item), -1)
    own_loop = loop_key(item.loop)
    later: dict[str, str] = {}
    # Later questions in the middle of a list, mapped to that list's first question.
    middle: dict[str, AgendaItem] = {}
    first_of_block = None
    for index, question in enumerate(questions):
        if question.loop and (index == 0 or loop_key(questions[index - 1].loop) != loop_key(question.loop)):
            first_of_block = question
        if index <= position:
            continue
        name = question.item.strip().lower()
        if own_loop:
            if loop_key(question.loop) == own_loop:
                later[name] = question.item
        elif question.loop and first_of_block is not question:
            middle[name] = first_of_block
        else:
            later[name] = question.item

    branches = []
    for piece in re.split(r"[;\n]", raw):
        line = " ".join(piece.split())
        if not line:
            continue
        match = _BRANCH_LINE.match(line)
        if not match:
            raise RuleError(f"'{line}' isn't a Go to I understand. {_BRANCH_EXAMPLE}")
        condition = match.group(1).strip()
        target = match.group(2).strip().strip("'\"")
        _check_condition(item, condition)
        if target.lower() == END_TARGET.lower():
            target = END_TARGET
        elif " ".join(target.lower().split()) == NEXT_ROW_TARGET.lower():
            if not own_loop:
                raise RuleError("Next row only works on a question with a For each list.")
            target = NEXT_ROW_TARGET
        elif target.lower() in later:
            target = later[target.lower()]
        elif own_loop:
            raise RuleError(
                f"'{target}' isn't a later question of the list '{item.loop}'. Inside a For each, Go to "
                "can jump to a later question of the same list, Next row, or End."
            )
        elif target.lower() in middle:
            first = middle[target.lower()]
            raise RuleError(
                f"'{target}' is in the middle of the For each list '{first.loop}' — jump to its first "
                f"question, '{first.item}', instead."
            )
        else:
            raise RuleError(
                f"'{target}' isn't a later question. Go to can only jump forward to a Question row, or End."
            )
        branches.append(Branch(condition=condition, target=target))
    return branches


def branch_problem(item: AgendaItem, agenda: list[AgendaItem]) -> str:
    """A sentence saying why this question's Go to can't be used, or "" when it is fine."""
    try:
        parse_branches(item, agenda)
    except RuleError as error:
        return f"{item.item}: {error}"
    return ""


def loop_problems(agenda: list[AgendaItem]) -> list[str]:
    """Why the For each lists can't be used: split up, or named like an agenda item."""
    questions = [item for item in agenda if item.is_question()]
    titles = {item.item.strip().lower() for item in agenda}
    problems = []
    last_seen: dict[str, int] = {}
    for index, item in enumerate(questions):
        key = loop_key(item.loop)
        if not key:
            continue
        if key in last_seen and last_seen[key] != index - 1:
            problems.append(
                f"{item.item}: the questions for the list '{item.loop}' must sit together, one after another."
            )
        last_seen[key] = index
    for name in {loop_key(item.loop): item.loop for item in questions if item.loop.strip()}.values():
        if loop_key(name) in titles:
            problems.append(
                f"'{name}' is both a For each list and an agenda item title — give the list its own name."
            )
    return problems


def agenda_problems(agenda: list[AgendaItem]) -> list[str]:
    """Why an agenda can't be saved yet: unreadable rules and Go to, split lists, repeated titles.

    Repeated titles matter because answers, tables and tags are all keyed by title — two
    items called "Salary" would share one answer.
    """
    problems = [problem for problem in (rule_problem(item) for item in agenda) if problem]
    problems.extend(problem for problem in (branch_problem(item, agenda) for item in agenda) if problem)
    problems.extend(loop_problems(agenda))
    titles = [item.item.strip().lower() for item in agenda]
    repeated = sorted({item.item for item in agenda if titles.count(item.item.strip().lower()) > 1})
    problems.extend(f"'{title}' is on the agenda twice — give each item its own title." for title in repeated)
    return problems


@dataclass(frozen=True)
class _Interval:
    """The numbers a Number rule (or Go to condition) lets through."""

    low: float = float("-inf")
    high: float = float("inf")
    low_included: bool = False
    high_included: bool = False

    def holds(self, number: float) -> bool:
        above = number > self.low or (self.low_included and number == self.low)
        below = number < self.high or (self.high_included and number == self.high)
        return above and below

    def covers(self, other: "_Interval") -> bool:
        """Whether every number `other` lets through is let through here too."""
        low_ok = other.low > self.low or (other.low == self.low and (self.low_included or not other.low_included))
        high_ok = other.high < self.high or (
            other.high == self.high and (self.high_included or not other.high_included)
        )
        return low_ok and high_ok

    def meets(self, other: "_Interval") -> bool:
        """Whether some number is let through by both."""
        low, high = max(self.low, other.low), min(self.high, other.high)
        if low < high:
            return True
        return low == high and self.holds(low) and other.holds(low)


def _number_interval(parsed: tuple | None) -> _Interval | None:
    """A parsed Number rule as an `_Interval`, or None for "any number"."""
    if not parsed:
        return None
    if parsed[0] == "between":
        return _Interval(parsed[1], parsed[2], True, True)
    if parsed[0] == "compare":
        operator, limit = parsed[1], parsed[2]
        return {
            ">": _Interval(low=limit),
            ">=": _Interval(low=limit, low_included=True),
            "<": _Interval(high=limit),
            "<=": _Interval(high=limit, high_included=True),
        }[operator]
    return None


def _range_words(interval: _Interval) -> str:
    if interval.low != float("-inf") and interval.high != float("inf"):
        return f"{_format_number(interval.low)} to {_format_number(interval.high)}"
    if interval.low != float("-inf"):
        return f"{'at least' if interval.low_included else 'more than'} {_format_number(interval.low)}"
    return f"{'at most' if interval.high_included else 'less than'} {_format_number(interval.high)}"


def agenda_warnings(agenda: list[AgendaItem]) -> list[str]:
    """Notes (not errors) about a Number question whose Go to reaches past its Rule (phase 57).

    The Rule is checked first, so an answer it refuses never gets to the Go to. With Rule
    `between 10000 and 500000` and Go to `if > 100000 go to End`, 700000 is refused — which
    surprises an organiser who expected it to end the questions.
    """
    warnings = []
    for item in agenda:
        if not item.is_question() or item.answer_type != ANSWER_NUMBER or not item.branch.strip():
            continue
        try:
            rule = _number_interval(_parse_rule(item))
            branches = parse_branches(item, agenda)
        except RuleError:
            continue  # `agenda_problems` already reports it.
        if rule is None:
            continue
        for branch in branches:
            try:
                condition = _number_interval(_parse_rule(replace(item, rule=branch.condition)))
            except RuleError:
                continue
            if condition is None or rule.covers(condition):
                continue
            if not rule.meets(condition):
                warnings.append(
                    f"{item.item}: 'if {branch.condition}' can never happen — the Rule only accepts "
                    f"{_range_words(rule)}."
                )
            else:
                warnings.append(
                    f"{item.item}: 'if {branch.condition}' also covers numbers the Rule refuses (it only "
                    f"accepts {_range_words(rule)}). Widen the Rule if bigger or smaller answers are fine."
                )
    return warnings


def describe_rule(item: AgendaItem) -> str:
    """The expected answer in plain words, e.g. "a number between 20000 and 60000"."""
    try:
        parsed = _parse_rule(item)
    except RuleError:
        parsed = None

    if item.answer_type == ANSWER_NUMBER:
        base = "a number"
        if parsed and parsed[0] == "compare":
            words = {">": "more than", ">=": "at least", "<": "less than", "<=": "at most"}
            return f"{base} {words[parsed[1]]} {_format_number(parsed[2])}"
        if parsed and parsed[0] == "between":
            return f"{base} between {_format_number(parsed[1])} and {_format_number(parsed[2])}"
        return base
    if item.answer_type == ANSWER_DATE:
        base = "a date (dd-mm-yyyy)"
        if parsed and parsed[0] in ("future", "past"):
            return f"{base} in the {parsed[0]}"
        if parsed and parsed[0] in ("after", "before"):
            return f"{base} {parsed[0]} {_format_date(parsed[1])}"
        if parsed and parsed[0] == "within":
            return f"{base} within the next {parsed[1]} days"
        return base
    if item.answer_type == ANSWER_YES_NO:
        return "yes or no"
    if item.answer_type == ANSWER_CHOICE:
        return "one of: " + ", ".join(choice_options(item.rule))
    if parsed and parsed[0] == "min_length":
        return f"a text answer of at least {parsed[1]} characters"
    return "a text answer"


def check_answer(item: AgendaItem, value: str, today: datetime.date) -> AnswerCheck:
    """Whether a converted answer is of the right type and passes the rule.

    `reason` is written to be read out to the invitee, so it says what is expected rather
    than what went wrong inside.
    """
    text = str(value or "").strip()
    expected = describe_rule(item)
    refusal = AnswerCheck(ok=False, reason=f"The answer needs to be {expected}.")
    if not text:
        return refusal

    try:
        parsed = _parse_rule(item)
    except RuleError:
        # Save-time checks stop this, but a hand-edited agenda must not trap the invitee.
        logger.warning("Question '%s' has an unreadable rule; accepting any answer.", item.item)
        parsed = None

    answer_type = item.answer_type
    if answer_type == ANSWER_NUMBER:
        try:
            number = _to_number(text)
        except ValueError:
            return refusal
        interval = _number_interval(parsed)
        if interval is not None and not interval.holds(number):
            # Saying which side it missed on helps: "700000 is too high" beats a bare range.
            side = "too high" if number > interval.low else "too low"
            return AnswerCheck(ok=False, reason=f"The answer needs to be {expected} — {_format_number(number)} is {side}.")
        return AnswerCheck(ok=True, value=_format_number(number))

    if answer_type == ANSWER_DATE:
        try:
            answer_date = _to_date(text)
        except ValueError:
            return refusal
        if parsed:
            kind = parsed[0]
            passes = (
                (kind == "future" and answer_date > today)
                or (kind == "past" and answer_date < today)
                or (kind == "after" and answer_date > parsed[1])
                or (kind == "before" and answer_date < parsed[1])
                or (kind == "within" and today <= answer_date <= today + datetime.timedelta(days=parsed[1]))
            )
            if not passes:
                return refusal
        return AnswerCheck(ok=True, value=_format_date(answer_date))

    if answer_type == ANSWER_YES_NO:
        word = _yes_no_word(text)
        return AnswerCheck(ok=True, value=word) if word else refusal

    if answer_type == ANSWER_CHOICE:
        for option in choice_options(item.rule):
            if option.lower() == text.lower():
                return AnswerCheck(ok=True, value=option)
        return refusal

    if parsed and parsed[0] == "min_length" and len(text) < parsed[1]:
        return refusal
    return AnswerCheck(ok=True, value=text)


def record_attempt(
    item: AgendaItem, previous: StepAnswer | None, check: AnswerCheck, reply: str, key: str = ""
) -> StepAnswer:
    """The question's state after one real attempt at answering it.

    Every attempt uses a try, the passing one included, so the organiser sees "answered on
    the 2nd try". The last failed try marks it Not answered so nobody is stuck forever.
    `key` is where it is stored (see `answer_key`); it defaults to the title.
    """
    current = previous or StepAnswer(item_ref=key or item.item)
    tries = current.tries + 1
    # The previous try's time is dropped: this attempt is "now" until it is saved, so the
    # turn judges a date Go to on the same day the reloaded page will (see `_answer_day`).
    if check.ok:
        return replace(
            current, value=check.value, status=STEP_ANSWERED, tries=tries, last_reply=reply, updated_at=""
        )
    status = STEP_NOT_ANSWERED if tries >= item.max_tries else STEP_PENDING
    return replace(current, value="", status=status, tries=tries, last_reply=reply, updated_at="")


def _answer_day(answer: StepAnswer) -> datetime.date:
    """The local day the answer was saved, so a date condition like "within 30 days" is
    judged once. Not saved yet (no time) means today.

    Judging it against today instead would let the path change weeks later, and a question
    that was asked could suddenly look skipped. The stored time is UTC; it is turned into
    the local day because `check_answer` is given the local today while the invitee answers
    — without that, an answer saved at 2 am in India would be judged a day earlier on reload.
    """
    stamp = str(answer.updated_at or "").strip()
    if not stamp:
        return datetime.date.today()
    try:
        saved = datetime.datetime.strptime(stamp[:19], "%Y-%m-%d %H:%M:%S")
        return saved.replace(tzinfo=datetime.timezone.utc).astimezone().date()
    except ValueError:
        pass
    try:
        return datetime.date.fromisoformat(stamp[:10])
    except ValueError:
        return datetime.date.today()


def _condition_matches(item: AgendaItem, condition: str, value: str, day: datetime.date) -> bool:
    if item.answer_type == ANSWER_YES_NO:
        return _yes_no_word(condition) == value
    if item.answer_type == ANSWER_CHOICE:
        return value.lower() in {option.lower() for option in choice_options(condition)}
    return check_answer(replace(item, rule=condition), value, day).ok


def branch_target(item: AgendaItem, answer: StepAnswer | None, agenda: list[AgendaItem]) -> str | None:
    """Where this answer jumps to (a question title or `END_TARGET`), or None to go on in order.

    Only an accepted answer branches: a question marked Not answered has no value to test.
    """
    if answer is None or answer.status != STEP_ANSWERED or not item.branch.strip():
        return None
    try:
        branches = parse_branches(item, agenda)
    except RuleError:
        # Save-time checks stop this, but a hand-edited agenda must not trap the invitee.
        logger.warning("Question '%s' has an unreadable Go to; going on in order.", item.item)
        return None
    day = _answer_day(answer)
    for branch in branches:
        if _condition_matches(item, branch.condition, answer.value, day):
            return branch.target
    return None


def _flat_steps(meeting: Meeting, loop_rows: dict[str, list[int]] | None) -> list[Step]:
    """Every step in agenda order, with each list's questions repeated for each of its rows.

    A list with no rows (none attached, or none matching this invitee) has no steps, so its
    questions are simply not asked.
    """
    questions = meeting.question_items()
    flat: list[Step] = []
    index = 0
    while index < len(questions):
        key = loop_key(questions[index].loop)
        if not key:
            flat.append(Step(questions[index], None, index))
            index += 1
            continue
        end = index
        while end < len(questions) and loop_key(questions[end].loop) == key:
            end += 1
        for row in (loop_rows or {}).get(key, []):
            flat.extend(Step(questions[order], row, order) for order in range(index, end))
        index = end
    return flat


def _same_row(first: Step, second: Step) -> bool:
    return first.row == second.row and loop_key(first.question.loop) == loop_key(second.question.loop)


def walk(meeting: Meeting, answers: dict[str, StepAnswer], loop_rows: dict[str, list[int]] | None = None) -> Walk:
    """Goes through the steps from the first, following Go to, until an open one or the end.

    `loop_rows` maps each list (by `loop_key`) to this invitee's row numbers. Worked out from
    the saved answers every time rather than stored, so there is no second record of the path
    that could disagree with the answers. Every jump moves forward, so this always ends.
    """
    flat = _flat_steps(meeting, loop_rows)
    orders = {item.item: order for order, item in enumerate(meeting.question_items())}
    path: list[Step] = []
    skipped: list[Step] = []
    index = 0
    while index < len(flat):
        step = flat[index]
        path.append(step)
        answer = answers.get(step.key)
        if answer is None or not answer.is_finished():
            return Walk(path, skipped, finished=False)
        target = branch_target(step.question, answer, meeting.agenda)
        following = index + 1
        if target == END_TARGET:
            following = len(flat)
        elif target == NEXT_ROW_TARGET:
            while following < len(flat) and _same_row(flat[following], step):
                following += 1
        elif target is not None:
            wanted = orders.get(target, len(orders))
            while following < len(flat) and flat[following].order < wanted:
                following += 1
        skipped.extend(flat[index + 1 : following])
        index = following
    return Walk(path, skipped, finished=True)


def question_path(
    meeting: Meeting, answers: dict[str, StepAnswer], loop_rows: dict[str, list[int]] | None = None
) -> list[Step]:
    """The steps this invitee has been asked so far, the open one last."""
    return walk(meeting, answers, loop_rows).path


def next_question(
    meeting: Meeting, answers: dict[str, StepAnswer], loop_rows: dict[str, list[int]] | None = None
) -> Step | None:
    """The open step on this invitee's path, or None once the questions are done."""
    return walk(meeting, answers, loop_rows).current


def questions_left(
    meeting: Meeting, answers: dict[str, StepAnswer], loop_rows: dict[str, list[int]] | None = None
) -> int:
    """How many questions are still open on this invitee's path; 0 once they may close (phase 56).

    Counts the open one and every unfinished step after it. A later Go to may still skip some,
    so this is "at most", which is what a "3 left" note should say anyway.
    """
    current = next_question(meeting, answers, loop_rows)
    if current is None:
        return 0
    flat = _flat_steps(meeting, loop_rows)
    start = next((index for index, step in enumerate(flat) if step.key == current.key), 0)
    return sum(1 for step in flat[start:] if not (step.key in answers and answers[step.key].is_finished()))


def skipped_questions(
    meeting: Meeting, answers: dict[str, StepAnswer], loop_rows: dict[str, list[int]] | None = None
) -> list[str]:
    """Titles of the ordinary (not For each) questions a Go to jumped over, so never asked."""
    return [step.item for step in walk(meeting, answers, loop_rows).skipped if step.row is None]


def progress_text(
    meeting: Meeting,
    current: Step,
    answers: dict[str, StepAnswer],
    loop_rows: dict[str, list[int]] | None = None,
    row_label: str = "",
) -> str:
    """ "Question 3 of 4" along this invitee's path, or "Invoices: row 2 of 12, question 1 of 3".

    `row_label` names the row (phase 57), giving "Invoices — INV-102: row 2 of 12, question 1 of 3".

    For an ordinary question the "of" is the ordinary questions asked so far plus the ones
    still ahead in agenda order, so it can drop after a jump.
    """
    if current.row is not None:
        rows = (loop_rows or {}).get(loop_key(current.question.loop), [])
        row_number = rows.index(current.row) + 1 if current.row in rows else 1
        block = loop_questions(meeting, current.question.loop)
        question_number = next((index for index, item in enumerate(block, start=1) if item.item == current.item), 1)
        name = f"{current.question.loop} — {row_label}" if row_label else current.question.loop
        return (
            f"{name}: row {row_number} of {len(rows)}, "
            f"question {question_number} of {len(block)}"
        )

    ordinary = [step for step in question_path(meeting, answers, loop_rows) if step.row is None]
    position = next(
        (index for index, step in enumerate(ordinary, start=1) if step.item == current.item), len(ordinary) + 1
    )
    questions = meeting.question_items()
    ahead = sum(1 for item in questions[current.order + 1 :] if not item.loop)
    return f"Question {position} of {position + ahead}"


def display_answer(answer: StepAnswer | None) -> str:
    """One cell of the organiser's Question answers table."""
    if answer is None:
        return ""
    if answer.status == STEP_ANSWERED:
        return answer.value
    if answer.status == STEP_NOT_ANSWERED:
        said = answer.last_reply.strip()
        return f'⚠ Not answered (said: "{said}")' if said else "⚠ Not answered"
    return f"… trying ({answer.tries} so far)" if answer.tries else ""
