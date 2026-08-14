"""Turning a typed-in list of model names into profiles.

A profile row is one provider *and* one model — that is what `llm.client.build_model` and the
session picker read. Adding a provider's three models therefore means three rows, and retyping
the base URL and key twice. These helpers let Settings accept the models in one box and fan
them out, without changing what a row means to the rest of the app.
"""


def parse_model_names(raw: str) -> list[str]:
    """Splits the Settings "Models" box into model names.

    Accepts one per line, comma-separated, or a mix of both, because a model list is usually
    pasted from somewhere that already chose one of those. Duplicates are dropped
    case-insensitively — two profiles differing only in the case of the model name would be
    the same connection listed twice.
    """
    names: list[str] = []
    seen: set[str] = set()
    for chunk in (raw or "").replace("\r", "\n").replace(",", "\n").split("\n"):
        name = chunk.strip()
        if not name or name.casefold() in seen:
            continue
        seen.add(name.casefold())
        names.append(name)
    return names


def profile_label(nickname: str, model: str, is_only_model: bool) -> str:
    """The nickname a fanned-out profile is saved under.

    A single model keeps the nickname exactly as typed, so adding one model behaves as it
    always did; several need telling apart in the picker, so each carries its model name.
    """
    if is_only_model:
        return nickname
    return f"{nickname} — {model}"
