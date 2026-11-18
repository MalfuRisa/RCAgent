"""Render checked statements, never the unverified free report paragraphs."""


def render_checked(base, checks, items, errors):
    # Native reports already contain the saved readable body and review notes.
    if base.get("runtime") in {"rcagent", "opencode"} and isinstance(base.get("body"), str):
        return dict(base)
    sections = {name: [] for name in ('observations', 'hypotheses', 'exclusions', 'limitations', 'next_steps')}
    for item in items:
        sections[item['section']].append(item['content'])
    # Facts and interpretations come only from retained statements, including
    # when a malformed report item was rejected. Free report prose cannot add facts.
    for name, types in [('observations', ('observation', 'causal')), ('hypotheses', ('hypothesis',)), ('exclusions', ('exclusion',))]:
        sections[name] = [c['statement']['content'] for c in checks if c['retained'] and c['statement']['type'] in types]
    sections['limitations'] = list(dict.fromkeys(sections['limitations'] + [limit for c in checks if c['retained'] for limit in c['statement']['limitations']]))
    causes = [check['mechanism_statement'] for check in checks if check.get('supports_cause')]
    result = dict(base, fallback=not any(c['retained'] for c in checks),
                  report_origin='checked_model_statements' if any(c['retained'] for c in checks) else 'program_fallback',
                  statements=[c['statement'] for c in checks if c['retained']], report_items=items,
                  report_errors=errors, candidate_resources=[c['statement']['resource_id'] for c in checks if c['retained'] and not c['supports_cause'] and c['statement'].get('resource_id')])
    if items or any(sections.values()):
        result['evidence_summary'] = '\n'.join(sections['observations'] + sections['exclusions']) or '未取得可支持确定判断的新观测；已保存查询记录可回查。'
        parts = []
        for label, name in [('候选解释（未确认）', 'hypotheses'), ('范围与资料缺口', 'limitations'), ('下一步', 'next_steps')]:
            if sections[name]:
                parts.append(label + '：\n' + '\n'.join(sections[name]))
        result['investigation_summary'] = '\n'.join(parts) or '仅保留上述已核对的有限观测。'
    else:
        result['investigation_summary'] = '模型未提供可接受的结构化报告条目；使用工作台后备报告。\n' + result['investigation_summary']
    if errors:
        result['investigation_summary'] += '\n未接受部分建议：' + '; '.join(e['reason'] for e in errors)
    if causes:
        result.update(root_cause='; '.join(causes), answer_status='supported', detection=True)
    else:
        result.update(root_cause='根因未决：现有观测与候选解释不足以确认具体机制。', answer_status='inconclusive', localization=None)
        result['investigation_summary'] += '\n确定程度：根因未决；候选对象不属于已确认故障定位。'
    return result
