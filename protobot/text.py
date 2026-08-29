"""Plain-text rendering for decoded chat components."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

from .translations import TRANSLATIONS

__all__ = ["plain_text", "format_translation"]

#: Language files use only ``%s`` and ``%1$s`` placeholders, plus ``%%``.
_PLACEHOLDER = re.compile(r"%%|%(?:(\d+)\$)?s")


def format_translation(
    pattern: str,
    args: Sequence[str],
    *,
    append_unused: bool = False,
) -> str:
    """Fill a translation pattern with already-rendered arguments.

    ``%s`` consumes the next argument, ``%1$s`` a specific one (1-based, as in
    the language files), ``%%`` is a literal percent sign.  A placeholder with
    no matching argument renders as nothing rather than raising -- servers do
    send patterns and argument lists that disagree.

    ``append_unused`` appends the arguments the pattern never referenced.  It
    is meant for the unknown-key case, where the "pattern" is the bare
    translation key: without it the arguments -- which carry the actual player
    name and message -- would be dropped.
    """

    out: list[str] = []
    consumed = False
    auto = 0
    index = 0
    length = len(pattern)
    while index < length:
        if pattern[index] != "%":
            out.append(pattern[index])
            index += 1
            continue
        match = _PLACEHOLDER.match(pattern, index)
        if match is None:  # a stray percent sign is literal
            out.append("%")
            index += 1
            continue
        index = match.end()
        if match.group(0) == "%%":
            out.append("%")
            continue
        position = match.group(1)
        if position is None:
            slot, auto = auto, auto + 1
        else:
            slot = int(position) - 1
        consumed = True
        out.append(args[slot] if 0 <= slot < len(args) else "")
    text = "".join(out)
    if append_unused and args and not consumed:
        return " ".join([text, *args]) if text else " ".join(args)
    return text


def plain_text(component, *, translations: Mapping[str, str] | None = None) -> str:
    """Extract readable text from a decoded chat component (str/dict/list).

    ``translate`` components are formatted, not concatenated: the pattern comes
    from the server's own ``fallback`` when it sent one (it is already written
    for this exact message), otherwise from the built-in ``en_us`` table in
    :mod:`protobot.translations`, otherwise the key is shown with its arguments
    appended so nothing is lost.  Pass ``translations`` to override the table
    for one call; :func:`protobot.translations.register_translations` changes it
    process-wide.
    """

    if isinstance(component, str):
        return component
    if isinstance(component, list):
        return "".join(plain_text(item, translations=translations) for item in component)
    if isinstance(component, dict):
        parts: list[str] = []
        if "text" in component:
            parts.append(str(component["text"]))
        if "translate" in component:
            parts.append(_translate(component, translations))
            if "extra" in component:
                parts.append(plain_text(component["extra"], translations=translations))
        else:
            for key in ("with", "extra"):
                if key in component:
                    parts.append(plain_text(component[key], translations=translations))
        # Some plugins serialize text content under an empty key instead of
        # "text" (e.g. {'': '123'}); render it rather than dropping the message.
        if "" in component and isinstance(component[""], str):
            parts.append(component[""])
        return "".join(parts)
    return str(component)


def _translate(component: dict, translations: Mapping[str, str] | None) -> str:
    key = str(component.get("translate"))
    raw = component.get("with")
    if raw is None:
        arguments: list = []
    elif isinstance(raw, list):
        arguments = list(raw)
    else:  # a lone argument, not wrapped in a list
        arguments = [raw]
    rendered = [plain_text(item, translations=translations) for item in arguments]
    # A fallback is written by the server for this very message (e.g. a plugin
    # reusing a vanilla key with extra arguments), so it beats our table. With
    # no arguments to fill in it is taken verbatim: it is then plain text, and
    # a stray "%s" in it would otherwise be eaten as an empty placeholder.
    fallback = component.get("fallback")
    if isinstance(fallback, str):
        return format_translation(fallback, rendered) if rendered else fallback
    table = TRANSLATIONS if translations is None else translations
    pattern = table.get(key)
    if pattern is not None:
        return format_translation(str(pattern), rendered)
    return format_translation(key, rendered, append_unused=True)
