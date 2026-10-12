"""Pure boot-proof tests; no event-log, registry, or device API calls."""
import copy
import unittest
from unittest.mock import MagicMock, patch

from dsbridge.platform.windows import boot_identity as boot


def identity(first=100, day="12"):
    return dict(format=1, source="windows-system-startup", machine="a" * 64,
                started=dict(record_id=first, computer="test-pc",
                             time=f"2026-10-{day}T01:00:01Z", start_time=f"2026-10-{day}T01:00:00Z"),
                full_boot=dict(record_id=first + 5, computer="test-pc",
                               time=f"2026-10-{day}T01:00:01Z", boot_type=0))


class BootIdentityTests(unittest.TestCase):
    def setUp(self):
        self.old, self.current = identity(), identity(200, "13")
        self.capture = patch.object(boot, "capture_boot_identity", return_value=self.current).start()
        self.events = patch.object(boot, "_events", side_effect=self.read_event).start()
        self.addCleanup(patch.stopall)

    def read_event(self, provider, event_id, **kwargs):
        return [self.old["started" if event_id == 12 else "full_boot"]]

    def test_later_boot_needs_matching_retained_original_events(self):
        proof = boot.prove_later_boot(self.old)
        self.assertEqual(proof["boot_before"], self.old)
        self.assertEqual(proof["boot_after"], self.current)
        self.assertEqual(self.events.call_count, 2)

    def test_same_kernel_session_does_not_prove_reboot(self):
        self.capture.return_value = self.old
        self.assertIsNone(boot.prove_later_boot(self.old))
        self.events.assert_not_called()

    def test_machine_copy_missing_log_or_reused_log_id_never_proves_reboot(self):
        self.current["machine"] = "b" * 64
        self.assertIsNone(boot.prove_later_boot(self.old))
        self.current["machine"] = "a" * 64
        self.events.side_effect = lambda *args, **kwargs: []
        self.assertIsNone(boot.prove_later_boot(self.old))
        self.events.side_effect = lambda *args, **kwargs: [{**self.old["started"], "time": "2020-01-01T00:00:00Z"}]
        self.assertIsNone(boot.prove_later_boot(self.old))

    def test_clock_change_without_new_record_or_backwards_time_is_not_proof(self):
        self.current["started"]["record_id"] = self.old["started"]["record_id"]
        self.assertIsNone(boot.prove_later_boot(self.old))
        self.current = identity(200, "11")
        self.capture.return_value = self.current
        self.assertIsNone(boot.prove_later_boot(self.old))

    def test_legacy_malformed_and_unavailable_evidence_fail_closed(self):
        for saved in (None, {}, {**self.old, "format": True}, {**self.old, "format": 2}, {**self.old, "machine": "invalid"}):
            self.assertIsNone(boot.prove_later_boot(saved))
        for value in (True, "100", None):
            saved = copy.deepcopy(self.old)
            saved["started"]["record_id"] = value
            self.assertIsNone(boot.prove_later_boot(saved))
        self.capture.side_effect = OSError("access denied")
        self.assertIsNone(boot.prove_later_boot(self.old))


class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.value = identity()
        registry = MagicMock(REG_SZ=1, HKEY_LOCAL_MACHINE=2, KEY_READ=4, KEY_WOW64_64KEY=8)
        registry.QueryValueEx.return_value = ("machine-test", 1)
        patch.dict("sys.modules", {"winreg": registry}).start()
        self.events = patch.object(boot, "_events", side_effect=lambda provider, event_id, **kwargs:
                                   [self.value["started" if event_id == 12 else "full_boot"]]).start()
        self.addCleanup(patch.stopall)

    def test_capture_pairs_full_startup_events(self):
        result = boot.capture_boot_identity()
        self.assertEqual(result["started"], self.value["started"])
        self.assertEqual(len(result["machine"]), 64)
        self.assertNotIn("machine-test", result.values())

    def test_hybrid_resume_unrelated_computer_and_far_event_fail_closed(self):
        for changes in ({"boot_type": 1}, {"computer": "other"}, {"time": "2026-10-12T01:05:01Z"}):
            with self.subTest(changes=changes):
                self.value = identity()
                self.value["full_boot"].update(changes)
                with self.assertRaises(ValueError):
                    boot.capture_boot_identity()

    def test_parser_binds_provider_guid_channel_and_start_time(self):
        xml = """<Event xmlns='http://schemas.microsoft.com/win/2004/08/events/event'>
        <System><Provider Name='Microsoft-Windows-Kernel-General' Guid='{a68ca8b7-004f-d7b6-a698-07e2de0f1f5d}'/>
        <EventID>12</EventID><TimeCreated SystemTime='2026-10-12T01:00:01Z'/><EventRecordID>100</EventRecordID>
        <Channel>System</Channel><Computer>TEST-PC</Computer></System>
        <EventData><Data Name='StartTime'>2026-10-12T01:00:00Z</Data></EventData></Event>"""
        self.assertEqual(boot._parse_event(xml, boot._GENERAL, 12), self.value["started"])
        for changed in (xml.replace("<Channel>System", "<Channel>Application"),
                        xml.replace(boot._GENERAL[1], "{00000000-0000-0000-0000-000000000000}"),
                        "<!DOCTYPE event>" + xml):
            with self.assertRaises(ValueError):
                boot._parse_event(changed, boot._GENERAL, 12)


if __name__ == "__main__":
    unittest.main()
