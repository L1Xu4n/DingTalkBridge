"""Black-box acceptance: real CLI import + real MCP child process, no network."""
import json
import os
from datetime import datetime, timezone
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class AcceptanceTests(unittest.TestCase):
    def test_cli_import_and_mcp_queries_round_trip(self):
        """Verify imported synthetic records over MCP without starting native collection."""
        with tempfile.TemporaryDirectory(prefix='dingtalkbridge-acceptance-') as tmp:
            folder = Path(tmp)
            config_path = folder / 'config.json'
            config_path.write_text(json.dumps({'data_dir': str(folder / 'data'), 'retention_days': 30}), encoding='utf-8')
            fixture = folder / 'fixture.jsonl'
            records = [
                {'id': f'acceptance-{i}', 'conversation_id': 'synthetic-school',
                 'conversation_title': '合成测试会话', 'sender_id': 'fixture-sender',
                 'sender_name': '测试用户', 'text': f'合成架构会议记录 {i}',
                 'timestamp': datetime.now(timezone.utc).isoformat(), 'source': 'synthetic-acceptance',
                 'confidence': 1, 'metadata': {'synthetic': True}}
                for i in range(3)
            ]
            fixture.write_text('\n'.join(json.dumps(x, ensure_ascii=False) for x in records)+'\n', encoding='utf-8')
            env = dict(os.environ, PYTHONPATH=str(ROOT/'src'), PYTHONUTF8='1')
            imported = subprocess.run([sys.executable, '-m', 'dingtalkbridge', '--config', str(config_path), 'import-jsonl', str(fixture)],
                                      cwd=ROOT, env=env, capture_output=True, encoding='utf-8', timeout=20)
            self.assertEqual(imported.returncode, 0, imported.stderr)
            self.assertNotIn('合成架构会议记录', imported.stdout + imported.stderr)
            requests = [
                {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-11-25', 'capabilities': {}, 'clientInfo': {'name': 'acceptance', 'version': '1'}}},
                {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
                {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list', 'params': {}},
                {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': {'name': 'get_messages', 'arguments': {'conversation_id': 'synthetic-school', 'limit': 2}}},
                {'jsonrpc': '2.0', 'id': 4, 'method': 'tools/call', 'params': {'name': 'search_messages', 'arguments': {'query': '架构', 'limit': 10}}},
                {'jsonrpc': '2.0', 'id': 5, 'method': 'tools/call', 'params': {'name': 'get_context', 'arguments': {'message_id': 'acceptance-1', 'before': 1, 'after': 1}}},
                {'jsonrpc': '2.0', 'id': 6, 'method': 'tools/call', 'params': {'name': 'poll_events', 'arguments': {'limit': 2}}},
                {'jsonrpc': '2.0', 'id': 7, 'method': 'resources/read', 'params': {'uri': 'bridge://status'}},
                {'jsonrpc': '2.0', 'id': 8, 'method': 'tools/call', 'params': {'name': 'search_messages', 'arguments': {'query': "' OR 1=1 --", 'limit': 10}}},
            ]
            payload = ''.join(json.dumps(x, ensure_ascii=False)+'\n' for x in requests)
            served = subprocess.run([sys.executable, '-m', 'dingtalkbridge', '--config', str(config_path), 'serve', '--no-collector'],
                                    cwd=ROOT, env=env, input=payload, capture_output=True, encoding='utf-8', timeout=20)
            self.assertEqual(served.returncode, 0, served.stderr)
            lines = [json.loads(line) for line in served.stdout.splitlines()]
            responses = {item['id']: item for item in lines}
            self.assertEqual(set(responses), set(range(1, 9)))
            for result in lines:
                self.assertEqual(result.get('jsonrpc'), '2.0')
                self.assertNotIn('error', result)
            tools = responses[2]['result']['tools']
            self.assertEqual({x['name'] for x in tools}, {'bridge_status', 'list_conversations', 'get_messages', 'search_messages', 'list_message_types', 'get_context', 'poll_events', 'list_attachments', 'list_group_files', 'read_attachment'})
            self.assertTrue(all(x['annotations']['readOnlyHint'] for x in tools))
            def value(n):
                """Decode one successful MCP tool result from the acceptance session."""
                result = responses[n]['result']
                self.assertFalse(result.get('isError', False), result)
                return result.get('structuredContent') or json.loads(result['content'][0]['text'])
            self.assertEqual(len(value(3)['messages']), 2)
            self.assertEqual(len(value(4)['messages']), 3)
            self.assertGreaterEqual(len(value(5)['messages']), 2)
            self.assertEqual(len(value(6)['messages']), 2)
            self.assertEqual(len(value(8)['messages']), 0)
            self.assertNotIn('合成架构会议记录', served.stderr)


if __name__ == '__main__':
    unittest.main()
