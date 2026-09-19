"""Reading and running a calculated-column expression, e.g. `bonus = basic * 0.12`.

**Nothing here ever calls `eval` or `exec`, and nothing here imports anything at run
time.** The requirements document (section 2.1) makes "no arbitrary/free-form code
execution" a design principle, and an expression box is exactly where that principle would
otherwise leak: `eval("__import__('os').system(...)")` is one careless line away.

So the expression is read by a hand-written tokenizer with a **whitelist**: numbers,
column references, `+ - * /`, the comparisons `> < >= <= = == !=`, the two words `if` and
`else`, and brackets. That is the entire vocabulary, and it is short enough to print.
Anything the tokenizer does not recognise is a syntax error, which means a new attack does
not need a new rule to be blocked - it is blocked by not being on the list. There is no
function call syntax, no attribute access, no names other than columns, and no way to
spell one.

Evaluation is over whole pandas Series rather than row by row, so a million-row table
costs one vectorised multiply rather than a million Python calls.

Columns are written between square brackets - `[net sales] * 0.1` - so that a column whose
name has a space or a symbol in it needs no special handling, which is most of them in real
uploads. A bare word is also accepted as a column name when it matches one exactly, because
`basic * 0.12` is what people type first.

**if/else is a choice between two numbers, not a language.** `price * 0.9 if quantity >
100 else price` picks one of two *numeric* answers per row. There are no string literals,
so a text answer ("high", "low") is still `add_conditional_column`'s job, not this box's.
Two rules worth knowing, both matching the way a spreadsheet behaves:

- Division by zero gives a blank rather than an error or an infinity.
- A test that cannot be read - a blank cell, or text that is not a number - counts as *not
  true*, so those rows take the `else` answer.
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

#: `if` / `else` only when the word ends there, so a column called `ifscode` is still a
#: column. The longest-match column check runs *before* this anyway, so a column actually
#: named `if` also keeps winning; this is the second line of defence, not the first.
_KEYWORD = re.compile(r"(if|else)(?![A-Za-z0-9_])")

#: What the user may type -> the one spelling the evaluator works with. `=` and `<>` are
#: here because they are what a spreadsheet user reaches for first.
_COMPARISON_SPELLINGS = {
    ">=": ">=",
    "<=": "<=",
    "==": "==",
    "!=": "!=",
    "<>": "!=",
    ">": ">",
    "<": "<",
    "=": "==",
}

#: Longest first, so `>=` is never read as `>` followed by a stray `=`.
_COMPARISON_STARTS = sorted(_COMPARISON_SPELLINGS, key=len, reverse=True)

#: The canonical comparison operators, after `=` and `<>` have been folded in.
_COMPARISON_OPERATORS = frozenset({">", "<", ">=", "<=", "==", "!="})

#: Operator -> (precedence, is_left_associative), plus unary minus.
#: Comparisons sit below the arithmetic, so `qty * 2 > 100` compares the product.
_OPERATORS = {
    ">": (1, True),
    "<": (1, True),
    ">=": (1, True),
    "<=": (1, True),
    "==": (1, True),
    "!=": (1, True),
    "+": (2, True),
    "-": (2, True),
    "*": (3, True),
    "/": (3, True),
}

#: The token kinds the shunting-yard stage expects.
_NUMBER_TOKEN = "number"
_COLUMN_TOKEN = "column"
_OPERATOR_TOKEN = "operator"
_KEYWORD_TOKEN = "keyword"
_LEFT_BRACKET = "("
_RIGHT_BRACKET = ")"

#: An already-worked-out answer standing in for a bracketed `if/else` that had to be
#: evaluated ahead of the rest. Never produced by the tokenizer, only during evaluation.
_VALUE_TOKEN = "value"

#: Stands in for unary minus so it can carry its own (higher) precedence without being
#: confused with subtraction. Never typed by a user.
_NEGATE = "neg"

_IF = "if"
_ELSE = "else"


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

        comparison = next(
            (
                spelling
                for spelling in _COMPARISON_STARTS
                if text.startswith(spelling, position)
            ),
            None,
        )
        if comparison:
            tokens.append(Token(_OPERATOR_TOKEN, _COMPARISON_SPELLINGS[comparison]))
            position += len(comparison)
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

        keyword_match = _KEYWORD.match(text, position)
        if keyword_match:
            tokens.append(Token(_KEYWORD_TOKEN, keyword_match.group()))
            position = keyword_match.end()
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
            f"+ - * / brackets, the comparisons > < >= <= = != and if/else."
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

    True at the start, and straight after another operator, an opening bracket or an
    `if`/`else` - which is what makes `-5`, `2 * -3`, `(-x + 1)` and `0 if x > 1 else -1`
    all read correctly.
    """
    if not tokens:
        return True
    last = tokens[-1]
    return last.kind in (_OPERATOR_TOKEN, _KEYWORD_TOKEN, _LEFT_BRACKET)


def referenced_columns(expression: str, known_columns: list[str]) -> list[str]:
    """The columns an expression reads, in first-appearance order, without running it.

    This is what `required_columns` reports for a calculated-column step, so phase 27 can
    check next month's upload actually has them. Every side of an `if/else` counts: a
    column only one branch reads still has to be there next month.

    Raises:
        InvalidStepParamsError: if the expression doesn't tokenize.
    """
    tokens = tokenize(expression, known_columns)
    # Checked here, not only at run time: this is the call `validate_add_calculated_column`
    # makes, and a formula that cannot be read should never enter a saved pipeline.
    _check_structure(tokens)

    seen: list[str] = []
    for token in tokens:
        if token.kind == _COLUMN_TOKEN and token.value not in seen:
            seen.append(str(token.value))
    return seen


def _check_structure(tokens: list[Token]) -> None:
    """Reads the shape of a formula without any data behind it.

    The same walk `_evaluate_tokens` does, minus the arithmetic: every `if` has its `else`,
    no part is empty, brackets match, and no level compares three values.

    Raises:
        InvalidStepParamsError: on any of those.
    """
    if not tokens:
        raise InvalidStepParamsError(
            "Part of the formula is empty. Write it as: answer if test else other answer."
        )

    split = _split_choice(tokens)
    if split is not None:
        for part in split:
            _check_structure(part)
        return

    placeholders: list[Token] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token.kind != _LEFT_BRACKET:
            placeholders.append(token)
            index += 1
            continue

        closing = _matching_bracket(tokens, index)
        inner = tokens[index + 1 : closing]
        if any(item.kind == _KEYWORD_TOKEN for item in inner):
            _check_structure(inner)
            placeholders.append(Token(_VALUE_TOKEN, 0.0))
        else:
            placeholders.extend(tokens[index : closing + 1])
        index = closing + 1

    _reject_chained_comparisons(placeholders)
    to_postfix(placeholders)


def to_postfix(tokens: list[Token]) -> list[Token]:
    """Shunting-yard: infix tokens to postfix, so evaluation needs no recursion.

    `if`/`else` never reach here: they are split off structurally beforehand, because a
    three-part choice is not something a two-sided operator table can describe.

    Raises:
        InvalidStepParamsError: on mismatched brackets, or a misplaced `if`/`else`.
    """
    output: list[Token] = []
    stack: list[Token] = []

    for token in tokens:
        if token.kind in (_NUMBER_TOKEN, _COLUMN_TOKEN, _VALUE_TOKEN):
            output.append(token)
        elif token.kind == _KEYWORD_TOKEN:
            raise InvalidStepParamsError(
                f"'{token.value}' isn't in the right place. Write it as: "
                f"answer if test else other answer."
            )
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

    Division by zero gives a blank rather than an exception or an infinity - the same
    answer Excel's IFERROR-wrapped formulas give, and the one that keeps a single bad row
    from failing a whole pipeline.

    A comparison on its own (`amount > 1000`) is a perfectly good formula: it gives 1 where
    the test passes and 0 where it doesn't, which is the flag column people usually want.

    Returns:
        `(result_series, warnings)`. A warning names any column whose text could not be
        read as a number, with a count, so "why is my total blank?" is answerable on screen.

    Raises:
        InvalidStepParamsError: if the expression doesn't parse, or is structurally
            incomplete (`basic *`, or an `if` with no `else`).
    """
    known_columns = [str(column) for column in frame.columns]
    warnings: list[str] = []
    result = _evaluate_tokens(frame, tokenize(expression, known_columns), warnings)

    if not isinstance(result, pd.Series):
        # A formula of pure numbers, like `100 / 4`. Broadcast it so the column still has
        # one value per row.
        result = pd.Series(float(result), index=frame.index, dtype="float64")
    return result.astype("float64"), warnings


def _evaluate_tokens(
    frame: pd.DataFrame, tokens: list[Token], warnings: list[str]
) -> pd.Series | float:
    """One expression, or one branch of one, worked out.

    Calls itself for each side of an `if/else`, which is also what makes
    `a if x else b if y else c` work: the `else` side is simply another expression.
    """
    if not tokens:
        raise InvalidStepParamsError(
            "Part of the formula is empty. Write it as: answer if test else other answer."
        )

    split = _split_choice(tokens)
    if split is not None:
        true_tokens, condition_tokens, false_tokens = split
        condition = _evaluate_tokens(frame, condition_tokens, warnings)
        true_values = _evaluate_tokens(frame, true_tokens, warnings)
        false_values = _evaluate_tokens(frame, false_tokens, warnings)
        return _choose(frame, condition, true_values, false_values)

    without_brackets = _fold_bracketed_choices(frame, tokens, warnings)
    _reject_chained_comparisons(without_brackets)
    return _evaluate_postfix(frame, to_postfix(without_brackets), warnings)


def _split_choice(tokens: list[Token]) -> tuple[list[Token], list[Token], list[Token]] | None:
    """Cuts `answer if test else other` into its three parts, or returns None.

    Only keywords outside every bracket count, so `(a if x else b) * 2` is left alone here
    and dealt with by `_fold_bracketed_choices` instead. The first `else` after the first
    `if` is the matching one, which is what makes a chain read right to left:
    `a if x else b if y else c` means `a if x else (b if y else c)`.

    Raises:
        InvalidStepParamsError: on an `if` with no `else`, or an `else` with no `if`.
    """
    depth = 0
    if_at = -1
    else_at = -1
    for index, token in enumerate(tokens):
        if token.kind == _LEFT_BRACKET:
            depth += 1
        elif token.kind == _RIGHT_BRACKET:
            depth -= 1
        elif token.kind == _KEYWORD_TOKEN and depth == 0:
            if token.value == _IF and if_at == -1:
                if_at = index
            elif token.value == _ELSE and if_at == -1:
                raise InvalidStepParamsError(
                    "There's an 'else' with no 'if' before it. Write it as: "
                    "answer if test else other answer."
                )
            elif token.value == _ELSE and else_at == -1:
                else_at = index

    if if_at == -1:
        return None
    if else_at == -1:
        raise InvalidStepParamsError(
            "This formula has an 'if' but no 'else'. Say what the other rows should be, "
            "for example: price * 0.9 if quantity > 100 else price"
        )

    return tokens[:if_at], tokens[if_at + 1 : else_at], tokens[else_at + 1 :]


def _fold_bracketed_choices(
    frame: pd.DataFrame, tokens: list[Token], warnings: list[str]
) -> list[Token]:
    """Works out any bracketed `if/else` first and drops its answer in as a single value.

    `(0 if qty > 5 else 1) * price` has no keyword outside the brackets, so the
    shunting-yard stage would meet an `if` it cannot place. Evaluating the bracket ahead of
    time removes it.
    """
    if not any(token.kind == _KEYWORD_TOKEN for token in tokens):
        return tokens

    folded: list[Token] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token.kind != _LEFT_BRACKET:
            folded.append(token)
            index += 1
            continue

        closing = _matching_bracket(tokens, index)
        inner = tokens[index + 1 : closing]
        if any(item.kind == _KEYWORD_TOKEN for item in inner):
            folded.append(Token(_VALUE_TOKEN, _evaluate_tokens(frame, inner, warnings)))
        else:
            folded.extend(tokens[index : closing + 1])
        index = closing + 1

    return folded


def _matching_bracket(tokens: list[Token], opening: int) -> int:
    """Where the bracket opened at `opening` closes.

    Raises:
        InvalidStepParamsError: if it never closes.
    """
    depth = 0
    for index in range(opening, len(tokens)):
        if tokens[index].kind == _LEFT_BRACKET:
            depth += 1
        elif tokens[index].kind == _RIGHT_BRACKET:
            depth -= 1
            if depth == 0:
                return index
    raise InvalidStepParamsError("There's a '(' with no matching ')' in the formula.")


def _reject_chained_comparisons(tokens: list[Token]) -> None:
    """Refuses `a < b < c`, which would otherwise quietly compare a yes/no against a number.

    Counted per bracket level and reset at each `if`/`else`, so `1 if a > b else 0` and
    `(a > b) + (c > d)` are both still fine.

    Raises:
        InvalidStepParamsError: if one bracket level holds two comparisons.
    """
    counts = [0]
    for token in tokens:
        if token.kind == _LEFT_BRACKET:
            counts.append(0)
        elif token.kind == _RIGHT_BRACKET:
            if len(counts) > 1:
                counts.pop()
        elif token.kind == _KEYWORD_TOKEN:
            counts[-1] = 0
        elif token.kind == _OPERATOR_TOKEN and token.value in _COMPARISON_OPERATORS:
            counts[-1] += 1
            if counts[-1] > 1:
                raise InvalidStepParamsError(
                    "A formula can compare only two values at a time. Write 'amount > 100' "
                    "rather than '50 < amount > 100'."
                )


def _evaluate_postfix(
    frame: pd.DataFrame, postfix: list[Token], warnings: list[str]
) -> pd.Series | float:
    """Runs postfix tokens over whole columns at once.

    Raises:
        InvalidStepParamsError: if the formula is structurally incomplete.
    """
    stack: list[pd.Series | float] = []

    for token in postfix:
        if token.kind == _NUMBER_TOKEN:
            stack.append(float(token.value))
            continue

        if token.kind == _VALUE_TOKEN:
            stack.append(token.value)
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
            stack.append(-_as_number(stack.pop()))
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
    return stack[0]


def _choose(
    frame: pd.DataFrame,
    condition: pd.Series | float,
    true_values: pd.Series | float,
    false_values: pd.Series | float,
) -> pd.Series:
    """Picks one answer per row: the `if` side where the test passes, the `else` side where not.

    A test that cannot be read - a blank cell, or text that isn't a number - counts as not
    true, so those rows take the `else` answer. That is the same "blank rather than an
    error" habit division by zero already follows: one unreadable row should not fail a
    whole pipeline.
    """
    if isinstance(condition, pd.Series):
        if condition.dtype == bool:
            mask = condition.to_numpy()
        else:
            # A plain number used as a test: 0 and blank are false, anything else is true.
            mask = condition.fillna(0.0).to_numpy().astype(bool)
    else:
        mask = bool(condition)

    chosen = np.where(mask, _as_values(true_values), _as_values(false_values))
    # `np.where` collapses to a single value when nothing involved was a column, as in
    # `1 if 2 > 1 else 0`. Spread it back out so the column still has one value per row.
    spread = np.broadcast_to(chosen, (len(frame.index),)).astype("float64")
    return pd.Series(spread, index=frame.index, dtype="float64")


def _as_values(value: pd.Series | float) -> "np.ndarray | float":
    """A branch's answer as plain numbers, so `np.where` lines it up by position."""
    if isinstance(value, pd.Series):
        return value.astype("float64").to_numpy()
    return float(value)


def _apply_operator(
    operator: str, left: pd.Series | float, right: pd.Series | float
) -> pd.Series | float | bool:
    """One arithmetic or comparison step.

    Division by zero yields blank rather than infinity. A comparison yields yes/no, which
    either feeds an `if/else` or - used on its own - becomes a 1/0 flag column.
    """
    if operator in _COMPARISON_OPERATORS:
        return _compare(operator, left, right)

    # A yes/no from an earlier comparison counts as 1 or 0 here, so `(a > b) + (c > d)`
    # adds two flags rather than running numpy's logical-or on two boolean columns.
    left = _as_number(left)
    right = _as_number(right)

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


def _as_number(value: pd.Series | float) -> pd.Series | float:
    """A yes/no answer read as 1 or 0; anything already numeric left alone."""
    if isinstance(value, pd.Series) and value.dtype == bool:
        return value.astype("float64")
    if isinstance(value, (bool, np.bool_)):
        return float(value)
    return value


def _compare(
    operator: str, left: pd.Series | float, right: pd.Series | float
) -> pd.Series | bool:
    """One comparison. A blank on either side is never greater than, less than or equal to anything."""
    if operator == ">":
        return left > right
    if operator == "<":
        return left < right
    if operator == ">=":
        return left >= right
    if operator == "<=":
        return left <= right
    if operator == "==":
        return left == right
    return left != right
