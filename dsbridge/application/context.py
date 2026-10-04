"""Composition root: assemble native services without exposing them to the view."""
from dsbridge.runtime.paths import RuntimePaths
from dsbridge.controllers.registry import ControllerRegistry, InputRouter


class ApplicationContext:
    def __init__(self, paths, *, registry=None, dependencies=None, diagnostics=None):
        self.paths = paths.prepare()
        self.diagnostics = diagnostics
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
        port = InputRouter(self.registry)
        port.select(mode)
        return port

    def create_backend(self, mode):
        profile = self.registry.profiles[mode]
        self.dependencies.require_keys(profile.dependencies)
        return self.registry.adapters[profile.adapter].backend_factory(self.paths, self.paths.config(), profile)

    def detect_connection(self, mode, slot):
        """A separate input instance keeps native COM ownership on this worker."""
        from dsbridge.core.connection import ControllerConnection
        adapter = self.registry.adapters[self.registry.profiles[mode].adapter]
        source = adapter.input_factory()
        try:
            if source.get_state(slot) is None:
                return ControllerConnection("disconnected", detail="请唤醒或重新连接手柄。")
            describe = getattr(source, "connection", None)
            return describe(slot) if describe else ControllerConnection(detail="当前手柄扩展未提供连接方式检测。")
        finally:
            source.close()

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
        from dsbridge.controllers.flydigi.apex6.recovery import recover
        return recover(self.paths)
