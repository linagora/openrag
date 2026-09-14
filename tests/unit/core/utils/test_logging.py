import json
import logging
import sys
from types import SimpleNamespace

import pytest
from core.utils.logging import (
    _STDLIB_LOGGERS_QUIETED,
    _STDLIB_LOGGERS_TO_FLATTEN,
    RESERVED_JSON_KEYS,
    escape_markup,
    get_logger,
    json_record,
    json_sink,
    mask_email,
    terminal_formatter,
)
from loguru import logger


def _reset_stdlib_logging():
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    root.setLevel(logging.WARNING)
    for name in _STDLIB_LOGGERS_TO_FLATTEN:
        lg = logging.getLogger(name)
        lg.handlers = []
        lg.propagate = True
    for name in _STDLIB_LOGGERS_QUIETED:
        logging.getLogger(name).setLevel(logging.NOTSET)


def _restore_default_logger() -> None:
    """Reinstall the default sinks against the *real* stderr.

    Under pytest ``sys.stderr`` is a per-test ``CaptureIO`` that is closed at
    teardown; ``get_logger()`` would bind loguru's only sink to it and every
    later loguru call in the session would hit a closed stream.
    """
    captured, sys.stderr = sys.stderr, sys.__stderr__
    try:
        get_logger()
    finally:
        sys.stderr = captured


@pytest.fixture(autouse=True)
def _stdlib_logging_is_restored():
    """Undo any stdlib-logging mutation after *every* test in this module.

    ``get_logger`` in JSON mode installs an :class:`InterceptHandler` on the
    stdlib root and lowers/raises library logger levels. Left behind, that
    handler makes unrelated modules' stdlib records surface as loguru output
    and makes this module's own results depend on test order.
    """
    try:
        yield
    finally:
        _reset_stdlib_logging()


def test_mask_email_keeps_first_char_and_domain():
    assert mask_email("alice@example.com") == "a***@example.com"


def test_mask_email_redacts_short_and_uppercase_local_parts():
    assert mask_email("a@example.com") == "a***@example.com"
    assert mask_email("Bob.Smith@corp.io") == "B***@corp.io"


def test_mask_email_handles_missing_or_malformed_values():
    assert mask_email(None) == "***"
    assert mask_email("") == "***"
    assert mask_email("not-an-email") == "***"
    assert mask_email(123) == "***"


def test_escape_markup_escapes_angle_brackets():
    s = "From <notifications@github.com>"
    out = escape_markup(s)
    assert out == r"From \<notifications@github.com\>"


def test_escape_markup_escapes_backslashes_first():
    s = r"path\to\file <tag>"
    out = escape_markup(s)
    # backslashes doubled + angle brackets escaped
    assert out == r"path\\to\\file \<tag\>"


def test_terminal_formatter_appends_exception_placeholder():
    # Regression guard: a callable ``format`` means loguru does NOT auto-append
    # "\n{exception}", so the template must carry it or tracebacks are dropped.
    template = terminal_formatter({"extra": {}})
    assert template.endswith("\n{exception}")
    # Record fields stay as placeholders so loguru substitutes them safely.
    assert "{level: <8}" in template
    assert "{message}" in template


def test_terminal_formatter_escapes_and_brace_guards_extra():
    template = terminal_formatter({"extra": {"file_id": "a<b>", "n": "{x}"}})
    # markup escaped (angle brackets) and braces doubled so extra values are
    # never reparsed as loguru placeholders.
    assert r"file_id=a\<b\>" in template
    assert "n={{x}}" in template
    assert template.endswith("\n{exception}")


@pytest.fixture()
def json_lines():
    """Capture what ``json_sink`` would write, one parsed object per record."""
    captured: list[dict] = []

    def _sink(message):
        captured.append(json_record(message.record))

    handler_id = logger.add(_sink, level="DEBUG")
    try:
        yield captured
    finally:
        logger.remove(handler_id)


def test_json_record_has_reserved_keys_first(json_lines):
    logger.info("hello")
    (payload,) = json_lines
    assert list(payload)[:6] == ["ts", "level", "logger", "function", "line", "msg"]
    assert payload["level"] == "INFO"
    assert payload["msg"] == "hello"
    assert payload["logger"] == __name__
    # ISO-8601 with a timezone offset: "2026-09-07T14:03:12.481000+02:00"
    assert payload["ts"][10] == "T" and payload["ts"][-6] in "+-" and payload["ts"][-3] == ":"


def test_json_record_lifts_extra_to_top_level(json_lines):
    logger.bind(partition="docs", task_id="t-1").info("bound")
    (payload,) = json_lines
    assert payload["partition"] == "docs"
    assert payload["task_id"] == "t-1"
    assert "extra" not in payload


def test_json_record_renames_colliding_extra(json_lines):
    logger.bind(level="custom", msg="shadow").info("collision")
    (payload,) = json_lines
    assert payload["level"] == "INFO"
    assert payload["msg"] == "collision"
    assert payload["extra_level"] == "custom"
    assert payload["extra_msg"] == "shadow"
    assert "exception" in RESERVED_JSON_KEYS


def test_json_record_includes_traceback_on_exception(json_lines):
    try:
        raise ValueError("boom")
    except ValueError:
        logger.exception("failed")
    (payload,) = json_lines
    assert payload["level"] == "ERROR"
    assert "ValueError: boom" in payload["exception"]
    assert "Traceback" in payload["exception"]


def test_json_record_omits_exception_key_without_one(json_lines):
    logger.warning("plain")
    (payload,) = json_lines
    assert "exception" not in payload


def test_json_record_omits_exception_key_for_an_empty_exc_info(json_lines):
    """``exc_info=True`` outside an ``except`` block must not fake a traceback.

    stdlib hands such a record ``(None, None, None)``, from which loguru still
    builds a RecordException; formatting it yields ``"NoneType: None\n"``,
    which Grafana would render as a traceback. Every intercepted library can
    produce this shape, so the JSON sink has to reject it.
    """
    logger.opt(exception=(None, None, None)).error("no exception attached")
    (payload,) = json_lines
    assert "exception" not in payload


def test_json_mode_omits_exception_key_for_stdlib_empty_exc_info(capsys):
    config = SimpleNamespace(verbose=SimpleNamespace(level="INFO", format="json"))
    try:
        get_logger(config)
        logging.getLogger("somelib").error("nothing raised", exc_info=True)
        payload = json.loads(capsys.readouterr().err)
        assert payload["msg"] == "nothing raised"
        assert "exception" not in payload
    finally:
        _restore_default_logger()


def test_json_sink_writes_one_parsable_line_without_ansi(capsys):
    handler_id = logger.add(json_sink, level="DEBUG")
    try:
        logger.bind(request_id="req_1").info("line <b>bold</b> {curly}")
    finally:
        logger.remove(handler_id)
    err = capsys.readouterr().err
    assert err.count("\n") == 1
    assert "\x1b" not in err
    payload = json.loads(err)
    assert payload["msg"] == "line <b>bold</b> {curly}"
    assert payload["request_id"] == "req_1"


def test_json_sink_stringifies_unserialisable_values(capsys):
    handler_id = logger.add(json_sink, level="DEBUG")
    try:
        logger.bind(path=SimpleNamespace(x=1)).info("obj")
    finally:
        logger.remove(handler_id)
    payload = json.loads(capsys.readouterr().err)
    assert payload["path"] == "namespace(x=1)"


def test_get_logger_json_mode_installs_json_sink(capsys):
    config = SimpleNamespace(verbose=SimpleNamespace(level="INFO", format="json"))
    try:
        get_logger(config).info("json mode")
        payload = json.loads(capsys.readouterr().err)
        assert payload["msg"] == "json mode"
    finally:
        _restore_default_logger()


def test_get_logger_text_mode_keeps_colour(capsys):
    config = SimpleNamespace(verbose=SimpleNamespace(level="INFO", format="text"))
    try:
        get_logger(config).info("text mode")
        err = capsys.readouterr().err
        assert "\x1b[" in err
        assert "text mode" in err
    finally:
        _restore_default_logger()


def test_json_mode_intercepts_stdlib_loggers(capsys):
    config = SimpleNamespace(verbose=SimpleNamespace(level="INFO", format="json"))
    try:
        get_logger(config)
        logging.getLogger("uvicorn.error").info("Application startup complete.")
        payload = json.loads(capsys.readouterr().err)
        assert payload["msg"] == "Application startup complete."
        assert payload["level"] == "INFO"
        assert payload["logger"] == "uvicorn.error"
        # The frame walk must land on the code that *called* stdlib logging —
        # this very test function — not on ``logging.callHandlers`` (line 1762
        # of CPython 3.12's logging/__init__.py), which is what a walk that
        # never leaves the logging module reports.
        assert payload["function"] == "test_json_mode_intercepts_stdlib_loggers"
        assert isinstance(payload["line"], int)
        assert payload["line"] != 1762
    finally:
        _restore_default_logger()


def test_json_mode_forwards_stdlib_exc_info(capsys):
    config = SimpleNamespace(verbose=SimpleNamespace(level="INFO", format="json"))
    try:
        get_logger(config)
        try:
            raise RuntimeError("stdlib boom")
        except RuntimeError:
            logging.getLogger("ray").exception("worker died")
        payload = json.loads(capsys.readouterr().err)
        assert payload["level"] == "ERROR"
        assert "RuntimeError: stdlib boom" in payload["exception"]
    finally:
        _restore_default_logger()


def test_json_mode_respects_configured_level_for_stdlib(capsys):
    config = SimpleNamespace(verbose=SimpleNamespace(level="WARNING", format="json"))
    try:
        get_logger(config)
        logging.getLogger("uvicorn.access").info("GET / 200")
        assert capsys.readouterr().err == ""
    finally:
        _restore_default_logger()


def test_json_mode_accepts_loguru_only_levels(capsys):
    """``LOG_LEVEL=TRACE`` (a loguru-only level) must not crash the app.

    ``basicConfig(level="TRACE")`` would raise ``ValueError: Unknown level``;
    the stdlib root is set to loguru's *numeric* level instead (TRACE = 5),
    which stdlib accepts, so the sink and the root agree.
    """
    config = SimpleNamespace(verbose=SimpleNamespace(level="TRACE", format="json"))
    try:
        get_logger(config).trace("trace me")
        payload = json.loads(capsys.readouterr().err)
        assert payload["level"] == "TRACE"
        assert payload["msg"] == "trace me"
    finally:
        _restore_default_logger()


def test_json_mode_quiets_chatty_library_loggers(capsys):
    """Interception ships *every* stdlib logger, so the chatty HTTP/asyncio
    libraries are capped at WARNING — otherwise ``LOG_LEVEL=DEBUG`` floods the
    collector with one line per outbound request."""
    config = SimpleNamespace(verbose=SimpleNamespace(level="DEBUG", format="json"))
    try:
        get_logger(config)
        logging.getLogger("httpx").info("GET /x")
        assert capsys.readouterr().err == ""

        logging.getLogger("httpx").warning("slow")
        payload = json.loads(capsys.readouterr().err)
        assert payload["logger"] == "httpx"
        assert payload["level"] == "WARNING"
    finally:
        _restore_default_logger()


def test_json_mode_ships_unlisted_library_loggers_at_log_level(capsys):
    """Only the five named loggers are capped; anything else follows
    ``LOG_LEVEL`` (that is what makes ``DEBUG`` a troubleshooting setting)."""
    config = SimpleNamespace(verbose=SimpleNamespace(level="DEBUG", format="json"))
    try:
        get_logger(config)
        # A name no installed library owns, so no import-time setLevel of its
        # own can decide the outcome of this test.
        logging.getLogger("some.chatty.library").debug("connecting")
        # Last line, not the whole capture: a loguru stderr sink installed by an
        # earlier test module still points at that test's (now closed) capsys
        # stream, and loguru prints its own handler-error block to the *current*
        # stderr when it fails. Reading the last line keeps this order-independent.
        payload = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
        assert payload["logger"] == "some.chatty.library"
        assert payload["level"] == "DEBUG"
    finally:
        _restore_default_logger()


def test_text_mode_leaves_stdlib_logging_alone(capsys):
    _reset_stdlib_logging()
    config = SimpleNamespace(verbose=SimpleNamespace(level="INFO", format="text"))
    try:
        get_logger(config)
        assert not any(type(h).__name__ == "InterceptHandler" for h in logging.getLogger().handlers)
    finally:
        _restore_default_logger()


def test_json_mode_sets_stdlib_root_to_the_configured_level(capsys):
    """The stdlib root mirrors ``LOG_LEVEL`` numerically so libraries' own
    ``isEnabledFor(DEBUG)`` guards stay false at INFO — otherwise the openai
    client model-dumps the full prompt on every call only for loguru to drop
    it, and at DEBUG the untruncated prompt ships to the collector."""
    try:
        get_logger(SimpleNamespace(verbose=SimpleNamespace(level="INFO", format="json")))
        assert logging.getLogger().level == logging.INFO
        assert not logging.getLogger("openai._base_client").isEnabledFor(logging.DEBUG)
        get_logger(SimpleNamespace(verbose=SimpleNamespace(level="TRACE", format="json")))
        assert logging.getLogger().level == 5
        assert logging.getLogger("some.lib").isEnabledFor(logging.DEBUG)
    finally:
        _restore_default_logger()


def test_intercept_handler_tolerates_bad_format_calls(capsys):
    """A mis-formatted stdlib call in a library must not raise into the
    library — stdlib's own handlers swallow it through ``handleError``."""
    try:
        get_logger(SimpleNamespace(verbose=SimpleNamespace(level="INFO", format="json")))
        logging.getLogger("some.lib").info("value %d", "not-a-number")  # must not raise
        err = capsys.readouterr().err
        assert "Logging error" in err  # stdlib's handleError report, not a traceback out of the caller
    finally:
        _restore_default_logger()


def test_intercept_is_idempotent_and_does_not_close_foreign_handlers(capsys, tmp_path):
    """``get_logger()`` runs at import in ~70 modules: repeated JSON-mode calls
    must leave exactly one interceptor on the root and must detach — not
    ``close()`` — a handler another tool installed (pytest's ``--log-file``
    is a ``FileHandler``; closing it leaves the file empty for the session)."""
    foreign = logging.FileHandler(tmp_path / "tool.log")
    logging.getLogger().addHandler(foreign)
    config = SimpleNamespace(verbose=SimpleNamespace(level="INFO", format="json"))
    try:
        get_logger(config)
        get_logger(config)
        interceptors = [h for h in logging.getLogger().handlers if type(h).__name__ == "InterceptHandler"]
        assert len(interceptors) == 1
        assert foreign not in logging.getLogger().handlers
        assert foreign.stream is not None and not foreign.stream.closed  # FileHandler.close() would None it
    finally:
        foreign.close()
        _restore_default_logger()


def test_json_mode_keeps_an_operator_set_library_level(capsys):
    """An explicit ``httpx`` DEBUG (set by an operator troubleshooting a hung
    call) survives later ``get_logger()`` calls: only loggers still at
    ``NOTSET`` are capped."""
    logging.getLogger("httpx").setLevel(logging.DEBUG)
    try:
        get_logger(SimpleNamespace(verbose=SimpleNamespace(level="DEBUG", format="json")))
        assert logging.getLogger("httpx").level == logging.DEBUG
        assert logging.getLogger("httpcore").level == logging.WARNING
    finally:
        _restore_default_logger()
