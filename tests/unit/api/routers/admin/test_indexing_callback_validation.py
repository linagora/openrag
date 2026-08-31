"""The upload routes must reject an unusable callback_url before queueing."""

from types import SimpleNamespace

import pytest
from api.routers.admin.indexing import _validate_callback_url
from fastapi import HTTPException


def _config(*, allow_private: bool = False) -> SimpleNamespace:
    return SimpleNamespace(indexing_callback=SimpleNamespace(allow_private_urls=allow_private))


def test_public_https_callback_is_accepted() -> None:
    _validate_callback_url("https://cozy.example.com/ai/index/status", _config())


def test_private_callback_url_is_rejected_by_default() -> None:
    with pytest.raises(HTTPException) as exc:
        _validate_callback_url("http://127.0.0.1:8080/cb", _config())

    assert exc.value.status_code == 400


def test_private_callback_url_is_accepted_under_the_dev_opt_in() -> None:
    _validate_callback_url("http://cozy.localhost:8080/cb", _config(allow_private=True))


def test_no_callback_url_is_a_noop() -> None:
    _validate_callback_url(None, _config())


@pytest.mark.parametrize("callback_url", ["http://[::1/cb", "https://cozy.example.com:abc/cb"])
def test_malformed_callback_url_is_a_bad_request_not_a_crash(callback_url: str) -> None:
    """urlparse's own ``.port`` raises ValueError on these; unguarded that is a 500."""
    with pytest.raises(HTTPException) as exc:
        _validate_callback_url(callback_url, _config())

    assert exc.value.status_code == 400
