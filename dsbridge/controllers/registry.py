"""Explicit adapter registration. No probing or output during discovery."""
from dsbridge.core.ports import API_VERSION, ControllerAdapter


class ControllerRegistry:
    def __init__(self):
        self.adapters = {}
        self.profiles = {}

    def register(self, adapter: ControllerAdapter):
        if adapter.api_version != API_VERSION:
            raise ValueError("手柄扩展接口版本不兼容")
        if adapter.key in self.adapters or not adapter.profiles:
            raise ValueError("适配器重复或没有桥接模式")
        keys = [profile.key for profile in adapter.profiles]
        labels = [profile.label for profile in adapter.profiles]
        if len(set(keys)) != len(keys) or any(key in self.profiles or key in (0, 2, 3, 4) for key in keys):
            raise ValueError("桥接模式 ID 冲突或使用了已停用的模式 ID")
        if len(set(labels)) != len(labels) or set(labels) & {p.label for p in self.profiles.values()}:
            raise ValueError("桥接模式名称重复")
        for profile in adapter.profiles:
            if profile.adapter != adapter.key or profile.sony != "dualsense":
                raise ValueError("适配器与 Sony 桥接配置不匹配")
        self.adapters[adapter.key] = adapter
        self.profiles.update((p.key, p) for p in adapter.profiles)

    def describe(self):
        return [{"id": a.key, "name": a.name, "brand": a.brand, "models": a.models,
                 "connections": a.connections, "capabilities": a.capabilities,
                 "profiles": [p.key for p in a.profiles], "api_version": a.api_version}
                for a in self.adapters.values()]


class InputRouter:
    """The view selects a profile; only its adapter owns the input API."""
    def __init__(self, registry, *, resolve_connection=None):
        self.registry = registry
        self.resolve_connection = resolve_connection or (lambda connection: connection)
        self._key = None
        self._input = None
        self._prepared_input = None

    def select(self, mode):
        key = self.registry.profiles[mode].adapter
        if self._key != key:
            self.close()
            self._input = self.registry.adapters[key].input_factory()
            self._key = key

    @property
    def slots(self):
        return getattr(self._input, "slots", (0, 1, 2, 3))

    def label(self, slot):
        label = getattr(self._input, "label", None)
        return label(slot) if label else "XInput " + str(slot + 1) + "（Windows 槽位 " + str(slot) + "）"

    def get_state(self, slot):
        return self._input.get_state(slot)

    def connection(self, slot):
        from dsbridge.core.connection import ControllerConnection
        describe = getattr(self._input, "connection", None)
        return self.resolve_connection(describe(slot)) if describe else ControllerConnection()

    def rebind(self, expected):
        """Resolve the captured device after a planned refresh, never another pad."""
        rebind = getattr(self._input, "rebind", None)
        if rebind is None:
            raise RuntimeError("当前手柄扩展不支持输入热切换")
        self._prepared_input = None
        return rebind(expected)

    def prepare_input(self, slot):
        """Resolve the selected physical model once, before virtual creation.

        Extensions keep their own input semantics; a built-in device's mapping
        is chosen anew at every start and does not alter physical/UI state.
        """
        from dsbridge.controllers.flydigi.apex6.input_mapping import mapping_for, unchanged
        self._prepared_input = None
        if self._key not in ("xinput", "flydigi.apex6.receiver"):
            return unchanged
        describe = getattr(self._input, "connection", None)
        if describe is None:
            return unchanged
        connection = self.resolve_connection(describe(slot))
        transform = mapping_for(connection)
        if connection.device_id:
            self._prepared_input = (slot, connection)
        return transform

    def validate_input(self, slot):
        """Recheck a mapped physical identity once, after virtual startup.

        This is not another model query: an existing receiver session may own
        that interface. Providers without this optional method stay compatible.
        """
        if self._prepared_input is None:
            return True
        prepared_slot, connection = self._prepared_input
        if slot != prepared_slot:
            return False
        validate = getattr(self._input, "validate_connection", None)
        return validate(slot, connection) if validate else True

    def set_rumble(self, slot, left, right):
        return self._input.set_rumble(slot, left, right)

    def close(self):
        self._prepared_input = None
        if self._input:
            self._input.close()
