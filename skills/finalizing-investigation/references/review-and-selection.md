> 历史 R1–R3 示例，仅用于理解旧材料；正式ID、门控、提交与配额不适用于自有主链。当前入口不加载本文件。

# 任务与报告的两个出口

以下是合同示意，ID 都要使用当前实际记录。

Worker：
```json
{"supporting_observation_ids":["<正式观测>"],"opposing_observation_ids":[],"interpretation":"原值已核验，机制未决","limitations":["缺少独立对照"],"unresolved_reasons":{"<当前任务问题>":"无法确认因果"}}
```

Supervisor 提交未决报告：
```json
{"observation_ids":[],"unresolved_reasons":{"<当前问题>":"没有可引用的正式观测"},"limitations":["原始材料仍保留，无法证明机制"],"next_steps":[],"claims":[]}
```

有正式事实时把实际 observation_id 放入 observation_ids，不重新提交其来源对象。
claims 可以省略；有假设时明确 type=hypothesis 和关联的正式观测，不冒充 causal。

accepted 返回实际 packet_id 后，再给出 PacketSelection：
```json
{"packet_id":"<刚接受的包>","selected_statement_ids":[],"conclusion_status":"inconclusive","unresolved_question_ids":["<该包未决问题>"]}
```

有 statement 时从该包真实集合选择，不能总清空；没有 statement 的未决包才能使用空集合。
最终 gate 通过并非自动确认根因。程序部分导出也不等于模型接受了报告。


已有正式观测的报告与对应选择（演示 ID）：
```json
{"observation_ids":["<O>"],"unresolved_reasons":{"<Q>":"该字段已核验，机制仍未知"},"limitations":["只证明实际字段，不证明因果"],"next_steps":[],"claims":[]}
```
```json
{"packet_id":"<P>","selected_statement_ids":["<S>"],"conclusion_status":"inconclusive","unresolved_question_ids":["<Q>"]}
```
