"""Own hidden fixture only; never discovers, opens, or attaches to DingTalk.

Build synthetic first. This script rejects production names and requires the exact
compile-time fixture directory. All process output consists only of counts/state.
"""
from __future__ import annotations
import json
import pathlib
import queue
import subprocess
import threading
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parent
BIN = ROOT / "bin" / "0.4.0" / "synthetic"
HOST = BIN / "DingTalkBridgeNativeSyntheticHost.exe"
CONTROLLER = BIN / "BridgeControllerSynthetic.exe"


def line_timeout(pipe, timeout=5):
    result = queue.Queue()
    threading.Thread(target=lambda: result.put(pipe.readline()), daemon=True).start()
    return result.get(timeout=timeout)


class Fixture:
    def __init__(self, milliseconds=5000):
        self.process = subprocess.Popen([str(HOST), "--lifetime-ms", str(milliseconds)], stdin=subprocess.DEVNULL,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        text=True, creationflags=subprocess.CREATE_NO_WINDOW)
        first = json.loads(line_timeout(self.process.stdout))
        if first["pid"] != self.process.pid:
            self.close()
            raise AssertionError("fixture pid mismatch")
        self.hwnd = first["hwnd"]

    def command(self, *extra, duration=1):
        return [str(CONTROLLER), "--pid", str(self.process.pid), "--hwnd", str(self.hwnd),
                "--duration", str(duration), "--session", "synthetic_test", *extra]

    def finish(self):
        output, error = self.process.communicate(timeout=15)
        if self.process.returncode or error:
            raise AssertionError("fixture failed (no application content logged)")
        return json.loads(output.strip().splitlines()[-1])

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            self.process.communicate(timeout=5)


@unittest.skipUnless(HOST.is_file() and CONTROLLER.is_file(), "build synthetic fixture first")
class BootstrapTests(unittest.TestCase):
    def test_normal_stop_and_forward(self):
        fixture = Fixture(3500)
        try:
            run = subprocess.run(fixture.command(), capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL,
                                 creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertEqual(run.returncode, 0, run.stderr)
            records = [json.loads(x) for x in run.stdout.splitlines()]
            self.assertEqual(records[0]["event"], "active")
            self.assertEqual(records[-1]["event"], "stopped")
            self.assertEqual(records[-1]["active"], 0)
            self.assertEqual(records[-1]["worker_alive"], 0)
            self.assertEqual(records[-1]["bootstrap_invocations"], 1)
            result = fixture.finish()
            self.assertTrue(result["saw_active"])
            self.assertTrue(result["saw_stopped"])
            self.assertEqual(result["dispatch_calls"], result["forward_calls"])
        finally:
            fixture.close()

    def test_controller_death_stops_target_worker(self):
        fixture = Fixture(3500)
        controller = None
        try:
            controller = subprocess.Popen(fixture.command(duration=30), stdout=subprocess.PIPE, stdin=subprocess.DEVNULL,
                                          stderr=subprocess.PIPE, text=True,
                                          creationflags=subprocess.CREATE_NO_WINDOW)
            active = json.loads(line_timeout(controller.stdout))
            self.assertEqual(active["active"], 1)
            controller.kill()
            controller.communicate(timeout=5)
            result = fixture.finish()
            self.assertTrue(result["saw_stopped"])
            self.assertEqual(result["status"], 6)
            self.assertEqual(result["worker_alive"], 0)
            self.assertEqual(result["dispatch_calls"], result["forward_calls"])
        finally:
            if controller and controller.poll() is None:
                controller.kill()
                controller.communicate(timeout=5)
            fixture.close()

    def test_target_independently_rejects_wrong_creation_time(self):
        fixture = Fixture(2500)
        try:
            run = subprocess.run(fixture.command("--fixture-bad-target-start"), capture_output=True, stdin=subprocess.DEVNULL,
                                 text=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertEqual(run.returncode, 1)
            result = json.loads(run.stdout.strip().splitlines()[-1])
            self.assertEqual(result["status"], 7)
            self.assertEqual(result["identity_failures"], 1)
            result = fixture.finish()
            self.assertFalse(result["saw_active"])
            self.assertEqual(result["dispatch_calls"], result["forward_calls"])
        finally:
            fixture.close()

    def test_new_session_same_target_after_stop(self):
        fixture = Fixture(4500)
        try:
            for _ in range(2):
                run = subprocess.run(fixture.command(duration=0.5), capture_output=True, stdin=subprocess.DEVNULL,
                                     text=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
                self.assertEqual(run.returncode, 0, run.stderr)
                result = json.loads(run.stdout.strip().splitlines()[-1])
                self.assertEqual(result["bootstrap_invocations"], 1)
                self.assertEqual(result["active"], 0)
            result = fixture.finish()
            self.assertTrue(result["saw_stopped"])
            self.assertEqual(result["dispatch_calls"], result["forward_calls"])
        finally:
            fixture.close()

    def test_controller_rejects_zero_thread_and_invalid_session(self):
        for arguments in (["--pid", "0", "--hwnd", "0"],
                          ["--pid", "1", "--hwnd", "1", "--session", "bad/path"],
                          ["--pid", "1", "--hwnd", "1", "--mode", "capture"]):
            run = subprocess.run([str(CONTROLLER), *arguments], capture_output=True, text=True,
                                 timeout=5, creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertEqual(run.returncode, 1)
            self.assertEqual(json.loads(run.stderr)["error"], "invalid_configuration")


if __name__ == "__main__":
    unittest.main(verbosity=2)
