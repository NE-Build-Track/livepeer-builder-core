"""Every form a refused setting's error can be logged in, for the tests that say no value is in any.

``str(error)`` is what the engine's own commands print. An embedding application may log any of the
others when it builds its settings. A test that raises the refusal passes the values in a variable: the
traceback quotes the line that raised it.
"""

import traceback

from pydantic import ValidationError


def forms(error: ValidationError) -> dict[str, str]:
    return {
        "str(error)": str(error),
        "repr(error)": repr(error),
        "error.errors()": repr(error.errors()),
        "error.errors(include_context=False)": repr(error.errors(include_context=False)),
        "error.json()": error.json(),
        "error.json(include_context=False)": error.json(include_context=False),
        "the traceback": "".join(traceback.format_exception(error)),
    }


def carried(error: ValidationError, *values: str) -> list[str]:
    """Where a refusal repeats one of ``values``, or still reaches what held them: empty when it does
    neither."""
    found = [
        f"{value!r} is in {name}" for name, text in forms(error).items() for value in values if value in text
    ]
    # The error pydantic raised first holds every value given. Python prints a chained error with the one
    # that replaced it, and a logger that walks the chain reaches it.
    found += [
        f"error.{link} is {chained!r}"
        for link in ("__cause__", "__context__")
        if (chained := getattr(error, link)) is not None
    ]
    # A rule's own exception kept as context holds whatever the rule gave it, and its traceback's frames
    # hold the settings. Its words are the message, which the forms above already show.
    found += [
        f"the ctx of {detail['loc']} keeps the exception {name!r}"
        for detail in error.errors()
        for name, value in detail.get("ctx", {}).items()
        if isinstance(value, BaseException)
    ]
    return found
