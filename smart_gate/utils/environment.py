"""Which server this station is talking to, as a stable identity.

Every piece of local state — allowlist cache, sync watermarks, staff roster and
embeddings, the event and punch queues, device provisioning — belongs to ONE
server. A gate pointed at UAT and then at production is talking to two
different worlds: staff_uids mean different things, watermarks were learned
against a different clock, and a queue drained to the wrong side would write
test decisions into permanent production records.

So local data is partitioned by environment, and the environment is derived
from the configured API base URL: the host plus path, normalised so cosmetic
differences (scheme, case, a trailing slash) do not fork the data, then hashed
to a short directory-safe key. The human-readable label — the host — is what
the UI shows so an operator never has to guess which server a gate is on.

Pure functions, no filesystem: the mapping from URL to key must be testable and
must never change once shipped, or every station would lose its data on
upgrade.
"""

from __future__ import annotations

import hashlib
from urllib.parse import urlsplit

KEY_LENGTH = 12


def normalize_base_url(base_url: str) -> str:
    """``https://Portal.Example/api/gate/`` → ``portal.example/api/gate``.

    Scheme is dropped (http vs https is transport, not identity), the host is
    lower-cased, and trailing slashes go — those are the differences an
    operator makes by accident, and none of them is a different server.
    A different path IS a different server: ``/api/gate`` and ``/api/gate-v2``
    would be two deployments.
    """
    text = (base_url or "").strip()
    if not text:
        return ""
    if "://" not in text:
        text = "//" + text
    parts = urlsplit(text)
    host = (parts.hostname or "").lower()
    if parts.port and parts.port not in (80, 443):
        host = f"{host}:{parts.port}"
    path = parts.path.rstrip("/")
    return f"{host}{path}"


def environment_key(base_url: str) -> str:
    """Short, stable, directory-safe identity for a server."""
    normalised = normalize_base_url(base_url) or "unconfigured"
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()[:KEY_LENGTH]


def environment_label(base_url: str) -> str:
    """What to show a person: the host, e.g. ``portal.sitedu.info``."""
    normalised = normalize_base_url(base_url)
    if not normalised:
        return "not configured"
    return normalised.split("/", 1)[0]
