"""Single bounded MSAA structure-only probe; prints/saves no UI text."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import subprocess
from dingtalkbridge.collector import DEFAULT_PROCESS_PATH, ensure_helper
from dingtalkbridge.config import load_config

parser=argparse.ArgumentParser()
parser.add_argument('--config',required=True)
args=parser.parse_args()
config=load_config(args.config)
root=Path(__file__).resolve().parent
framework=Path(os.environ.get('WINDIR',r'C:\Windows'))/'Microsoft.NET/Framework64/v4.0.30319'
executable=root/'MsaaStructureProbe.exe'
subprocess.run([str(framework/'csc.exe'),'/nologo','/target:exe','/platform:x64',f'/out:{executable}',f'/reference:{framework / "Accessibility.dll"}',f'/reference:{framework / "System.Web.Extensions.dll"}',str(root/'Probe.cs')],check=True,capture_output=True,timeout=30)
native=ensure_helper(config.data_dir)
geometry=subprocess.run([str(native),'--mode','regions','--process-path',DEFAULT_PROCESS_PATH],capture_output=True,text=True,encoding='utf-8-sig',check=True,timeout=20)
regions=json.loads(geometry.stdout)
if regions.get('status')!='available' or regions.get('desktop_kind')!='interactive_default' or len(regions.get('regions',[]))!=1:
    raise SystemExit('No unambiguous verified source region; probe stopped.')
region=regions['regions'][0]
result=subprocess.run([str(executable),region['message_hwnd'],str(region['pid']),DEFAULT_PROCESS_PATH],capture_output=True,text=True,encoding='utf-8-sig',timeout=40)
events=[json.loads(line) for line in result.stdout.splitlines() if line.strip()]
report={'kind':'read_only_msaa_structure_probe','text_properties_read':False,'source_class':region['message_class'],'events':events,'exit_code':result.returncode}
(root/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
print(json.dumps(report,ensure_ascii=False))
raise SystemExit(result.returncode)
