"""duplifind.tokenize — Python reference entry for unit u-tokenize-1.

In a real migration this side is the unit's live Python module
(``src/duplifind/tokenize.py``). The oracle never sees the code — it spawns
``python -c RUNNER <module:function>`` and reads per-run records from stdout,
so this stand-in is exercised exactly as the real unit would be.

The legacy set-returning API is deliberate: it is the one input shape that
makes a run *incomparable* (a real ``set`` is not JSON-serializable), which
the Stage-2 judge-first demo below needs on record.
"""


def tokenize(text: str) -> list[str]:
    if text == "":
        raise ValueError("empty input")  # Invariant: never returns []
    return text.lower().split()


def legacy_tokens(text: str) -> set[str]:
    return set(tokenize(text))


def run(rec: dict):
    """oracle entry point: one input record -> one JSON-serializable value."""
    if rec.get("legacy"):
        return legacy_tokens(rec["text"])
    return tokenize(rec["text"])
