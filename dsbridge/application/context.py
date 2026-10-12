"""Composition root: assemble native services without exposing them to the view."""
from dsbridge.runtime.paths import RuntimePaths
from dsbridge.runtime.controller_profiles import ControllerProfiles
from dsbridge.controllers.registry import ControllerRegistry, InputRouter


def require_input_recovered(result):
    """A completed worker does not imply that its recovery completed."""
    if not isinstance(result, dict) or result.get("restored") is not True or result.get("refresh_pending") is not False:
        detail = (result.get("refresh_error") or result.get("restore_error")) if isinstance(result, dict) else None
        raise RuntimeError("手柄输入恢复尚未完成：" + (detail or "设备刷新仍待确认，请查看恢复记录。"))
    if result.get("warnings"):
        raise RuntimeError("设备恢复仍有未完成项：" + "；".join(map(str, result["warnings"])))
    return result


class ApplicationContext:
    def __init__(self, paths, *, registry=None, dependencies=None, diagnostics=None):
        self.paths = paths.prepare()
        self.diagnostics = diagnostics
        self.controller_profiles = ControllerProfiles(self.paths.state)
        self.registry = registry or self.default_registry(paths)
        self.dependencies = dependencies
        if dependencies is None:
            from dsbridge.dependencies.manager import DependencyManager
            self.dependencies = DependencyManager(paths)

    @staticmethod
    def default_registry(paths):
        from dsbridge.controllers.xinput import adapter as xinput
        from dsbridge.controllers.flydigi.apex6.adapter import adapter as apex6
        from dsbridge.controllers.plugins import load_enabled
        registry = ControllerRegistry()
        registry.register(xinput())
        registry.register(apex6())
        load_enabled(registry, paths)
        return registry

    @classmethod
    def default(cls):
        return cls(RuntimePaths.discover())

    def create_input(self, mode):
        port = InputRouter(self.registry, resolve_connection=self.resolve_connection)
        port.select(mode)
        return port

    def create_backend(self, mode, *, input_isolated=False):
        profile = self.registry.profiles[mode]
        self.dependencies.require_keys(profile.dependencies)
        if input_isolated and mode == 5:
            from dsbridge.controllers.flydigi.apex6.bridge import ReceiverFeedback
            return ReceiverFeedback(self.paths, self.paths.config(), isolation_factory=None)
        return self.registry.adapters[profile.adapter].backend_factory(self.paths, self.paths.config(), profile)

    def create_transition(self, input_port, index, mode, events):
        if mode not in (1, 5):
            return None
        from dsbridge.core.connection import ControllerConnection
        describe = getattr(input_port, "connection", None)
        connection = describe(index) if callable(describe) else None
        supported = (isinstance(connection, ControllerConnection)
                     and connection.kind in ("usb", "receiver")
                     and connection.vendor_id == 0x37D7
                     and connection.product_id == 0x2502)
        if not supported:
            if mode == 5:
                raise RuntimeError("四马达桥接未能确认所选八爪鱼接收器或 USB 身份，请重新检测。")
            # The established generic/BT bridge does not require a PnP restart.
            # Only the tested physical USB layout enters the hot-switch path;
            # InputTransition still independently validates its complete tree.
            try:
                events.put(("log", "此连接沿用原有 DS 普通桥接；游戏运行中自动切换尚未验证，请先启动桥接再打开游戏。"))
            except Exception:
                pass
            return None
        from dsbridge.platform.windows.input_transition import InputTransition
        from dsbridge.platform.windows.hot_switch_setup import ensure_hot_switch_service
        self.dependencies.require_keys(self.registry.profiles[mode].dependencies)
        transition = InputTransition(self.paths, input_port, index, mode, events,
                                     ensure_service=ensure_hot_switch_service)
        transition.create_backend = lambda: self.create_backend(mode, input_isolated=True)
        return transition

    def detect_connection(self, mode, slot):
        """A separate input instance keeps native COM ownership on this worker."""
        from dsbridge.core.connection import ControllerConnection
        adapter = self.registry.adapters[self.registry.profiles[mode].adapter]
        source = adapter.input_factory()
        try:
            if source.get_state(slot) is None:
                return ControllerConnection("disconnected", detail="请唤醒或重新连接手柄。")
            describe = getattr(source, "connection", None)
            connection = describe(slot) if describe else ControllerConnection(detail="当前手柄扩展未提供连接方式检测。")
            return self.resolve_connection(connection)
        finally:
            source.close()

    def resolve_connection(self, connection):
        return self.controller_profiles.resolve(connection)

    def set_bluetooth_pro(self, connection, enabled):
        return self.controller_profiles.set_bluetooth_pro(connection, enabled)

    def connection_profile(self, current, connection):
        # Extension profiles retain their own input provider and slot namespace.
        if current not in (1, 5):
            return current
        return connection.profile if connection.profile in self.registry.profiles else 1

    def test_motors(self, mode, input_port, slot, notify):
        profile = self.registry.profiles[mode]
        return self.registry.adapters[profile.adapter].motor_test(self.paths, input_port, slot, notify)

    def repair_audio(self):
        from dsbridge.platform.windows.audio import repair_audio_defaults
        return repair_audio_defaults(self.paths.state)

    def recover(self):
        from dsbridge.platform.windows.hot_switch_lease import recover as recover_hot_switch
        from dsbridge.controllers.flydigi.apex6.recovery import recover
        result = require_input_recovered(recover_hot_switch(self.paths))
        recover(self.paths)
        return result
