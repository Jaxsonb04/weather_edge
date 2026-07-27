"""Ladder-depth capture: parsing and the never-raises fetch wrapper.

This instrumentation exists to answer one open question from the 2026-07-27
performance analysis: whether liquidity beyond top-of-book (currently the
only depth ever recorded) would let the research sleeve capture more of its
approved edge. It is observation only -- these tests pin that a malformed or
unreachable API response degrades to ``None``/empty rather than ever raising
into the scan pipeline, since a failure here must never affect a live scan.

The response shape is taken from a live call against
api.elections.kalshi.com/trade-api/v2/markets/{ticker}/orderbook on
2026-07-27: ``{"orderbook_fp": {"yes_dollars": [[price, size], ...],
"no_dollars": [...]}}``, dollar-string prices, sizes as strings, best price
LAST in each list.
"""

from __future__ import annotations

import pytest

from sfo_kalshi_quant.orderbook_capture import (
    OrderbookDepth,
    OrderbookLevel,
    capture_orderbook_depth,
    depth_levels_json,
    parse_orderbook_response,
)


def test_parses_real_kalshi_response_shape():
    payload = {
        "orderbook_fp": {
            "no_dollars": [
                ["0.9300", "11.00"],
                ["0.9400", "3.00"],
                ["0.9500", "3.00"],
                ["0.9600", "6.00"],
                ["0.9700", "745.00"],
            ],
            "yes_dollars": [["0.0100", "522.00"]],
        }
    }
    depth = parse_orderbook_response(payload)
    assert depth is not None
    assert depth.no[-1] == OrderbookLevel(price=0.97, size=745.0)
    assert depth.no[0] == OrderbookLevel(price=0.93, size=11.0)
    assert depth.yes == (OrderbookLevel(price=0.01, size=522.0),)


def test_empty_book_is_a_valid_result_not_none():
    """An illiquid or just-listed market has zero resting orders -- real, not an error."""

    payload = {"orderbook_fp": {"no_dollars": [], "yes_dollars": []}}
    depth = parse_orderbook_response(payload)
    assert depth == OrderbookDepth(yes=(), no=())


def test_missing_side_key_defaults_to_empty():
    payload = {"orderbook_fp": {"yes_dollars": [["0.50", "10"]]}}
    depth = parse_orderbook_response(payload)
    assert depth is not None
    assert depth.no == ()
    assert depth.yes == (OrderbookLevel(price=0.50, size=10.0),)


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        {"orderbook_fp": None},
        {"orderbook_fp": "not a dict"},
        {"orderbook_fp": {"yes_dollars": "not a list"}},
        {"orderbook_fp": {"yes_dollars": [["only-one-element"]]}},
        {"orderbook_fp": {"yes_dollars": [["not-a-number", "10"]]}},
        {"orderbook_fp": {"yes_dollars": [["1.50", "10"]]}},  # price out of [0,1]
        {"orderbook_fp": {"yes_dollars": [["0.50", "-1"]]}},  # negative size
        "a bare string",
        42,
    ],
)
def test_malformed_shapes_return_none_not_raise(payload):
    assert parse_orderbook_response(payload) is None


def test_depth_levels_json_is_compact_and_order_preserving():
    depth = OrderbookDepth(
        yes=(OrderbookLevel(0.10, 5.0), OrderbookLevel(0.11, 12.0)),
        no=(),
    )
    assert depth_levels_json(depth.yes) == [[0.10, 5.0], [0.11, 12.0]]
    assert depth_levels_json(depth.no) == []


class _FakeClient:
    def __init__(self, response=None, exc=None):
        self._response = response
        self._exc = exc
        self.calls = []

    def get_orderbook(self, ticker, depth=3):
        self.calls.append((ticker, depth))
        if self._exc is not None:
            raise self._exc
        return self._response


def test_capture_returns_parsed_depth_on_success():
    client = _FakeClient(
        response={"orderbook_fp": {"yes_dollars": [["0.50", "10"]], "no_dollars": []}}
    )
    result = capture_orderbook_depth(client, "KXHIGHTSFO-26JUL28-T78", levels=3)
    assert result == OrderbookDepth(yes=(OrderbookLevel(0.50, 10.0),), no=())
    assert client.calls == [("KXHIGHTSFO-26JUL28-T78", 3)]


def test_capture_never_raises_on_network_failure():
    client = _FakeClient(exc=OSError("connection reset"))
    assert capture_orderbook_depth(client, "KXHIGHTSFO-26JUL28-T78") is None


def test_capture_never_raises_on_arbitrary_exception():
    """Broad by design: this path must survive any failure mode, not just network ones."""

    client = _FakeClient(exc=ValueError("unexpected"))
    assert capture_orderbook_depth(client, "KXHIGHTSFO-26JUL28-T78") is None


def test_capture_returns_none_on_malformed_response_without_raising():
    client = _FakeClient(response={"unexpected": "shape"})
    assert capture_orderbook_depth(client, "KXHIGHTSFO-26JUL28-T78") is None
