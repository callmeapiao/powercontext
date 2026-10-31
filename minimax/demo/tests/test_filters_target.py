"""Target behaviour: filtering orders by channel.

These tests describe the behaviour required by ``SPEC-002``.  They are expected
to fail before the feature is implemented and to pass afterwards.  The file is
part of the fixed test set: it must not change between the two runs.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orderflow.filters import filter_orders  # noqa: E402

ORDERS = [
    {"id": 1, "status": "paid", "amount": 10.0, "channel": "app"},
    {"id": 2, "status": "paid", "amount": 20.0, "channel": "mini_program"},
    {"id": 3, "status": "pending", "amount": 30.0, "channel": "web"},
]


def test_filter_orders_by_channel():
    """Orders are filtered by the channel they belong to."""

    selected = filter_orders(ORDERS, channel="app")
    assert [order["id"] for order in selected] == [1]

    selected = filter_orders(ORDERS, channel="mini_program")
    assert [order["id"] for order in selected] == [2]


def test_filter_orders_channel_accepts_new_channel_value():
    """The ``mini_program`` channel is a valid channel value."""

    selected = filter_orders(ORDERS, channel="mini_program")
    assert selected, "mini_program is a documented channel and must select orders"


def test_filter_orders_channel_is_case_insensitive():
    """Channel comparison ignores case, as required by SPEC-002 v2."""

    selected = filter_orders(ORDERS, channel="MINI_PROGRAM")
    assert [order["id"] for order in selected] == [2]


def test_filter_orders_channel_combines_with_other_filters():
    """Channel filtering combines with the existing filters."""

    selected = filter_orders(ORDERS, status="paid", channel="app")
    assert [order["id"] for order in selected] == [1]


def test_filter_orders_channel_none_keeps_every_order():
    """``channel=None`` does not filter by channel."""

    selected = filter_orders(ORDERS, channel=None)
    assert [order["id"] for order in selected] == [1, 2, 3]


def test_filter_orders_skips_orders_without_channel():
    """Orders without a channel field never match a channel filter."""

    orders = [{"id": 9, "status": "paid", "amount": 5.0}]
    assert filter_orders(orders, channel="app") == []
