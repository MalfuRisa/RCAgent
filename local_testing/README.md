# 当前选测：PRD29

`python -m unittest local_testing.tests.test_dispatch_delivery_r8 -v`覆盖新旧资源派工、一次纠正保持原题、权限、阶段返回、默认程序检查、短focused提示、单调预算/排队/启动/清理和部分流。

选测旧R5/R6原文/精度/分页保护；PRD27旧默认业务seed及完整模型Supervisor断言不再作为当前验收。真实实验须单独授权，脚本通过不等于真实任务成功。

以下保留历史记录。

# 当前验证入口：PRD27

`python -m local_testing.verify_task_convergence_r7 NEW_OUTPUT`运行本批和受影响R5/R6的短行为检查，
保存日志及正常graph脚本产物。模型替身每次绑定独立工具定义、捕获实际messages；
MCP替身位于原生ClientSession调用边界，使用已归档的Phase5原生工具Schema。
不发送真实模型/MCP请求，不据脚本时间承诺真实提速。

可单独运行`python -m unittest local_testing.tests.test_task_convergence_r7 -v`。
覆盖阶段返回、真实参数路径范围检查、会话/来源隔离、续作、共享并发、取消/鉴权/deadline、
helper修参、程序检查、草稿保护和Supervisor零远程调用。

以下均为历史实验记录，不构成追加真实模型调查的授权。

# R6 Phase5：一次真实观察（历史）

沿正常run_investigation入口，显式RCAGENT_V5_GATEWAY_BINARY选最新Phase5构建物；旧默认R31二进制不能作为Phase5验证。
本轮相关检查：test_phase5_investigation_r6与受影响的test_owned_investigation_r5；不默认全量或重复付费模型。
完成后可用 python -m local_testing.analyse_phase5_r6 <本批根目录> 汇总真实轨迹，不发模型/MCP请求。
以下R5与更早记录保留各自历史范围。

# 当前验证入口：PRD24 R5

默认产品已改为RCAgent Python自有循环，不需要OpenCode。
运行 `python -m local_testing.run_investigation NEW_OUTPUT TASK_JSON`。
短行为检查 `python -m unittest local_testing.tests.test_owned_investigation_r5 -v`。
仅选测和一轮有数据的真实现场，不重跑历史付费归档探索/全量回归。
下文R4以及更早实验说明全部为历史，不是当前产品依赖或执行要求。

# R4 默认产品入口

当前默认内核为OpenCode原生会话，运行方式与产物见仓库README。
`python -m local_testing.run_investigation NEW_OUTPUT TASK_JSON`调用正常产品图，
输入incident_text/target_*/query_window，Worker正文先保存，Supervisor同session复核。
默认无应用deadline、阶段/字符/token/金额额度；旧配置只用于显式legacy。
短验证为`python -m unittest discover -s local_testing/tests -p test_opencode_kernel_r4.py`，
只检查新运行接口/取消/正文保留/来源计数，不代表真实模型联调。

以下R1–R3内容是历史说明，其中归档真实模型自由探索已取消，不应再按旧命令启动。
历史fixtures可用于必要的确定性检查；旧模型/catalog注入需显式runtime="legacy"。

# RCAgent local testing

Entry points for local, offline-friendly acceptance of the RCAgent ↔ MCP
Gateway chain. Nothing here reads Kubernetes, Prometheus or Tempo directly: all
evidence goes through the MCP Gateway (`MCP_GATEWAY_URL`, default
`http://127.0.0.1:8888/mcp`).

## Running Python (WSL)

The project runs in WSL. The existing Poetry virtualenv can be used directly, so
no `poetry install` is required:

```bash
cd /mnt/d/Business/Agents/RCAgent
export PYTHONPATH=$PWD
PY=/root/.cache/pypoetry/virtualenvs/rcagent-oNAfnZ6w-py3.13/bin/python
```

Credentials stay in the environment (`.env`, git-ignored). Never hard-code them:

```bash
set -a; . /path/to/your/.env; set +a
```

## Natural-language entry point

```bash
# What does RCAgent extract from an incident message? (no Gateway, no model)
$PY local_testing/show_incident_parse.py
$PY local_testing/show_incident_parse.py --text "payment 服务最近 15 分钟一直报 500，帮我看看"

# Triage target discovery only: Gateway calls, no model calls
$PY local_testing/run_incident_text_pipeline.py --discovery-only \
    --incident-text "payment 服务最近 15 分钟一直报 500，帮我看看"

# Full investigation: Triage → Planner → Worker → Supervisor (needs LLM config)
$PY local_testing/run_incident_text_pipeline.py \
    --incident-text "payment 服务最近 15 分钟一直报 500，帮我看看"

# Point at another Gateway without touching code
$PY local_testing/run_incident_text_pipeline.py --gateway-url http://127.0.0.1:8888/mcp \
    --incident-text "payment 服务报 500"
```

The report contains the ordered tool calls, per-step duration, total TTA, and a
breakdown of **upstream facts** / **agent inferences** / **data gaps**.

## Preserved structured path (regression)

```bash
$PY local_testing/run_mcp_mock_pipeline.py       # structured Pod target, full graph
$PY local_testing/run_worker_baseline.py
$PY local_testing/capture_mcp_mock_pipeline.py   # writes an auditable event log
```

## Tests

Standard library `unittest` — no extra dependencies:

```bash
$PY -m unittest discover -s local_testing/tests -t local_testing/tests -v
```

* `test_incident_text.py` — extraction, "do not invent", structured precedence,
  LLM-proposal grounding.
* `test_triage_discovery.py` — the five-tool discovery chain, ambiguity,
  empty/truncated/missing data, structured-path adapter regressions.
* `test_triage_routing.py` — Triage entry-point routing: structured wins,
  ambiguous stops, insufficient asks.

All three are hermetic: the Gateway and the model are replaced by fakes.

## Configuration

### Check model submission compatibility before a full investigation

The model must accept the actual structured submission protocol, not just reply
to a plain chat request. All agents now inherit the explicitly configured
protocol; the default is `function_calling`. Set
`RCAGENT_LLM_STRUCTURED_OUTPUT_METHOD=json_schema` only for a verified endpoint.
`RCAGENT_LLM_ADVANCED_STRUCTURED_OUTPUT_METHOD` optionally overrides the advanced
model's setting. Invalid settings fail locally; there is no silent fallback.

```bash
$PY local_testing/check_llm_compatibility.py --output /tmp/rcagent-llm-check.json
# Use this as a launch gate: a failed check prevents the launch command.
$PY local_testing/check_llm_compatibility.py && "${PY%/python}/langgraph" dev --no-browser
```

This makes four real model requests using the Triage, Planner, Worker-summary
and Supervisor schemas with synthetic, non-empty values. It makes no MCP calls.
If both model roles use the same model/endpoint/protocol, checks are reused.
Each request has a 30-second SDK timeout and no retries. A failure exits with
status 1 and records the error type/HTTP status without printing SDK bodies or
credentials. It checks submission compatibility, not diagnosis accuracy.
It is a separate pre-launch command, not an automatic hook in a running API.
Ordinary chat and query-tool binding keep their existing behavior. Malformed
structured results still raise errors; no empty diagnosis is substituted.

Offline regression for the actual HTTP payloads and parsing:

```bash
$PY -m unittest discover -s local_testing/tests -p test_llm_compatibility.py -v
```

| Variable | Purpose | Default |
|---|---|---|
| `MCP_GATEWAY_URL` | MCP Gateway endpoint | `http://127.0.0.1:8888/mcp` |
| `MCP_GATEWAY_AUTH_TOKEN` | Bearer token, if Gateway auth is on | empty |
| `RCAGENT_TOOL_TIMEOUT_SECONDS` | Per-tool Gateway timeout | `30` |
| `RCAGENT_DEFAULT_WINDOW_MINUTES` | Lookback when the text gives no window | `15` |
| `RCAGENT_LLM_MODEL` / `RCAGENT_LLM_BASE_URL` / `RCAGENT_LLM_API_KEY` | Model access | — |
| `RCAGENT_LLM_STRUCTURED_OUTPUT_METHOD` | Structured submission: `function_calling` or `json_schema` | `function_calling` |
| `RCAGENT_LLM_ADVANCED_STRUCTURED_OUTPUT_METHOD` | Advanced model protocol override | inherits shared setting |

## 事故工作台（四轮实现）

父图会为每次调查创建 incident_id/run_id。默认元数据位于 `var/incident-workspaces/workspace.sqlite3`，不可变查询原文位于 `artifacts/<incident_id>/<artifact_id>.json`；`var/` 已由现有 Git 忽略规则覆盖。`RCAGENT_WORKSPACE_ROOT` 必须为运行环境可访问的绝对路径（WSL 使用 Linux 路径）。继续事故需显式携带 incident_id 和相同访问范围，新 run 不覆盖旧尝试。

模型自动收到已确认/未知的资源身份、查询限制、任务回答状态、证据摘要和带来源的判断。它可以调用本地 `get_resource`、`query_evidence`、`read_evidence`、`read_artifact` 读取原文；这些工具不接受事故 ID 或文件路径。证据默认 20 条，最多 100 条；原文默认最多 20 条/8000 字符，可用 field/offset 或 char_offset 翻页。模型读取记录关联到 ContextManifest；visible_messages_artifact_id 保存该次模型真正看到的输入。

配置位于 config/settings.py，环境变量统一加 `RCAGENT_` 前缀。2026-10-28运行默认值：调查总期限1800秒、普通模型请求180秒、Planner/Supervisor请求各自独立300秒；SDK连接也允许角色时限，不再被旧60秒提前取消。并发1、间隔2秒、传输最多2次重试、Worker最多16步及身份/来源检查保持。`RCAGENT_ALLOWED_NAMESPACES=*` 默认允许输入集群内只读定位，可配置逗号分隔白名单；无已确认集群不会猜测。`MAX_TOOL_CALLS=8` 保持原名称，在执行前计数并阻止超额调用。

运行路径默认使用完整请求v2计量，整理软目标96,000字符，工程硬线512,000字符，局部Repair上限32,768字符。字符值是有限的工程保护，不能当作模型token窗口。24k/48k旧对照保留在显式压力测试中，不再作为普通运行主验收。

Planner超时、无效输出、空计划或传输重试耗尽时，若原目标身份和症状有效，程序登记有明确来源的保底调查任务，再进入Worker/Supervisor；鉴权、429全局停止、总期限、取消和未知身份不绕过。可选阶段交接失败保留合法外层结果，未接受的观测/材料仍不提交。`RCAGENT_STAGE_HANDOFF_REQUIRED=true`可显式恢复严格对照。恢复记录进入后续工作台与最终data_gaps，完整流程结束也不掩盖调查缺口。

active Supervisor报告请求超时后，允许最多`RCAGENT_SUPERVISOR_FINALIZATION_MAX_STEPS=4`个额外模型步骤，经原Binding/总deadline完成“小阶段登记原文字段→资料包→短选择”。正式观测为零时必须先核验原文字段，不能把读过资料或模型草稿直接当正式事实。程序从已经可见且经原文核验的字段生成候选；有候选时，模型只用`register_report_observations`选择候选编号和现有问题，程序带入冻结的路径、值、版本及空材料覆盖声明，再走共享提交校验，避免重填机械字段。无合法候选时仍只能走原严格交接。资料包/来源/反证规则保持；恢复失败仍明确生成兜底并记录原错误，不能称正常提交完成。`resume_finalization.py`可在明确指定的失败存档副本上验证该路径：原终态不修改，副本从记录中的终结前run状态建立分支，保留历史调用及预算来源；这是一条额外的真实模型报告恢复验证，不冒充原完整run已成功。

工具和模型都使用异步请求；一次模型超时与总调查截止时间生效。SDK 重试设为 0，避免一次预算内偷偷产生多次模型请求。Worker 完成执行可以同时保持 answer_status=inconclusive。Supervisor 可以再次调度已结束但未回答的问题，或用 followup_tasks 注册具体补查问题。priority 只排序，报告按 task_id/attempt_id 合并。

最小运行仍使用现有入口，准备 MCP 与模型环境后执行：

```text
python -m local_testing.run_incident_text_pipeline --help
```

无网络回归（使用项目 Python 3.13）：

```text
python -m unittest discover -s local_testing/tests -t local_testing/tests -v
python -m unittest discover -s local_testing/tests -p "test_workspace*.py" -v
python -m compileall -q agents config models tools workspace context graph.py
```

test_workspace_graph 运行实际编译的 parent_graph，使用脚本化模型和保留原 13 工具 Schema 的假 Gateway。IAM 前六个查询和原 Supervisor 报告取自只读历史归档；CoreDNS 的成功定位与缺口是单独标记的合成样本，不代表内网曾查询到它。本批完整图测试显式关闭字符预算；test_acceptance_rework 的原有可见消息预算用例显式保持开启。设置 RCAGENT_TEST_ARCHIVE 为测试结果目录可按测试名导出图状态、工具/模型计数、工作台记录与事件（测试临时目录仍会清理）。test_live_parent 的假模型只读取实际消息，验证并行复用、报告纠正、Planner 429、Worker 超时和未命中早停。

首版根因规则集中在 workspace/reports.py：库存未命中、空 trace、no_peer、快速回复和网络症状均不能证明明确配置根因。当前唯一直接机制规则为 UID/范围匹配、没有缺口、终止时间落在查询窗口内且明确报告 OOMKilled 的容器终止证据，同时必须说明限制；历史库存型 runtime 返回不满足它。其余候选保留为假设。最终报告从工作台重建，保存原建议、最终版本和审计，已有 L4 数值不会因其他资源缺口被删除。

SQLite 为单机首版；多实例、崩溃恢复、动态观测缓存和真实根因准确率不在本轮验收内。

### 2026-10-13 五项修复配置

默认 `RCAGENT_CONTEXT_BUDGET_ENABLED=true`，仅接受 true/false。本批联调显式设为 false，保留精确模型输入、字符数、名义上限和 would_exceed_budget；恢复时改回 true。不会自动压缩输入或切换模型。

模型发送共享当前事件循环与账号入口，默认 `RCAGENT_MODEL_MAX_IN_FLIGHT=1`、`RCAGENT_MODEL_MIN_INTERVAL_SECONDS=2`、`RCAGENT_MODEL_MAX_RETRIES=2`。SDK 重试为 0，429/连接/502/503/504 才有限重试；超时、认证和输入过长不重试。model_call/model_attempt 记录排队、发送、重试、错误及可用计量，未知 token 保持 null。

Service 后备默认 `RCAGENT_SERVICE_FALLBACK_MAX_PODS=3`，按 namespace/name/UID 排序，明确已查、失败与未查清单。当前 Gateway 指标不能过滤 cluster，Trace 不能过滤 cluster/namespace；保留范围并跳过这些路线，转查已验证 Pod。库存不证明 Ready 或实际接流量。

run 输入可设置 `query_policy={"force_refresh":false,"allow_partial":false}`，仅接受本地布尔选项，不添加到 MCP schema。复用使用原 evidence ID，完整实际窗口和有效参数必须相同；身份变化、未知窗口/状态、失败和默认不接受的部分结果不会命中。

Supervisor 最多纠正 `RCAGENT_SUPERVISOR_MAX_CORRECTIONS=1` 次。观测/假设/排除报告条目正文必须等于所引用的已接受陈述；最终正文从陈述生成。直接机制仅接受原有 OOM 规则，其他方向保持候选；独立改写不能夹带新的归因。

真实只读复测入口（项目 Python 3.13，WSL 内运行）：

```text
python local_testing/run_live_verification.py --output /absolute/path/to/new-results --preflight-only
python local_testing/run_live_verification.py --output /absolute/path/to/new-results
```

该入口使用现有 .env 模型配置，通过只读 kubectl 将已有 Gateway 凭据保留在内存中，固定走 K8s Gateway。输出路由/镜像/13 Tool schema、实际参数、工作台记录、模型尝试、节点耗时和报告。真实模型失败与 MCP 查询成功分别报告，不能把最终文件生成视为诊断成功。

可用 `--case iam-pod` 单独复测一个场景。一次性凭据使用 `--use-key-stdin`，或通过进程环境 `RCAGENT_ONETIME_API_KEY` 配合 `--use-key-env` 传入；不写入 .env。`--resume-report-from /absolute/path/to/saved-case` 复制已保存工作台，在同事故、原范围和查询窗口内创建新 run，仅运行 Supervisor/gate；单独报告新增 MCP/模型次数。它用于报告恢复验证，不能代替一次全新完整父流程验收。临时修改超时的对照结果应与默认配置结果分别记录。

第 9 份 PRD 验收使用 `--case iam-pod --acceptance-baseline`：进程内固定模型 60 秒、调查 600 秒、在途 1、间隔 2 秒、重试 2、Supervisor 纠正 1；字符预算由 `--budget on|off` 选择，**默认 on**（2026-10-14 之后的上下文改造要验证紧凑输入，旧基线用 `--budget off` 复现）。2026-10-16 的内网联调尝试用 `--acceptance-baseline --budget on`，在库存查询即被远端 Gateway→Server/Querier 无端点阻断，模型调用 0，详见 `D:/Business/Agents/RCAgent/学习材料归档/04-Agent学习/改动优化/上下文输入改造-2026-10-14/intranet-verification-2026-10-16/BLOCKER.md`。当前库存必须返回唯一、范围匹配且带 UID 的 IAM Pod，否则记录 blocked_environment 与 inventory_error/not_found/partial/identity_unresolved 分类和原始结果，0 模型，不生成占位目标。正常 Agent not_found 契约不变。模型诊断输出包含角色、初次提交/纠正阶段、排队/发送耗时、输入大小、实际注入证据 ID、用量及精确消息 artifact。

父状态、报告和 run 的执行结果统一由 `workspace/execution.py` 归一化。父错误优先，Worker 按 task_id/attempt_id 稳定选择；其他错误及被替换停止原因分别保存在 execution_errors/secondary_stop_reasons。正常完成允许 stop_reason=null；新 run 清空旧执行原因，历史尝试仍可回查。

### 2026-10-14 上下文输入改造：展示层、角色输入与离线回放

存档仍是全部原始记录（records/events/artifacts）；模型看到的是投影后的工作台（`context/assembly.py`）加必要消息。`RCAGENT_CONTEXT_VIEW_MODE=compact`（默认）使用投影，`legacy` 保留旧组装，仅用于同快照对比与回滚。`RCAGENT_CONTEXT_HISTORY_RECENT_BATCHES=2` 表示最近两次完整调用批次原样保留，更早的批次保留请求/返回配对但长结果变为证据卡、受限摘要或 `no_reduction` 记录。`RCAGENT_CONTEXT_READ_MAX_CHARS=4000`（512–8000）是单次本地读取的字符上限，不影响全局 `CONTEXT_MAX_CHARS`；角色提示、必要消息和工作台共同计入同一字符预算。

本地只读工具为 6 个：`get_resource`、`query_evidence`、`read_evidence`、`read_artifact`、`read_task`、`read_assessment`。`query_evidence` 支持 resource_id/task_id/tool_name/source_status/时间起止筛选，游标分页稳定于本次快照；`read_evidence`/`read_artifact` 支持 `view=auto|metadata|raw` 与 `field`/`char_offset` 回读；每次读取都记录引用、页范围、tool_call_id 和所属上下文。

每次本地读取都会写入同事故的 `read_receipt` 记录（来源、版本/哈希、view/field/range、完整与未完整路径、身份/窗口、缺口、分页、返回正文 artifact 与最终字符数），返回体带 `read_receipt_id`；去重只依据**当前可见消息**引用的读取记录，metadata/目录/局部页/旧版本/其他角色的读取不会删除工作台里的观测。失败的读取只留 `local_read_error` 审计，不产生覆盖记录。调用输入按类型分类：系统规则、原始要求、任务要求、已核验读取详情与当前纠正受保护，只有工具结果在预算超限时按规则换成带真实存档编号的预览。

离线回放（无网络、无模型）用保存的记录重建快照并报告某个角色/阶段实际会看到的输入：

```text
python -m local_testing.replay_context --records <records.json> --incident <incident_id> \
    --role supervisor --phase local_read --mode compact --budget on --out <output-dir>
```

可选 `--history <保存的消息artifact.json>` 复现累积历史，`--max-chars` 换预算，`--task-id/--attempt-id/--run-id` 指定角色位置，`--artifacts <目录>` 指到该事故的 artifact 文件目录（否则按 dump 旁边的 `workspaces/artifacts/<incident>` 或 `workspace/artifacts/<incident>` 自动查找），`--snapshot-label` 标注这次回放的是哪份快照（例如“版本 88 重建”或“最终记录”）。请求窗口取该事故 run 记录里的真实窗口，run 没有时用证据返回窗口并在报告里标 `query_window_source`。每次回放同时导出 `*-visible-input.json`（真实可见输入、manifest、拒绝候选、读取记录），被拒绝时保留完整候选而不是空 manifest。
固定样本的实测报告见 `D:/Business/Agents/RCAgent/学习材料归档/04-Agent学习/改动优化/上下文输入改造-2026-10-14/acceptance-repair`。

### 第19节整体改造：2026-10-22续作

`RCAGENT_CONTEXT_LIFECYCLE_MODE=observe` 是当前默认值：只保存新选择候选与阶段提案，不增发模型请求、不生成虚构的已提交成果。`legacy` 用于旧输入对照；`active` 显式启用阶段提交、精确字段核验和规范请求 v2 计量。active 不接受 legacy 展示路径，也不会被旧预算关闭开关绕过。尚未满足全部 R4 条件，默认没有切换为 active，用户 `.env` 未修改。

compact 路径默认 `RCAGENT_CONTEXT_BUDGET_POLICY=soft`。2026-10-25曾采用24,000软目标/96,000硬线，10月28日起运行默认调整为96,000/512,000；原48k失败回放仍可独立保留作压力对照。先走有限精简阶梯，必要信息仍装不下时从完整短卡重组，在独立工程硬线内继续。这些值不是已核准的模型token容量。补读和交接余量使用独立硬线，不因超过软目标自动禁止查询。扩容后角色收到自然停顿点交接的软提示，必要补读仍允许；真正超过硬线时拒发并保存完整候选。manifest记录policy、实际软/硬限制、扩容尝试和完整请求大小。

`RCAGENT_CONTEXT_BUDGET_POLICY=strict` 恢复原单上限行为，用于同输入对照与回滚；原有严格上限回归显式使用此模式，新软目标行为由 `test_soft_context_budget` 单独验证。legacy 展示路径继续保留旧预算行为。软目标不是删除证据的许可：Triage/Planner 当前没有自主补读循环，因此保留它们所需的固定字段短卡；同一观测只注入一次，`question_ids` 保留它与各问题的关联。历史整理仍以已核验阶段成果收起旧段，没有新增每次 Tool 后调用 LLM 的摘要请求。

active 计量包括安装版本 SDK 实际转换的 messages、工具定义、结构化输出定义及控制字段；保留原 v1 和各组成。该字符计量不是 token 估计，服务端隐藏模板不可观测。模型 429 固定 stop_run，持久化后阻止同 run 后续发送，SDK 自带重试关闭；连接/503 继续沿用最多两次统一重试。

新增行为验证：`test_stage_lifecycle.py`、`test_stage_correction.py`、`test_report_packets.py`、`test_runtime_recovery.py`。真实模型本地入口 `run_local_acceptance.py` 只允许loopback MCP，先验证库存身份，沿用当前配置的角色/调查时限，不再写回旧60/600秒。`archived_mcp_server.py`仅按有效参数精确匹配历史请求并原样返回，不调用Querier、不访问远端，不能称为内网现场验收。

`replay_stage_repair.py`默认保留历史soft24k/hard48k压力回放；加`--runtime-budget`后使用当前运行预算，要求下一次模型调用实际执行本地读取，并经过Supervisor、报告资料包和最终gate。这是记录数据配脚本模型的流程验证，不能证明真实LLM理解或根因准确性。

本批产物：`D:/Business/Agents/RCAgent/学习材料归档/04-Agent学习/改动优化/整体上下文改造-2026-10-19/`。真实模型第二次运行于 Triage 收到429并停止；完整真实调查、55,095长历史的多阶段完成仍未通过，详见第12份交付文档的续作记录。

### 长存档调查：2026-10-24续作

新增 `replay_long_investigation.py`，从旧 `records.json` 与配套 workspace 读取并校验原始来源，在新工作区执行实际 Worker → Supervisor → report_packet → gate。每次输出目录必须不存在，失败记录不覆盖。运行方式：

```text
python -m local_testing.replay_long_investigation --source <archive-dir> --output <new-output-dir>
```

该入口使用只读取实际模型输入的脚本适配器，不访问网络，不调用真实模型。原结束时快照只作为资料来源，不能称为调用时快照的原位续跑。相同正文的 artifact 显式建立别名；证据来源记录不合并。`source-range-coverage.json`核对每个原来源的连续区间，`model-visible-inputs.json`和`supervisor-visible-inputs.json`保存实际输入。读完字符并提交只证明展示与核验范围，不能证明语义理解或因果关系。

新增 `test_material_ranges.py` 验证局部页、尾页、字符缺口、混合批次、跨 attempt、批次共用预算，以及输入缩小后关键事实/反证/限制保留。active 允许已核验页按精确来源/字段/版本/范围接替；旧段仍完整归档。已提交的页不再整批重注入，关键计数须另存字段观测；必需反证仍受保护。Supervisor 容量不足时可以用受限工具准备已核验事实的最终资料包，无需继续读新资料。

本次离线长链路通过证据见产物 `offline/2026-10-24/` 与 `DELIVERY-2026-10-24.md`。默认仍为 observe，完整真实模型调查与三场景验收仍须独立完成。
## PRD20 R2：单一通用调查入口

`graph.build_parent_graph(catalog=..., model=...)` 只委派
`workflow.investigation.build_investigation_graph`。`profile` 仅作旧调用兼容，
不再选择 CPU/legacy 流程。Triage、Planner、Worker、Supervisor、上下文、
查询执行器与报告 gate 都使用同一实现，工具目录由平台配置和权限决定。
Worker 默认16步，最后2步仅保留提交；完整原始数据、自己的读取依据和范围核验仍然保留。

v5 命令：

```bash
$PY -m local_testing.run_investigation <全新输出目录> <身份预检目录> [已授权的后端完整revision]
```

旧 `local_testing.run_v5_cpu` 只委派此命令。runner 需要单独构建的
`/tmp/rcagent-v5-cpu-gateway`、可信现有模型配置、rd2只读权限和预检文件；
会发出真实后端/模型请求，不由普通 unittest discovery 自动运行。
每次使用自己拥有的随机Bearer loopback实例和新工作区，用完只停止自己的进程。
省略revision时核验main_v5镜像/API版本一致；显式revision只用于已授权的当前现场，
不得为通过验收修改部署。统一调查作用域启用 active 生命周期；不继承旧CPU独立上下文政策。

本地机制验证：

```bash
$PY -m unittest discover -s local_testing/tests -p 'test_unified*.py'
$PY -m unittest discover -s local_testing/tests -p test_diagnostic_adapter.py
$PY -m unittest discover -s local_testing/tests -v
```

同一图使用历史CPU/网络原值和内存/存储fixture；替身验证机制，不能证明真实LLM诊断质量。
`local_testing/unified_scenarios.py` 是当前公共合同测试夹具，默认采用生产soft96k/硬512k预算；
严格24k拒绝、软目标、硬上限及取消等边界仍有独立测试。

R2实施、最终全量结果、保留的历史失败、实际后端版本和真实模型超时记录，
见桌面产出目录的20 PRD、21交付文档和材料证据根的 `R2/continued-full-final.json`。
预检成功、旧CPU报告和纯替身结果均不等于新流程真实取证/报告闭环。

## PRD20 R3：统一上下文接续、恢复与失败导出

统一provider现在实际复用历史工具批次外置、精确表示共享、受保护页保留、覆盖驱动去重与96k软目标/512k工程线。Worker可单独调用`commit_investigation_stage`，成功后继续同一task/attempt；换段不结束任务，也不重置模型步数。Supervisor接收正式观测/反证和来源目录，最终选择使用必要报告包视图。

瞬态请求失败在原总期限和角色预算中最多一次改变视图的恢复；unified_sre禁用sender层嵌套重试，SDK重试仍为0。鉴权、429全局停止、取消与总deadline不恢复。结构化解析可作一次本地纠正，失败稿不成为正式观测。

真实runner使用既有配置默认180/300/300/1800秒，不再入口强制覆盖预算。可用`RCAGENT_V5_GATEWAY_BINARY`指定持久的、从现有v5公司源码只读构建的本地二进制（例如忽略目录`var/bin`）；未设置时仍兼容`/tmp/rcagent-v5-cpu-gateway`。实际文件/摘要应核验，不假设跨WSL命令的临时文件仍存在。只清理自己拥有的loopback实例。

节点中途失败时，runner可从新工作区SQLite定位唯一incident/run导出，不依赖下一个完整节点输出。`records.json`包含所有已持久化类型，`export-status.json`明确区分无工作区与导出失败，`context-composition.json`和`report.md`保留输入组成与可读部分结果。`program_partial_result`不是模型报告成功。

本次真实尝试及失败分析保留在材料证据根`R3/real-01`；共19次原生MCP调用成功，三项Worker取证后正式提交未被接受，最终未形成模型报告。原始证据、拒绝稿、角色读取和正式观测分别记录。上下文接续机制与真实模型诊断质量分别验收，详细差距见同一20 PRD、21交付说明与`R3/implementation-notes.md`。


## PRD22 R3: stable references and phased submission

The common Worker and Supervisor now use `record_observations(ref_ids, question_ids, limitations)`.
`complete_task` and the public stage tool reference formal observations; the strict raw proposal DTO remains an internal/legacy replay contract.
Previously delivered references remain authorized across view clipping and segment changes for the same reader; reading a skill, finding a search snippet, or knowing another reader's ID does not grant field authority.

Current role budgets are exploration (Worker 14 / Supervisor 8), consolidation 4, repair 2 and pure skill reads 3.
They share persisted role state and the sender; consolidation keeps local search/read/register/complete tools and stops remote queries.
Runtime-activated skill entries and actual model `read_skill` calls are recorded separately.

`local_testing/fixtures/investigation_history.json` explicitly selects read-only historical reports.
`RCAGENT_INVESTIGATION_HISTORY_ROOT` and `RCAGENT_INVESTIGATION_HISTORY_MANIFEST` configure that local mount.
Search is a locator; exact current readers establish coverage, and historical materials never become current-run facts.

Targeted checks (no real model or remote MCP):

```bash
$PY -m unittest discover -s local_testing/tests -p 'test_*r3.py' -v
```

The real archive evaluation is opt-in and paid:

```bash
$PY -m local_testing.evaluate_investigation_skills /absolute/fresh/output --cases R3
```

It replays only exact R2 live-03 recorded tool parameters through the same complete product graph, labels every result `archived_fixture`, and never contacts remote MCP.
Its deterministic calibration and any real-model result must be reported separately.
An empty unresolved report or program partial export does not satisfy the R3 evidence-backed acceptance.
Current remote versions, windows, costs and full-suite outcomes belong in PRD22 / delivery23 and the independent R3-2026-10-30 evidence directory.
