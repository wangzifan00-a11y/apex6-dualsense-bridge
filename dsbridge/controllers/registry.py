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
    def __init__(self, registry):
        self.registry = registry
        self._key = None
        self._input = None

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

    def set_rumble(self, slot, left, right):
        return self._input.set_rumble(slot, left, right)

    def close(self):
        if self._input:
            self._input.close()
