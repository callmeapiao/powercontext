---
id: SPEC-002
version: "1"
title: 渠道筛选条件
kind: spec
status: active
scope: orderflow.filter_orders
---

`filter_orders` 需要支持按渠道筛选，参数名为 `channel`。

- 渠道取值目前只有 `app` 与 `web` 两种。
- 筛选按**精确匹配**进行：订单的 `channel` 字段必须与传入值完全相等。
- 传入 `channel=None` 表示不按渠道过滤。

本版本为初始版本，后续如有语义调整会发布新版本并作废本版本。
