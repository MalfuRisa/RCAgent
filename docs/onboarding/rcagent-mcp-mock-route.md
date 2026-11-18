# RCAgent 本机 Mock 链路

## 现在实际怎么走

```text
RCAgent
  → 真实 MCP Gateway（8888）
  → Queries SDK / gRPC
  → rcagent-mcp-mock（19888）
  → 真实 Querier Handler、Engine、诊断规则
  → Fake K8s / Observability / Trace Provider
```

Mock 放在 Gateway 后面。RCAgent 不知道数据是真是假，也不会绕过 Gateway。

## RCAgent 侧改了什么

- Worker 继续使用 Gateway 发布的 8 个 Tool；
- Triage 改为通过 Gateway 获取 runtime、network、application 三类摘要；
- Planner 改为根据 `resolve_pod` 和网络上下游信息补充调查上下文；
- 不再从 Triage/Planner 直接创建 K8s、Prometheus、Jaeger 或旧 Datagraph 客户端；
- 原来的 Agent 编排顺序没有改变。

当前 Gateway 只能解析具体 Pod，不能按 namespace 自动找出所有异常 Pod。因此一次调查要明确提供：

```text
target_cluster
target_namespace
target_resource（具体 Pod 名）
```

这属于当前 Gateway 能力边界，不应该由 RCAgent 绕过 Gateway 偷查 K8s 来补齐。

## Mock 能模拟什么

Mock 用 namespace 选择场景，目前包括：

- HTTP 5xx；
- 下游慢请求；
- TCP 建连失败；
- 高重传、高 RTT；
- 单副本异常和同伴对比；
- L4 正常但 L7 异常；
- Provider 失败；
- Trace 命中、缺 ID、查不到、超时间窗、部分缺失。

诊断结果不是写死的 JSON。假数据先进入 Querier 的真实规则，再由 Gateway 转成 MCP Tool
结果。Provider 调用证据写在 WSL：

```text
/var/lib/rcagent-mcp-mock/evidence/provider-calls.jsonl
```

## 已经验证到哪

- Gateway 能发现 8 个 Tool；
- `resolve_pod` 能解析场景 Pod；
- runtime、network、application Summary 能经过 Gateway 返回；
- abnormal request、Trace 和 `diagnose_pod` 能走到真实 Querier 规则；
- RCAgent 的 Triage、Planner 和并行 Worker 已在这条链上产生真实调用；
- 未连接真实 K8s，所以这些结果证明的是“本机全链路可运行”，不是“生产数据正确”。

## 服务位置

| 服务 | WSL 位置/端口 |
| --- | --- |
| RCAgent | 项目目录运行，`2024` |
| MCP Gateway | systemd：`insights-mcp-gateway`，`8888` |
| Mock 后端 | systemd：`rcagent-mcp-mock`，`19888` |

启动命令见项目根目录 README。
