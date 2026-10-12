"""Transport routing must not make a new service mandatory for legacy BT."""
import queue
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from dsbridge.application.context import ApplicationContext
from dsbridge.core.connection import ControllerConnection
from dsbridge.core.modes import PROFILES


class ContextTransitionTests(unittest.TestCase):
    def setUp(self):
        self.context = ApplicationContext.__new__(ApplicationContext)
        self.context.paths = Mock()
        self.context.registry = SimpleNamespace(profiles=PROFILES)
        self.context.dependencies = Mock()
        self.events = queue.Queue()

    def port(self, kind, vendor=0x37D7, product=0x2502):
        return Mock(connection=Mock(return_value=ControllerConnection(
            kind, device_id="selected-exact-id", vendor_id=vendor, product_id=product)))

    def test_apex_usb_and_receiver_keep_hot_switch_for_both_builtin_modes(self):
        for kind in ("usb", "receiver"):
            for mode in (1, 5):
                with self.subTest(kind=kind, mode=mode), patch(
                        "dsbridge.platform.windows.input_transition.InputTransition") as transition, patch(
                        "dsbridge.platform.windows.hot_switch_setup.ensure_hot_switch_service") as ensure:
                    port = self.port(kind)
                    result = self.context.create_transition(port, 2, mode, self.events)
                    self.assertIs(result, transition.return_value)
                    port.connection.assert_called_once_with(2)
                    transition.assert_called_once_with(self.context.paths, port, 2, mode,
                                                       self.events, ensure_service=ensure)
                    ensure.assert_not_called()
                    with patch.object(self.context, "create_backend") as backend:
                        result.create_backend()
                        backend.assert_called_once_with(mode, input_isolated=True)

    def test_bluetooth_stays_legacy_even_when_model_is_known_apex(self):
        with patch("dsbridge.platform.windows.input_transition.InputTransition") as transition:
            result = self.context.create_transition(self.port("bluetooth"), 0, 1, self.events)
        self.assertIsNone(result)
        transition.assert_not_called()
        self.context.dependencies.require_keys.assert_not_called()
        event = self.events.get_nowait()
        self.assertEqual(event[0], "log")
        self.assertIn("普通桥接", event[1])
        self.assertIn("尚未验证", event[1])
        self.assertTrue(self.events.empty())

    def test_generic_usb_or_receiver_does_not_require_restart_service(self):
        for kind, vendor, product in (("usb", 0x045E, 0x02FD),
                                      ("receiver", 0x045E, 0x02FD),
                                      ("usb", 0x37D7, 0x9999),
                                      ("usb", 0x9999, 0x2502),
                                      ("unknown", 0x37D7, 0x2502)):
            with self.subTest(kind=kind, vendor=vendor, product=product), patch(
                    "dsbridge.platform.windows.input_transition.InputTransition") as transition:
                result = self.context.create_transition(self.port(kind, vendor, product),
                                                        0, 1, self.events)
                self.assertIsNone(result)
                transition.assert_not_called()
        self.context.dependencies.require_keys.assert_not_called()

    def test_generic_provider_without_connection_metadata_stays_legacy(self):
        result = self.context.create_transition(SimpleNamespace(), 0, 1, self.events)
        self.assertIsNone(result)
        self.context.dependencies.require_keys.assert_not_called()

    def test_four_motor_mode_does_not_silently_fall_back_on_wrong_identity(self):
        for port in (self.port("bluetooth"), self.port("unknown"),
                     self.port("usb", product=0x9999), SimpleNamespace()):
            with self.subTest(port=port), patch(
                    "dsbridge.platform.windows.input_transition.InputTransition") as transition:
                with self.assertRaisesRegex(RuntimeError, "四马达桥接未能确认"):
                    self.context.create_transition(port, 0, 5, self.events)
                transition.assert_not_called()
        self.assertTrue(self.events.empty())
        self.context.dependencies.require_keys.assert_not_called()

    def test_extension_mode_keeps_its_own_lifecycle_without_probing(self):
        port = Mock()
        self.assertIsNone(self.context.create_transition(port, 0, 7, self.events))
        port.connection.assert_not_called()

    def test_generic_backend_factory_remains_the_existing_local_bridge(self):
        from dsbridge.controllers.xinput import backend_factory
        self.context.registry.adapters = {"xinput": SimpleNamespace(backend_factory=backend_factory)}
        with patch("dsbridge.virtual.dualsense.runtime.LocalViiper") as local:
            backend = self.context.create_backend(1)
            self.assertIs(backend, local.return_value)
            local.assert_called_once_with(self.context.paths, self.context.paths.config.return_value)

    def test_failed_log_sink_does_not_block_legacy_bridge(self):
        events = Mock()
        events.put.side_effect = RuntimeError("closed UI")
        self.assertIsNone(self.context.create_transition(self.port("bluetooth"), 0, 1, events))


if __name__ == "__main__":
    unittest.main()
