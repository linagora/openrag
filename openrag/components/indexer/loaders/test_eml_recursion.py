"""Regression test for #364 — EmlLoader nested .eml recursion is capped.

The loader recursed into .eml attachments via ``aload_document`` with no
depth counter, so .eml files nested inside one another recursed without
bound. The fix threads a ``_eml_recursion_depth`` keyword and stops
descending once the operator-tunable ``loader.eml_max_recursion_depth``
is reached.
"""

import base64

import pytest


def _make_eml_with_attached_eml(inner_bytes: bytes, subject: str = "outer") -> bytes:
    """Build a multipart/mixed .eml whose attachment (Content-Disposition:
    attachment; filename=nested.eml) is the given inner .eml bytes.
    """
    encoded = base64.b64encode(inner_bytes).decode("ascii")
    boundary = "============TEST_BOUNDARY============"
    return (
        f"Subject: {subject}\r\n"
        "From: a@example.com\r\n"
        "To: b@example.com\r\n"
        "MIME-Version: 1.0\r\n"
        f'Content-Type: multipart/mixed; boundary="{boundary}"\r\n'
        "\r\n"
        f"--{boundary}\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n"
        "\r\n"
        "outer body\r\n"
        f"--{boundary}\r\n"
        'Content-Type: application/octet-stream; name="nested.eml"\r\n'
        'Content-Disposition: attachment; filename="nested.eml"\r\n'
        "Content-Transfer-Encoding: base64\r\n"
        "\r\n"
        f"{encoded}\r\n"
        f"--{boundary}--\r\n"
    ).encode("ascii")


def _make_leaf_eml() -> bytes:
    return b"Subject: leaf\r\nFrom: a@example.com\r\nTo: b@example.com\r\n\r\nleaf body\r\n"


@pytest.mark.asyncio
async def test_eml_recursion_caps_at_max_depth(tmp_path):
    from components.indexer.loaders.eml_loader import EmlLoader

    # A single outer .eml whose attachment is another .eml. We seed the
    # call at depth = cap - 1, so processing the outer's attachment (which
    # would push us past the cap) trips the guard. This proves the depth
    # cap stops the descent before the nested .eml is loaded again.
    eml_path = tmp_path / "nested.eml"
    eml_path.write_bytes(_make_eml_with_attached_eml(_make_leaf_eml()))

    loader = object.__new__(EmlLoader)
    loader.loader_classes = {".eml": EmlLoader}
    loader.kwargs = {}
    loader.max_eml_recursion_depth = 5

    seeded_depth = loader.max_eml_recursion_depth - 1
    doc = await loader.aload_document(str(eml_path), _eml_recursion_depth=seeded_depth)
    assert "recursion depth limit" in doc.page_content
    # The body of the outer .eml must still be retained
    assert "outer body" in doc.page_content


@pytest.mark.asyncio
async def test_eml_below_cap_does_not_skip(tmp_path):
    """At depth 0 the guard does not fire — attachments are still attempted."""
    from components.indexer.loaders.eml_loader import EmlLoader

    eml_path = tmp_path / "nested.eml"
    eml_path.write_bytes(_make_eml_with_attached_eml(_make_leaf_eml()))

    loader = object.__new__(EmlLoader)
    loader.loader_classes = {".eml": EmlLoader}
    loader.kwargs = {}
    loader.max_eml_recursion_depth = 5

    doc = await loader.aload_document(str(eml_path), _eml_recursion_depth=0)
    # The guard message should NOT appear at low depth
    assert "recursion depth limit" not in doc.page_content


def test_recursion_cap_lives_in_loader_config():
    """The cap must come from the loader config (operator-tunable), not be
    hardcoded in the loader. Assert the contract: a positive integer."""
    from config import load_config

    cap = load_config().loader.eml_max_recursion_depth
    assert isinstance(cap, int)
    assert cap > 0
