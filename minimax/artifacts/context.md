=== 任务描述 ===
给 filter_orders 增加 channel 渠道筛选条件，需要支持 mini_program 渠道

=== 当前适用的规格条目（必须遵守） ===
--- SPEC-001@1 | 订单筛选基本语义 | kind=spec | source=specs/SPEC-001-order-status.md
scope: orderflow.filter_orders
`filter_orders` 必须保持"全部条件同时满足"的语义：任何给定条件不满足时，该订单被排除。

- 未给定 `status` 时不按状态过滤。
- 未给定 `min_amount` 时不按金额过滤。
- 过滤结果保持输入顺序。
- 输入为空集合时返回空列表。

这些行为在新增任何筛选条件后仍然必须成立，属于回归范围。

--- SPEC-002@2 | 渠道筛选条件 | kind=spec | source=specs/SPEC-002-channel-filter-v2.md
scope: orderflow.filter_orders
`filter_orders` 需要支持按渠道筛选，参数名为 `channel`。

- 渠道取值扩展为 `app`、`web`、`mini_program` 三种，`mini_program` 为新增渠道。
- 筛选按**大小写不敏感**进行：比较前双方都转换为小写，即传入 `MINI_PROGRAM` 与传入
  `mini_program` 结果一致。
- 传入 `channel=None` 表示不按渠道过滤。
- 订单缺少 `channel` 字段时，该订单在任何渠道筛选条件下都不被选中。

本版本作废 `SPEC-002@v1`，实现必须以本版本为准。

=== 已失效的决策（不要遵循） ===
--- DECISION-001@1 | 渠道匹配采用精确匹配且区分大小写 | 已被 SPEC-002@2 取代 | source=specs/DECISION-001-channel-matching.md
为减少实现复杂度，渠道匹配统一采用精确匹配且区分大小写：订单 `channel` 字段与传入参数
必须逐字符相等，大小写不同视为不同渠道。

该决策适用于 `app`、`web` 两种渠道。

--- SPEC-002@1 | 渠道筛选条件 | 已被 SPEC-002@2 取代 | source=specs/SPEC-002-channel-filter-v1.md
`filter_orders` 需要支持按渠道筛选，参数名为 `channel`。

- 渠道取值目前只有 `app` 与 `web` 两种。
- 筛选按**精确匹配**进行：订单的 `channel` 字段必须与传入值完全相等。
- 传入 `channel=None` 表示不按渠道过滤。

本版本为初始版本，后续如有语义调整会发布新版本并作废本版本。

=== 需要修改的代码文件 ===
--- orderflow/filters.py
"""Order filtering rules for the orderflow service."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

#: Statuses an order may hold.
ORDER_STATUSES = ("pending", "paid", "shipped", "cancelled")


def filter_orders(
    orders: Iterable[Mapping[str, Any]],
    status: str | None = None,
    min_amount: float | None = None,
) -> list[Mapping[str, Any]]:
    """Return the orders that match every supplied filter.

    Args:
        orders: order records to filter. Each record may carry ``id``,
            ``status``, ``amount`` and ``channel`` keys.
        status: keep only orders whose status equals this value.
        min_amount: keep only orders whose amount is greater than or equal
            to this value.

    Returns:
        The matching order records, in their original order.
    """

    selected: list[Mapping[str, Any]] = []
    for order in orders:
        if status is not None and order.get("status") != status:
            continue
        if min_amount is not None and float(order.get("amount", 0)) < min_amount:
            continue
        selected.append(order)
    return selected

=== 相关符号 ===
--- orderflow/filters.py :: filter_orders (function) line 12
def filter_orders(
    orders: Iterable[Mapping[str, Any]],
    status: str | None = None,
    min_amount: float | None = None,
) -> list[Mapping[str, Any]]:
    """Return the orders that match every supplied filter.

    Args:
        orders: order records to filter. Each record may carry ``id``,
            ``status``, ``amount`` and ``channel`` keys.
        status: keep only orders whose status equals this value.
        min_amount: keep only orders whose amount is greater than or equal
            to this value.

    Returns:
        The matching order records, in their original order.
    """

    selected: list[Mapping[str, Any]] = []
    for order in orders:
        if status is not None and order.get("status") != status:
            continue
        if min_amount is not None and float(order.get("amount", 0)) < min_amount:
            continue
        selected.append(order)
    return selected

=== 用于验证的测试 ===
--- tests/test_filters_regression.py :: test_filter_orders_by_status (regression)
def test_filter_orders_by_status():
    selected = filter_orders(ORDERS, status="paid")
    assert [order["id"] for order in selected] == [1]

--- tests/test_filters_regression.py :: test_filter_orders_without_filters_returns_everything (regression)
def test_filter_orders_without_filters_returns_everything():
    selected = filter_orders(ORDERS)
    assert [order["id"] for order in selected] == [1, 2, 3]

--- tests/test_filters_regression.py :: test_filter_orders_unknown_status_returns_empty (regression)
def test_filter_orders_unknown_status_returns_empty():
    assert filter_orders(ORDERS, status="unknown") == []

--- tests/test_filters_regression.py :: test_filter_orders_by_min_amount (regression)
def test_filter_orders_by_min_amount():
    selected = filter_orders(ORDERS, min_amount=20.0)
    assert [order["id"] for order in selected] == [2, 3]

--- tests/test_filters_regression.py :: test_filter_orders_empty_input (regression)
def test_filter_orders_empty_input():
    assert filter_orders([], status="paid") == []

--- tests/test_filters_regression.py :: test_filter_orders_combines_status_and_amount (regression)
def test_filter_orders_combines_status_and_amount():
    selected = filter_orders(ORDERS, status="paid", min_amount=5.0)
    assert [order["id"] for order in selected] == [1]

--- tests/test_filters_target.py :: test_filter_orders_by_channel (target)
def test_filter_orders_by_channel():
    """Orders are filtered by the channel they belong to."""

    selected = filter_orders(ORDERS, channel="app")
    assert [order["id"] for order in selected] == [1]

    selected = filter_orders(ORDERS, channel="mini_program")
    assert [order["id"] for order in selected] == [2]

--- tests/test_filters_target.py :: test_filter_orders_channel_accepts_new_channel_value (target)
def test_filter_orders_channel_accepts_new_channel_value():
    """The ``mini_program`` channel is a valid channel value."""

    selected = filter_orders(ORDERS, channel="mini_program")
    assert selected, "mini_program is a documented channel and must select orders"

--- tests/test_filters_target.py :: test_filter_orders_skips_orders_without_channel (target)
def test_filter_orders_skips_orders_without_channel():
    """Orders without a channel field never match a channel filter."""

    orders = [{"id": 9, "status": "paid", "amount": 5.0}]
    assert filter_orders(orders, channel="app") == []

--- tests/test_filters_target.py :: test_filter_orders_channel_is_case_insensitive (target)
def test_filter_orders_channel_is_case_insensitive():
    """Channel comparison ignores case, as required by SPEC-002 v2."""

    selected = filter_orders(ORDERS, channel="MINI_PROGRAM")
    assert [order["id"] for order in selected] == [2]

--- tests/test_filters_target.py :: test_filter_orders_channel_combines_with_other_filters (target)
def test_filter_orders_channel_combines_with_other_filters():
    """Channel filtering combines with the existing filters."""

    selected = filter_orders(ORDERS, status="paid", channel="app")
    assert [order["id"] for order in selected] == [1]

--- tests/test_filters_target.py :: test_filter_orders_channel_none_keeps_every_order (target)
def test_filter_orders_channel_none_keeps_every_order():
    """``channel=None`` does not filter by channel."""

    selected = filter_orders(ORDERS, channel=None)
    assert [order["id"] for order in selected] == [1, 2, 3]
