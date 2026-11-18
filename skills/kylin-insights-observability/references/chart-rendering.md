# KylinInsight 指标图表渲染约定

KylinInsight Web 的指标量图组件是 `MetricChartV2`（ECharts）。Agent 侧出图与它保持同一口径：

- 横轴为时间（type: time），纵轴按单位换算（µs → ms / s，bps → Kbps / Mbps，bytes → KB / MB / GB），百分比为百分制（0–100）。
- 配色（CHART_PALETTE）：#3b7cff、#14b8a6、#f5a524、#8b5cf6、#0ea5e9、#5fb83d、#ec4899、#64748b。
- 折线 1.5px、圆点 4px、平滑；面积图渐变透明度 0.22；分隔线虚线 #ebeef5；轴标签 #909399、11px。
- 阈值线：橙色 #ff7d00 虚线，标签「关注 / 严重阈值 X」放在线的右上方（insideEndTop）。
- 每个指标一张图，同一张图里每个对象（客户端服务 → 服务端服务）一条线，最多 6 条。

## 工具返回的图表数据
- `insights_query_metrics` / `insights_metric_history` / `insights_generate_dashboard` 的结果里，每张图都有：标题、单位、序列（[Unix 毫秒, 值]）、阈值。
- `chartFormat: "echarts"` 时，工具直接返回可 `setOption` 的 ECharts option（与 Web 同口径），可写进一个 HTML 文件渲染：

```html
<div id="c" style="width:760px;height:320px"></div>
<script src="https://cdn.jsdelivr.net/npm/echarts@6/dist/echarts.min.js"></script>
<script>echarts.init(document.getElementById('c')).setOption(/* 工具返回的 option */);</script>
```
（内网环境请把 echarts.min.js 换成内网地址；stdio 模式下工具已经在本机写好了离线可打开的 SVG / HTML。）
- `chartFormat: "mermaid"` 时返回 Mermaid `xychart-beta`，可直接放进 Markdown 回答。

## Web 端组件源码（节选，供需要自行渲染的 Agent 参考）

```ts
export function buildMetricChartOption(
  input: BuildMetricChartOptionInput,
): EChartsOption {
  if (input.config.style.type === 'pie') {
    return buildPieOption(input);
  }
  const { config, getChartRect, series, theme, timeRange } = input;
  const categories = input.categories ?? null;
  // 分类轴没有时间概念，不支持拖拽选时间段
  const brush = input.brush && !categories;
  const tooltipBase = buildChartTooltipBase(theme);
  const units = resolveYAxisUnits(series);
  const isBar = config.style.type === 'bar';
  const bounds = resolveTimeBounds(series, timeRange, isBar && !categories);
  const spansDays =
    bounds.min !== undefined &&
    bounds.max !== undefined &&
    bounds.max - bounds.min > DAY_MS;
  const seriesMap = new Map(series.map((item) => [item.key, item]));
  const thresholdAttached = new Set<string>();

  const yAxis = units.map((unit, index) => {
    const { axis } = config;
    const percentFixed = unit === '%' && !axis.dynamic;
    const axisSeries = series.filter((item) => item.unit === unit);
    const formatOptions = axisSeries[0]?.formatOptions;
    const percentMax = formatOptions?.percentAsFraction ? 1 : 100;
    let reference = Math.max(Math.abs(axis.min ?? 0), Math.abs(axis.max ?? 0));
    for (const item of axisSeries) {
      reference = Math.max(reference, Math.abs(item.threshold?.value ?? 0));
      for (const [, value] of item.data) {
        if (value !== null && Number.isFinite(value)) {
          reference = Math.max(reference, Math.abs(value));
        }
      }
    }
    const axisFormatter = createMetricAxisFormatter(
      reference,
      unit,
      formatOptions,
    );
    return {
      axisLabel: {
        color: theme.axisLabel,
        fontSize: 11,
        formatter: axisFormatter,
        margin: 6,
      },
      axisLine: { show: false },
      axisTick: { show: false },
      max: axis.max ?? (percentFixed ? percentMax : undefined),
      min: axis.min ?? (axis.dynamic ? undefined : 0),
      position: index === 0 ? 'left' : 'right',
      scale: axis.dynamic,
      splitLine: {
        lineStyle: { color: theme.splitLine, type: 'dashed' },
        show: index === 0 && axis.showSplitLine,
      },
      splitNumber: 3,
      type: 'value',
    };
  });

  const seriesOption = series.map((item) => {
    const yAxisIndex = resolveAxisIndex(units, item.unit);
    const attachThreshold =
      item.threshold && !thresholdAttached.has(item.metricKey);
    if (attachThreshold) {
      thresholdAttached.add(item.metricKey);
    }
    const text = item.threshold
      ? formatThreshold(item.threshold, item.unit, item.formatOptions)
      : '';

    return {
      areaStyle:
        config.style.type === 'area'
          ? {
              color: {
                colorStops: [
                  { color: item.color, offset: 0 },
                  { color: 'rgba(255,255,255,0)', offset: 1 },
                ],
                type: 'linear',
                x: 0,
                x2: 0,
                y: 0,
                y2: 1,
              },
              opacity: 0.22,
            }
          : undefined,
      barMaxWidth: categories ? 28 : 14,
      connectNulls: false,
      data: categories
        ? alignToCategories(item.data, categories.length)
        : item.data,
      emphasis: { focus: 'series' },
      id: item.key,
      itemStyle: { color: item.color },
      lineStyle: { color: item.color, width: 1.5 },
      markLine: attachThreshold
        ? {
            animation: false,
            data: [{ yAxis: item.threshold?.value ?? 0 }],
            label: {
              color: THRESHOLD_COLOR,
              fontSize: 11,
              formatter: () => text,
              position: 'insideEndTop',
            },
            lineStyle: { color: THRESHOLD_COLOR, type: 'dashed', width: 1 },
            silent: true,
            symbol: 'none',
          }
        : undefined,
      name: item.name,
      showSymbol: config.style.showSymbol,
      smooth: config.style.smooth,
      // 堆叠按纵轴分组，不同单位的序列不会叠在一起
      stack: config.style.stack ? `stack-${yAxisIndex}` : undefined,
      symbol: 'circle',
      symbolSize: 4,
      type: isBar ? 'bar' : 'line',
      yAxisIndex,
    };
  });

  const option: EChartsOption = {
    animationDuration: 300,
    brush: brush
      ? {
          brushMode: 'single',
          brushStyle: {
            borderColor: withChartAlpha(BRUSH_COLOR, 0.7),
            borderWidth: 1,
            color: withChartAlpha(BRUSH_COLOR, 0.12),
          },
          brushType: 'lineX',
          inBrush: { opacity: 1 },
          outOfBrush: { colorAlpha: 1, opacity: 1 },
          removeOnClick: true,
          throttleDelay: 0,
          throttleType: 'debounce',
          // 仅保留与「横向选时间段」一致的按钮，避免默认注入 rect / polygon 等二维刷选
          toolbox: ['lineX', 'clear'],
          transformable: false,
          xAxisIndex: 0,
        }
      : undefined,
    // 底部分割条覆盖图表底边，预留空间避免遮挡 x 轴标签
    grid: {
      bottom:
        config.legend.show && config.legend.position === 'bottom' ? 12 : 2,
      containLabel: true,
      left: 4,
      right: 10,
      top: hasExplicitToolbox(config) ? GRID_TOP_WITH_TOOLBOX : GRID_TOP,
    },
    series: seriesOption as any,
    toolbox: buildToolbox(config, theme),
    tooltip: config.tooltip.show
      ? {
          ...tooltipBase,
          // 挂到 body 时由 getChartRect 换算定位，不再受图表框限制
          appendToBody: Boolean(getChartRect),
          // 分类柱状图用整列阴影指示，时间轴保持虚线
          axisPointer:
            categories && isBar
              ? {
                  shadowStyle: { color: withChartAlpha(BRUSH_COLOR, 0.06) },
                  type: 'shadow',
                }
              : {
                  lineStyle: { color: theme.axisLabel, type: 'dashed' },
                  type: 'line',
                },
          confine: !getChartRect,
          extraCssText: getChartRect
            ? `${tooltipBase.extraCssText}max-width:420px;z-index:9999;`
            : `${tooltipBase.extraCssText}max-width:min(420px,100%);`,
          formatter: (params: any) => {
            // 分类轴的点值是数字，时间轴是 [时间, 值]，统一成 [横轴, 值]
            const list = (Array.isArray(params) ? params : [params])
              .map((item) => ({
                ...item,
                value: categories
                  ? [item.dataIndex, item.value]
                  : (item.value as [number, null | number]),
              }))
              .filter(
                (item) =>
                  item.value?.[1] !== null && item.value?.[1] !== undefined,
              );
            if (list.length === 0) {
              return '';
            }
            const { sort, maxItems } = config.tooltip;
            if (sort !== 'none') {
              list.sort((left, right) =>
                sort === 'desc'
                  ? right.value[1] - left.value[1]
                  : left.value[1] - right.value[1],
              );
            }
            const shown = maxItems > 0 ? list.slice(0, maxItems) : list;
            const header = categories
              ? escapeHtml(categories[list[0].value[0]] ?? '')
              : dayjs(list[0].value[0]).format('YYYY-MM-DD HH:mm:ss');
            const rows = shown
              .map((item) => {
                const target = seriesMap.get(item.seriesId);
                const value = formatMetricValue(
                  item.value[1],
                  target?.unit ?? '',
                  false,
                  target?.formatOptions,
                );
                return `<div style="display:flex;align-items:center;gap:6px;line-height:20px;">
                  <span style="flex:none;width:8px;height:8px;border-radius:2px;background:${escapeHtml(item.color)}"></span>
                  <span style="min-width:0;max-width:280px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:${theme.tooltipSecondary}">${escapeHtml(item.seriesName)}</span>
                  <span style="flex:none;margin-left:auto;padding-left:12px;font-weight:600;color:${theme.tooltipPrimary}">${escapeHtml(value)}</span>
                </div>`;
              })
              .join('');
            const more =
              list.length > shown.length
                ? `<div style="margin-top:2px;color:${theme.tooltipSecondary}">… 另有 ${list.length - shown.length} 项</div>`
                : '';
            return `<div style="font-size:12px;"><div style="margin-bottom:4px;color:${theme.tooltipSecondary}">${header}</div>${rows}${more}</div>`;
          },
          position: (point, _params, _dom, _rect, size) => {
            const chartRect = getChartRect?.();
            return chartRect
              ? resolveFloatingTooltipPosition(
                  point,
                  size.contentSize,
                  chartRect,
                  { height: window.innerHeight, width: window.innerWidth },
                )
              : resolveTooltipPosition(point, size.contentSize, size.viewSize);
          },
          trigger: 'axis',
        }
      : { show: false
```
