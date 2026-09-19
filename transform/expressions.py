"""Reading and running a calculated-column expression, e.g. `bonus = basic * 0.12`.

**Nothing here ever calls `eval` or `exec`, and nothing here imports anything at run
time.** The requirements document (section 2.1) makes "no arbitrary/free-form code
execution" a design principle, and an expression box is exactly where that principle would
otherwise leak: `eval("__import__('os').system(...)")` is one careless line away.

So the expression is read by a hand-written tokenizer with a **whitelist**: numbers,
column references, `+ - * /`, and brackets. Anything the tokenizer does not recognise is a
syntax error, which means a new attack does not need a new rule to be blocked — it is
blocked by not being on the list. There is no function call syntax, no attribute access,
no names other than columns, and no way to spell one.

Evaluation is over whole pandas Series rather than row by row, so a million-row table
costs one vectorised multiply rather than a million Python calls.

Columns are written between square brackets — `[net sales] * 0.1` — so that a column whose
name has a space or a symbol in it needs no special handling, which is most of them in real
uploads. A bare word is also accepted as a column name when it matches one exactly, because
`basic * 0.12` is what people type first.
"""

import logging
import re
from dataclasses import dataclass

import numpy as np
import pandas as pd

from transform.exceptions import InvalidStepParamsError

logger = logging.getLogger(__name__)

#: Guards against a pasted essay rather than an expression. Well above any real formula.
MAX_EXPRESSION_LENGTH = 500

_NUMBER = re.compile(r"\d+(?:\.\d+)?")
_BARE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_ ]*")

#: Operator -> (precedence, is_left_associative). Only these four, plus unary minus.
_OPERATORS = {
    "+": (1, True),
    "-": (1, True),
    "*": (2, True),
    "/": (2, True),
}

#: The token kinds the shunting-yard stage expects.
_NUMBER_TOKEN = "number"
_COLUMN_TOKEN = "column"
_OPERATOR_TOKEN = "operator"
_LEFT_BRACKET = "("
_RIGHT_BRACKET = ")"

#: Stands in for unary minus so it can carry its own (higher) precedence without being
#: confused with subtraction. Never typed by a user.
_NEGATE = "neg"


@dataclass(frozen=True)
class Token:
    kind: str
    value: object


def tokenize(expression: str, known_columns: list[str]) -> list[Token]:
    """Splits an expression into tokens, rejecting anything not on the whitelist.

    `known_columns` is needed during tokenizing, not just afterwards: a bare column name
    may contain spaces (`net sales`), so the only way to know where the name ends is to
    match it against the columns that actually exist. Longest match wins, so `net sales`
    beats `net` when both are columns.

    Raises:
        InvalidStepParamsError: on an unrecognised character, an over-long expression, or
            a name that isn't a column of the table.
    """
    text = (expression or "").strip()
    if not text:
        raise InvalidStepParamsError("Type a formula, for example: basic * 0.12")
    if len(text) > MAX_EXPRESSION_LENGTH:
        raise InvalidStepParamsError(
            f"That formula is too long ({len(text)} characters, the limit is "
            f"{MAX_EXPRESSION_LENGTH})."
        )

    # Longest first, so a column named `net sales` is preferred over one named `net`.
    by_length = sorted((str(column) for column in known_columns), key=len, reverse=True)
    tokens: list[Token] = []
    position = 0

    while position < len(text):
        character = text[position]

        if character.isspace():
            position += 1
            continue

        if character == "[":
            closing = text.find("]", position)
            if closing == -1:
                raise InvalidStepParamsError(
                    "A column name opened with '[' was never closed with ']'."
                )
            column = text[position + 1 : closing].strip()
            if column not in known_columns:
                raise InvalidStepParamsError(_unknown_column_message(column, known_columns))
            tokens.append(Token(_COLUMN_TOKEN, column))
            position = closing + 1
            continue

        number_match = _NUMBER.match(text, position)
        if number_match:
            tokens.append(Token(_NUMBER_TOKEN, float(number_match.group())))
            position = number_match.end()
            continue

        if character in _OPERATORS:
            if _is_unary_position(tokens) and character == "-":
                tokens.append(Token(_OPERATOR_TOKEN, _NEGATE))
            elif _is_unary_position(tokens) and character == "+":
                pass  # A leading plus is a no-op; drop it rather than reject it.
            else:
                tokens.append(Token(_OPERATOR_TOKEN, character))
            position += 1
            continue

        if character in (_LEFT_BRACKET, _RIGHT_BRACKET):
            tokens.append(Token(character, character))
            position += 1
            continue

        matched_column = next(
            (column for column in by_length if text.startswith(column, position)), None
        )
        if matched_column:
            tokens.append(Token(_COLUMN_TOKEN, matched_column))
            position += len(matched_column)
            continue

        bare_match = _BARE_NAME.match(text, position)
        if bare_match:
            # A word-shaped run that matched no column. Naming it is far more useful than
            # pointing at the character, and it is also the path that rejects `import`,
            # `os`, `__class__` and every other identifier: none of them is a column.
            raise InvalidStepParamsError(
                _unknown_column_message(bare_match.group().strip(), known_columns)
            )

        raise InvalidStepParamsError(
            f"'{character}' can't be used in a formula. You can use column names, numbers, "
            f"+ - * / and brackets."
        )

    return tokens


def _unknown_column_message(name: str, known_columns: list[str]) -> str:
    available = ", ".join(str(column) for column in known_columns[:12])
    more = ", ..." if len(known_columns) > 12 else ""
    return (
        f"'{name}' isn't a column in this table. Columns available: {available}{more}. "
        f"Put a name in square brackets if it has spaces, like [net sales]."
    )


def _is_unary_position(tokens: list[Token]) -> bool:
    """Whether a `-` here means "negate" rather than "subtract".

    True at the start, and straight after another operator or an opening bracket — which
    is what makes `-5`, `2 * -3` and `(-x + 1)` read correctly.
    """
    if not tokens:
        return True
    last = tokens[-1]
    return last.kind == _OPERATOR_TOKEN or last.kind == _LEFT_BRACKET


def referenced_columns(expression: str, known_columns: list[str]) -> list[str]:
    """The columns an expression reads, in first-appearance order, without running it.

    This is what `required_columns` reports for a calculated-column step, so phase 27 can
    check next month's upload actually has them.

    Raises:
        InvalidStepParamsError: if the expression doesn't tokenize.
    """
    seen: list[str] = []
    for token in tokenize(expression, known_columns):
        if token.kind == _COLUMN_TOKEN and token.value not in seen:
            seen.append(str(token.value))
    return seen


def to_postfix(tokens: list[Token]) -> list[Token]:
    """Shunting-yard: infix tokens to postfix, so evaluation needs no recursion.

    Raises:
        InvalidStepParamsError: on mismatched brackets.
    """
    output: list[Token] = []
    stack: list[Token] = []

    for token in tokens:
        if token.kind in (_NUMBER_TOKEN, _COLUMN_TOKEN):
            output.append(token)
        elif token.kind == _OPERATOR_TOKEN:
            if token.value == _NEGATE:
                # Unary minus binds tighter than any binary operator and is
                # right-associative, so it simply waits on the stack.
                stack.append(token)
                continue
            precedence, left_associative = _OPERATORS[str(token.value)]
            while stack and stack[-1].kind == _OPERATOR_TOKEN:
                top = stack[-1]
                if top.value == _NEGATE:
                    output.append(stack.pop())
                    continue
                top_precedence, _ = _OPERATORS[str(top.value)]
                if top_precedence > precedence or (
                    top_precedence == precedence and left_associative
                ):
                    output.append(stack.pop())
                else:
                    break
            stack.append(token)
        elif token.kind == _LEFT_BRACKET:
            stack.append(token)
        elif token.kind == _RIGHT_BRACKET:
            while stack and stack[-1].kind != _LEFT_BRACKET:
                output.append(stack.pop())
            if not stack:
                raise InvalidStepParamsError("There's a ')' with no matching '(' in the formula.")
            stack.pop()

    while stack:
        top = stack.pop()
        if top.kind == _LEFT_BRACKET:
            raise InvalidStepParamsError("There's a '(' with no matching ')' in the formula.")
        output.append(top)

    return output


def _as_numeric(frame: pd.DataFrame, column: str) -> pd.Series:
    """One column as numbers, with anything unreadable becoming blank.

    Uploads arrive as text by design (see `cleaner.loaders`), so `1,250.00` and `(500)` are
    strings here. They are converted rather than refused, because a user typing
    `qty * rate` means the numbers in those columns whether or not they have yet run a
    "change column type" step.
    """
    series = frame[column]
    if pd.api.types.is_numeric_dtype(series):
        return series.astype("float64")

    text = series.astype("string").str.strip()
    # Accounting negatives: (500) means -500.
    negated = text.str.match(r"^\(.*\)$", na=False)
    text = text.str.replace(r"^\((.*)\)$", r"\1", regex=True)
    text = text.str.replace(",", "", regex=False)
    numbers = pd.to_numeric(text, errors="coerce")
    return numbers.where(~negated, -numbers).astype("float64")


def evaluate(frame: pd.DataFrame, expression: str) -> tuple[pd.Series, list[str]]:
    """Runs an expression against a table, returning the resulting column and any warnings.

    Division by zero gives a blank rather than an exception or an infinity — the same
    answer Excel's IFERROR-wrapped formulas give, and the one that keeps a single bad row
    from failing a whole pipeline.

    Returns:
        `(result_series, warnings)`. A warning names any column whose text could not be
        read as a number, with a count, so "why is my total blank?" is answerable on screen.

    Raises:
        InvalidStepParamsError: if the expression doesn't parse, or is structurally
            incomplete (`basic *`).
    """
    known_columns = [str(column) for column in frame.columns]
    postfix = to_postfix(tokenize(expression, known_columns))

    warnings: list[str] = []
    stack: list[pd.Series | float] = []

    for token in postfix:
        if token.kind == _NUMBER_TOKEN:
            stack.append(float(token.value))
            continue

        if token.kind == _COLUMN_TOKEN:
            column = str(token.value)
            numbers = _as_numeric(frame, column)
            unreadable = int(numbers.isna().sum() - frame[column].isna().sum())
            if unreadable > 0:
                warnings.append(
                    f"'{column}': {unreadable:,} value(s) couldn't be read as a number and "
                    f"were treated as blank."
                )
            stack.append(numbers)
            continue

        if token.value == _NEGATE:
            if not stack:
                raise InvalidStepParamsError("The formula is incomplete - a '-' has nothing after it.")
            stack.append(-stack.pop())
            continue

        if len(stack) < 2:
            raise InvalidStepParamsError(
                f"The formula is incomplete - '{token.value}' needs a value on both sides."
            )
        right = stack.pop()
        left = stack.pop()
        stack.append(_apply_operator(str(token.value), left, right))

    if len(stack) != 1:
        raise InvalidStepParamsError(
            "That formula isn't complete. Check for a missing operator between two values."
        )

    result = stack[0]
    if not isinstance(result, pd.Series):
        # A formula of pure numbers, like `100 / 4`. Broadcast it so the column still has
        # one value per row.
        result = pd.Series(float(result), index=frame.index, dtype="float64")
    return result.astype("float64"), warnings


def _apply_operator(
    operator: str, left: pd.Series | float, right: pd.Series | float
) -> pd.Series | float:
    """One arithmetic step, with division by zero yielding blank rather than infinity."""
    if operator == "+":
        return left + right
    if operator == "-":
        return left - right
    if operator == "*":
        return left * right

    # Division. numpy would emit a RuntimeWarning and produce inf/-inf/nan; blanking the
    # zero divisor first is both quieter and the answer a spreadsheet user expects.
    if isinstance(right, pd.Series):
        safe_right = right.replace(0, np.nan)
    else:
        safe_right = np.nan if right == 0 else right
    return left / safe_right
