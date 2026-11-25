# RCAgent

RCAgent 使用只读 MCP 工具调查运维问题。Planner 按已知对象和时间窗安排查询，Worker 返回事实和来源。必答项齐备时进入成稿，开放问题可根据回执续查。每次运行保存正文、工具调用和可回查的原始证据。

## 配置和运行

使用 Python 3.13 和 Poetry。在仓库根目录执行 `poetry install`，再复制 `.env.example` 为 `.env`，填入以下配置。

| 配置 | 用法 |
| --- | --- |
| `RCAGENT_LLM_BASE_URL`、`RCAGENT_LLM_MODEL`、`RCAGENT_LLM_API_KEY` | 填写支持流式工具调用的 OpenAI 兼容接口、模型名和凭据。 |
| `RCAGENT_MCP_PROFILE` | 选择 `native`、`business` 或 `integrated`。 |
| `RCAGENT_NATIVE_MCP_URL`、`RCAGENT_BUSINESS_MCP_URL` | 填写对应的 Streamable HTTP MCP 地址。联合模式配置两套服务。服务需要认证时，填写同一前缀的 `_TOKEN` 变量。 |

准备一个 `task.json`，写明调查问题和绝对 Unix 秒时间窗。

```json
{
  "incident_text": "请查看 client-a 作为客户端访问 service-a 时的普通响应时延。",
  "query_window": {"start_time": 1767225600, "end_time": 1767229200},
  "review_mode": "program"
}
```

按实际数据替换题面里的客户端、服务和时间窗，再运行以下命令。输出目录需为新目录。

```sh
poetry run python -m local_testing.run_investigation var/runs/example task.json
```

LangGraph 入口为 `graph.py:parent_graph`。使用 Studio 时可安装 `poetry install -E studio`，再运行 `poetry run langgraph dev`。默认端到端运行预算为 300 秒。若在本机启动原生 HTTP 网关，按示例配置可执行文件及其两个后端地址。

## 验证与证据

```sh
poetry run python -m unittest discover -s local_testing/tests
```

本地验证用合成工具回执走产品图，覆盖证据齐备、继续补查和空回执。实际调查保存 `report.md`、`result.json`、`events.jsonl`、`sources.json` 和 `sources/`。CLI 在输出正文后写入 `response-completed.json`。按正文的来源编号可以查找原始响应。
