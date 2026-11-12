"""n8n/proof.json: the result line says PASS only when the desk answered all ten requests with exactly one `new` and
nine `duplicate` (what a correct desk answers to ten requests for one lead) AND one email and one CRM row exist. The
expression is cut out of the file and run in node with stand-in node outputs. n8n itself is NOT run here: its own
expression engine, and how it finds the output of the Code node "Wait for the desk to finish" from the Set node four
steps later, are not proven by these tests (one real n8n run is needed, see docs/n8n-recipe.md)."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

PROOF = Path(__file__).resolve().parents[1] / "n8n" / "proof.json"
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
TEN_OK = ",".join(["new"] + ["duplicate"] * 9)


def _flow() -> dict[str, object]:
    return json.loads(PROOF.read_text(encoding="utf-8"))


def _expression() -> str:
    node = next(n for n in _flow()["nodes"] if n["name"] == "Show the result")  # type: ignore[attr-defined]
    value = next(a["value"] for a in node["parameters"]["assignments"]["assignments"] if a["name"] == "result")
    assert value.startswith("={{ ") and value.endswith(" }}")
    return value[4:-3]


def _result(statuses: str, emails: int, crm: int) -> str:
    data = {
        "Wait for the desk to finish": {"statuses": statuses},
        "Read the demo outbox": {"count": emails},
        "Read the demo CRM": {"count": crm},
    }
    js = (
        "const data = JSON.parse(process.argv[1]);"
        " const $ = (n) => ({ item: { json: data[n] }, first: () => ({ json: data[n] }) });"
        " console.log(" + _expression() + ");"
    )
    done = subprocess.run(["node", "-e", js, json.dumps(data)], capture_output=True, text=True, check=True)  # noqa: S603, S607
    return done.stdout.strip()


def test_the_expression_names_only_nodes_that_exist() -> None:
    names = {n["name"] for n in _flow()["nodes"]}  # type: ignore[attr-defined]
    used = set(re.findall(r'\$\("([^"]+)"\)', _expression()))
    assert used == {"Wait for the desk to finish", "Read the demo outbox", "Read the demo CRM"}
    assert used <= names


@needs_node
def test_pass_needs_one_new_nine_duplicates_one_email_and_one_crm_row() -> None:
    assert _result(TEN_OK, 1, 1) == "PASS"
    assert _result(",".join(["duplicate"] * 9 + ["new"]), 1, 1) == "PASS"  # the order does not matter
    assert _result(TEN_OK, 2, 1) == "MISMATCH"
    assert _result(TEN_OK, 1, 0) == "MISMATCH"
    assert _result(TEN_OK, 0, 0) == "MISMATCH"


@needs_node
@pytest.mark.parametrize(
    "statuses",
    [
        ",".join(["new"] + ["no verdict"] * 9),  # nine lost requests
        ",".join(["no verdict"] * 10),
        "new,duplicate",  # fewer than ten answers at all
        ",".join(["new"] + ["duplicate"] * 10),  # eleven answers
        ",".join(["review"] * 10),  # ten answers, none of them what a correct desk says here
        ",".join(["new"] * 2 + ["duplicate"] * 8),  # two new leads
        ",".join(["duplicate"] * 10),  # no new lead
        ",".join(["new"] + ["duplicate"] * 8 + ["review"]),
        ",".join(["new"] + ["duplicate"] * 8 + ["no verdict"]),  # one request lost (for example a 503 busy answer)
        "",
    ],
)
def test_any_other_set_of_answers_is_a_mismatch_even_with_one_email_and_one_crm_row(statuses: str) -> None:
    assert _result(statuses, 1, 1) == "MISMATCH"
