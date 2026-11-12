"""Check runner: asks a live model for a label on ~12 made-up leads, 3 runs, per model, through the same adapter the
desk uses (lead_desk.live_claude). Prints ONLY counts, cost, valid-label rate, flip rate and latency. Never prints the
key or any reply text.

Usage (run it from your own terminal, not from a script that runs without you):
  python scripts/run_live_check.py --key-file PATH --base-url ADDRESS --ledger PATH \\
      --cap-run 0.50 --cap-total 3 --price-date YYYY-MM-DD --model ID=IN_PRICE,OUT_PRICE [--model ...]
IN_PRICE and OUT_PRICE are US$ per million tokens, read from the provider's price page on --price-date.
Use the model ids and prices from your provider's own price page (the form is ID=IN_PRICE,OUT_PRICE).
Nothing is assumed: without caps, prices and a price date the script refuses to start. It prints the exact
worst-case cost of the whole run and waits for y/N before the first call. --base-url is the provider address
(default: the desk's own default address). The key is sent in a header of every request; any address other than
the provider's own or this computer is refused. The leads' e-mail addresses are made up (example.com)."""

from __future__ import annotations

import argparse
import statistics
import sys
import time
import uuid
from decimal import ROUND_CEILING, Decimal, InvalidOperation, localcontext
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from lead_desk.config import LiveSettings  # noqa: E402
from lead_desk.constants import (  # noqa: E402
    AI_LABELS,
    LIVE_DEFAULT_BASE_URL,
    LIVE_DEFAULT_EFFORT,
    LIVE_DEFAULT_MAX_TOKENS,
)
from lead_desk.live_claude import MONEY_CONTEXT, LiveError, LiveQualifier, SpendLedger, worst_case_usd  # noqa: E402

LEADS = 12
RUNS = 3
PCT = 100
P95 = 0.95
EXIT_REFUSED = 2
BODY_OVERHEAD = 600  # bytes of JSON and system text around the question (a safe upper bound, for the estimate)


def _model_arg(text: str) -> tuple[str, Decimal, Decimal]:
    try:
        model, prices = text.split("=", 1)
        p_in, p_out = prices.split(",", 1)
        return model.strip(), Decimal(p_in), Decimal(p_out)
    except (ValueError, InvalidOperation) as e:
        raise argparse.ArgumentTypeError("--model needs ID=IN_PRICE,OUT_PRICE") from e


def _usd_arg(text: str) -> Decimal:
    try:
        return Decimal(text)
    except InvalidOperation as e:
        raise argparse.ArgumentTypeError(f"not a number: {text!r}") from e


def _emails(n: int) -> list[tuple[str, str]]:
    return [(f"L{i:012x}", f"made.up.{i}@example.com") for i in range(1, n + 1)]


def _pct(x: float) -> str:
    return f"{x * PCT:.0f}%"


def run_model(s: LiveSettings, leads: list[tuple[str, str]], runs: int, ledger: SpendLedger) -> dict[str, object]:
    q = LiveQualifier(s, ledger)
    labels: dict[str, list[str]] = {lid: [] for lid, _ in leads}
    errors: dict[str, int] = {}
    latencies: list[float] = []
    calls = 0
    for _ in range(runs):
        for lid, email in leads:
            t0 = time.monotonic()
            calls += 1
            try:
                labels[lid].append(q.qualify(lid, email))
            except (LiveError, OSError) as e:
                errors[type(e).__name__] = errors.get(type(e).__name__, 0) + 1
            finally:
                latencies.append((time.monotonic() - t0) * 1000)
    valid = sum(len(v) for v in labels.values())
    full = [v for v in labels.values() if len(v) == runs]
    flips = sum(1 for v in full if len(set(v)) > 1)
    spent_run, _ = ledger.totals(s.run_id)
    lat = sorted(latencies)
    return {
        "calls": calls,
        "valid": valid,
        "valid_rate": valid / calls if calls else 0.0,
        "flip_rate": flips / len(full) if full else None,
        "full_leads": len(full),
        "errors": errors,
        "p50_ms": statistics.median(lat) if lat else 0.0,
        "p95_ms": lat[min(len(lat) - 1, int(len(lat) * P95))] if lat else 0.0,
        "spent": spent_run,
        "labels_seen": sorted({x for v in labels.values() for x in v if x in AI_LABELS}),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--key-file", required=True, help="file holding the key; read by this script only")
    ap.add_argument("--base-url", default=LIVE_DEFAULT_BASE_URL)
    ap.add_argument("--ledger", required=True, help="spend ledger file (kept; counts across runs)")
    ap.add_argument("--cap-run", type=_usd_arg, required=True, help="US$ cap for one model run")
    ap.add_argument("--cap-total", type=_usd_arg, required=True, help="US$ cap over the whole ledger")
    ap.add_argument("--price-date", required=True)
    ap.add_argument("--model", action="append", required=True, type=_model_arg, help="ID=IN_PRICE,OUT_PRICE")
    ap.add_argument("--max-tokens", type=int, default=LIVE_DEFAULT_MAX_TOKENS)
    ap.add_argument("--effort", default=LIVE_DEFAULT_EFFORT)
    ap.add_argument("--timeout", type=float, default=8.0)
    ap.add_argument("--leads", type=int, default=LEADS)
    ap.add_argument("--runs", type=int, default=RUNS)
    a = ap.parse_args(argv)
    if a.leads < 1 or a.runs < 1:
        ap.error("--leads and --runs must each be at least 1")
    batch = uuid.uuid4().hex[:8]
    leads = _emails(a.leads)
    settings: list[LiveSettings] = []
    try:
        for model, p_in, p_out in a.model:
            settings.append(
                LiveSettings(
                    model=model,
                    key_file=Path(a.key_file),
                    price_in_per_mtok=p_in,
                    price_out_per_mtok=p_out,
                    price_date=a.price_date,
                    cap_run_usd=a.cap_run,
                    cap_total_usd=a.cap_total,
                    ledger_path=Path(a.ledger),
                    run_id=f"check-{batch}-{model}",
                    base_url=a.base_url,
                    timeout_seconds=a.timeout,
                    max_tokens=a.max_tokens,
                    effort=a.effort,
                )
            )
    except ValueError as e:
        print(f"refused: {e}")
        return EXIT_REFUSED
    # worst case: every call of every model, the longest question this script can send
    q_bytes = max(len(f"lead id: {lid}\ne-mail: {em}\nAnswer: hot, warm or cold?") for lid, em in leads) + BODY_OVERHEAD
    with localcontext(MONEY_CONTEXT):  # the money context: exact sums, rounded UP so the figure is never too low
        worst = sum(worst_case_usd(s, q_bytes) * a.leads * a.runs for s in settings)
        worst_shown = worst.quantize(Decimal("0.0001"), rounding=ROUND_CEILING)
    print(f"models: {len(settings)}, calls per model: {a.leads * a.runs}, prices dated {a.price_date}")
    print(f"expected MAXIMUM cost of this whole run: US${worst_shown} (caps: run {a.cap_run}, total {a.cap_total})")
    if input("Start? [y/N] ").strip().lower() != "y":
        print("stopped, nothing was called")
        return 1
    ledger = SpendLedger(Path(a.ledger))
    for s in settings:
        try:
            r = run_model(s, leads, a.runs, ledger)
        except (LiveError, OSError) as e:  # the spend ledger could not be read: nothing more is spent
            print(
                f"stopped: {type(e).__name__}: the spend ledger could not be read or written; nothing more was called"
            )
            return EXIT_REFUSED
        flip = "n/a" if r["flip_rate"] is None else _pct(float(r["flip_rate"]))  # type: ignore[arg-type]
        print(
            f"model {s.model}: calls {r['calls']}, valid labels {r['valid']} ({_pct(float(r['valid_rate']))}),"  # type: ignore[arg-type]
            f" flip rate {flip} over {r['full_leads']} leads with all runs valid,"
            f" latency p50 {float(r['p50_ms']):.0f} ms p95 {float(r['p95_ms']):.0f} ms,"  # type: ignore[arg-type]
            f" cost US${r['spent']:.5f} (US${Decimal(str(r['spent'])) * PCT / int(str(r['calls'])):.5f} per 100 calls),"
            f" errors by class: {r['errors'] or 'none'}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
