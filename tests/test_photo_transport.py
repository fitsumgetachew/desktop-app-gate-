"""How a staff photo is fetched — both contract-legal shapes, and the token.

The portal serves `/sync/staff-photo/{uid}/{position}` with the bytes directly
(200), but reserves the right to hand off to signed storage (302) later. Both
must work, and in neither case may the gate's bearer token reach the storage
layer.

That last point is not theoretical. ``requests`` does strip Authorization when
following a redirect, but only when the *hostname* changes — a same-host hand-off
(a hosting rewrite, say) keeps it. Since the portal is served from a hosting
domain, the client follows the redirect by hand instead of inheriting somebody
else's rule.
"""

import pytest
import requests

from smart_gate.services.api_client import ApiClient
from smart_gate.utils.config import load_config

JPEG = b"\xff\xd8\xffPHOTOBYTES"


class _Response:
    def __init__(self, status_code=200, content=b"", headers=None):
        self.status_code = status_code
        self.content = content
        self.headers = headers or {}

    @property
    def is_redirect(self):
        return self.status_code in (301, 302, 303, 307, 308) and "Location" in self.headers

    is_permanent_redirect = property(lambda self: self.status_code in (301, 308))

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code), response=self)


class _Session:
    """Records every GET so the tests can assert on headers, not just results."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, headers=None, timeout=None, allow_redirects=True, **kw):
        self.calls.append(
            {"url": url, "headers": headers or {}, "allow_redirects": allow_redirects}
        )
        return self.responses.pop(0)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "app.env"))
    config = load_config()
    config.api_base_url = "https://portal.example/api/gate"
    return ApiClient(config)


# ── 200: the bytes come straight back ─────────────────────────────────


def test_a_relative_url_answered_with_200_returns_the_bytes(client):
    client.session = _Session(_Response(200, JPEG))

    assert client.download_photo("/sync/staff-photo/stf-1/1", token="TOK") == JPEG

    call = client.session.calls[0]
    assert call["url"] == "https://portal.example/api/gate/sync/staff-photo/stf-1/1"
    assert call["headers"]["Authorization"] == "Bearer TOK"


# ── 302: followed by hand, with no credential on the second hop ───────


def test_a_302_is_followed_to_storage(client):
    client.session = _Session(
        _Response(302, headers={"Location": "https://storage.example/signed.jpg?sig=x"}),
        _Response(200, JPEG),
    )

    assert client.download_photo("/sync/staff-photo/stf-1/1", token="TOK") == JPEG
    assert client.session.calls[1]["url"] == "https://storage.example/signed.jpg?sig=x"


def test_the_bearer_token_never_reaches_the_redirect_target(client):
    client.session = _Session(
        _Response(302, headers={"Location": "https://storage.example/signed.jpg"}),
        _Response(200, JPEG),
    )

    client.download_photo("/sync/staff-photo/stf-1/1", token="TOK")

    portal_hop, storage_hop = client.session.calls
    assert portal_hop["headers"]["Authorization"] == "Bearer TOK"
    assert "Authorization" not in storage_hop["headers"]


def test_the_token_is_withheld_even_when_storage_is_the_same_host(client):
    """The case ``requests`` would NOT protect: same hostname, so its own
    redirect handling forwards the header. Ours must not."""
    client.session = _Session(
        _Response(302, headers={"Location": "https://portal.example/storage/signed.jpg"}),
        _Response(200, JPEG),
    )

    client.download_photo("/sync/staff-photo/stf-1/1", token="TOK")

    assert "Authorization" not in client.session.calls[1]["headers"]


def test_redirects_are_never_followed_automatically(client):
    """If the library followed them for us the header rule would be its choice,
    not ours — so the first hop must explicitly opt out."""
    client.session = _Session(
        _Response(302, headers={"Location": "https://storage.example/x.jpg"}),
        _Response(200, JPEG),
    )

    client.download_photo("/sync/staff-photo/stf-1/1", token="TOK")

    assert client.session.calls[0]["allow_redirects"] is False


def test_a_redirect_without_a_location_is_an_error_not_a_hang(client):
    client.session = _Session(_Response(302, headers={}))

    with pytest.raises(requests.HTTPError):
        client.download_photo("/sync/staff-photo/stf-1/1", token="TOK")


# ── Absolute URLs keep working ────────────────────────────────────────


def test_an_absolute_url_is_fetched_verbatim_and_unauthenticated(client):
    """Still contract-legal: the signature in the URL is the credential, and a
    bearer header is at best ignored by S3/GCS and at worst rejected."""
    client.session = _Session(_Response(200, JPEG))

    got = client.download_photo("https://storage.example/signed.jpg?sig=x", token="TOK")

    assert got == JPEG
    call = client.session.calls[0]
    assert call["url"] == "https://storage.example/signed.jpg?sig=x"
    assert "Authorization" not in call["headers"]


def test_a_failed_download_raises(client):
    client.session = _Session(_Response(404))

    with pytest.raises(requests.HTTPError):
        client.download_photo("/sync/staff-photo/stf-1/1", token="TOK")
