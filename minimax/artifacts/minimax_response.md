```python
"""Order filtering rules for the orderflow service."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

#: Statuses an order may hold.
ORDER_STATUSES = ("pending", "paid", "shipped", "cancelled")

#: Channels an order may belong to.
ORDER_CHANNELS = ("app", "web", "mini_program")


def filter_orders(
    orders: Iterable[Mapping[str, Any]],
    status: str | None = None,
    min_amount: float | None = None,
    channel: str | None = None,
) -> list[Mapping[str, Any]]:
    """Return the orders that match every supplied filter.

    Args:
        orders: order records to filter. Each record may carry ``id``,
            ``status``, ``amount`` and ``channel`` keys.
        status: keep only orders whose status equals this value.
        min_amount: keep only orders whose amount is greater than or equal
            to this value.
        channel: keep only orders whose channel equals this value, ignoring
            case.

    Returns:
        The matching order records, in their original order.
    """

    selected: list[Mapping[str, Any]] = []
    normalized_channel = channel.lower() if channel is not None else None

    for order in orders:
        if status is not None and order.get("status") != status:
            continue
        if min_amount is not None and float(order.get("amount", 0)) < min_amount:
            continue
        if normalized_channel is not None:
            order_channel = order.get("channel")
            if order_channel is None or str(order_channel).lower() != normalized_channel:
                continue
        selected.append(order)
    return selected
```