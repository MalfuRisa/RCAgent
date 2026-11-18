"""Preserve all task attempts; priority is presentation order only."""
def merge_rca_analyses(left, right):
    result, positions = [], {}
    for analysis in left + right:
        task = analysis.get("task", {})
        key = (task.get("task_id"), analysis.get("attempt_id"))
        if key[0] is None:
            result.append(analysis)
        elif key in positions:
            result[positions[key]] = analysis
        else:
            positions[key] = len(result)
            result.append(analysis)
    return sorted(result, key=lambda a: a.get("task", {}).get("priority", 0))
