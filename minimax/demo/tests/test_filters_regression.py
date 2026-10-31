"""Regression behaviour that must keep working before and after the change."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orderflow.filters import filter_orders  # noqa: E402

ORDERS = [
    {"id": 1, "status": "paid", "amount": 10.0},
    {"id": 2, "status": "pending", "amount": 20.0},
    {"id": 3, "status": "shipped", "amount": 30.0},
]


def test_filter_orders_by_status():
    selected = filter_orders(ORDERS, status="paid")
    assert [order["id"] for order in selected] == [1]


def test_filter_orders_by_min_amount():
    selected = filter_orders(ORDERS, min_amount=20.0)
    assert [order["id"] for order in selected] == [2, 3]


def test_filter_orders_without_filters_returns_everything():
    selected = filter_orders(ORDERS)
    assert [order["id"] for order in selected] == [1, 2, 3]


def test_filter_orders_unknown_status_returns_empty():
    assert filter_orders(ORDERS, status="unknown") == []


def test_filter_orders_empty_input():
    assert filter_orders([], status="paid") == []


def test_filter_orders_combines_status_and_amount():
    selected = filter_orders(ORDERS, status="paid", min_amount=5.0)
    assert [order["id"] for order in selected] == [1]
