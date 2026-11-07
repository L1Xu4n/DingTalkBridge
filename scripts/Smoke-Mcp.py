"""Launch a locally generated MCP config and report only protocol/status counts."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess

root = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser()
parser.add_argument('--write-report', type=Path)
parser.add_argument('--mcp-config', type=Path, default=root / 'mcp.local.json')
options = parser.parse_args()
configuration = json.loads(options.mcp_config.read_text(encoding='utf-8'))['mcpServers']['dingtalkbridge']
requests = [
    {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-11-25', 'capabilities': {}, 'clientInfo': {'name': 'deployment-smoke', 'version': '1'}}},
    {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
    {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
    {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': {'name': 'bridge_status', 'arguments': {}}},
    {'jsonrpc': '2.0', 'id': 4, 'method': 'resources/read', 'params': {'uri': 'bridge://status'}},
]
completed = subprocess.run([configuration['command'], *configuration['args']], env=dict(os.environ, **configuration.get('env', {})),
                           input=''.join(json.dumps(x)+'\n' for x in requests), text=True, encoding='utf-8', capture_output=True, timeout=20)
if completed.returncode or completed.stderr:
    raise SystemExit('MCP deployment smoke failed; inspect selected paths and permissions (no raw output shown).')
responses = {x['id']: x for x in map(json.loads, completed.stdout.splitlines())}
if set(responses) != {1, 2, 3, 4} or any('error' in x for x in responses.values()):
    raise SystemExit('MCP deployment smoke failed: invalid protocol responses.')
status_result = responses[3]['result']
if status_result.get('isError'):
    raise SystemExit('MCP deployment smoke failed: status tool error.')
status = status_result.get('structuredContent') or json.loads(status_result['content'][0]['text'])
tools = responses[2]['result']['tools']
expected = {'bridge_status', 'list_conversations', 'get_messages', 'search_messages', 'get_context', 'poll_events'}
if {tool['name'] for tool in tools} != expected or not all(tool['annotations']['readOnlyHint'] for tool in tools):
    raise SystemExit('MCP deployment smoke failed: unexpected tool surface.')
report = {
    'verified_at_utc': datetime.now(timezone.utc).isoformat(),
    'success': True,
    'launch_source': 'local_mcp_configuration',
    'protocol_version': responses[1]['result']['protocolVersion'],
    'server_name': responses[1]['result']['serverInfo']['name'],
    'read_only_tool_count': len(tools),
    'status_resource_read': bool(responses[4]['result']['contents']),
    'dataset_kind': status.get('dataset_kind'),
    'record_count': status.get('record_count', status.get('message_count', 0)),
    'collection_state': status.get('collection_state'),
    'verified_message_count': status.get('verified_message_count', 0),
    'message_bodies_printed': False,
    'stderr_empty': True,
}
if options.write_report:
    options.write_report.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
print(json.dumps(report, ensure_ascii=False, indent=2))
