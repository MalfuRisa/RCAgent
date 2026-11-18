> 历史 R1–R3 示例，仅用于理解旧材料；正式ID、门控、提交与配额不适用于自有主链。当前入口不加载本文件。

# 引用拒绝后的实际纠正

旧失败是把原文提案与资料缺口混在 complete_task：缺 expected_value 或路径无法解析，
整批失败后又没有下一次调用。现在先登记引用，再用正式 ID 完成任务。

| 类别 | 下一步 |
| --- | --- |
| unknown_ref | find_evidence_references 按材料/字段检索已读引用；不能编造 ID |
| not_visible | 当前角色用本地精读获取自己的合法读回；不能借用 Worker 读取资格 |
| scope_mismatch / invalid_question | 核对当前 run、任务、目标和问题；不跨范围替换 |
| source_unavailable / source_stale | 保留错误，检查原来源；不能把旧引用静默解析为新值 |
| invalid_shape | 按实际 Schema 只提供引用、问题和限制 |
| phase_disallowed | 使用当前 allowed_tools；整理/修正期不重新远端取证 |

例如第一次登记含一个不存在 ID，整个批次被拒。移除或合法精读该引用后，
用原本想表达的相关事实重新提交。不要把失败批的任何条目写成已接受。
已有成功批仍在，直接引用其 observation_id。

收到拒绝后查看真实 remaining，而非假定有无限重试。需要先重显时用本地 reader；
无法修正就说明具体资料缺口，不填猜测原值。
