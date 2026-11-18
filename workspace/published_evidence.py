"""Check and render the program's canonical evidence independently of draft prose."""

from datetime import datetime, timedelta, timezone

from .history_evidence import history_candidates
from .native_materials import pointer_value

ZONE = timezone(timedelta(hours=8))


def verify_group(outputs, group):
    window = group['window']
    for path in group['paths']:
        source = outputs.sources.get(path['source_id'])
        time = (source or {}).get('resolved_time') or {}
        if not source or source['status'] != 'ok' or any(time.get(k)!=v for k,v in window.items()):
            return False
    step = group['sampling_seconds'] * 1000
    for change in group['single_path_candidates']:
        if not verify_change(outputs,change,change['timestamp_ms'],step,group):
            return False
    for item in group['same_time_candidates']:
        for change in item['rising']:
            if not verify_change(outputs,change,item['timestamp_ms'],step,group):
                return False
    return True


def verify_change(outputs, change, timestamp, step, group):
    raw = outputs.materials.raw.get(change['source_id'])
    if raw is None:
        return False
    for label, offset in (('before',-step),('current',0),('after',step)):
        point = change.get(label)
        if point is None or (point['value'] is None and point['pointer'] is None):
            continue
        try:
            chart_pointer = point['pointer'].split('/series/', 1)[0]
            chart = pointer_value(raw, chart_pointer)
            original = pointer_value(raw, point['pointer'])
            if chart.get('unit') != group['unit'] or chart.get('metricKey') != group['metric']:
                return False
            if original != [timestamp+offset,point['value']]:
                return False
        except (KeyError, IndexError, TypeError, ValueError, AttributeError):
            return False
    return True


def checked_history(outputs):
    evidence = history_candidates(outputs)
    if evidence is None:
        return None
    groups = [group for group in evidence['groups'] if verify_group(outputs,group)]
    return {**evidence, 'groups':groups, 'groups_failed_source_check':len(evidence['groups'])-len(groups)}


def clock(seconds, format='%H:%M:%S'):
    return datetime.fromtimestamp(seconds,ZONE).strftime(format)


def format_change(change, unit):
    path = change['path']
    points = [change.get(key) for key in ('before','current','after')]
    values = ' → '.join(str(p['value']) if p and p['value'] is not None else '未知' for p in points)
    refs = '、'.join(f'\x60{p["pointer"]}\x60' for p in points if p and p.get('pointer'))
    return f'{path["client"]} → {path["server"]} 为 {values} {unit}，来源 {change["source_id"]} 的 {refs}'


def evidence_text(evidence, unresolved_references=()):
    if not evidence and not unresolved_references:
        return ''
    lines = ['## 程序核对的时序证据']
    for group in (evidence or {}).get('groups',[])[:6]:
        window = group['window']
        start, end = clock(window['start'],'%Y-%m-%d %H:%M:%S'), clock(window['end'],'%Y-%m-%d %H:%M:%S')
        finite = group['timestamps_with_values_on_two_or_more_paths']
        comparable = group['timestamps_with_adjacent_deltas_on_two_or_more_paths']
        lines.append(f'- {group["dataset"]} 的 \x60{group["metric"]}\x60（{group["unit"]}），UTC+8 {start}–{end}，'
                     f'请求采样间隔 {group["sampling_seconds"]} 秒。至少两条路径同刻有值 **{finite}** 个时点，'
                     f'至少两条路径各有前后相邻点而可比较涨幅 **{comparable}** 个时点。')
        coverage = '；'.join(f'{p["path"][0]} → {p["path"][1]} {p["finite_points"]} 个有限点（{p["source_id"]}）'
                            for p in group['paths'][:6])
        lines.append(f'  样本覆盖 {len(group["paths"])} 条返回路径，已列 {min(6,len(group["paths"]))} 条。{coverage}。')
        if comparable == 0:
            lines.append('  当前样本不足以判断多条路径是否同步变化。')
        if group['single_path_candidates']:
            change = group['single_path_candidates'][0]
            lines.append(f'  {clock(change["timestamp_ms"]/1000)} 的单路径变化候选，{format_change(change,group["unit"])}。')
        if group['same_time_candidates']:
            item = group['same_time_candidates'][0]
            lines.append(f'  {clock(item["timestamp_ms"]/1000)} 的同刻上升候选，可比 {item["compared"]} 条，'
                         f'缺相邻点 {item["missing"]} 条。')
            lines.extend('  - '+format_change(change,group['unit'])+'。' for change in item['rising'][:2])
    if evidence:
        if len(evidence['groups']) > 6 or evidence['received_group_count'] > len(evidence['groups']):
            lines.append('- 部分时序组未展示或未通过来源校验，详见结构化核对记录。')
        if evidence['unconfirmed_dimensions']:
            lines.append('- 部分时序的维度未确认，原始来源为 ' + '、'.join(evidence['unconfirmed_dimensions']) + '。')
        lines.append('观测点和路径身份以工具范围为限，缺点保持未知。同期上升候选不证明共同原因。')
    if unresolved_references:
        lines.append('正文中这些引用未通过本次归档检查：' + '、'.join(unresolved_references) + '。')
    return '\n'.join(lines)
