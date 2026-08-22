"""Canonical plate-number handling.

Everything that compares, stores or transmits a plate number MUST go through
``normalize_plate``.  The ALPR pipeline emits ``ABC1234`` while humans and the
admin portal type ``ABC-1234``; without a single canonical form the AI-detected
plates silently miss every allowlist lookup.

Canonical form: uppercase, alphanumeric only (all separators stripped).
The server stores the same canonical form.
"""

from __future__ import annotations

import re

_NON_ALNUM = re.compile(r"[^0-9A-Za-z]+")


def normalize_plate(value: str | None) -> str:
    """Return the canonical form of a plate number.

    >>> normalize_plate(" abc-1234 ")
    'ABC1234'
    >>> normalize_plate(None)
    ''
    """
    if not value:
        return ""
    return _NON_ALNUM.sub("", value).upper()
