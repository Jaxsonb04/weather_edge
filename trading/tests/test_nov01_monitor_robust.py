"""One unreadable Kalshi market must not stop exits for the rest of the book.

Audit F13 (2026-11-01): the monitor's per-ticker market fallback caught only
network errors. A payload that failed to parse (a bracket label in a numeric
field, a missing key, a null market, an undecodable body) escaped the lookup
and left the paper monitor before its exit loop, so no stop or take-profit ran
for any position in any paper account until that market left the open book.

These tests drive the real KalshiPublicClient end to end (get_json ->
get_markets / get_market -> MarketBin.from_kalshi); only urllib's urlopen is
replaced, so nothing touches the network.
"""

from __future__ import annotations

import json
from collections import Counter
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from unittest.mock import patch
from urllib.parse import parse_qs, unquote, urlparse

import pytest

from sfo_kalshi_quant import kalshi as kalshi_module
from sfo_kalshi_quant.account import LIVE_STABILITY_ACCOUNT_ID
from sfo_kalshi_quant.cli import main
from sfo_kalshi_quant.db import PaperStore
from sfo_kalshi_quant.models import MarketBin, TradeDecision
from sfo_kalshi_quant.monitor import _monitor_market_lookup
from sfo_kalshi_quant.research_policy import TARGET_POLICY_V6


TARGET = "2026-06-12"
EVENT = "KXHIGHTSFO-26JUN12"
LIVE_TICKER = f"{EVENT}-B74.5"
RESEARCH_TICKER = f"{EVENT}-B82.5"
BAD_TICKER = f"{EVENT}-B70.5"
UNDECODABLE = object()  # stands for a response body that is not UTF-8


@pytest.fixture(autouse=True)
def _plain_monitor_environment(monkeypatch):
    monkeypatch.delenv("PAPER_SAME_DAY_MODEL_HEARTBEAT_ENABLED", raising=False)
    monkeypatch.delenv("KALSHI_ENV", raising=False)


def _yes_decision(ticker: str, floor: float) -> TradeDecision:
    return TradeDecision(
        ticker=ticker,
        label=f"{floor:g}° to {floor + 1:g}°",
        action="BUY_YES",
        approved=True,
        probability=0.6,
        probability_lcb=0.5,
        yes_bid=0.38,
        yes_ask=0.40,
        spread=0.02,
        fee_per_contract=0.02,
        cost_per_contract=0.42,
        edge=0.18,
        edge_lcb=0.08,
        kelly_fraction=0.01,
        recommended_contracts=5.0,
        expected_profit=0.9,
        reasons=[],
        side="YES",
        entry_bid_size=50.0,
        entry_ask_size=50.0,
        strike_type="between",
        floor_strike=floor,
        cap_strike=floor + 1,
    )


def _no_decision(ticker: str) -> TradeDecision:
    return TradeDecision(
        ticker=ticker,
        label="82° to 83°",
        action="BUY_NO",
        approved=True,
        probability=0.35,
        probability_lcb=0.25,
        yes_bid=0.19,
        yes_ask=0.21,
        spread=0.02,
        fee_per_contract=0.013,
        cost_per_contract=0.803,
        edge=0.05,
        edge_lcb=0.01,
        kelly_fraction=0.01,
        recommended_contracts=5.0,
        expected_profit=0.2,
        reasons=[],
        side="NO",
        entry_bid=0.77,
        entry_ask=0.79,
        entry_bid_size=50.0,
        entry_ask_size=50.0,
        strike_type="between",
        floor_strike=82.0,
        cap_strike=83.0,
    )


def _payload(ticker: str, *, yes_bid: float, floor: float) -> dict[str, object]:
    no_bid = round(1.0 - yes_bid - 0.02, 2)
    return {
        "ticker": ticker,
        "event_ticker": EVENT,
        "title": "Highest temperature in San Francisco?",
        "yes_sub_title": f"{floor:g}° to {floor + 1:g}°",
        "strike_type": "between",
        "floor_strike": floor,
        "cap_strike": floor + 1,
        "yes_bid_dollars": f"{yes_bid:.4f}",
        "yes_ask_dollars": f"{yes_bid + 0.02:.4f}",
        "no_bid_dollars": f"{no_bid:.4f}",
        "no_ask_dollars": f"{no_bid + 0.02:.4f}",
        "yes_bid_size_fp": "50.00",
        "yes_ask_size_fp": "50.00",
        "no_bid_size_fp": "50.00",
        "no_ask_size_fp": "50.00",
        "status": "active",
    }


# Both readable markets have crashed far past the catastrophic floor, so each
# lot must stop on this pass whatever the model says.
READABLE_MARKETS = {
    # Live YES bought at 0.42, now bid 0.05: about -90%.
    LIVE_TICKER: _payload(LIVE_TICKER, yes_bid=0.05, floor=74.0),
    # Research NO bought at 0.803, now bid 0.18: about -78%.
    RESEARCH_TICKER: _payload(RESEARCH_TICKER, yes_bid=0.80, floor=82.0),
}


def _healthy_bad_market() -> dict[str, object]:
    # Crashed as well, so the lot would stop if its market could be read.
    return _payload(BAD_TICKER, yes_bid=0.05, floor=70.0)


def _unreadable_market(variant: str):
    payload = _healthy_bad_market()
    if variant == "label_in_numeric_field":  # the PR #156 incident class
        payload["floor_strike"] = "70°"
    elif variant == "non_numeric_price":
        payload["yes_bid_dollars"] = "n/a"
    elif variant == "missing_key":
        del payload["event_ticker"]
    elif variant == "null_market":
        return None
    elif variant == "undecodable_body":
        return UNDECODABLE
    else:
        raise AssertionError(variant)
    return payload


class _Response:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def _body(payload) -> _Response:
    if payload is UNDECODABLE:
        return _Response(b"\xff\xfe\xfa not utf-8")
    return _Response(json.dumps(payload).encode())


def _fake_urlopen(bad_market, requests: list[str]):
    def urlopen(request, timeout=None):
        requests.append(request.full_url)
        url = urlparse(request.full_url)
        path = url.path.rsplit("/trade-api/v2/", 1)[-1]
        markets = {**READABLE_MARKETS, BAD_TICKER: bad_market}
        if path == "markets":
            rows = [markets[ticker] for ticker in parse_qs(url.query)["tickers"][0].split(",")]
            if any(row is UNDECODABLE for row in rows):
                return _body(UNDECODABLE)
            return _body({"markets": rows, "cursor": ""})
        if path.startswith("markets/"):
            market = markets[unquote(path.split("/", 1)[1])]
            return _body(market if market is UNDECODABLE else {"market": market})
        raise AssertionError(f"unexpected Kalshi request: {request.full_url}")

    return urlopen


def _seed_two_account_book(db_path):
    """Live Stability and Research ROI V6 lots, plus one lot on the bad market.

    The unreadable lot is the newest, so the monitor fetches and inspects it
    first: the case where one bad market used to block every other exit.
    """

    store = PaperStore(db_path)
    ids = {
        "live": store.record_paper_order(TARGET, _yes_decision(LIVE_TICKER, 74.0)),
        "research": store.record_paper_order(TARGET, _no_decision(RESEARCH_TICKER)),
        "unreadable": store.record_paper_order(TARGET, _yes_decision(BAD_TICKER, 70.0)),
    }
    assert None not in ids.values()
    with store.connect() as conn:
        conn.execute(
            "UPDATE paper_orders SET account_id=?, risk_profile='research', "
            "research_sleeve=?, research_policy_version=?, policy_fingerprint=? "
            "WHERE id=?",
            (
                TARGET_POLICY_V6.account_id,
                TARGET_POLICY_V6.sleeve.value,
                TARGET_POLICY_V6.policy_version,
                TARGET_POLICY_V6.policy_fingerprint,
                ids["research"],
            ),
        )
        conn.execute(
            "UPDATE paper_account_ledger SET account_id=? WHERE order_id=?",
            (TARGET_POLICY_V6.account_id, ids["research"]),
        )
    assert _order(store, ids["live"])[1] == LIVE_STABILITY_ACCOUNT_ID
    assert _order(store, ids["unreadable"])[1] == LIVE_STABILITY_ACCOUNT_ID
    assert _order(store, ids["research"])[1] == TARGET_POLICY_V6.account_id
    return store, ids


def _run_monitor(db_path, bad_market):
    requests: list[str] = []
    out, err = StringIO(), StringIO()
    with (
        patch.object(kalshi_module, "urlopen", _fake_urlopen(bad_market, requests)),
        redirect_stdout(out),
        redirect_stderr(err),
    ):
        code = main(["--db-path", str(db_path), "--no-color", "paper-monitor"])
    return code, out.getvalue(), err.getvalue(), requests


def _order(store, order_id):
    with store.connect() as conn:
        return conn.execute(
            "SELECT status, account_id FROM paper_orders WHERE id=?", (order_id,)
        ).fetchone()


def _snapshots(store, order_id):
    with store.connect() as conn:
        return conn.execute(
            "SELECT action, reason, live_bid, market_status FROM paper_monitor_snapshots "
            "WHERE order_id=? ORDER BY id",
            (order_id,),
        ).fetchall()


@pytest.mark.parametrize(
    ("variant", "error_name"),
    [
        ("label_in_numeric_field", "ValueError"),
        ("non_numeric_price", "ValueError"),
        ("missing_key", "KeyError"),
        ("null_market", "TypeError"),
        ("undecodable_body", "UnicodeDecodeError"),
    ],
)
def test_one_unreadable_market_does_not_stop_exits_in_either_account(
    tmp_path, variant, error_name
):
    db_path = tmp_path / "paper.db"
    store, ids = _seed_two_account_book(db_path)

    code, out, err, requests = _run_monitor(db_path, _unreadable_market(variant))

    # Every readable position, in both economically separate accounts, is
    # still managed on this pass.
    for key in ("live", "research"):
        assert _order(store, ids[key])[0] == "PAPER_CLOSED", key
        assert _snapshots(store, ids[key])[-1][0] == "CLOSE_STOP_LOSS", key
    assert "paper monitor inspected 3, closed 2" in out

    # The unreadable lot is held with its own auditable reason. It has no
    # quote, and it is not mislabelled as a transient network failure.
    assert _order(store, ids["unreadable"])[0] == "PAPER_FILLED"
    snapshots = _snapshots(store, ids["unreadable"])
    assert len(snapshots) == 1
    action, reason, live_bid, market_status = snapshots[0]
    assert action == "HOLD_UNREADABLE_MARKET"
    assert error_name in reason
    assert live_bid is None and market_status is None
    with store.connect() as conn:
        actions = {row[0] for row in conn.execute("SELECT action FROM paper_monitor_snapshots")}
    assert "FETCH_FAILED" not in actions

    # The pass still fails loudly so systemd's OnFailure alert fires, and it
    # names the market that could not be read.
    assert code == 1
    assert BAD_TICKER in err and error_name in err

    # Isolation does not turn into a request storm: no market is fetched on
    # its own more than once per pass.
    singles = Counter(url for url in requests if "/markets/" in url)
    assert singles and max(singles.values()) == 1


def test_unreadable_lot_is_managed_again_once_its_market_reads(tmp_path):
    db_path = tmp_path / "paper.db"
    store, ids = _seed_two_account_book(db_path)

    code, _, _, _ = _run_monitor(db_path, _unreadable_market("label_in_numeric_field"))
    assert code == 1
    assert _order(store, ids["unreadable"])[0] == "PAPER_FILLED"

    # The hold is not sticky: once Kalshi serves a readable payload, the same
    # lot's catastrophic stop fires and the pass exits cleanly.
    code, out, err, _ = _run_monitor(db_path, _healthy_bad_market())
    assert code == 0, err
    assert _order(store, ids["unreadable"])[0] == "PAPER_CLOSED"
    assert [row[0] for row in _snapshots(store, ids["unreadable"])] == [
        "HOLD_UNREADABLE_MARKET",
        "CLOSE_STOP_LOSS",
    ]
    assert "paper monitor inspected 1, closed 1" in out


def test_lookup_records_a_parse_failure_against_that_ticker_only():
    observed: dict[str, str] = {}

    class Client:
        def get_markets(self, tickers):
            raise ValueError("one row in the batch could not be parsed")

        def get_market(self, ticker):
            if ticker == "BAD":
                raise ValueError("could not convert string to float: '70°'")
            return MarketBin.from_kalshi(_payload(ticker, yes_bid=0.40, floor=68.0))

    result = _monitor_market_lookup(
        Client(), ["BAD", "GOOD-A", "GOOD-B"], observed_at_by_ticker=observed
    )

    assert isinstance(result["BAD"], ValueError)
    assert isinstance(result["GOOD-A"], MarketBin)
    assert isinstance(result["GOOD-B"], MarketBin)
    # Only markets that were actually read carry a quote time.
    assert set(observed) == {"GOOD-A", "GOOD-B"}


@pytest.mark.parametrize("error", [AssertionError, AttributeError, RuntimeError])
def test_lookup_still_raises_errors_that_are_not_about_market_data(error):
    # Only payload-parsing errors are isolated per market. Anything else is a
    # bug in this process and must keep stopping the pass at once.
    class Client:
        def get_market(self, ticker):
            raise error("unexpected failure")

    with pytest.raises(error):
        _monitor_market_lookup(Client(), ["A", "B"])
