# 当前默认行为：PRD29 派工与五分钟交付

默认入口仍为RCAgent自有Python graph。Planner用TaskSpec.tools选择已发现的原生只读能力，程序负责分类；旧business_tools集中兼容，不自动补业务查询权限。纯资源任务可显式选择k8s_resource/k8s_node_ids。派工全拒绝只纠正原题一次，不默认换成流量探测。

每个Worker独立会话；first_result覆盖真实资源/业务调用，answer用于一个问题必要的短关联链。默认review_mode=program，Supervisor模型调用为0；可选focused最多20秒，只给短提示，不重写完整稿。来源引用与控制event回执分别检查。

默认完整交付预算300秒，单调时钟共享：200秒停止调查，270秒停止生成，余量保存/清理/返回；更早用户deadline按比例留收尾。角色排队和重试不重置预算。runtime.json记录实际预算；CLI response-completed.json及实验caller文件记录完整返回边界，不用首片段当完成。

同run共享模型并发1、Worker并发2和工具并发8。显式取消不启动汇总。业务成功、空、本地拒绝、后端失败分别保存；正文完整不等于用户目标完成，evidence默认unchecked。

请求快照默认关闭，可通过进程RCAGENT_NATIVE_TRACE_REQUESTS=true启用。公开快照、首片段、工具原文、task/conversation/round/call关联保留；不是HTTP wire或私有推理。当前短检查见local_testing/tests/test_dispatch_delivery_r8.py。旧测试中默认业务seed、共享角色和完整模型复核的断言属于被本PRD替代的行为，不能据旧断言改变当前合同。

以下为历史说明，其中与本节冲突的默认值以本节和PRD29为准。

# RCAgent 本机怎么跑

## PRD27：小任务反馈与一次轻量复核

Planner持续主控：首轮一个定位/探测seed，首批实际业务调用结算后Worker无工具小结，
Planner据阶段结果继续、派发独立问题或交付。普通任务限定一个数据集/业务族和窗口，
需要换范围时交回Planner；能机械检查的范围在实际发送前拒绝，不声称证明where语义或对象归属。

每个Worker有独立Conversation与绑定工具，整个run共享MCP连接、原文与限流。
Worker在途默认5（`RCAGENT_NATIVE_WORKER_CONCURRENCY`），第六项等待空闲槽。模型在途默认1
（`RCAGENT_NATIVE_MODEL_CONCURRENCY`），工具全run默认8（`RCAGENT_NATIVE_CONCURRENCY`）。
这些是并发上限，不是调查总调用额度，也不固定启动三个Worker。

Planner只用本地读取、`dispatch_business`/`dispatch_native`两个来源入口、受控直查`query_business_metrics`和主动成稿`close_investigation`；Supervisor一次无工具复核、零远程MCP。
每阶段保存`tasks/<task_id>/answer.md`；完整草稿先存`draft-answer.md`，
`worker-answer.md`为Planner汇总的兼容副本。复核不完整则最终正文继续使用草稿。

`events.jsonl`含task/conversation/model_round/planner_decision关联及首流片段时间。
可选`RCAGENT_NATIVE_TRACE_REQUESTS=true`保存实际ChatModel公开输入和工具定义，默认false；
不是HTTP线缆抓包或私有推理。`checks.json`提供程序来源/时间/页计数检查，不能认证结论。

本批本地脚本验收：`python -m local_testing.verify_task_convergence_r7 NEW_OUTPUT`，
使用可控模型和MCP传输替身，不连接真实服务，不证明真实提速。

## Phase5 运行入口

默认MCP集合已移出 dimensions，加入 describe_query 与 meta_tag_values，共14项；本地材料工具另计。
Phase5实际运行必须显式选择本次构建的新网关，例如设置 `RCAGENT_V5_GATEWAY_BINARY` 为本机Phase5二进制。
旧默认 `var/bin/rcagent-r31-v5-http-gateway` 仅为历史兼容，不能据它声称验证了Phase5。
本轮进程清除旧 `RCAGENT_NATIVE_MCP_URL/TOKEN` 覆盖，模型/.env不变。
查询范围、分页、helper选择与来源信息直接保留；元数据列表不作为业务行统计。

默认入口为 `graph.build_parent_graph()` / LangGraph `rcagent`，执行链由 RCAgent Python 自己实现：
`initialize → Planner派工 ↔ 独立Worker阶段回传 → 保存草稿 → 一次无工具Supervisor → publish`。
不启动、导入或委托 OpenCode CLI/HTTP/SDK；OpenCode 仅供独立历史对照实验。

在 WSL KylinUbuntu 使用项目 Python 环境。模型沿 `.env` 的当前模型、地址、凭据，
通过普通 ChatOpenAI 流式调用；工具通过一个 run 共享的原生 MCP ClientSession。
默认启动只监听 loopback 的本地 v5 HTTP MCP，连接当前107后端；也可显式设置
`RCAGENT_NATIVE_MCP_URL/TOKEN` 使用已有连接。Gateway 只提供数据工具，不承担调查决策。

```json
{"incident_text":"观察这个Pod的运行情况，说明支持依据和缺口", "target_cluster":"rd2",
 "target_namespace":"kylin-insights", "target_resource":"<Pod>",
 "query_window":{"start_time":"<带时区时间>","end_time":"<带时区时间>"}}
```

```text
python -m local_testing.run_investigation /absolute/new-output /absolute/task.json
```

正常产品图及上述CLI使用同一条自有路径。Planner持续决策，各角色有独立固定前缀和追加历史；
阶段交接仅含阶段文字、来源导航与范围卡，不复制其他Worker完整中间历史。
原生14项只读名单与服务工具取交集，Schema原样绑定；没有54工具门槛、元数据登记或Pod ID前置。
多工具同批独立并发（全run默认8，更多只排队），每项错误按原始call ID顺序配对。

Planner草稿先保存，复核完整文字作为 `review-answer.md` 和最终正文，无标题门槛。
失败/取消保留已有成果；`final_report.body` 为产品正文，证据默认 `unchecked`，不代表真实性认证。
`events.jsonl` 是公开事件，`sources/*.json` 保存完整工具原文，`sources.json` 区分返回页条数和total。
大返回外置预览，可用 `read_material(source_id,pointer,offset,limit,max_chars)` 持续分页。
本地材料与skill接口不接受任意磁盘路径；history只有明确挂载才有内容，默认不挂载旧实验。

平时追加历史，已知真实容量时按接近容量或服务实际context错误才压缩旧完整轮次，保留调用配对、
原问题、近期轮次和原文入口。未知模型容量不猜硬阈值，可配置 `RCAGENT_MODEL_CONTEXT_TOKENS` /
`RCAGENT_MODEL_OUTPUT_RESERVE_TOKENS`。模型输入估算不是费用/步数预算。

四个项目skill保留单份Markdown：Planner前、Supervisor前分别加载一次；每个Worker会话首轮前加载起步/恢复入口一次，详细参考按需读取；
容量接续时加载continuing指导。无强制观测ID/报告包/加载回执，无skill读取额度。
历史ScopeReview、结构化InvestigationPlan、14+4+2、动态workbench、candidate、observation/packet/gate不参与新链。
旧算法仅可显式 `build_parent_graph(runtime="legacy",catalog=...,model=...)` 读取历史夹具，不自动回退。

默认无调查总deadline、角色deadline、token/费用/调用量停止线。用户可传Unix秒 `deadline`；
取消API `workflow.investigation.cancel(run_id)` 仅影响当前Python进程中的该run。
MCP连接10秒、单请求300秒、LLM流式空闲300秒；持续进展不按总时长截断。
LLM临时服务故障最多2次传输重试，SDK重试关闭。业务日志403不等于连接认证失败。
归档写失败会记入限制并保留内存答案；不能宣称失败文件已落盘。

设计参考 OpenCode v1.18.31 的 session/tools、prompt、processor、compaction、tool/truncate：
https://github.com/anomalyco/opencode/tree/v1.18.31/packages/opencode/src
本批为Python独立实现，未复制上游TS实质代码；上游许可证MIT。运行不依赖该软件。

下文记录的是历史本机Mock链路。8888/19888与8工具说明针对该链路，不能代替当前v5独立实例、
平台版本/身份预检，也不能作为真实取证成功证据。

先记住一句话：**代码在 Windows 的 D 盘，所有服务实际跑在 WSL 的 `KylinUbuntu` 里。**

现在没有内网也能跑通完整链路：

```text
RCAgent :2024
  → 本机真实 MCP Gateway :8888
  → rcagent-mcp-mock :19888
  → Querier 真实 Handler / Engine / 诊断规则
  → 假的 K8s、网络、应用和 Trace 原始数据
```

这里没有让 RCAgent 直接读假数据。RCAgent 看到的始终是 Gateway 提供的 8 个 MCP Tool，
跟以后连接真实 K8s 时一样。现在只是把 Gateway 后面的数据源换成了本机 Mock。

## 平时怎么启动

Mock 和 Gateway 已经注册成 WSL 系统服务。一般启动 WSL 后，它们会自己起来。先检查：

```powershell
wsl -d KylinUbuntu -- systemctl is-active rcagent-mcp-mock
wsl -d KylinUbuntu -- systemctl is-active insights-mcp-gateway
```

两条命令都返回 `active` 就可以了。如果没有起来，手动启动：

```powershell
wsl -d KylinUbuntu -- sudo systemctl start rcagent-mcp-mock insights-mcp-gateway
```

然后启动 RCAgent：

```powershell
wsl -d KylinUbuntu
cd /mnt/d/Business/Agents/RCAgent
/root/.local/bin/poetry run langgraph dev
```

看到 `Registering graph with id 'rcagent'` 后，RCAgent 就跑在：

- API：`http://127.0.0.1:2024`
- 接口文档：`http://127.0.0.1:2024/docs`

`2025` 不是这个项目的网页端口。RCAgent 目前没有单独做业务前端，主要通过 LangGraph
API、接口文档或 Studio 调用。

## 怎么确认 MCP 链路是真的通了

只测 Gateway 和 Tool，不调用大模型：

```powershell
wsl -d KylinUbuntu
cd /mnt/d/Business/Agents/RCAgent
/root/.local/bin/poetry run python -m local_testing.smoke_fixture
```

输出里应该看到：

- `tool_count` 是 `8`；
- `resolve_pod` 能解析 `checkout-0`；
- application summary 的错误率是 `0.1`；
- `diagnose_pod` 返回 Querier 规则生成的证据和候选根因。

想把 Triage、Planner、Worker、Supervisor 连同 `deepseek-v4-flash` 一起跑一遍：

```powershell
/root/.local/bin/poetry run python -m local_testing.run_mcp_mock_pipeline
```

这个测试用的目标是：

```text
cluster   = test-cluster
namespace = b01-http-5xx
pod       = checkout-0
```

Mock 还带了慢下游、建连失败、高重传、高 RTT、单副本异常和 Trace 缺失等场景。
场景名就是 namespace，Pod 大多是 `checkout-0`；单副本场景 `b05-*` 的目标是
`checkout-2`。

## 以后有内网了怎么切回真实 K8s

现在 Gateway 的下游指向本机 `19888`。最初的 K8s 配置已经备份，不需要改代码：

```powershell
wsl -d KylinUbuntu -- sudo bash /mnt/d/Business/Agents/RCAgent/local_testing/systemd/use-k8s-backend.sh
```

切回 Mock：

```powershell
wsl -d KylinUbuntu -- sudo bash /mnt/d/Business/Agents/RCAgent/local_testing/systemd/install-local-chain.sh
```

后一条命令也会重新编译 Mock，所以修改了场景实现后执行一次即可。

## 各端口负责什么

| 端口 | 谁在用 | 用途 |
| --- | --- | --- |
| `2024` | RCAgent | LangGraph API |
| `8888` | MCP Gateway | RCAgent 实际调用的 MCP 服务 |
| `19888` | rcagent-mcp-mock | Gateway 下游的 gRPC 假数据后端 |

不再使用原来的 `8890` Python fixture 作为主链路，因为它会绕过 Gateway。

## 这次没有启动什么

- 没有启动 AIOpsLab；
- 没有把 Server 串到 Gateway 和 Querier 中间；
- 没有连接 K8s 或公司内网；
- 没有改 RCAgent 的 Triage → Planner → Worker → Supervisor 核心流程。
# Business MCP试用配置（2026-11-14）

默认仍使用 native14。设置 `RCAGENT_MCP_PROFILE=business`、
`RCAGENT_BUSINESS_MCP_URL` 与可选 `RCAGENT_BUSINESS_MCP_TOKEN` 后，
正常 `local_testing/run_investigation.py <新输出目录> <输入JSON>` 使用独立的14项业务只读工具（含路径清单）。
该配置不启动原Go网关、不回退到native；业务MCP和AI Server须预先可用。
沿用300秒总预算、200秒取证和270秒生成截止，以及公开请求快照、来源原文和程序复核。
业务Skill为 `skills/kylin-insights-observability/`；词典来自对方MCP构建产物。

大盘查询传 `scopePolicy=require-target`，后续复用已解析的绝对窗口。
业务API输出是汇总数据，原值/unit/stat与展示换算分开；不是底层数据库全集。
部署与认证仍由目标服务管理。此次本地实验使用环回实例、独立SQLite、关闭内层模型和巡检，
不代表IAM验收。真实模型权限或数据缺口可阻断调查，按时返回错误不算业务目标通过。
本批输入输出及实际限制见第32份交付说明。

设置 `RCAGENT_MCP_PROFILE=integrated` 可同时使用业务与原生只读来源。
业务来源仍须配置 `RCAGENT_BUSINESS_MCP_URL` 和所需凭据；原生来源使用
`RCAGENT_NATIVE_MCP_URL`，留空时启动本次运行拥有的原生网关。
两个服务的配置与认证分别管理。每个 Worker 任务只绑定一类来源，换来源由 Planner
另派任务；某来源不可用时保留独立可执行的其他任务，错误会进入归档，不静默换源。

路径中的服务 ID 不能直接作为 Pod ID；需要资源详情时，先取得原生类型化身份依据。
同名、相同配置后端地址均不证明跨来源对象相同或共享存储。路径 TopN、覆盖范围与
queryMeta 的请求构造/后端回报分开保留，原始返回先归档，精简视图可按 JSON Pointer 读回。
角色职责与工具方法分开，Worker 交阶段事实和缺口，由 Planner 统一生成最终答复。

结构化首批查数可用 `handoff_mode=facts`。已知结果由程序交回原值和来源引用，
需要解释的阶段继续用 `worker_note`。可用 `catalog_targets` 指定当前任务需要的目录字段。
固定主窗与允许的明确对照窗分别派工。应用时序资料按同一来源、口径和时间戳整理候选，
保留缺失点和观察点限制。
