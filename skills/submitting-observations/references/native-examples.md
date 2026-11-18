# 按需查询与恢复示例

## 已有材料先用
若本run已经取得某表的datasources和字段，优先据此查询原窗口。来源A的成功方法可复用，来源A的历史数值仍不能替代本次现场。不同表即使名字相近，也应核对各自字段。

## 缺用法或字段
help输入平铺，例如 {"endpoint":"list"} 只读静态用法。已知database/table时，overview可看该表的实际datasources。fields可用 {"endpoint":"list","section":"fields","database":"<实际库>","table":"<实际表>","field_kind":"tag","field_names":["<实际字段或方向别名>"],"limit":12}。占位符不是可直接执行参数；不要求每次查询前调用help。按当前已知缺口选择tag/metric/all，避免无目的读取全部元数据。

使用返回的原name、client/server别名、unit、description、is_agg、operators；permissions向量不是IAM认证。selection/provenance/unknowns/warnings说明的是本次元数据响应。next_offset翻页会独立重读来源，不保证同一快照。helper没有返回的内部元数据无法通过read_material读回。

## 定位与错误
meta_tag_values的value可能是平台数字ID，display_name可能是名称，按具体tag核对，不机械把所有value当Pod ID。有限候选若无窗口/未删除过滤信息，就保留这些缺口；无证据不要声称cluster/namespace过滤已应用。

错误details.stage=business_status、backend_code=500只说明业务失败；retry_advice=unknown时不能说“字段不存在”。对照本表字段、数据源和原文后再选择下一步。若合法查询正常空，解释它的实际对象、窗口和条件；换窗口应有比较目的，不能把空记录说成健康。

## 分页与陈述
本页90行、data.total=0，meta.pagination.total_status=not_requested：应写“返回90行，总数未请求”，不能说总数为0。reported total与实际页行数冲突则明确冲突。selection.returned_items=12是helper元数据投影，不是12条业务记录。分页unknown/null不推导完整性。

host日志须列关联依据；记录了权限拒绝只能说明该次请求被拒，不能证明最终状态或业务因果。引用source_id足够，无正式ID登记门槛。
