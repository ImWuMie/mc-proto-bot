"""Plain-text rendering for decoded chat components."""

from __future__ import annotations

__all__ = ["plain_text"]


def plain_text(component) -> str:
    """Extract readable text from a decoded chat component (str/dict/list)."""

    if isinstance(component, str):
        return component
    if isinstance(component, list):
        return "".join(plain_text(item) for item in component)
    if isinstance(component, dict):
        parts: list[str] = []
        if "text" in component:
            parts.append(str(component["text"]))
        if "translate" in component:
            # A fallback carries the fully resolved plain text (e.g. /tell);
            # prefer it over the bare translation key.
            if "fallback" in component:
                parts.append(str(component["fallback"]))
                if "extra" in component:
                    parts.append(plain_text(component["extra"]))
            else:
                parts.append(str(component["translate"]))
                for key in ("with", "extra"):
                    if key in component:
                        parts.append(plain_text(component[key]))
        else:
            for key in ("with", "extra"):
                if key in component:
                    parts.append(plain_text(component[key]))
        # Some plugins serialize text content under an empty key instead of
        # "text" (e.g. {'': '123'}); render it rather than dropping the message.
        if "" in component and isinstance(component[""], str):
            parts.append(component[""])
        return "".join(parts)
    return str(component)
