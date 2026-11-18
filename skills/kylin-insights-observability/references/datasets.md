# 可查询的数据集与指标（由 @kylin/ai-contracts 的取数目录生成，勿手改）

调用 insights_query_metrics 时，datasetId / 指标 key / 过滤字段 key 只能取自本表；以运行时 insights_catalog 的返回为准。

## network-path — 网络路径指标
来源：flow_metrics.network_map
说明：客户端服务→服务端服务之间的 TCP / 流量指标；比例为百分制，时延单位 µs
过滤字段：`auto_service_0`（客户端服务）、`auto_service_1`（服务端服务）、`pod_cluster`（集群（客户端或服务端任一端））、`pod_ns`（命名空间（客户端或服务端任一端））

| 指标 key | 名称 | 单位 |
| --- | --- | --- |
| `tcp_retrans_ratio` | TCP重传比例 | % |
| `tcp_client_retrans_ratio` | 客户端重传比例 | % |
| `tcp_server_retrans_ratio` | 服务端重传比例 | % |
| `tcp_establish_fail_ratio` | TCP建连失败比例 | % |
| `tcp_rtt` | TCP建连时延 | µs |
| `tcp_system_latency` | TCP系统时延 | µs |
| `tcp_zero_window` | TCP零窗口 | - |
| `flow_rate` | 流量速率 | bps |
| `send_flow_rate` | 发送流量速率 | bps |
| `receive_flow_rate` | 接收流量速率 | bps |
| `new_connection_rate` | 新建连接速率 | /s |
| `active_connection_rate` | 活跃连接速率 | /s |
| `application_request_rate` | 应用请求速率 | /s |
| `application_exception_ratio` | 应用异常比例 | % |
| `application_response_latency` | 应用响应时延 | µs |

## app-path — 应用路径指标
来源：flow_metrics.application_map
说明：客户端服务→服务端服务的应用层（L7）指标；比例为百分制，时延单位 µs
过滤字段：`auto_service_0`（客户端服务）、`auto_service_1`（服务端服务）、`pod_cluster`（集群（客户端或服务端任一端））、`pod_ns`（命名空间（客户端或服务端任一端））

| 指标 key | 名称 | 单位 |
| --- | --- | --- |
| `request_rate` | 请求速率 | /s |
| `server_response_rate` | 响应速率 | /s |
| `client_error_ratio` | 客户端异常比例 | % |
| `server_error_ratio` | 服务端异常比例 | % |
| `server_response_time` | 服务端响应时延 | µs |
| `percentile95` | 95分位时延 | µs |
| `percentile99` | 99分位时延 | µs |

## system-cpu-usage — 系统·CPU 使用率（按节点）
来源：cpu_trace.cpu_time_stats
说明：节点汇总行（cpu_id=-1）各状态按核数累加，不是百分比：占比 = 该状态 / 各状态之和（idle + user + system + iowait + irq + softirq …）
过滤字段：`pod_node`（节点）

| 指标 key | 名称 | 单位 |
| --- | --- | --- |
| `cpu_usage_user` | 用户态（核数累加值） | - |
| `cpu_usage_system` | 内核态（核数累加值） | - |
| `cpu_usage_iowait` | IO 等待（核数累加值） | - |
| `cpu_usage_softirq` | 软中断（核数累加值） | - |

## system-cpu-load — 系统·CPU 负载（按节点）
来源：cpu_trace.cpu_loadavg
过滤字段：`pod_node`（节点）

| 指标 key | 名称 | 单位 |
| --- | --- | --- |
| `cpu_loadavg_load1` | 1 分钟负载 | - |
| `cpu_loadavg_load5` | 5 分钟负载 | - |
| `cpu_loadavg_load15` | 15 分钟负载 | - |

## system-memory — 系统·节点内存
来源：mem_trace.mem_info_stats
过滤字段：`pod_node`（节点）

| 指标 key | 名称 | 单位 |
| --- | --- | --- |
| `mem_info_stats_mem_available` | 可用内存 | bytes |
| `mem_info_stats_mem_free` | 空闲内存 | bytes |
| `mem_info_stats_swap_in` | Swap 换入 | 页 |
| `mem_info_stats_swap_out` | Swap 换出 | 页 |

## system-process-drill — 系统·进程下钻（节点→Pod→进程 pid / tid）
来源：cpu_trace / mem_trace / io_trace / net_trace 的事件与统计表
说明：定位 CPU / 内存 / 存储 / 网络问题落在哪个 Pod、哪个进程（含 pid、tid、进程名）；按 pod_node 过滤，不指定节点时取 CPU 负载最高的 2 个节点；数值为窗口内事件次数（内存为占 limit 的百分比 / 进程 RSS 字节 / 网卡丢包个每秒）；一次最多选 4 个指标
过滤字段：`pod_node`（节点）

| 指标 key | 名称 | 单位 |
| --- | --- | --- |
| `cpu.runqlat` | CPU·CPU 运行队列延迟事件 | - |
| `cpu.softirq` | CPU·软中断延迟事件 | - |
| `cpu.hungtask` | CPU·hungtask 事件 | - |
| `cpu.softlockup` | CPU·softlockup 事件 | - |
| `mem.oom` | 内存·OOM Kill 事件 | - |
| `mem.reclaim` | 内存·内存直接回收停顿事件 | - |
| `mem.pagefail` | 内存·内存页回收失败事件 | - |
| `mem.pod` | 内存·Pod 内存用量 | - |
| `mem.process` | 内存·进程内存占用 | - |
| `io.process` | 存储·磁盘 IO 慢请求 | - |
| `net.skb` | 网络·网络丢包事件 | - |
| `net.latency` | 网络·网络接收延迟事件 | - |
| `net.qlatency` | 网络·网络发送延迟事件 | - |
| `net.rst` | 网络·TCP RST 事件 | - |
| `net.dev` | 网络·网卡丢包 / 错误 | - |
