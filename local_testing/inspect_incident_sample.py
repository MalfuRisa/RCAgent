"""Read-only Kubernetes sample selection; no exec, writes or model requests."""
import argparse
import json
import re
import subprocess
from datetime import datetime, timezone, timedelta
from pathlib import Path


def kubectl(*args):
    result = subprocess.run(['kubectl', '--request-timeout=8s', '-n', 'kylin-insights', *args],
                            capture_output=True, text=True, timeout=15)
    if result.returncode:
        return {'query_error': result.stderr[-1000:]}
    if '-o' in args:
        return json.loads(result.stdout)
    return result.stdout


def scrub(line):
    if re.search(r'password|passwd|secret|authorization|api[_ -]?key|\btoken\b|sk-[\w.-]+', line, re.I):
        return '<redacted potentially sensitive log line>'
    return re.sub(r'(https?://[^\s?]+)\?\S+', r'\1?<redacted>', line)


def abnormal_line(line):
    # Match application content, never a 5xx-shaped number in timestamps or IPs.
    content = re.sub(r'\x1b\[[0-9;]*m', '', line.split(' ', 1)[-1]).strip()
    try:
        entry = json.loads(content)
    except ValueError:
        if re.match(r'\[[\d.]+ms\]\s+\[rows:', content):
            return False  # SQL predicates containing status=failed are not failures
        return bool(re.search(r'\berror\b|exception|timed? ?out|failed|refused|reset|HTTP/\S+"?\s+5\d\d\b', content, re.I))
    if not isinstance(entry, dict):
        return False
    status = entry.get('status_code', entry.get('status'))
    message = str(entry.get('msg', ''))
    if 'websocket: close 1001' in message or 'websocket: close 1005' in message:
        return False  # normal browser disconnect is not an established fault
    return (str(entry.get('level', '')).upper() in ('ERROR', 'FATAL', 'PANIC')
            or isinstance(status, int) and status >= 500
            or bool(re.search(r'\berror\b|exception|timed? ?out|failed|refused|reset', message, re.I)))


def inspect(output):
    output.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    pods = kubectl('get', 'pods', '-o', 'json')
    if 'query_error' in pods:
        (output / 'snapshot.json').write_text(json.dumps(pods))
        print(json.dumps(pods))
        return
    selected = [p for p in pods['items'] if p['metadata']['name'].startswith(('iam-server-', 'frontend-'))]
    inventory = []
    candidates = []
    for pod in selected:
        name = pod['metadata']['name']
        inventory.append({'name': name, 'uid': pod['metadata']['uid'], 'labels': pod['metadata'].get('labels'),
                          'created_at': pod['metadata']['creationTimestamp'], 'status': pod.get('status')})
        logs = kubectl('logs', name, '--all-containers=true', '--timestamps=true', '--since=6h', '--tail=500')
        lines = logs.splitlines() if isinstance(logs, str) else []
        matches = [scrub(line) for line in lines if abnormal_line(line)]
        (output / (name + '-error-lines.log')).write_text('\n'.join(matches), encoding='utf-8')
        timestamps = []
        for line in matches:
            try:
                stamp = datetime.fromisoformat(line.split(' ', 1)[0].replace('Z', '+00:00'))
                timestamps.append(stamp)
            except ValueError:
                continue
        if timestamps:
            end = min(max(timestamps) + timedelta(minutes=1), now)
            candidates.append({'pod': name, 'start_time': (end - timedelta(minutes=15)).isoformat(),
                               'end_time': end.isoformat(), 'matching_lines': len(matches),
                               'basis': name + '-error-lines.log', 'selection_only_not_root_cause': True})
    services = kubectl('get', 'services', '-o', 'json')
    service_rows = [{'name': s['metadata']['name'], 'selector': s['spec'].get('selector'), 'ports': s['spec'].get('ports')}
                    for s in services.get('items', []) if 'iam' in s['metadata']['name'] or 'frontend' in s['metadata']['name']]
    events = kubectl('get', 'events', '-o', 'json')
    event_rows = [{k: e.get(k) for k in ('reason', 'message', 'firstTimestamp', 'lastTimestamp', 'eventTime', 'count', 'involvedObject')}
                  for e in events.get('items', []) if e.get('involvedObject', {}).get('name', '').startswith(('iam', 'frontend'))]
    snapshot = {'observed_at': now.isoformat(), 'namespace': 'kylin-insights', 'pods': inventory,
                'services': service_rows, 'events': event_rows, 'candidate_windows': candidates,
                'log_scope': 'last 6h; at most 500 lines per Pod; matching lines only, sensitive lines redacted'}
    (output / 'snapshot.json').write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'pod_names': [p['name'] for p in inventory], 'events': len(event_rows),
                      'candidate_windows': candidates}, ensure_ascii=False))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    inspect(parser.parse_args().output)
