# 平台阈值（由 @kylin/ai-contracts 的 METRIC_THRESHOLDS 生成，勿手改）

超过阈值只是「疑似异常」的排查线索，不是故障定论。百分比为百分制，时延原始单位为 µs。

| 指标 key | 名称 | 关注阈值 | 严重阈值 |
| --- | --- | --- | --- |
| `tcp_retrans_ratio` | TCP重传比例 | > 1% | > 5% |
| `tcp_client_retrans_ratio` | 客户端重传比例 | > 1% | > 5% |
| `tcp_server_retrans_ratio` | 服务端重传比例 | > 1% | > 5% |
| `tcp_establish_fail_ratio` | TCP建连失败比例 | > 0% | > 1% |
| `tcp_rtt` | TCP建连时延 | > 100 ms | > 500 ms |
| `tcp_zero_window` | TCP零窗口 | > 0 | - |
| `application_exception_ratio` | 应用异常比例 | > 1% | - |
| `server_response_time` | 服务端响应时延 | > 500 ms | > 1 s |
| `percentile95` | 95分位时延 | > 1 s | > 3 s |
| `client_error_ratio` | 客户端异常比例 | > 5% | > 20% |
| `server_error_ratio` | 服务端异常比例 | > 2% | > 10% |
