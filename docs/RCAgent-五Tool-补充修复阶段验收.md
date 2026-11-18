# RCAgent 五 Tool 补充修复：本机阶段验收

日期：2026-09-30。状态：**阶段完成，完整 RCA 结论未验收**。本报告覆盖补充 PRD 的 P0 修复和目前能在外网本机验证的部分，不把旧误判当成成功案例。

## 先说结果

- 本机链路能跑：RCAgent（WSL）→ 新起的 Gateway `127.0.0.1:18889` → Mock Querier `:20889` / Mock Server `:19002`。真实 Gateway 发布 **13 个 Tool**，Gateway 跨进程集成测试通过。原来的 18888/19001/20888 进程没有被替换。
- 上一次说 `payment-db-0` “15 分钟零流量、网络中断、没有 Ready 副本”是**误判**。这轮把 Worker 的原始 Tool 返回接入最终证据闸门：`no_data` 不会变成零流量，`ready_status=unknown` 不会变成 NotReady；没有正向故障证据就删除确定性定位，并留下待验证缺口。
- **完整 A/F/G 场景还没有通过**。本机模型第一次调用返回 `429 insufficient_quota`（token-plan 额度耗尽），Planner、Worker、Supervisor 没有完成，所以不能说修后的最终 RCA 已验收。当前无内网，也没做真实 K8s/CI 验收。

## 具体修了什么

1. RCAgent 的最终报告闸门现在读取 Worker `ToolMessage`，按调用 ID 对应实际 Tool 返回，不再拿 Worker 自己写的分析段落冒充上游证据。它区分观测值、真 0、缺数据、查无目标、错误、超时和未知；正常的小幅网络数值也不会直接算故障证据。
2. Mock 普通 `payments/payment` 与独立故障 `payments-db/payment-db-fault` 各有自己的 Service、selector、Pod、UID 和诊断命名空间。原场景中的 `payment-db-0` 只是下游线索，查它的诊断不能把未覆盖数据补成全零；独立场景才有正向 L4 异常。
3. Gateway 的目标搜索在上一轮新增提交里已经修好截断语义。本轮补了真实 gRPC 测试，核对独立数据库故障场景不会串到普通 payment 命名空间。
4. 实验 runner 现在把图内 Triage/Planner/Worker 的 Tool 与模型可见事件写入脱敏 JSONL，记录参数、返回、错误和每次耗时。模型失败时也保留已发生的事件、阶段耗时，并用非零退出码提示自动化“未完成”。这只是可观察轨迹，不声称拿到了模型内部逐字思考。

## 本机实测

| 场景 | 这次看到什么 | 判定 |
| --- | --- | --- |
| A：自然语言 payment 500 + `prod-a/payments` | 发现阶段五个新 Tool 经真实 Gateway 调用，查到 payment 入口错误率峰值 0.42、错误 Trace 及 `payment-db/SELECT` 失败 Span；完整图第一次 LLM 调用即 429 | 发现阶段通过；最终结论**未验收** |
| B：只说“帮我看看” | 0 次 Tool 调用；要求提供服务名或明确 Pod | 通过 |
| C：只说“payment 服务报 500” | 返回同名候选并停在 Triage，不替用户选集群 | 通过 |
| D：截断但只露一个候选 | 真实 Gateway→Mock Server `limit=1` 测到 `truncated=true` 且不能返回 `ok/not_found`；RCAgent 图路由单测要求确认 | 两段分别通过；跨进程整图待补 |
| E：只说唯一服务 checkout | 不给结构化范围也能找到唯一 Service/Pod；指标与 Trace 为空，明确记为数据缺口 | 发现阶段通过；完整图未验收 |
| F：独立数据库网络故障 | Mock Server 可解析 `prod-a/payments-db/payment-db-0`；真实旧 Tool 查询到建连失败率 **0.62**、重传率 **0.35**、平均 RTT **480ms**、RST **48**、零窗口 **17** | 正向工具证据通过；最终模型结论未验收 |
| G：旧结构化 Pod 输入 | 上一轮曾跑过；本轮未在新代码上完整重跑 | 待补 |
| H：上游失败/超时 | Gateway 集成测试验证五个新 Tool 上游不可达均报错；另用接入但不答复的 TCP/RPC 对端逐项测到五 Tool 的 deadline/timeout 错误 | 五 Tool 超时通过；原八 Tool 的 Server 故障隔离仍待补 |

额外反例：正常 payment Pod 的 `get_pod_network_summary` 实测建连失败率 **0**、重传率 **0.005**、平均 RTT **12ms**；这组“有样本但无明显网络故障”的结果与 `no_data` 不同。普通场景直接诊断 `payments/payment-db-0` 返回 `provider_error`，不能据此说网络中断。独立故障场景的 `diagnose_pod` 同时包含正向网络候选和其他 `no_data` 缺口，最终结论必须逐条引用相应网络指标，不能用整份诊断的单一状态偷换事实。

## 测试口径与证据

- RCAgent：`python -m unittest discover -s local_testing/tests -t local_testing/tests -q`，**109 个通过**（包含证据闸门与调用摘要测试）。
- Server Mock：`go test -mod=vendor -tags=jsoniter ./querier/cmd/fake-provider-querier ./querier/grpc/diagnosis/scenario ./cmd/fake-resource-server -count=1`，三包通过；两个 Mock 的 Linux 正式二进制已构建并启动。
- Gateway：`go test ./internal/mcpserver ./internal/service -count=1` 通过；独立脚本构建真实 Gateway 和两 Mock，真实 gRPC 的 `TestMockChain` 全部通过，包含新数据库身份隔离测试和 `limit=1` 截断测试。随后新增了五 Tool 逐项 RPC deadline 测试，`go test -run TestMockChain_RPCTimeoutForEachNewTool -count=1 -timeout 60s ./internal/mcpserver` 通过。
- [机器可读场景结果](D:/kylin/test-results/rcagent-five-tools-20260930/supplemental-chain/A-retry.json)、[完整图事件 JSONL](D:/kylin/test-results/rcagent-five-tools-20260930/supplemental-chain/A-retry.events.jsonl)、[诊断探针结果](D:/kylin/test-results/rcagent-five-tools-20260930/supplemental-chain/diagnosis-probes.json)。`A-retry` 记录了 5 个图内 Gateway 调用与 429，不包含 Worker 调用；不能把它当成完整 TTA 成功样本。

## 下一步门槛

恢复可用的 `deepseek v4 flash` 额度或由用户提供新的可用模型凭证后，重新跑 A/F/G 的完整图，核对最终报告和 Worker 全部 Tool 返回；再补 D 的跨进程截断图实验，以及 H 的原八 Tool 在 Server 故障时仍可用的隔离检查。上述测试都过了，才把本轮标为完整验收。回内网后仍需核对真实 Prometheus 指标名、Tempo 标签、EndpointSlice、Server 9001 资源库和 CI；本机 Mock 不能替代这些检查。

三个分支仍是 `codex/rcagent-five-mcp-tools`；本轮不 push、不建 MR、不合并，也未读取或提交 `.env` 内容。本地 `.env` 是被 Git 忽略的隔离区文件。原工作区不动。
