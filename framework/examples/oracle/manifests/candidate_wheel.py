"""u-tokenize-1 candidate — stands in for the PyO3 wheel's frozen API.

In a real migration this side is the installed wheel: the Rust core behind
an unchanged Python API (same module shape, same entry signature — "the
Python API is frozen"). The runner cannot tell the two apart, and it must
not be able to: the wheel *is* the entry point.

This candidate carries ONE canonical injected bug for the Stage-2
judge-first validation: the empty-input ValueError is swallowed and an
empty list is returned instead (the "swallowed error" bug class from
oracle.py self-test). Everything else matches the reference.
"""


def run(rec: dict):
    """oracle entry point — buggy port: empty input returns [] instead of
    raising ValueError('empty input')"""
    return rec["text"].lower().split()
