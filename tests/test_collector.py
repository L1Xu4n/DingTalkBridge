from __future__ import annotations

import copy
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from dingtalkbridge.collector import CollectorError, _helper_lines, _launch_helper, run_collect, screen_context_records


def snapshot():
    return {
        "type": "snapshot", "status": "available", "observed_at": "2026-11-06T17:00:00Z",
        "collector_session_id": "test-session", "scope_generation": 1,
        "evidence": [{"pid": 123, "window_id": "abc", "texts": [
            {"text": "Visible synthetic evidence", "control_type": "Text"},
            {"text": "Another synthetic label", "control_type": "ListItem"}]}],
    }


class CollectorRecordTests(unittest.TestCase):
    def test_screen_context_does_not_claim_message_or_identity(self):
        event = snapshot()
        event["desktop_kind"] = "interactive_default"
        records = list(screen_context_records(event))
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record["source"], "windows_uia_screen")
        self.assertEqual(record["sender_id"], "unknown")
        self.assertEqual(record["confidence"], 0)
        self.assertEqual(record["metadata"]["kind"], "screen_context")
        self.assertFalse(record["metadata"]["is_message"])
        self.assertEqual(record["metadata"]["account_identity"], "unknown")
        self.assertEqual(record["metadata"]["desktop_kind"], "interactive_default")
        self.assertEqual(record["metadata"]["content_classification"], "unverified_visible_text")
        self.assertEqual(record["metadata"]["timestamp_kind"], "observation_time")
        self.assertIn("test-session:1", record["conversation_id"])

    def test_deduplication_and_scope_isolation(self):
        first = snapshot()
        repeated = copy.deepcopy(first)
        repeated["observed_at"] = "2026-11-06T17:00:05Z"
        self.assertEqual(next(screen_context_records(first))["id"], next(screen_context_records(repeated))["id"])
        repeated["scope_generation"] += 1
        self.assertNotEqual(next(screen_context_records(first))["id"], next(screen_context_records(repeated))["id"])
        repeated["collector_session_id"] = "another-run"
        self.assertNotEqual(next(screen_context_records(first))["conversation_id"], next(screen_context_records(repeated))["conversation_id"])

    def test_missing_context_fails_closed(self):
        for field in ("collector_session_id", "observed_at"):
            event = snapshot()
            del event[field]
            self.assertEqual(list(screen_context_records(event)), [])
        event = snapshot()
        event["status"] = "no_visible_window"
        self.assertEqual(list(screen_context_records(event)), [])

    def test_empty_or_malformed_evidence_is_not_ingested(self):
        event = snapshot()
        event["evidence"] = [None, {"pid": True, "window_id": "abc", "texts": [{"text": "bad pid"}]}, {"pid": 1, "window_id": "x", "texts": [{"text": " "}, {"text": 3}]}]
        self.assertEqual(list(screen_context_records(event)), [])

    def test_large_unicode_context_fits_store_limit(self):
        event = snapshot()
        event["evidence"][0]["texts"] = [{"text": "中" * 100000}]
        record = next(screen_context_records(event))
        self.assertLessEqual(len(record["text"].encode("utf-8")), 128 * 1024)
        self.assertTrue(record["metadata"]["truncated"])

    def test_invalid_arguments_fail_before_helper_launch(self):
        with tempfile.TemporaryDirectory() as directory:
            config = SimpleNamespace(data_dir=Path(directory))
            with patch("dingtalkbridge.collector.ensure_helper") as build:
                for args in ({"mode": "other"}, {"interval": 0}, {"duration": float("nan")}, {"pid": True}):
                    with self.assertRaises(CollectorError):
                        run_collect(config, None, args)
                build.assert_not_called()


class FakeProcess:
    def __init__(self, events):
        self.stdout = io.StringIO("".join(json.dumps(event) + "\n" for event in events))
        self.returncode = None

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.returncode = 0
        return 0

    def terminate(self):
        self.returncode = -15


class CollectorSupervisorTests(unittest.TestCase):
    def test_isolated_desktop_is_not_reported_as_missing_user_window(self):
        event = snapshot()
        event.update(status="isolated_desktop", desktop_kind="isolated_sandbox", windows=[], evidence=[])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("dingtalkbridge.collector.ensure_helper", return_value=root / "helper.exe"), patch("dingtalkbridge.collector._launch_helper", return_value=FakeProcess([event])):
                result = run_collect(SimpleNamespace(data_dir=root), None, {"mode": "snapshot"})
            self.assertEqual(result["status"], "isolated_desktop")
            self.assertEqual(result["desktop_kind"], "isolated_sandbox")
            self.assertIn("does not mean DingTalk has no user window", result["limitation"])
            self.assertEqual(result["ingested_screen_contexts"], 0)

    def test_default_persists_only_bounded_text_free_summary(self):
        event = snapshot()
        event["process_ids"] = [123]
        event["windows"] = [{"pid": 123, "window_id": "abc", "safe_text_count": 2}]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = SimpleNamespace(data_dir=root, database_path=root / "real.sqlite3")
            process = FakeProcess([event, {"type": "status", "status": "stopped"}])
            store = SimpleNamespace(path=config.database_path, ingest=lambda record: self.fail("default must not ingest context"))
            with patch("dingtalkbridge.collector.ensure_helper", return_value=root / "helper.exe"), patch("dingtalkbridge.collector._launch_helper", return_value=process):
                result = run_collect(config, store, {"mode": "snapshot"})
            evidence = Path(result["evidence_file"]).read_text(encoding="utf-8")
            status = (root / "collector-status.json").read_text(encoding="utf-8")
            self.assertNotIn("Visible synthetic evidence", evidence + status)
            self.assertNotIn("texts", json.dumps(json.loads(evidence)))
            self.assertLessEqual(len(evidence.encode("utf-8")), 65536)
            self.assertFalse(result["running"])
            self.assertIn("stopped_at", result)
            self.assertIn("heartbeat_at", result)
            self.assertEqual(result["database_path_hash"], hashlib.sha256(str(config.database_path.resolve()).casefold().encode("utf-8")).hexdigest())

    def test_opt_in_context_still_uses_store_policy_and_scope_changes(self):
        event = snapshot()
        event["process_ids"] = [123]
        event["windows"] = [{"pid": 123, "window_id": "abc"}]
        replacement = copy.deepcopy(event)
        replacement["windows"][0]["window_id"] = "def"
        replacement["evidence"][0]["window_id"] = "def"
        records = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SimpleNamespace(path=root / "real.sqlite3", ingest=lambda record: records.append(record) or False)
            with patch("dingtalkbridge.collector.ensure_helper", return_value=root / "helper.exe"), patch("dingtalkbridge.collector._launch_helper", return_value=FakeProcess([event, replacement])):
                result = run_collect(SimpleNamespace(data_dir=root), store, {"mode": "watch", "duration": 2, "include_screen_context": True})
            self.assertEqual(result["ingested_screen_contexts"], 0)
            self.assertEqual(result["skipped_screen_contexts"], 2)
            self.assertEqual(result["scope_generation"], 1)
            self.assertNotEqual(records[0]["conversation_id"], records[1]["conversation_id"])

    def test_provider_timeout_does_not_block_supervisor(self):
        process = FakeProcess([])
        with patch("dingtalkbridge.collector.time.monotonic", side_effect=[0, 21]):
            with self.assertRaisesRegex(CollectorError, "timed out"):
                list(_helper_lines(process, "probe", 0, 5))

    def test_windows_launch_denial_is_a_controlled_error(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch("dingtalkbridge.collector.subprocess.Popen", side_effect=PermissionError("sensitive text must not be echoed")):
                with self.assertRaises(CollectorError) as captured:
                    _launch_helper(["synthetic-helper"], Path(directory) / "stop")
            self.assertNotIn("sensitive text", str(captured.exception))

    def test_late_launch_is_stopped_after_startup_deadline(self):
        release = threading.Event()
        stopped = threading.Event()
        process = FakeProcess([])

        def delayed_launch(*args, **kwargs):
            release.wait(2)
            return process

        with tempfile.TemporaryDirectory() as directory:
            with patch("dingtalkbridge.collector.subprocess.Popen", side_effect=delayed_launch), patch("dingtalkbridge.collector._stop_process", side_effect=lambda *args: stopped.set() or True):
                with self.assertRaisesRegex(CollectorError, "startup timed out"):
                    _launch_helper(["synthetic-helper"], Path(directory) / "stop", startup_timeout=0.01)
                release.set()
                self.assertTrue(stopped.wait(2))


@unittest.skipUnless(os.name == "nt", "Windows .NET policy reflection without UI access")
class NativePolicyTests(unittest.TestCase):
    def test_source_geometry_policy_masks_inputs_and_rejects_unknown_owner(self):
        # Load the compiled assembly as data; invoke only the production pure
        # predicate. Main, UIA, window enumeration and all P/Invokes are unused.
        from dingtalkbridge.collector import ensure_helper
        with tempfile.TemporaryDirectory() as directory:
            helper = ensure_helper(Path(directory))
            script = r'''
param([string]$AssemblyPath)
$ErrorActionPreference='Stop'
$assembly=[Reflection.Assembly]::LoadFile($AssemblyPath)
$method=$assembly.GetType('DingTalkUia').GetMethod('GeometryInputDecision',[Reflection.BindingFlags]'NonPublic,Static')
$cases=@(
    @('writable_value_pattern',$false,$true,$true,$true),
    @('unknown_value_pattern',$false,$true,$true,$true),
    @('document_control',$false,$true,$true,$true),
    @('writable_value_pattern',$false,$false,$false,$true),
    @('writable_value_pattern',$false,$true,$false,$true),
    @('edit_control',$true,$true,$false,$true),
    @('password',$true,$true,$false,$true),
    @('input_marker',$true,$true,$false,$true),
    @('writable_value_pattern',$false,$true,$false,$false),
    @($null,$false,$true,$true,$true)
)
$results=@($cases | ForEach-Object { $method.Invoke($null,[object[]]$_) })
$renderer=$assembly.GetType('DingTalkUia').GetMethod('IsNarrowRendererOwner',[Reflection.BindingFlags]'NonPublic,Static')
$rendererCases=@(
    @($true,$true,'Intermediate D3D Window',$true,$true,$true),
    @($true,$false,'Intermediate D3D Window',$true,$true,$true),
    @($true,$true,'Other Window',$true,$true,$true),
    @($true,$true,'Intermediate D3D Window',$false,$true,$true),
    @($true,$true,'Intermediate D3D Window',$true,$false,$true),
    @($true,$true,'Intermediate D3D Window',$true,$true,$false),
    @($false,$true,'Intermediate D3D Window',$true,$true,$true)
)
$rendererResults=@($rendererCases | ForEach-Object { $renderer.Invoke($null,[object[]]$_) })
$depth=$assembly.GetType('DingTalkUia').GetMethod('GeometryPolicyDepth',[Reflection.BindingFlags]'NonPublic,Static')
$depthCases=@(@(0,$false),@(0,$true),@(1,$true),@(9,$true))
$depthResults=@($depthCases | ForEach-Object { $depth.Invoke($null,[object[]]$_) })
$rectType=$assembly.GetType('DingTalkUia+NativeRect')
$listType=([System.Collections.Generic.List[object]]).GetGenericTypeDefinition().MakeGenericType([type[]]@($rectType))
function Make-Rect([int[]]$v) {
    $r=[Activator]::CreateInstance($rectType)
    $rectType.GetField('Left').SetValue($r,$v[0]); $rectType.GetField('Top').SetValue($r,$v[1])
    $rectType.GetField('Right').SetValue($r,($v[0]+$v[2])); $rectType.GetField('Bottom').SetValue($r,($v[1]+$v[3]))
    return $r
}
$union=$assembly.GetType('DingTalkUia').GetMethod('MaskUnionArea',[Reflection.BindingFlags]'NonPublic,Static')
$roi=Make-Rect @(0,0,100,100)
$unionCases=@(
    @{rects=@()},
    @{rects=,@(0,0,100,100)},
    @{rects=@(@(0,0,60,100),@(40,0,60,100))},
    @{rects=,@(-50,-50,100,100)},
    @{rects=@(@(0,0,20,20),@(80,80,20,20))},
    @{rects=,@(200,200,20,20)}
)
$unionResults=@($unionCases | ForEach-Object {
    $maskList=[Activator]::CreateInstance($listType)
    foreach($v in $_.rects) { $maskList.Add((Make-Rect $v)) }
    $union.Invoke($null,[object[]]@($maskList,$roi))
})
ConvertTo-Json -Compress -InputObject @{decisions=$results;renderer=$rendererResults;depth=$depthResults;union=$unionResults}
'''
            path = Path(directory) / "policy.ps1"
            path.write_text(script, encoding="utf-8")
            completed = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-File", str(path), str(helper)], capture_output=True, text=True, encoding="utf-8-sig", timeout=20, check=True, creationflags=subprocess.CREATE_NO_WINDOW)
            actual = json.loads(completed.stdout)
            self.assertEqual(actual["decisions"], ["mask", "mask", "mask", "error", "outside_source_tree", "mask", "mask", "mask", "mask", "traverse"])
            self.assertEqual(actual["renderer"], [True] + [False] * 6)
            self.assertEqual(actual["depth"], [0, 1, 1, 9])
            self.assertEqual(actual["union"], [0, 10000, 10000, 2500, 800, 0])


@unittest.skipUnless(os.name == "nt" and os.environ.get("DINGTALKBRIDGE_UIA_INTEGRATION") == "1", "opt-in Windows synthetic UIA integration")
class NativeCollectorTests(unittest.TestCase):
    def test_fixture_subscription_filtering_and_stop(self):
        from dingtalkbridge.collector import ensure_helper
        project = Path(__file__).resolve().parents[1]
        framework = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Microsoft.NET/Framework64/v4.0.30319"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = root / "DingTalk.exe"
            subprocess.run([str(framework / "csc.exe"), "/nologo", "/target:winexe", f"/out:{fixture}", "/reference:System.Windows.Forms.dll", "/reference:System.Drawing.dll", str(project / "native/fixtures/DingTalkFixture.cs")], check=True, capture_output=True)
            helper = ensure_helper(root)
            process = subprocess.Popen([str(fixture)], creationflags=subprocess.CREATE_NO_WINDOW)
            try:
                # Native helper polls once/sec, so startup does not require a sleep.
                observed = subprocess.run([str(helper), "--mode", "watch", "--duration", "5", "--interval", "1", "--process-path", str(fixture), "--pid", str(process.pid)], capture_output=True, text=True, encoding="utf-8-sig", timeout=15, check=True)
                events = [json.loads(line) for line in observed.stdout.splitlines() if line.strip()]
                captures = [event for event in events if event.get("type") == "snapshot"]
                self.assertTrue(any(event["event_subscriptions"] >= 1 for event in captures))
                self.assertTrue(any(event["reason"] == "uia_event" for event in captures))
                self.assertTrue(any(event["evidence"] for event in captures))
                self.assertNotIn("fixture-secret", observed.stdout)
                self.assertEqual(events[-1]["status"], "stopped")
                self.assertTrue(all(event.get("message_count", 0) == 0 for event in events))
            finally:
                process.terminate()
                process.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
