---
id: SPEC-001
version: "1"
title: 订单筛选基本语义
kind: spec
status: active
scope: orderflow.filter_orders
---

`filter_orders` 必须保持"全部条件同时满足"的语义：任何给定条件不满足时，该订单被排除。

- 未给定 `status` 时不按状态过滤。
- 未给定 `min_amount` 时不按金额过滤。
- 过滤结果保持输入顺序。
- 输入为空集合时返回空列表。

这些行为在新增任何筛选条件后仍然必须成立，属于回归范围。
