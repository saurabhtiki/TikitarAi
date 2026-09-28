"""The flow picture of a meeting's questions (phase 54), as Graphviz DOT text.

`st.graphviz_chart` draws DOT text directly, so no extra package is needed. The picture
follows the same reading of the agenda as the invitee page: questions in order, Go to jumps
read by `steps.parse_branches`, and a For each list drawn as one box repeated per row. A
Go to that can't be read is drawn in red rather than left out, because spotting a wrong
jump is what the picture is for.
"""

from meetings import steps
from meetings.model import AgendaItem

_JUMP_COLOUR = "#1f6feb"
_PROBLEM_COLOUR = "#d1242f"


def _quote(text: str) -> str:
    """A DOT string literal; a newline becomes DOT's own line break."""
    escaped = str(text).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'


def _node_label(number: int, item: AgendaItem) -> str:
    return f"{number}. {item.item}\n{steps.describe_rule(item)}"


def _blocks(questions: list[AgendaItem]) -> list[tuple[str, list[int]]]:
    """Runs of neighbouring questions: (list name or "", their positions)."""
    blocks: list[tuple[str, list[int]]] = []
    for index, item in enumerate(questions):
        key = steps.loop_key(item.loop)
        if blocks and key and steps.loop_key(blocks[-1][0]) == key:
            blocks[-1][1].append(index)
        else:
            blocks.append((item.loop if key else "", [index]))
    return blocks


def flow_dot(agenda: list[AgendaItem]) -> str:
    """DOT text for the questions' flow, or "" when the agenda has no Question rows."""
    questions = [item for item in agenda if item.is_question()]
    if not questions:
        return ""

    node_by_title: dict[str, str] = {}
    for index, item in enumerate(questions):
        node_by_title.setdefault(item.item.strip().lower(), f"q{index}")
    others = [item.item for item in agenda if not item.is_question() and item.item.strip()]

    lines = [
        "digraph {",
        '  node [shape=box, style="rounded", fontname="Helvetica", fontsize=11];',
        '  edge [fontname="Helvetica", fontsize=10];',
        '  start [label="Start", shape=oval];',
        f'  end [label={_quote("End of questions" if others else "End")}, shape=oval];',
    ]
    if others:
        lines.append(f'  talk [label={_quote("Discussion: " + ", ".join(others))}];')
        lines.append('  finish [label="End", shape=oval];')
        lines.append("  end -> talk;")
        lines.append("  talk -> finish;")

    blocks = _blocks(questions)
    for block_number, (name, positions) in enumerate(blocks):
        indent = "  "
        if name:
            lines.append(f"  subgraph cluster_{block_number} {{")
            lines.append(f"    label={_quote('For each row of ' + name)}; style=dashed;")
            indent = "    "
        for index in positions:
            lines.append(f"{indent}q{index} [label={_quote(_node_label(index + 1, questions[index]))}];")
        if name:
            lines.append("  }")

    lines.append("  start -> q0;")
    for block_number, (name, positions) in enumerate(blocks):
        for index in positions[:-1]:
            lines.append(f"  q{index} -> q{index + 1};")
        after = f"q{positions[-1] + 1}" if block_number + 1 < len(blocks) else "end"
        if name:
            first = f"q{positions[0]}"
            lines.append(f'  q{positions[-1]} -> {first} [label="next row", style=dashed, constraint=false];')
            lines.append(f'  q{positions[-1]} -> {after} [label="after the last row"];')
        else:
            lines.append(f"  q{positions[-1]} -> {after};")

    lines.extend(_jump_lines(questions, agenda, node_by_title, blocks))
    lines.append("}")
    return "\n".join(lines)


def _jump_lines(
    questions: list[AgendaItem],
    agenda: list[AgendaItem],
    node_by_title: dict[str, str],
    blocks: list[tuple[str, list[int]]],
) -> list[str]:
    first_of_block = {index: f"q{positions[0]}" for _, positions in blocks for index in positions}
    lines = []
    for index, item in enumerate(questions):
        if not item.branch.strip():
            continue
        try:
            branches = steps.parse_branches(item, agenda)
        except steps.RuleError:
            lines.append(
                f'  q{index}_problem [label="Go to problem", shape=note, color="{_PROBLEM_COLOUR}", '
                f'fontcolor="{_PROBLEM_COLOUR}"];'
            )
            lines.append(
                f'  q{index} -> q{index}_problem [label="?", color="{_PROBLEM_COLOUR}", '
                f'fontcolor="{_PROBLEM_COLOUR}", style=dashed];'
            )
            continue
        for branch in branches:
            label = f"if {branch.condition}"
            if branch.target == steps.END_TARGET:
                target = "end"
            elif branch.target == steps.NEXT_ROW_TARGET:
                target = first_of_block[index]
                label += ": next row"
            else:
                target = node_by_title[branch.target.strip().lower()]
            lines.append(
                f'  q{index} -> {target} [label={_quote(label)}, color="{_JUMP_COLOUR}", '
                f'fontcolor="{_JUMP_COLOUR}", constraint=false];'
            )
    return lines
