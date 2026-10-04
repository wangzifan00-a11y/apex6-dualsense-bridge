import subprocess
import unittest
from usbip_owned import cleanup_export


class ExportCleanupTests(unittest.TestCase):
    def test_detaches_only_port_whose_current_location_matches_saved_owner(self):
        calls = []
        def run(args, **unused):
            calls.append(args[1:])
            return subprocess.CompletedProcess(args, 0, b"Port 3: usbip://localhost:3241/7-2\n" if args[1] == "port" else b"", b"")
        result = cleanup_export(dict(bus_id=7, device_id="2", usbip_port=3), run=run)
        self.assertTrue(result["detached"])
        self.assertIn(["detach", "--port", "3"], calls)
        self.assertEqual(calls[0], calls[-1])
        self.assertNotIn("--all", str(calls))
        self.assertNotIn("--stop-all", str(calls))

    def test_reassigned_port_is_preserved(self):
        for other in (b"localhost:3241/7-20\n", b"localhost:3241/17-2\n", b"192.168.1.8:3241/7-2\n", b"evil.localhost:3241/7-2\n"):
            calls = []
            def run(args, **unused):
                calls.append(args[1:])
                return subprocess.CompletedProcess(args, 0, b"Port 03: usbip://" + other if args[1] == "port" else b"", b"")
            with self.assertRaisesRegex(RuntimeError, "未移除其他设备"):
                cleanup_export(dict(bus_id=7, device_id="2", usbip_port=3), run=run)
            self.assertFalse(any(call[0] == "detach" for call in calls))

    def test_missing_port_only_cancels_exact_export_retries(self):
        calls = []
        def run(args, **unused):
            calls.append(args[1:])
            return subprocess.CompletedProcess(args, 0, b"", b"")
        result = cleanup_export(dict(bus_id=7, device_id="2", usbip_port=None), run=run)
        self.assertFalse(result["detached"])
        self.assertTrue(all("7-2" in call and "--stop" in call for call in calls))

    def test_unknown_or_global_port_is_rejected(self):
        for port in (0, -1, 256, "1"):
            with self.assertRaises(ValueError):
                cleanup_export(dict(bus_id=7, device_id="2", usbip_port=port), run=lambda *a, **kw: self.fail("driver should not be called"))
