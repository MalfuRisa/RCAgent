"""Real MCP preflight only: discovers current CPU identity without a diagnosis."""
import asyncio
import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from tools.v5_cpu_client import V5CPUClient, cpu_arguments
from local_testing.v5_gateway_session import gateway


def save(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2,
        default=lambda obj: obj.model_dump(mode='json') if hasattr(obj, 'model_dump') else str(obj)), encoding='utf-8')


async def preflight(output):
    output = Path(output)
    async with gateway(output) as (url, token):
        async with V5CPUClient(url, token) as client:
            save(output/'protocol.json', {'identity': client.identity, 'published': sorted(client.published),
                'allowed_schemas': client.schemas, 'route': url})
            async def call(name, args, label):
                result = await client.call('preflight', name, args)
                save(output/(label+'.json'), {'tool': name, 'arguments': args, 'result': result})
                return result
            await call('insights_system_version', {}, 'version')
            await call('insights_list_tables', {'database': 'cpu_trace'}, 'cpu-tables')
            await call('insights_get_tags', {'database': 'cpu_trace', 'table': 'runqlat_event'}, 'cpu-tags')
            end = datetime.now(timezone.utc).replace(microsecond=0)
            window = {'start_time': (end-timedelta(minutes=15)).isoformat(), 'end_time': end.isoformat()}
            found = await call('insights_list_cpu_events', cpu_arguments(window), 'cpu-discovery-15m')
            rows = found['data']['list']
            if not rows:
                window['start_time'] = (end-timedelta(hours=24)).isoformat()
                found = await call('insights_list_cpu_events', cpu_arguments(window), 'cpu-discovery-24h')
                rows = found['data']['list']
            candidates = [r for r in rows if r.get('pod_cluster') == 'rd2' and r.get('pod_node_id')]
            if candidates:
                row = candidates[0]
                await call('insights_k8s_resource', {'query': {'node_id': row['pod_node_id'],
                    'include': 'cluster'}}, 'node-identity')
                save(output/'target-input.json', {'query_window': window, 'sample': row,
                    'source': 'cpu-discovery-15m.json' if (end-datetime.fromisoformat(window['start_time'])).total_seconds() == 900 else 'cpu-discovery-24h.json',
                    'note': 'Discovery determines identity/window only; no diagnosis prefilled.'})
    print('preflight saved: '+str(output))


if __name__ == '__main__':
    asyncio.run(preflight(sys.argv[1]))
