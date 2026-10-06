import subprocess
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from dsbridge.virtual.dualsense.runtime import LocalViiper
from dsbridge.virtual.dualsense.transport import ViiperBackend


class Child:
    def __init__(self, terminate_error=False, stubborn=False):
        self.alive = True
        self.terminate_error = terminate_error
        self.stubborn = stubborn
        self.calls = []

    def poll(self):
        return None if self.alive else 0

    def terminate(self):
        self.calls.append("terminate")
        if self.terminate_error:
            raise OSError("terminate refused")
        self.alive = False

    def kill(self):
        self.calls.append("kill")
        if self.stubborn:
            raise OSError("kill refused")
        self.alive = False

    def wait(self, timeout):
        self.calls.append("wait")
        if self.alive:
            raise subprocess.TimeoutExpired("owned child", timeout)
        return 0


def runtime():
    value = LocalViiper.__new__(LocalViiper)
    value.backend = Mock(last_cleanup_error=None)
    value.process = Child()
    value.child_job = Mock()
    value.log_file = Mock()
    value.audio_guard = Mock(error=None)
    value.last_cleanup_error = None
    value.base = Path("unused-state")
    return value


class RuntimeShutdownTests(unittest.TestCase):
    def test_owned_process_exits_and_all_other_resources_release(self):
        bridge = runtime()
        process, job, log, audio = bridge.process, bridge.child_job, bridge.log_file, bridge.audio_guard
        bridge.stop()
        self.assertFalse(process.alive)
        self.assertEqual(process.calls, ["terminate", "wait"])
        job.close.assert_called_once()
        log.close.assert_called_once()
        audio.stop.assert_called_once()
        self.assertIsNone(bridge.process)
        self.assertIsNone(bridge.child_job)
        self.assertIsNone(bridge.audio_guard)

    def test_failed_terminate_uses_owned_child_kill(self):
        bridge = runtime()
        process = bridge.process = Child(terminate_error=True)
        bridge.stop()
        self.assertEqual(process.calls, ["terminate", "kill", "wait"])
        self.assertIsNone(bridge.process)

    def test_device_removal_error_does_not_skip_process_or_audio_cleanup(self):
        bridge = runtime()
        process, audio = bridge.process, bridge.audio_guard
        bridge.backend.stop.side_effect = OSError("USB unavailable")
        with self.assertRaisesRegex(RuntimeError, "USB unavailable"):
            bridge.stop()
        self.assertFalse(process.alive)
        audio.stop.assert_called_once()
        self.assertIsNone(bridge.audio_guard)

    def test_job_close_error_does_not_skip_log_or_audio(self):
        bridge = runtime()
        job, log, audio = bridge.child_job, bridge.log_file, bridge.audio_guard
        job.close.side_effect = OSError("job close failed")
        with self.assertRaisesRegex(RuntimeError, "job close failed"):
            bridge.stop()
        log.close.assert_called_once()
        audio.stop.assert_called_once()
        self.assertIs(bridge.child_job, job)  # Retain ownership for a later retry.

    def test_failure_to_reap_is_reported_and_keeps_the_owned_handle(self):
        bridge = runtime()
        process = bridge.process = Child(terminate_error=True, stubborn=True)
        audio = bridge.audio_guard
        with self.assertRaisesRegex(RuntimeError, "退出确认"):
            bridge.stop()
        self.assertIs(bridge.process, process)
        audio.stop.assert_called_once()

    def test_external_server_has_no_process_to_terminate(self):
        bridge = runtime()
        bridge.process = bridge.child_job = bridge.log_file = None
        with patch("subprocess.Popen") as popen:
            bridge.stop()
        popen.assert_not_called()
        self.assertIsNone(bridge.process)

    def test_cancel_after_audio_protection_restores_it_without_creating_device(self):
        bridge = runtime()
        bridge.process = bridge.child_job = bridge.log_file = None
        guard = Mock(error=None)
        checker = Mock(side_effect=[None, RuntimeError("pad disconnected")])
        with patch("dsbridge.platform.windows.audio.DefaultAudioGuard", return_value=guard):
            with self.assertRaisesRegex(RuntimeError, "pad disconnected"):
                bridge.start_cancellable(checker)
        guard.start.assert_called_once()
        guard.stop.assert_called_once()
        bridge.backend.start.assert_not_called()
        bridge.backend.start_cancellable.assert_not_called()

    def test_cancel_after_bus_creation_removes_only_the_owned_bus(self):
        bridge = ViiperBackend()
        commands = []
        def management(command):
            commands.append(command)
            return {"server": "VIIPER", "version": "test"} if command == "ping" else {"busId": 23}
        checker = Mock(side_effect=[None, None, RuntimeError("pad disconnected")])
        with patch.object(bridge, "_management", side_effect=management):
            with self.assertRaisesRegex(RuntimeError, "pad disconnected"):
                bridge.start_cancellable(checker)
        self.assertEqual(commands, ["ping", "bus/create", "bus/remove 23"])
        self.assertIsNone(bridge.bus_id)


if __name__ == "__main__":
    unittest.main()
