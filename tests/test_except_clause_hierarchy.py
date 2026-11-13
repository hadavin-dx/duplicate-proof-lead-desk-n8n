"""Pins the real `except` clauses that the static-analysis tidy-up shortened or kept (made-up data only).

The clauses are read from the source files with `ast`, not copied by hand, and every class name in them is looked up
in the module it is written in. So the tests fail when someone edits a clause, not only when Python moves a class:

* the handler order of each guarded function is exactly as listed below;
* the six handlers that hand MemoryError (and, in phase 1 of a send, SendRefused and SendUnreachable) straight back
  are still there, still only `raise`, and still come before the `except Exception` that would otherwise turn those
  errors into SendUnreachable or SendAmbiguous;
* a shortened clause still catches the names it lost (UnicodeError through ValueError, TimeoutError, URLError,
  socket.timeout and ssl.SSLError through OSError) and names no class twice (no class next to its own parent);
* no handler is hidden by an earlier, broader one in the same `try`;
* the errors that used to be re-raised by hand (CrmError, LiveCapReached) and LiveTransport are caught by no handler
  of the `try` they are raised in, so they still reach the caller unchanged."""

from __future__ import annotations

import ast
import builtins
import importlib
import socket
import ssl
import urllib.error
from pathlib import Path
from typing import Any

import pytest

from lead_desk import crm_hubspot, live_claude
from lead_desk.adapters import CrmNothingStored

REPO = Path(__file__).resolve().parent.parent
SENDERS = ("src/lead_desk/smtp_outbox.py", "src/lead_desk/smtp_provider.py")

# (file, qualified function name) -> the handlers of each `try` in that function, in source order.
HANDLERS: dict[tuple[str, str], list[list[str]]] = {
    ("src/lead_desk/crm_hubspot.py", "HubspotCrm._request"): [
        ["urllib.error.HTTPError", "urllib.error.URLError", "(OSError, ValueError, http.client.HTTPException)"],
        ["(CrmError, OSError, http.client.HTTPException)"],
    ],
    ("src/lead_desk/live_claude.py", "SpendLedger._totals"): [
        ["(ValueError, KeyError, TypeError, ArithmeticError, RecursionError, OSError, MemoryError)"],
        ["FileNotFoundError"],
    ],
    ("src/lead_desk/live_claude.py", "LiveQualifier._post_once"): [
        ["urllib.error.HTTPError", "(OSError, ValueError, http.client.HTTPException)"],
        ["(OSError, http.client.HTTPException)"],
        ["ValueError"],
    ],
    ("src/lead_desk/smtp_outbox.py", "SmtpOutbox.send"): [["(ValueError, MessageError)"], ["OSError"]],
    ("src/lead_desk/smtp_outbox.py", "SmtpOutbox._connect"): [
        ["MemoryError", "Exception"],
        ["MemoryError", "Exception"],
    ],
    ("src/lead_desk/smtp_outbox.py", "SmtpOutbox._dialogue"): [
        ["(SendRefused, SendUnreachable, MemoryError)", "Exception"],
        ["MemoryError", "Exception"],
        ["OSError"],
    ],
    ("src/lead_desk/smtp_provider.py", "SmtpProviderOutbox.send"): [["(ValueError, MessageError)"], ["OSError"]],
    ("src/lead_desk/smtp_provider.py", "SmtpProviderOutbox._connect"): [["MemoryError", "Exception"]],
    ("src/lead_desk/smtp_provider.py", "SmtpProviderOutbox._dialogue"): [
        ["(SendRefused, SendUnreachable, MemoryError)", "Exception"],
        ["MemoryError", "Exception"],
        ["OSError"],
    ],
    ("src/lead_desk/config.py", "read_approvers"): [["OSError"], ["(ValueError, RecursionError)"]],
    ("src/lead_desk/config.py", "_read_leadflow"): [["OSError"], ["(ValueError, RecursionError)"]],
    ("demo_only/fake_imap.py", "FakeImap._serve"): [["(OSError, ValueError, IndexError, KeyError)"]],
    ("demo_only/fake_smtp_tls.py", "FakeSmtp._serve"): [["(OSError, ValueError)"], ["OSError"]],
    ("load_tools/catcher.py", "Catcher.handle"): [
        ["(OSError, asyncio.IncompleteReadError, asyncio.LimitOverrunError)"]
    ],
}

# The six handlers whose whole body is `raise` and that must come before `except Exception` in the same `try`:
# (file, function, index of the try, index of the handler).
RERAISE_BEFORE_EXCEPTION = [
    ("src/lead_desk/smtp_outbox.py", "SmtpOutbox._connect", 0, 0),
    ("src/lead_desk/smtp_outbox.py", "SmtpOutbox._dialogue", 0, 0),
    ("src/lead_desk/smtp_outbox.py", "SmtpOutbox._dialogue", 1, 0),
    ("src/lead_desk/smtp_provider.py", "SmtpProviderOutbox._connect", 0, 0),
    ("src/lead_desk/smtp_provider.py", "SmtpProviderOutbox._dialogue", 0, 0),
    ("src/lead_desk/smtp_provider.py", "SmtpProviderOutbox._dialogue", 1, 0),
]

# A shortened clause and the classes it must still catch (the names that were removed from it).
STILL_CAUGHT = [
    ("src/lead_desk/config.py", "read_approvers", 1, 0, [UnicodeError]),
    ("src/lead_desk/config.py", "_read_leadflow", 1, 0, [UnicodeError]),
    ("src/lead_desk/smtp_outbox.py", "SmtpOutbox.send", 0, 0, [UnicodeError]),
    ("src/lead_desk/smtp_provider.py", "SmtpProviderOutbox.send", 0, 0, [UnicodeError]),
    ("src/lead_desk/smtp_outbox.py", "SmtpOutbox._dialogue", 2, 0, [TimeoutError]),
    ("src/lead_desk/smtp_provider.py", "SmtpProviderOutbox._dialogue", 2, 0, [TimeoutError]),
    ("load_tools/catcher.py", "Catcher.handle", 0, 0, [TimeoutError]),
    ("src/lead_desk/live_claude.py", "LiveQualifier._post_once", 0, 1, [urllib.error.URLError, socket.timeout]),
    ("demo_only/fake_imap.py", "FakeImap._serve", 0, 0, [ssl.SSLError]),
    ("demo_only/fake_smtp_tls.py", "FakeSmtp._serve", 0, 0, [ssl.SSLError]),
]

# An error raised inside the body of a `try` that no handler of that same `try` may catch.
PASSES_THROUGH = [
    ("src/lead_desk/crm_hubspot.py", "HubspotCrm._request", 0, crm_hubspot.CrmError),
    ("src/lead_desk/crm_hubspot.py", "HubspotCrm._request", 0, CrmNothingStored),
    ("src/lead_desk/live_claude.py", "SpendLedger._totals", 0, live_claude.LiveCapReached),
    ("src/lead_desk/live_claude.py", "LiveQualifier._post_once", 0, live_claude.LiveTransport),
]


DEMO_DIR = "demo_only"
DEMO_GONE = not (REPO / DEMO_DIR).is_dir()  # computed once; True only in a client copy with the whole folder deleted


def _present(rows: list[tuple[object, ...]], demo_gone: bool) -> list[object]:
    """The rows as pytest params. Only a row about a file under demo_only/ can be skipped, and only when that whole
    folder was deleted (a client copy, demo_gone). A product row, a typo in a path, or one demo file missing while the
    folder is there is not skipped: it runs and fails with FileNotFoundError."""
    return [
        pytest.param(
            *r,
            id=_ids([r])[0],
            marks=pytest.mark.skipif(
                demo_gone and str(r[0]).startswith(DEMO_DIR + "/"), reason=f"{r[0]}: {DEMO_DIR}/ was removed"
            ),
        )
        for r in rows
    ]


def _ids(rows: list[tuple[object, ...]]) -> list[str]:
    return [f"{r[0]}::{r[1]}" + "".join(f"[{x}]" for x in r[2:4] if isinstance(x, int)) for r in rows]


def _functions(node: ast.AST, prefix: str = "") -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    found: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.ClassDef):
            found.update(_functions(child, f"{prefix}{child.name}."))
        elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            found[prefix + child.name] = child
    return found


def _tries(path: str, function: str) -> list[ast.Try]:
    tree = ast.parse((REPO / path).read_text(encoding="utf-8"), filename=path)
    fn = _functions(tree).get(function)
    assert fn is not None, f"{path}: function {function} not found"
    tries = [n for n in ast.walk(fn) if isinstance(n, ast.Try) and n.handlers]
    return sorted(tries, key=lambda n: (n.lineno, n.col_offset))


def _object(node: ast.expr, namespace: dict[str, object]) -> object:
    if isinstance(node, ast.Name):
        return namespace[node.id] if node.id in namespace else getattr(builtins, node.id)
    if isinstance(node, ast.Attribute):
        return getattr(_object(node.value, namespace), node.attr)
    raise AssertionError(f"unexpected expression in an except clause: {ast.unparse(node)}")


def _classes(handler: ast.ExceptHandler, path: str) -> list[type[BaseException]]:
    """The classes a handler names, looked up in the module the handler is written in."""
    assert handler.type is not None, f"{path}:{handler.lineno}: bare except"
    names = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    module = importlib.import_module(path.removesuffix(".py").replace("/", ".").removeprefix("src."))
    found: list[type[BaseException]] = []
    for n in names:
        c = _object(n, vars(module))
        assert isinstance(c, type) and issubclass(c, BaseException), f"{path}:{handler.lineno}: {ast.unparse(n)}"
        found.append(c)
    return found


@pytest.mark.parametrize(("path", "function"), _present(list(HANDLERS), DEMO_GONE))
def test_handler_order_is_exactly_as_listed(path: str, function: str) -> None:
    seen = [[ast.unparse(h.type) if h.type else "" for h in t.handlers] for t in _tries(path, function)]
    assert seen == HANDLERS[(path, function)]


@pytest.mark.parametrize(("path", "function"), _present(list(HANDLERS), DEMO_GONE))
def test_no_class_is_named_twice_or_hidden_by_an_earlier_handler(path: str, function: str) -> None:
    for t in _tries(path, function):
        earlier: list[type[BaseException]] = []
        for h in t.handlers:
            mine = _classes(h, path)
            for i, a in enumerate(mine):
                for b in mine[i + 1 :]:
                    assert not (issubclass(a, b) or issubclass(b, a)), f"{path}:{h.lineno}: {a} and {b} overlap"
            for c in mine:
                hidden = [e.__name__ for e in earlier if issubclass(c, e)]
                assert not hidden, f"{path}:{h.lineno}: {c.__name__} is already caught by {hidden}"
            earlier += mine


@pytest.mark.parametrize(("path", "function", "t", "h"), _present(RERAISE_BEFORE_EXCEPTION, DEMO_GONE))
def test_memory_and_send_errors_are_handed_back_before_except_exception(
    path: str, function: str, t: int, h: int
) -> None:
    handlers = _tries(path, function)[t].handlers
    handler = handlers[h]
    body = handler.body
    assert len(body) == 1 and isinstance(body[0], ast.Raise) and body[0].exc is None, f"{path}:{handler.lineno}"
    assert MemoryError in _classes(handler, path)
    later = [c for x in handlers[h + 1 :] for c in _classes(x, path)]
    assert Exception in later, f"{path}:{handler.lineno}: no `except Exception` after the re-raise"


def test_the_two_senders_have_exactly_the_six_bare_re_raise_handlers() -> None:
    found = []
    for path in SENDERS:
        for node in ast.walk(ast.parse((REPO / path).read_text(encoding="utf-8"))):
            if isinstance(node, ast.ExceptHandler) and len(node.body) == 1:
                stmt = node.body[0]
                if isinstance(stmt, ast.Raise) and stmt.exc is None:
                    found.append((path, node.lineno))
    listed = [h for p, f, t, i in RERAISE_BEFORE_EXCEPTION for h in [(p, _tries(p, f)[t].handlers[i].lineno)]]
    assert sorted(found) == sorted(listed)


@pytest.mark.parametrize(("path", "function", "t", "h", "must_catch"), _present(STILL_CAUGHT, DEMO_GONE))
def test_shortened_clause_still_catches_the_names_it_lost(
    path: str, function: str, t: int, h: int, must_catch: list[type[BaseException]]
) -> None:
    classes = tuple(_classes(_tries(path, function)[t].handlers[h], path))
    for c in must_catch:
        assert issubclass(c, classes), f"{path}: {c.__name__} is no longer caught"


@pytest.mark.parametrize(
    ("path", "function", "t", "error"),
    PASSES_THROUGH,
    ids=[f"{p}::{f}::{e.__name__}" for p, f, _, e in PASSES_THROUGH],
)
def test_handed_on_error_is_caught_by_no_handler_of_its_try(
    path: str, function: str, t: int, error: type[BaseException]
) -> None:
    for handler in _tries(path, function)[t].handlers:
        assert not issubclass(error, tuple(_classes(handler, path))), f"{path}:{handler.lineno} catches {error}"


# The guard above is what keeps the no-demo copy honest, so it is pinned here, not only measured once by hand.
_GUARDED = (
    test_handler_order_is_exactly_as_listed,
    test_no_class_is_named_twice_or_hidden_by_an_earlier_handler,
    test_memory_and_send_errors_are_handed_back_before_except_exception,
    test_shortened_clause_still_catches_the_names_it_lost,
)


def _skip_condition(param: Any) -> object:
    (mark,) = [m for m in param.marks if m.name == "skipif"]
    return mark.args[0]


@pytest.mark.parametrize("demo_gone", [True, False])
def test_present_skips_only_demo_rows_and_only_when_the_folder_is_gone(demo_gone: bool) -> None:
    rows: list[tuple[object, ...]] = [
        ("src/lead_desk/smtp_outbox.py", "SmtpOutbox.send"),
        ("load_tools/anything.py", "f"),
        ("demo_only/fake_imap.py", "FakeImap._serve"),
        ("demo_only/sub/x.py", "g"),
        ("demo_onlyx/y.py", "h"),  # not inside the folder: its name only starts the same way
    ]
    params = _present(rows, demo_gone)
    assert [p.values for p in params] == rows  # type: ignore[attr-defined]
    got = [_skip_condition(p) for p in params]
    assert got == [False, False, demo_gone, demo_gone, False]
    assert all(type(c) is bool for c in got)


def test_demo_gone_is_the_state_of_the_folder_and_every_guarded_test_uses_it() -> None:
    assert DEMO_GONE is (not (REPO / DEMO_DIR).exists())  # a different probe than _present's own: they must agree
    seen = 0
    for fn in _GUARDED:
        (mark,) = [m for m in fn.pytestmark if m.name == "parametrize"]  # type: ignore[attr-defined]
        for param in mark.args[1]:
            path = str(param.values[0])
            assert _skip_condition(param) is (DEMO_GONE and path.startswith("demo_only/")), path
            seen += path.startswith("demo_only/")
    assert seen == 6  # the six demo rows are there (else this proves nothing)
    assert any(not str(r[0]).startswith("demo_only/") for r in [*HANDLERS, *RERAISE_BEFORE_EXCEPTION, *STILL_CAUGHT])
