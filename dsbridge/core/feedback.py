"""Sony-authored feedback, independent of packet format and physical controller."""
from dataclasses import dataclass


@dataclass(frozen=True)
class TriggerEffect:
    kind: str = "off"
    strengths: tuple = (0,) * 10
    frequency: int = 85
    start: int = 0
    end: int = 0
    strength: int = 0


@dataclass(frozen=True)
class TriggerUpdate:
    lane: int
    effect: TriggerEffect
    mode: int
    raw: bytes
    error: str | None = None


@dataclass(frozen=True)
class SonyFeedback:
    at: float
    # None preserves the previous value; (0, 0) is a game-authored stop.
    rumble: tuple[float, float] | None = None
    triggers: tuple[TriggerUpdate, ...] = ()
    pcm: tuple[tuple[int, ...], tuple[int, ...]] | None = None
    sample_rate: int = 3000
