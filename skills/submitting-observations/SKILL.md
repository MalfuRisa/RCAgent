---
name: submitting-observations
description: 围绕当前SRE问题取得最小事实，利用已有材料调整查询方法并准确引用原文。
---

# 起步、恢复与引用

只回答当前TaskSpec小问题，遵守effective_tools、数据集和窗口。first_result在首批真实资源/业务结果（成功、空、失败）结算后无工具小结；answer按理由完成必要短关联链，达到完成条件即交回。支持元数据不算目标结果，本地拒绝不算查过后端。需要换范围时交Planner，不自行扩大。收尾时停止取证，简洁返回关键原值、依据和未知项。

优先复用本run同表已有字段、table.datasources、成功调用方法及有来源的对象关联；需要时read_material精读，缺字段/用法则查insights_describe_query。本表字段不能跨表类推，旧窗口事实不能冒充当前事实。help平铺参数，endpoint必填；usage可无数据集，overview/fields须指定database/table，fields可按field_names/keyword缩小。它提供方法和元数据，不是已执行查询或现场事实。

endpoint只能是list/metric/dimensions；fields和overview是section的值，不是endpoint。以下为原生help形状；占位符必须换成实际值，不能直接执行：

```json
{"endpoint":"list"}
```

上例默认section=usage，只有静态用法，不查询下游业务。

```json
{"endpoint":"list","section":"overview","database":"<实际库>","table":"<实际表>"}
```

```json
{"endpoint":"list","section":"fields","database":"<实际库>","table":"<实际表>","field_kind":"tag","limit":12}
```

只有fields允许field_names/keyword/offset/limit等字段选择参数。本地rcagent_hint是应用层修参提示，不是MCP返回或现场证据；按路径手动修正，不假定程序已改请求。已知合法参数可直接查询，help不是许可前提。

定位名称时可用meta_tag_values的有限候选，或带窗业务行中的ID再查资源；按具体tag区分value/display_name，不把平台lcuuid当K8s UID。保留歧义、未应用过滤、删除项、候选不完整和无时间窗限制；候选不等于唯一身份认证。

空或失败时先分清语义和缺口，再选原文、help、修正参数或其他有依据的方法；不要盲换字段/时间或只改query_id。查看文本错误details的stage/reason/request_id/retry_advice，通用backend_error不能擅自解释成字段、表或数据源不存在。正常空、过滤未应用、元数据投影空页和用法未知要分别说明；空不证明健康或不存在，未决可交有限结论。

依据source_id引用对象、窗口、单位与原值，关联host不自动成为Pod直证。保留query_scope中requested/backend_reported/source_contract/unknown，runtime_match=unverified不写成已核实。分页区分返回行数、total未请求/缺失/冲突/未核验；total=0不等于零行或全集为空，未知has_more/有效页/截断不猜。helper的selection是元数据计数，不是业务行；只可读回本次收到的投影，独立翻页不保证同一快照。

详情接口的_id必须依据已有材料核对来源；接口没有时间请求字段时，不能声称程序已按任务窗口过滤。

可保留笔记，交回本小任务的自然语言阶段答复、关键原值与明确缺口，无正式观测ID/报告包。程序关联来源，指导不是证据。具体参数形状和恢复例子按需读[示例](references/native-examples.md)。

名称定位先查本次meta_tag_values候选，body为db/table/tag_name/limit，可用实际支持的where筛选。它没有offset，不把help的offset搬过来；有限候选没有目标不能证明不存在。得到有来源的ID再k8s_resource，核对返回对象关联。资源工具未返回Ready/restartCount时写未知，平台状态不等于实时健康。
