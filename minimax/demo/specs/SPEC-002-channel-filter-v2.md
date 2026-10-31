---
id: SPEC-002
version: "2"
title: 渠道筛选条件
kind: spec
status: active
supersedes: SPEC-002@v1, DECISION-001@1
scope: orderflow.filter_orders
---

`filter_orders` 需要支持按渠道筛选，参数名为 `channel`。

- 渠道取值扩展为 `app`、`web`、`mini_program` 三种，`mini_program` 为新增渠道。
- 筛选按**大小写不敏感**进行：比较前双方都转换为小写，即传入 `MINI_PROGRAM` 与传入
  `mini_program` 结果一致。
- 传入 `channel=None` 表示不按渠道过滤。
- 订单缺少 `channel` 字段时，该订单在任何渠道筛选条件下都不被选中。

本版本作废 `SPEC-002@v1`，实现必须以本版本为准。
