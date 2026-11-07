"""Verify real local screen context through MCP, never print message bodies."""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'src'))
from dingtalkbridge.config import load_config
from dingtalkbridge.store import Store


def verify(collector_session_id: str) -> dict:
    config_file = ROOT/'config.local.json'
    settings = load_config(config_file)
    with Store(settings.database_path, settings) as archive:
        local_status = archive.status()
        if local_status.get('collector', {}).get('collector_session_id') != collector_session_id:
            raise RuntimeError('The requested collection session is not the latest session')
        candidates = archive.get_messages(limit=min(100, settings.max_tool_results))['messages']
        candidates = [record for record in candidates
                      if record['source'] in {'windows_uia_screen', 'windows_msaa_screen', 'windows_accessibility_screen', 'windows_ocr_screen'}
                      and not record['metadata'].get('synthetic') and record['text'].strip()
                      and record['metadata'].get('collector_session_id') == collector_session_id]
    if not candidates:
        return {'success': False, 'reason': 'no_real_screen_context_in_configured_database',
                'message_bodies_printed': False}
    expected = candidates[-1]
    declaration = json.loads((ROOT/'mcp.example.json').read_text(encoding='utf-8'))['mcpServers']['dingtalkbridge']
    requests = [
        {'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-11-25','capabilities':{},'clientInfo':{'name':'live-data-verifier','version':'1'}}},
        {'jsonrpc':'2.0','method':'notifications/initialized'},
        {'jsonrpc':'2.0','id':2,'method':'tools/call','params':{'name':'bridge_status','arguments':{}}},
        {'jsonrpc':'2.0','id':3,'method':'tools/call','params':{'name':'get_messages','arguments':{'conversation_id':expected['conversation_id'],'limit':1}}},
        {'jsonrpc':'2.0','id':4,'method':'tools/call','params':{'name':'get_context','arguments':{'message_id':expected['id'],'before':0,'after':0}}},
    ]
    environment = dict(os.environ, **declaration.get('env', {}))
    environment['DINGTALKBRIDGE_CONFIG'] = str(config_file)
    child = subprocess.run([declaration['command'], *declaration['args']], env=environment,
                           input=''.join(json.dumps(item, ensure_ascii=False)+'\n' for item in requests),
                           capture_output=True, text=True, encoding='utf-8', timeout=20)
    if child.returncode or child.stderr:
        raise RuntimeError('MCP subprocess failed')
    responses = {frame['id']:frame for frame in map(json.loads, child.stdout.splitlines())}
    if set(responses) != {1,2,3,4} or any('error' in item for item in responses.values()):
        raise RuntimeError('MCP protocol failed')
    def result(identifier):
        value = responses[identifier]['result']
        if value.get('isError'):
            raise RuntimeError('MCP tool returned an error')
        return value.get('structuredContent') or json.loads(value['content'][0]['text'])
    status, messages, context = result(2), result(3)['messages'], result(4)
    received = context['message']
    if not received or received['id'] != expected['id'] or received['text'] != expected['text']:
        raise RuntimeError('MCP context mismatch')
    if not messages or messages[-1]['source'] != expected['source']:
        raise RuntimeError('MCP message retrieval mismatch')
    return {
        'success': True, 'verified_at_utc': datetime.now(timezone.utc).isoformat(),
        'protocol_version': responses[1]['result']['protocolVersion'],
        'dataset_kind': status['dataset_kind'], 'record_count': status['record_count'],
        'screen_context_count': status['screen_context_count'],
        'verified_structured_message_count': status['verified_message_count'],
        'collection_state': status['collection_state'],
        'last_observation_status': status.get('collector',{}).get('last_observation_status'),
        'desktop_kind': status.get('collector',{}).get('desktop_kind'),
        'source': received['source'], 'kind': received['metadata'].get('kind'),
        'collector_session_matches_latest': True,
        'evidence_region_counts': status.get('collector', {}).get('evidence_region_counts', {}),
        'timestamp_kind': received['metadata'].get('timestamp_kind'),
        'is_message': received['metadata'].get('is_message'),
        'nonempty_real_context': True, 'get_messages_succeeded': True,
        'get_context_exact_text_round_trip': True, 'message_bodies_printed': False,
        'secrets_printed': False,
    }


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--write-report', type=Path)
    parser.add_argument('--collector-session-id', required=True)
    args = parser.parse_args()
    try:
        report = verify(args.collector_session_id)
    except Exception as error:
        report = {'success': False, 'reason': type(error).__name__, 'message_bodies_printed': False}
    if args.write_report:
        args.write_report.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False,indent=2))
    raise SystemExit(0 if report['success'] else 1)
