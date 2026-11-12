"""tools/scrub_n8n_export.py: addresses of any scheme, look-alike and non-ASCII e-mail domains, and the connections of a
workflow. Every value that the repository's own scans would flag is built from parts at run time, so this file holds
no host name, address or e-mail of its own."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tools.scrub_n8n_export import _clean, scrub

REPO = Path(__file__).resolve().parents[1]

RESERVED = "example" + "." + "test"
REPLACEMENT_MAIL = "someone" + "@" + RESERVED  # what the scrub writes in place of an address


def _j(*labels: str) -> str:
    return ".".join(labels)


def _u(scheme: str, rest: str) -> str:
    return scheme + ":" + "//" + rest


def _at(user: str, domain: str) -> str:
    return user + "@" + domain


CORP = _j("files", "corp", "example", "org")
DB = _j("db", "internal", "corp")
KEPT = [
    "write to a@example.com",
    "a@sub.example.test",
    "end of sentence a@example.invalid.",
    "http://localhost:5678/webhook/x",
    "https://127.0.0.1/x",
    "http://localhost",
    "http://localhost/ok.",
    "ws://localhost:5678/socket",
    "{data: 1}",
    "use javascript: it works",
    "see data: below",
]
SECRETLIKE = "api_" + "key=" + "SOME" + "THING"  # a fake secret in a query: built from parts
# A localhost web address keeps scheme, host, port and path; the query and the fragment are dropped (an API key can sit
# in either): (input, what must be left)
QUERY_DROPPED = [
    ("http://localhost:5678/webhook/x?" + SECRETLIKE, "http://localhost:5678/webhook/x"),
    ("http://127.0.0.1:5679?x=1", "http://127.0.0.1:5679"),
    (_u("http", "localhost#top"), "http://localhost"),
    ("see https://127.0.0.1/p#access_token=abc end", "see https://127.0.0.1/p end"),
    ("ws://localhost:1/a?" + SECRETLIKE + "#f", "ws://localhost:1/a"),
    ("wss://localhost/s/?" + SECRETLIKE, "wss://localhost/s/"),
]
GONE = [
    # any scheme but http, https, ws and wss with a localhost host goes whole (once only a web address was thought of)
    ("file:" + "//localhost/home/someone/.ssh/id_rsa", ["someone", "id_rsa", "home"]),
    (_u("postgres", "localhost:5432/leads"), ["5432", "leads"]),
    (_u("ftp", "localhost/in/x.csv"), ["x.csv", "in/"]),
    (_u("ssh", "127.0.0.1:22/repo.git"), ["22", "repo"]),
    (_u("redis", "localhost"), ["redis"]),
    ("file:" + "//127.0.0.1/etc/passwd", ["etc", "passwd"]),
    # JSON-escaped slashes (a JSON text kept inside a string): the address is read as an address
    ("https:" + "\\/\\/" + CORP + "\\/x", [CORP, "corp"]),
    ("http:" + "\\/\\/localhost\\/x?" + SECRETLIKE, ["SOMETHING"]),
    (_u("ftp", "user:secretword@" + CORP + "/in/x.csv"), [CORP, "secretword", "user:", "in/x.csv", "corp"]),
    ("file:" + "///home/someone/.ssh/id_rsa", ["someone", "id_rsa"]),
    # the user name, the password, the host, the port and the database name must all be gone
    (_u("postgres", "app:pw1234@" + DB + ":5432/leads"), ["app:", "pw1234", DB, "5432", "leads"]),
    (_u("wss", _j("stream", "vendor", "net") + "/live"), [_j("vendor", "net"), "stream"]),
    (
        f"mail {_at('bob', _j('example', 'com', 'au'))} and {_at('jane', _j('example', 'com', 'evil', 'net'))}",
        [_j("example", "com", "au"), _j("evil", "net"), "bob@", "jane@"],
    ),
    (_at("x", _j("example", "com-evil", "org")), ["evil", "x@"]),  # the allowed name followed by - and more letters
    # localhost or 127.0.0.1 as a USER NAME or with a text port: the host is another one
    (_u("http", "localhost:x@" + RESERVED + "/p"), ["x@", RESERVED, "localhost:x"]),
    (_u("http", "127.0.0.1:pw@" + RESERVED + "/x"), ["pw@", RESERVED, "127.0.0.1:pw"]),
    (_u("http", "localhost@" + RESERVED + "/x"), ["@", RESERVED]),
    (_u("http", "127.0.0.1." + RESERVED + "/x"), [RESERVED]),
    (_u("http", "localhost:80@" + RESERVED), ["80@", RESERVED]),
    # the same with one slash (a browser reads it as the same address)
    ("https:" + "/" + CORP + "/x", [CORP, "corp"]),
    ("http:" + "/" + "localhost:x@" + RESERVED, ["x@", RESERVED]),
    # a tab, CR or LF right after a localhost host: a browser drops it, what follows is the same address
    (_u("http", "localhost\t@" + RESERVED), ["@", RESERVED, "\t"]),
    (_u("http", "localhost\n." + RESERVED), [RESERVED, "\n"]),
    (_u("http", "127.0.0.1\r\n@" + RESERVED + "/x"), ["@", RESERVED, "\r"]),
    (_u("http", "localhost:80\t@" + RESERVED), ["80", "@", RESERVED]),
    (_u("HTTPS", "LOCALHOST\t@" + RESERVED.upper()), [RESERVED.upper(), "@"]),
    ("a " + _u("ws", "localhost\t\t@" + RESERVED) + " b", [RESERVED, "@"]),
    # any other blank right after a local host, then an at-sign: a browser keeps the blank in the user name and reads
    # the text after the at-sign as the host (space, VT, FF, NBSP, other Unicode spaces, a run of mixed blanks)
    (_u("http", "localhost @" + RESERVED), ["@", RESERVED]),
    (_u("http", "localhost\u00a0@" + RESERVED), ["@", RESERVED, "\u00a0"]),
    (_u("http", "127.0.0.1 @" + RESERVED), ["@", RESERVED]),
    (_u("http", "localhost\x0b@" + RESERVED), ["@", RESERVED, "\x0b"]),
    (_u("http", "localhost\x0c@" + RESERVED), ["@", RESERVED, "\x0c"]),
    (_u("http", "localhost\u2003@" + RESERVED), ["@", RESERVED, "\u2003"]),
    (_u("http", "localhost\u3000@" + RESERVED), ["@", RESERVED, "\u3000"]),
    (_u("http", "localhost\u2028@" + RESERVED), ["@", RESERVED]),
    (_u("http", "localhost \u00a0\x0b\x0c  @" + RESERVED + "/x"), ["@", RESERVED]),
    (_u("http", "localhost:5678 @" + RESERVED), ["@", RESERVED, "5678"]),
    (_u("http", "localhost :80@" + RESERVED), ["80@", RESERVED]),  # a port-like word in the user name
    (_u("http", "localhost \u200b@" + RESERVED), ["@", RESERVED, "\u200b"]),  # a zero-width space in the user name
    (_u("http", "localhost\u200b@" + RESERVED), ["@", RESERVED, "\u200b"]),  # a zero-width space is not a blank
    (_u("http", "localhost @\uff20" + RESERVED), [RESERVED]),  # the full-width at-sign
    (_u("http", "localhost \uff20" + RESERVED), [RESERVED]),
    (_u("wss", "LOCALHOST\u00a0@" + RESERVED.upper()), [RESERVED.upper(), "@"]),
    (_u("http", "localhost\t @" + RESERVED), ["@", RESERVED]),  # a tab and then a space
    ("go " + _u("http", "localhost @" + RESERVED) + " now", [RESERVED, "@"]),
    # links that carry code or content
    ("open javascript:alert(document.cookie) now", ["alert", "cookie"]),
    ("data:text/plain;base64,QUJDREVGRw== end", ["QUJDREVGRw"]),
    # e-mail addresses with letters outside a-z: nothing of the address may stay
    (_at("josé", _j("corp", "example")), ["jos", "corp", "@" + _j("corp", "example")]),
    (_at("a", "exämple" + ".com"), ["ämple", "ä", "a@", ".com"]),
    (_at("用户", _j("corp", "example")), ["用", "corp"]),
    (_at("a", "例え" + _j("", "jp")), ["例", "jp"]),
    ("a＠" + _j("corp", "example"), ["corp", "＠"]),
    # an address right after a digit, "-", "+" or "." (a start look-behind once let these through whole)
    ("5" + _u("http", CORP + "/x"), [CORP, "corp"]),
    ("-" + _u("https", CORP + "/x"), [CORP, "corp"]),
    ("." + _u("ftp", CORP + "/x"), [CORP, "corp"]),
    ("+" + _u("http", CORP + "/x"), [CORP, "corp"]),
    ("a-" + _u("http", CORP + "/x"), [CORP, "corp"]),
    ("5" + _u("ftp", "user:secretword@" + CORP), [CORP, "secretword", "user:"]),
]
# What the scrub does NOT touch (stated in the module text): kept on purpose in these rows so that a change shows up.
LIMITS = [
    "//" + CORP + "/x",  # no scheme at all
    CORP + ":5432",  # a bare host and port
    "http%3A%2F%2F" + CORP + "%2Fx",  # the scheme with percent signs
    "http:" + "\\\\" + CORP + "\\x",  # the scheme with backslashes
    "data:,hello",  # data: without a media type
    "data:;base64,QUJD",
    "a" + "\ufe6b" + CORP,  # small at-sign (U+FE6B): not read as an at-sign
]


@pytest.mark.parametrize(("text", "left"), QUERY_DROPPED)
def test_a_localhost_web_address_loses_its_query_and_fragment(text: str, left: str) -> None:
    assert _clean(text) == left


@pytest.mark.parametrize("text", KEPT)
def test_allowed_values_are_kept(text: str) -> None:
    assert _clean(text) == text


@pytest.mark.parametrize(("text", "must_not_remain"), GONE)
def test_addresses_of_any_kind_are_replaced(text: str, must_not_remain: list[str]) -> None:
    out = _clean(text)
    assert not [w for w in must_not_remain if w in out], out


def test_a_non_ascii_address_becomes_the_plain_stand_in_and_nothing_of_it_stays() -> None:
    assert _clean(_at("a", "exämple" + ".com")) == REPLACEMENT_MAIL
    assert _clean("x " + _at("josé", _j("corp", "example")) + " y") == "x " + REPLACEMENT_MAIL + " y"


@pytest.mark.parametrize("text", LIMITS)
def test_limit_an_address_without_a_scheme_is_not_cleaned(text: str) -> None:
    """A stated limit of the module (see its text), pinned: free text is not parsed for bare hosts."""
    assert _clean(text) == text


# A blank after a complete local address followed by words: not an at-sign look-alike, so nothing is eaten.
KEEP_AFTER_BLANK = [
    "see " + _u("http", "localhost:5678/x y"),
    "see " + _u("http", "localhost:5678/x\u00a0y"),
    _u("http", "localhost y"),  # a bare local address, a space, a plain word
    _u("http", "localhost\u00a0words and more"),
    _u("http", "127.0.0.1\x0bwords"),
    _u("http", "localhost\u3000words"),
    _u("http", "localhost:5678 and /x@" + RESERVED),
    _u("http", "localhost x/y@" + RESERVED),  # a / ends the user name part
]


@pytest.mark.parametrize("text", KEEP_AFTER_BLANK)
def test_a_blank_after_a_local_address_without_an_at_sign_next_is_not_eaten(text: str) -> None:
    assert _clean(text) == text


def test_the_path_of_a_local_address_ends_the_address_before_a_blank_and_an_at_sign() -> None:
    assert _clean(_u("http", "localhost:5678/p?q=1 @" + RESERVED)) == "http://localhost:5678/p @" + RESERVED


def test_every_character_that_python_reads_as_a_blank_is_covered_right_after_a_local_host() -> None:
    """Every code point of Unicode that str.isspace() (and so the pattern's \\s) reads as a blank, in front of an
    at-sign: the host behind it is gone. Checked over the whole code space, not a hand-picked list."""
    blanks = [chr(c) for c in range(0x110000) if chr(c).isspace()]
    assert len(blanks) >= 25  # tab, LF, VT, FF, CR, FS..US, space, NEL, NBSP, U+2000.. and so on
    leaks = [
        hex(ord(c))
        for c in blanks
        for host in ("localhost", "127.0.0.1")
        if RESERVED in _clean(_u("http", host + c + "@" + RESERVED))
    ]
    assert not leaks, leaks


def test_a_line_break_right_after_a_localhost_host_costs_only_the_next_word() -> None:
    """The smaller safe change: the break and the next run of non-blank characters go, the text after that stays."""
    assert _clean(_u("http", "localhost") + "\nnext line") == "http://localhost/REPLACE-ME line"


def test_limit_a_bare_local_address_then_a_space_then_an_allowed_e_mail_address_is_replaced_too() -> None:
    """Stated over-cleaning (docstring and docs/settings.md): the e-mail address next to a bare local address, after a
    blank, is read as the user name part of that address."""
    assert _clean(_u("http", "localhost jane@" + RESERVED)) == "http://localhost/REPLACE-ME"


def test_limit_a_secret_inside_the_path_of_a_kept_address_stays() -> None:
    """Stated limit (docstring and docs/settings.md): only a real ? or # ends the path, so a token written inside the
    path, or a %3F-encoded query inside it, is kept."""
    token = "tok" + "en123"
    assert _clean("http://localhost:5678/webhook/" + token) == "http://localhost:5678/webhook/" + token
    assert _clean("http://localhost/x%3Fkey=" + token) == "http://localhost/x%3Fkey=" + token


def test_limit_an_address_that_an_expression_completes_loses_its_question_mark() -> None:
    """Stated over-cleaning: the string is read as a plain address and cut at the question mark."""
    assert _clean('"http://localhost:5678/x?" + $json.q') == '"http://localhost:5678/x" + $json.q'


# Short phrases for the limits and the over-cleaning. The test below requires (1) every phrase in BOTH the module text
# and docs/settings.md, (2) every item of the module's "NOT cleaned" bullets, and every item of the docs' "Not
# cleaned" list, to carry at least one of these phrases (so a limit written on one side only, with no phrase here, is
# caught), and (3) the two over-cleaning sentences to carry theirs.
LIMIT_PHRASES = (
    "an address without a scheme",
    "a bare host name",
    "a path that is not a url",
    "percent signs",
    "a secret inside the path",
    "%3f",
    "a data: link without a media type",
    "u+fe6b",
    "ideographic full stop",
    "the keys of a dictionary",
    "refers to another node by an old name",
)
OVER_CLEANING_PHRASES = ("an address that an expression completes", "a bare local address, a space and then an e-mail")
BLANK_PHRASES = (
    "a tab, a carriage return or a line feed",
    "a form feed, a no-break space or another unicode space",
    "an at-sign before any",
)


def _flat(text: str) -> str:
    import re

    return re.sub(r"\s+", " ", text.replace("`", "")).lower()


def _items(section: str) -> list[str]:
    """The list items of a limits section: split at the bullets of the module text and at each ';' that is not inside
    a pair of brackets."""
    items, depth, current = [], 0, ""
    for part in section.replace(" - ", " ; ").split(" "):
        depth += part.count("(") - part.count(")")
        if part == ";" and depth == 0:
            items.append(current.strip())
            current = ""
            continue
        current += " " + part
        if part.endswith(";") and depth == 0:
            items.append(current.strip())
            current = ""
    items.append(current.strip())
    return [i for i in items if i]


def _limit_sections() -> tuple[str, str, str, str]:
    import tools.scrub_n8n_export as module

    doc = _flat(module.__doc__ or "")
    settings = _flat((REPO / "docs" / "settings.md").read_text(encoding="utf-8"))
    mod_list = doc[doc.index("what is not cleaned") : doc.index("over-cleaning, also by design")]
    docs_start = settings.index("**not cleaned**")
    docs_list = settings[docs_start : settings.index("over-cleaning by design", docs_start)]
    return doc, settings, mod_list, docs_list


def test_the_docs_and_the_module_text_carry_the_same_limit_phrases_and_every_listed_limit_has_one() -> None:
    doc, settings, mod_list, docs_list = _limit_sections()
    missing = [
        (p, "module" if p not in doc else "docs")
        for p in (*LIMIT_PHRASES, *OVER_CLEANING_PHRASES, *BLANK_PHRASES)
        if p not in doc or p not in settings
    ]
    assert not missing, f"stated in only one of the module text and docs/settings.md: {missing}"
    for where, section in (("module text", mod_list), ("docs/settings.md", docs_list)):
        items = _items(section)[1 if where == "module text" else 0 :]  # the module's first piece is only a heading
        assert len(items) >= 5, (where, items)
        unlisted = [i for i in items if not any(p in i for p in LIMIT_PHRASES)]
        assert not unlisted, (
            f"{where} lists a limit that the shared phrase list does not know (add it to both): {unlisted}"
        )


def test_limit_an_ideographic_full_stop_ends_the_domain_of_an_address() -> None:
    """Stated limit: only the part up to the ideographic full stop (U+3002) is read as the domain."""
    stop = "\u3002"
    assert _clean("a@corp" + stop + "example") == REPLACEMENT_MAIL + stop + "example"


def test_limit_the_keys_of_a_dictionary_inside_node_parameters_are_not_cleaned() -> None:
    """Stated limit: only the values are cleaned. A key that is an address stays (a made-up name for the test)."""
    key = _u("http", CORP + "/k")
    out = scrub({"nodes": [{"name": "n", "parameters": {key: _u("http", CORP + "/v")}}]})
    assert out["nodes"][0]["parameters"] == {key: "http://localhost/REPLACE-ME"}


def _workflow(names: list[str]) -> dict[str, object]:
    link = [{"node": names[1], "type": "main", "index": 0}, {"node": names[2], "type": "main", "index": 0}]
    return {
        "name": "w " + _at("x", _j("corp", "example")),
        "nodes": [{"name": n, "type": "n8n-nodes-base.set", "parameters": {}} for n in names],
        "connections": {
            names[0]: {"main": [link]},
            names[1]: {"main": [[{"node": names[2], "type": "main", "index": 0}]]},
        },
    }


def test_connections_use_the_cleaned_node_names_and_leak_nothing() -> None:
    names = ["Start", "Mail " + _at("bob", _j("corp", "example")), "Mail " + _at("eve", _j("corp", "example"))]
    original = _workflow(names)
    snapshot = json.dumps(original, sort_keys=True)
    out = scrub(original)
    assert json.dumps(original, sort_keys=True) == snapshot, "the input must not be changed"
    text = json.dumps(out, ensure_ascii=False)
    assert "bob" not in text and "eve" not in text and "corp" not in text, text
    node_names = [n["name"] for n in out["nodes"]]
    assert len(set(node_names)) == 3, "two cleaned names must not be the same name"
    refs = set(out["connections"]) | {
        c["node"] for src in out["connections"].values() for branch in src["main"] for link in branch for c in [link]
    }
    assert refs <= set(node_names), f"dangling references: {sorted(refs - set(node_names))}"
    # the shape is the same: Start leads to both mails, the first mail leads to the second
    assert [c["node"] for c in out["connections"]["Start"]["main"][0]] == node_names[1:]
    assert [c["node"] for c in out["connections"][node_names[1]]["main"][0]] == [node_names[2]]


# every start character that is part of a scheme or a local part, in front of each form that must go
PREFIXES = ["", "5", "-", ".", "+", "_", "a", "a-", "a.", "9a", "--", "..", "5-", "(", "[", "="]


@pytest.mark.parametrize("prefix", PREFIXES)
@pytest.mark.parametrize(
    ("form", "must_not_remain"),
    [
        (_u("http", CORP + "/x"), [CORP]),
        (_u("ftp", CORP + "/x"), [CORP]),
        (_u("ssh", "git@" + CORP), [CORP, "git@"]),
        ("https:" + "/" + CORP + "/x", [CORP]),
        ("javascript:alert(document.cookie)", ["alert", "cookie"]),
        ("vbscript:msgbox", ["msgbox"]),
        ("data:text/plain;base64,QUJDREVGRw==", ["QUJDREVGRw"]),
        (_at("jane", CORP), ["jane@", CORP]),
        (_at("josé", CORP), ["jos", CORP]),
    ],
)
def test_a_prefix_in_front_of_an_address_does_not_let_it_through(
    prefix: str, form: str, must_not_remain: list[str]
) -> None:
    out = _clean(prefix + form)
    assert not [w for w in must_not_remain if w in out], out


@pytest.mark.parametrize("prefix", ["5", "-", ".", "+"])
def test_the_character_in_front_of_a_scheme_may_stay_but_the_address_goes(prefix: str) -> None:
    """A chosen result: the scheme must start with a letter, so a leading digit, "-", "." or "+" is not part of the
    address and stays; everything from the scheme on is replaced."""
    assert _clean(prefix + _u("http", CORP + "/x")) == prefix + "http://localhost/REPLACE-ME"


# --- linearity: a 1 MB string of the worst shape for each pattern must be scanned in a fixed small time ----------
BIG = 1_000_000
# Measured 0.02 to 0.10 s per pattern on a 2026 development machine (see the evidence file); 2 s is a limit with more
# than ten times the margin for a slow runner. A pattern that backtracks badly takes minutes, not seconds.
LIMIT_SECONDS = 2.0


def _worst_shapes(n: int) -> dict[str, str]:
    return {
        "letters": "a" * n,
        "dots": "." * n,
        "hyphens": "-" * n,
        "a.a.a. chain": "a." * (n // 2),
        "a-a-a- chain": "a-" * (n // 2),
        "many http prefixes without a host": "http" * (n // 4),
        "http: without slashes": "http:" * (n // 5),
        "http:// repeated": "http:// " * (n // 8),
        "long word run, then a character that does not match": "a" * (n - 1) + " ",
        "word-and-hyphen run, then !": "a-" * (n // 2 - 1) + "!",
        "many @": "a@" * (n // 2),
        "one @ then dotted labels": "a@" + "a." * (n // 2 - 1),
        "one @ then a long run": "x@" + "a" * (n - 2),
        "label chain that nearly is the allowed domain": "a@" + "example." * (n // 8),
        "scheme names without a colon": "javascript" * (n // 10),
        "data: prefixes": "data:a/" * (n // 7),
        "non-ASCII letters": "é" * n,
        "escaped slashes after a scheme-like word": "a:" + "\\/" * (n // 2),
        "scheme-like words with escaped slashes": "a:\\/\\" * (n // 5),
        "localhost address with a very long path": "http://localhost/" + "a" * (n - 17),
        "localhost address, then many ? and #": "http://localhost/" + "?#" * ((n - 17) // 2),
        "many localhost addresses": "http://localhost/ " * (n // 18),
        "many localhost-like hosts": ("http" + "://localhost.a ") * (n // 19),
        "localhost, then only tabs": "http" + "://localhost" + "\t" * (n - 16),
        "localhost, a tab, then a long run": "http" + "://localhost\t" + "a" * (n - 17),
        "many localhost addresses with a tab": ("http" + "://localhost\t@a ") * (n // 18),
        "many localhost addresses with a line break": ("http" + "://127.0.0.1:1\n") * (n // 16),
        "long scheme-like word run before localhost": "a" * (n - 20) + "://localhost\t",
        # blanks other than tab/CR/LF after the local host (space, VT, FF, NBSP, other Unicode spaces)
        "localhost, then only spaces": "http" + "://localhost" + " " * (n - 16),
        "localhost, then only no-break spaces": "http" + "://localhost" + "\u00a0" * (n - 16),
        "localhost, a long mixed blank run": "http" + "://localhost" + " \x0b\x0c\u00a0\u3000" * ((n - 16) // 5),
        "localhost, a long blank run, then a tab at the very end": "http" + "://localhost" + " " * (n - 17) + "\t",
        "localhost, spaces, then a long run without an at-sign": "http" + "://localhost  " + "a" * (n - 18),
        "localhost, spaces, then a long run, a slash, an at-sign": "http" + "://localhost  " + "a" * (n - 20) + "/@",
        "localhost, a space, then a long run ending in an at-sign": "http" + "://localhost " + "a" * (n - 17) + "@a",
        "many localhost addresses, a space, an at-sign": ("http" + "://localhost @a ") * (n // 17),
        "many localhost addresses, a no-break space, a word": ("http" + "://localhost\u00a0word ") * (n // 22),
        "many localhost addresses, two spaces, a long word": ("http" + "://127.0.0.1  " + "w" * 40 + " ") * (n // 58),
        "many localhost addresses with a full-width at-sign": ("http" + "://localhost \uff20a ") * (n // 18),
        "long scheme-like run, localhost, spaces": "a" * (n - 30) + "://localhost" + " " * 14,
    }


# The scan runs in its own Python process: a pattern that backtracks badly would otherwise hang the whole test run (a
# regex call cannot be interrupted); here the process is stopped at the hard limit and the test fails with a message.
_PROBE = """
import json, sys, time
sys.path.insert(0, sys.argv[1])
import tools.scrub_n8n_export as m
shapes = json.loads(sys.stdin.read())
pattern = getattr(m, sys.argv[2])
run = pattern if sys.argv[2] == "_clean" else (lambda text: pattern.sub("x", text))
slow = []
for shape, text in shapes.items():
    start = time.perf_counter()
    run(text)
    took = time.perf_counter() - start
    if took > float(sys.argv[3]):
        slow.append(shape + ": " + format(took, ".2f") + " s")
print(json.dumps(slow))
"""


@pytest.mark.parametrize("name", ["URL", "MAIL", "ACTIVE_LINK", "_LOCAL_BROKEN", "_clean"])
def test_each_pattern_scans_one_megabyte_of_its_worst_shapes_in_linear_time(name: str) -> None:
    hard_stop = LIMIT_SECONDS * len(_worst_shapes(0)) + 30  # the sum of all limits and the start of Python
    try:
        done = subprocess.run(  # noqa: S603 (our own interpreter and our own probe text)
            [sys.executable, "-c", _PROBE, str(REPO), name, str(LIMIT_SECONDS)],
            input=json.dumps(_worst_shapes(BIG)),
            capture_output=True,
            text=True,
            timeout=hard_stop,
            check=True,
        )
    except subprocess.TimeoutExpired:  # pragma: no cover - only when a pattern backtracks without bound
        pytest.fail(f"{name} did not finish 1 MB of its worst shapes in {hard_stop:.0f} s: it is not linear")
    slow = json.loads(done.stdout)
    assert not slow, f"{name} takes more than {LIMIT_SECONDS} s on 1 MB of: {slow}"
