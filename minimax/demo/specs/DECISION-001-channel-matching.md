---
id: DECISION-001
version: "1"
title: 渠道匹配采用精确匹配且区分大小写
kind: decision
status: active
scope: orderflow.filter_orders
---

为减少实现复杂度，渠道匹配统一采用精确匹配且区分大小写：订单 `channel` 字段与传入参数
必须逐字符相等，大小写不同视为不同渠道。

该决策适用于 `app`、`web` 两种渠道。
