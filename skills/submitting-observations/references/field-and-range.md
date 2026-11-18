> 历史 R1–R3 示例，仅用于理解旧材料；正式ID、门控、提交与配额不适用于自有主链。当前入口不加载本文件。

# 精确引用的范围

以下 ID 均为演示占位符，必须替换为当前实际回执。

```json
{"ref_ids":["<当前已读引用>"],"question_ids":["<当前问题>"],"limitations":["仅覆盖本页"]}
```

record_observations 接受后的 observations 数组给出 ref_id 与 observation_id 的映射。
完成工具选择 observation_id；不能把 ref_id、evidence_id 或 artifact_id 填成正式成果 ID。

标量和完整可见 JSON 子树可以成为引用。表格只涵盖实际显示的行/单元格；
文本字符范围采用返回的序列化字符单位。长字段未显示的尾部仍未读，需精确分页。
不要把一次读取的 limit 当作实际已返回数量；以 range / next / truncated 为准。

19 位整数、原始单位、时间字符串不转换成浮点数。null、空数组是真实原值；
missing、权限失败、超时和数据源错误是不同状态，不能当作健康证据。

count / distinct_count 的派生引用只计算已读的明确输入范围。
输入页面不足全量时不能宣称整个窗口的总量；程序复算不替代范围解释。
必要时精读当前页引用，确认 aggregate.operation、输入区间和局限，再选择登记。
