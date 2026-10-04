import copy
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import patch
import uuid

from windows_audio import (AudioDefaultError, DefaultAudioGuard, FLOWS, ROLES,
                           choose_fallback, is_sony_audio, repair_defaults)


def device(identity, name, flow):
    return {"id": identity, "name": name, "flow": flow, "instance_id": ""}


SPEAKER = device("real-speaker", "扬声器 (Realtek Audio)", "render")
MIC = device("real-mic", "麦克风阵列 (Realtek Audio)", "capture")
DS_SPEAKER = device("sony-speaker", "扬声器 (DualSense Wireless Controller)", "render")
DS_MIC = device("sony-mic", "耳机式麦克风 (DualSense Wireless Controller)", "capture")
USB = device("usb-speaker", "USB Headphones", "render")
VIRTUAL_MIC = device("remote-mic", "麦克风 (ToDesk Virtual Audio)", "capture")


class AudioState:
    def __init__(self):
        self.lock = threading.Lock()
        self.endpoints = [SPEAKER, MIC, DS_SPEAKER, DS_MIC, USB, VIRTUAL_MIC]
        self.defaults = {f"{flow}:{role}": SPEAKER if flow == "render" else MIC
                         for flow in FLOWS for role in ROLES}
        self.writes = []
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.closed = True

    def snapshot(self):
        with self.lock:
            return copy.deepcopy({"endpoints": self.endpoints, "defaults": self.defaults})

    def set_default(self, identity, flow, role):
        with self.lock:
            endpoint = next(item for item in self.endpoints if item["id"] == identity)
            self.defaults[f"{flow}:{role}"] = endpoint
            self.writes.append((flow, role, identity))

    def steal(self):
        with self.lock:
            self.defaults = {f"{flow}:{role}": DS_SPEAKER if flow == "render" else DS_MIC
                             for flow in FLOWS for role in ROLES}


class AudioRepairTests(unittest.TestCase):
    def test_restores_both_directions_and_all_three_roles(self):
        audio = AudioState()
        audio.steal()
        report = repair_defaults(audio)
        self.assertEqual(len(report["changes"]), 6)
        self.assertEqual({row["id"] for row in report["after"]["defaults"].values()},
                         {SPEAKER["id"], MIC["id"]})
        self.assertEqual(report["after"]["endpoints"], report["before"]["endpoints"])

    def test_does_not_override_normal_user_defaults(self):
        audio = AudioState()
        audio.defaults["render:2"] = USB
        audio.defaults["capture:2"] = VIRTUAL_MIC
        report = repair_defaults(audio)
        self.assertEqual(report["changes"], [])
        self.assertEqual(audio.writes, [])
        self.assertEqual(report["after"]["defaults"]["render:2"], USB)

    def test_saved_usb_choice_wins_over_internal_speaker(self):
        audio = AudioState()
        audio.steal()
        remembered = {f"render:{role}": USB for role in ROLES}
        repair_defaults(audio, remembered)
        self.assertTrue(all(audio.defaults[f"render:{role}"] == USB for role in ROLES))

    def test_disconnected_saved_device_is_never_restored(self):
        audio = AudioState()
        audio.steal()
        audio.endpoints.remove(USB)
        repair_defaults(audio, {"render:0": USB})
        self.assertEqual(audio.defaults["render:0"], SPEAKER)

    def test_wrong_flow_or_sony_saved_target_is_rejected(self):
        audio = AudioState()
        audio.steal()
        repair_defaults(audio, {"render:0": MIC, "render:1": DS_SPEAKER})
        self.assertEqual(audio.defaults["render:0"], SPEAKER)
        self.assertEqual(audio.defaults["render:1"], SPEAKER)

    def test_no_real_microphone_does_not_choose_virtual_capture(self):
        with self.assertRaises(AudioDefaultError):
            choose_fallback([DS_MIC, VIRTUAL_MIC], "capture")

    def test_ambiguous_speakers_require_explicit_choice(self):
        second = device("second", "扬声器 (Realtek USB Audio)", "render")
        with self.assertRaises(AudioDefaultError):
            choose_fallback([SPEAKER, second, DS_SPEAKER], "render")

    def test_sony_matching_does_not_catch_generic_usb_audio(self):
        self.assertTrue(is_sony_audio(DS_SPEAKER))
        self.assertTrue(is_sony_audio(DS_MIC))
        self.assertFalse(is_sony_audio(USB))


class AudioGuardTests(unittest.TestCase):
    def setUp(self):
        # Keep artifacts: cleanup must not recursively delete directories.
        self.base = Path(__file__).resolve().parent.parent / "测试记录" / ("音频-" + uuid.uuid4().hex)
        self.base.mkdir(parents=True)
        self.audio = AudioState()
        self.guard = DefaultAudioGuard(self.base, audio_factory=lambda: self.audio, interval=0.02)
        self.addCleanup(self.guard.stop)

    def wait_for(self, predicate):
        deadline = time.monotonic() + 2
        while not predicate():
            if self.guard.error:
                raise self.guard.error
            self.assertLess(time.monotonic(), deadline, "Audio protection did not react")
            time.sleep(0.005)

    def test_protects_device_arrival_after_start(self):
        self.guard.start()
        self.audio.steal()
        self.wait_for(lambda: self.guard.status()["restored_roles"] == 6)
        self.assertTrue(all(not is_sony_audio(row) for row in self.audio.snapshot()["defaults"].values()))

    def test_manual_switch_is_remembered_for_next_arrival(self):
        self.guard.start()
        self.audio.set_default(USB["id"], "render", 0)
        self.wait_for(lambda: self.guard.status()["defaults"].get("render:0") == USB["name"])
        self.audio.steal()
        self.wait_for(lambda: self.guard.status()["restored_roles"] == 6)
        self.assertEqual(self.audio.snapshot()["defaults"]["render:0"], USB)

    def test_stop_does_one_final_restore_and_releases_com_owner(self):
        self.guard.start()
        self.audio.steal()
        self.guard.stop()
        self.assertTrue(self.audio.closed)
        self.assertFalse(self.guard.status()["active"])
        self.assertTrue(all(not is_sony_audio(row) for row in self.audio.snapshot()["defaults"].values()))

    def test_restarts_reuse_original_audio_preferences(self):
        self.audio.set_default(USB["id"], "render", 0)
        self.guard.start()
        self.guard.stop()
        self.audio.steal()
        self.guard.start()
        self.assertEqual(self.audio.snapshot()["defaults"]["render:0"], USB)

    def test_failure_is_reported_before_virtual_device_creation(self):
        self.audio.endpoints = [DS_SPEAKER, DS_MIC]
        self.audio.steal()
        with self.assertRaises(AudioDefaultError):
            self.guard.start()
        self.assertIsInstance(self.guard.error, AudioDefaultError)
        self.assertTrue(self.audio.closed)


class RuntimeAudioGuardTests(unittest.TestCase):
    def test_guard_starts_before_backend_and_stops_after_device_removal(self):
        self.run_lifecycle(False)

    def test_backend_start_failure_still_releases_audio_guard(self):
        self.run_lifecycle(True)

    def run_lifecycle(self, fail):
        from viiper_runtime import LocalViiper
        base = Path(__file__).resolve().parent.parent / "测试记录" / ("音频生命周期-" + uuid.uuid4().hex)
        base.mkdir(parents=True)
        calls = []
        class Guard:
            error = None
            def __init__(self, _base): pass
            def start(self): calls.append("protect")
            def stop(self): calls.append("unprotect")
        class Backend:
            error = None
            def start(self):
                calls.append("create")
                if fail: raise RuntimeError("attachment failure")
            def stop(self): calls.append("remove")
        runtime = LocalViiper(base, {})
        runtime.backend = Backend()
        with patch("windows_audio.DefaultAudioGuard", Guard), patch.object(runtime, "_listening", return_value=True):
            if fail:
                with self.assertRaises(RuntimeError): runtime.start()
            else:
                runtime.start()
                runtime.stop()
        self.assertEqual(calls, ["protect", "create", "remove", "unprotect"])


if __name__ == "__main__":
    unittest.main()
