"""Public, OS-independent contracts for controller implementations (API v1)."""
from dataclasses import dataclass
from typing import Callable, Mapping, Protocol

API_VERSION = 1


class ControllerDisconnected(RuntimeError):
    """The selected physical input or its receiver control link was lost."""


class BridgeCancelled(RuntimeError):
    """A requested stop interrupted initialization before the bridge was armed."""


class InputPort(Protocol):
    slots: tuple
    def get_state(self, slot) -> Mapping[str, int] | None: ...
    def set_rumble(self, slot, left: float, right: float) -> bool | None: ...
    def close(self) -> None: ...


class BridgeBackend(Protocol):
    error: Exception | None
    def start(self) -> None: ...
    def update(self, state: Mapping[str, int]) -> None: ...
    def poll_feedback(self) -> tuple[float, float] | None: ...
    def stop(self) -> None: ...


@dataclass(frozen=True)
class BridgeProfile:
    key: int | str
    label: str
    detail: str
    adapter: str
    sony: str = "dualsense"
    managed_output: bool = False
    unique_input: bool = False
    dependencies: tuple[str, ...] = ("usbip", "hidhide")
    test_label: str = "测试左右震动"
    start_notice: str = "游戏需选虚拟 Sony 手柄，并关闭该游戏的 Steam Input；双输入时请按说明配置 HidHide。"
    stopped_notice: str = "已停止转换。"


@dataclass(frozen=True)
class ControllerAdapter:
    """Factories are lazy. Importing/registering an adapter never opens hardware."""
    key: str
    name: str
    brand: str
    models: tuple[str, ...]
    connections: tuple[str, ...]
    capabilities: tuple[str, ...]
    input_factory: Callable
    backend_factory: Callable
    motor_test: Callable
    profiles: tuple[BridgeProfile, ...]
    api_version: int = API_VERSION
