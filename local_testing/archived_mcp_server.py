"""Standalone local MCP replay of unmodified recorded responses; no backend engine.

Only exact effective requests from the selected archive are answered. This is
recorded-data testing, never a current cluster observation or real Gateway run.
"""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    from mcp.server.fastmcp import FastMCP
    from mcp.types import Tool, TextContent
    from tools.contracts import effective_parameters
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--records', type=Path, required=True)
    parser.add_argument('--port', type=int, default=18992)
    args = parser.parse_args()
    records = json.loads(args.records.read_text(encoding='utf-8'))['evidence']
    schemas = json.loads((ROOT / 'local_testing/fixtures/workspace_gateway_schemas.json').read_text())
    available = {row['tool_name'] for row in records}
    server = FastMCP('RCAgent archived MCP replay', host='127.0.0.1', port=args.port,
                     streamable_http_path='/mcp', stateless_http=True, json_response=True)

    @server._mcp_server.list_tools()
    async def list_tools():
        return [Tool(name=row['name'], description='Replay only: exact recorded request; not live data.',
                     inputSchema=row['inputSchema']) for row in schemas if row['name'] in available]

    @server._mcp_server.call_tool()
    async def call_tool(name, arguments):
        effective = effective_parameters(name, arguments)
        matched = [row for row in records if row['tool_name'] == name
                   and effective_parameters(name, row['arguments']) == effective]
        raw = matched[0]['observations'] if matched else {
            'status': 'error', 'error_type': 'recording_not_found',
            'data_gaps': [{'source': 'local_recording', 'reason': 'No exact recorded request matches these effective parameters'}]}
        return [TextContent(type='text', text=json.dumps(raw, ensure_ascii=False))]

    server.run(transport='streamable-http')


if __name__ == '__main__':
    main()
