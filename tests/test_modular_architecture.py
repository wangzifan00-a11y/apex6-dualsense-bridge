"""Boundary and extension tests: use a different-brand fake, never real motors."""
import ast
from dataclasses import replace
import json
from pathlib import Path
import queue
import subprocess
import sys
import time
import unittest
from unittest.mock import Mock, patch
import uuid

from dsbridge.application.context import ApplicationContext
from dsbridge.controllers.registry import ControllerRegistry
from dsbridge.core.engine import Engine
from dsbridge.core.ports import BridgeProfile, ControllerAdapter
from dsbridge.core.managed_bridge import ManagedFeedbackBridge
from dsbridge.runtime.paths import RuntimePaths

ROOT = Path(__file__).resolve().parents[1]
STATE = dict(buttons=0, left_trigger=0, right_trigger=0, lx=0, ly=0, rx=0, ry=0)


def folder():
    result = ROOT / "测试记录" / "模块化" / uuid.uuid4().hex
    result.mkdir(parents=True)
    return result


class DifferentBrandInput:
    slots = ("pad-serial",)
    def __init__(self):
        self.writes = []
    def get_state(self, slot):
        return dict(STATE) if slot == self.slots[0] else None
    def label(self, slot):
        return "ExampleBrand " + slot
    def set_rumble(self, slot, *value):
        self.writes.append((slot, value))
    def close(self):
        pass


class VirtualSony:
    error = None
    def start(self):
        self.started = True
    def update(self, state):
        self.last_input = state
    def poll_feedback(self):
        return (.4, .2)
    def stop(self):
        self.started = False


class ModularTests(unittest.TestCase):
    def make_adapter(self):
        source, sony = DifferentBrandInput(), VirtualSony()
        profile = BridgeProfile("example.ps5", "测试品牌原生桥接", "仅供自动测试", "example.brand", dependencies=())
        adapter = ControllerAdapter("example.brand", "Example", "ExampleBrand", ("Model X",), ("usb",),
                                    ("input", "rumble.stereo"), lambda: source,
                                    lambda paths, config, profile: sony, lambda *args: None, (profile,))
        return adapter, source, sony

    def test_new_brand_uses_same_engine_without_integer_mode_or_xinput_slot(self):
        adapter, source, sony = self.make_adapter()
        registry = ControllerRegistry()
        registry.register(adapter)
        path = folder()
        dependencies = Mock()
        context = ApplicationContext(RuntimePaths(ROOT, path), registry=registry, dependencies=dependencies)
        port = context.create_input("example.ps5")
        self.assertEqual(port.slots, ("pad-serial",))
        self.assertIn("ExampleBrand", port.label(port.slots[0]))
        events = queue.Queue()
        engine = Engine(port, events, context.create_backend, profiles=registry.profiles)
        engine.start("pad-serial", "example.ps5")
        deadline = time.monotonic() + 2
        while not source.writes and time.monotonic() < deadline:
            time.sleep(.005)
        engine.stop()
        engine.thread.join(2)
        self.assertFalse(engine.running)
        self.assertEqual(source.writes[0][0], "pad-serial")
        self.assertAlmostEqual(source.writes[0][1][0], .28)
        self.assertAlmostEqual(source.writes[0][1][1], .14)
        self.assertEqual(source.writes[-1], ("pad-serial", (0, 0)))
        self.assertFalse(sony.started)
        dependencies.require_keys.assert_called_once_with(())

    def test_duplicate_reserved_and_wrong_version_adapter_rejected_atomically(self):
        adapter, *_ = self.make_adapter()
        for candidate in (
            replace(adapter, api_version=999),
            replace(adapter, profiles=(replace(adapter.profiles[0], key=4),)),
            replace(adapter, profiles=(replace(adapter.profiles[0], key=2),)),
            replace(adapter, profiles=(replace(adapter.profiles[0], sony="dualshock4"),)),
            replace(adapter, profiles=(replace(adapter.profiles[0], sony="xbox"),)),
        ):
            registry = ControllerRegistry()
            with self.assertRaises(ValueError):
                registry.register(candidate)
            self.assertEqual(registry.adapters, {})
        registry.register(adapter)
        with self.assertRaises(ValueError):
            registry.register(adapter)
        self.assertEqual(len(registry.profiles), 1)

    def test_assets_and_state_are_independent_and_helpers_receive_state(self):
        path = folder()
        assets, state = path / "程序 中文", path / "用户记录"
        assets.mkdir()
        paths = RuntimePaths(assets, state).prepare()
        (assets / "viiper-config.json").write_text('{"port":3242}', encoding="utf-8")
        self.assertEqual(paths.config()["port"], 3242)
        (state / "viiper-config.json").write_text('{"port":3243}', encoding="utf-8")
        self.assertEqual(paths.config()["port"], 3243)
        command = paths.command("--receiver-input-guard", "a" * 32, 123)
        self.assertIn(str(assets / "app.py"), command)
        self.assertEqual(command[command.index("--data-dir") + 1], str(state))
        helper = paths.command("--check-input-visibility", executable=assets / "接收器输入检查.exe")
        self.assertEqual(helper[0], str(assets / "接收器输入检查.exe"))
        self.assertNotIn(str(state / "app.py"), command)

    def test_default_assets_do_not_depend_on_working_directory(self):
        self.assertEqual(RuntimePaths.discover().assets, ROOT)
        self.assertNotEqual(RuntimePaths.discover().state, ROOT)

    def test_core_has_no_native_brand_ui_or_transport_imports(self):
        for path in (ROOT / "dsbridge/core").glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("dsbridge."):
                    self.assertTrue(node.module.startswith("dsbridge.core."), f"{path.name}: {node.module}")
        result = subprocess.run([sys.executable, "-c", "import dsbridge.core.engine,sys; print([m for m in sys.modules if m.startswith(('dsbridge.platform','tkinter','dsbridge.controllers'))])"], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "[]")

    def test_ordinary_gui_does_not_enumerate_sound_capture_devices(self):
        for name in ("window.py", "presentation.py", "dependencies.py"):
            source = (ROOT / "dsbridge/ui" / name).read_text(encoding="utf-8")
            self.assertNotIn("all_speakers", source)
            self.assertNotIn("all_microphones", source)
            self.assertNotIn("audio_haptics", source.replace('("audio_haptics", "xbox_ds_backend", "probe_xbox_ds")', "()"))

    def test_managed_bridge_restores_input_even_after_virtual_cleanup_failure(self):
        order = []
        output = Mock(error=None, last_cleanup_error=None)
        output.stop.side_effect = lambda: order.append("motors")
        source = Mock(error=None, last_cleanup_error=None)
        def fail():
            order.append("virtual")
            raise RuntimeError("USB removal failed")
        source.stop.side_effect = fail
        lease = Mock(error=None)
        lease.stop.side_effect = lambda: order.append("input")
        bridge = ManagedFeedbackBridge(output, lambda sink: source, decoder=lambda *args: None, isolation=lease)
        with self.assertRaisesRegex(RuntimeError, "USB removal"):
            bridge.stop()
        self.assertEqual(order, ["motors", "virtual", "input"])

    def test_disabled_plugin_is_not_imported_and_enabled_plugin_registers(self):
        from dsbridge.controllers.plugins import load_enabled
        path = folder()
        paths = RuntimePaths(ROOT, path)
        package = "dsbridge_ext_test_" + uuid.uuid4().hex
        directory = path / "plugins" / package
        directory.mkdir(parents=True)
        (directory / "__init__.py").write_text("def register(registry):\n    registry.seen = True\n", encoding="utf-8")
        registry = ControllerRegistry()
        load_enabled(registry, paths)
        self.assertNotIn(package, sys.modules)
        (path / "adapters.json").write_text(json.dumps([package]), encoding="utf-8")
        load_enabled(registry, paths)
        self.assertTrue(registry.seen)

    def test_plugin_path_traversal_is_rejected(self):
        from dsbridge.controllers.plugins import load_enabled
        path = folder()
        (path / "adapters.json").write_text('["../outside"]', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "扩展包名称"):
            load_enabled(ControllerRegistry(), RuntimePaths(ROOT, path))
