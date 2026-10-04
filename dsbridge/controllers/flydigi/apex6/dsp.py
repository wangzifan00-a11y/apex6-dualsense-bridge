"""Stereo 3 kHz -> 1 kHz waveform DSP and DualSense trigger approximation.

Waveforms are from the validated haptic PCM lane only. Adaptive trigger static
resistance is rendered as position-dependent vibration, never claimed as force.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import math


class StereoDecimator:
    """49-tap Blackman FIR at 350 Hz, 8 ms delay, streaming 3:1 decimation."""
    def __init__(self):
        n = 49
        cutoff = 350 / 3000
        taps = []
        for i in range(n):
            x = i - (n - 1) / 2
            sinc = 2 * cutoff if x == 0 else math.sin(2 * math.pi * cutoff * x) / (math.pi * x)
            window = 0.42 - 0.5 * math.cos(2 * math.pi * i / (n - 1)) + 0.08 * math.cos(4 * math.pi * i / (n - 1))
            taps.append(sinc * window)
        total = sum(taps)
        self.taps = tuple(value / total for value in taps)
        self.reset()

    def reset(self):
        self.history = (deque([0.] * 49, maxlen=49), deque([0.] * 49, maxlen=49))
        self.phase = 0

    def feed(self, left, right):
        if len(left) != len(right):
            raise ValueError("Stereo haptic lanes must have the same length")
        output = []
        for pair in zip(left, right):
            for lane, value in enumerate(pair):
                if not isinstance(value, (int, float)) or not math.isfinite(value):
                    raise ValueError("Invalid haptic sample")
                self.history[lane].appendleft(value)
            self.phase = (self.phase + 1) % 3
            if self.phase == 0:
                output.append(tuple(sum(tap * sample for tap, sample in zip(self.taps, channel))
                                    for channel in self.history))
        return output


from dsbridge.core.feedback import TriggerEffect


class TriggerRenderer:
    def __init__(self):
        self.effect = TriggerEffect()
        self.phase = 0.
        self.previous = None
        self.armed = True
        self.pulse_remaining = 0

    def set_effect(self, effect):
        if effect != self.effect:
            self.effect = effect
            self.armed = True
            self.previous = None
            self.pulse_remaining = 0

    def render(self, position, gain=1.0):
        effect = self.effect
        position = max(0, min(255, position))
        strength, frequency = 0., effect.frequency
        if effect.kind in ("texture", "vibration"):
            strength = effect.strengths[min(9, position * 10 // 256)] / 8
        elif effect.kind == "legacy" and position >= effect.start:
            strength = effect.strength / 255
        elif effect.kind == "weapon":
            if position < effect.start:
                self.armed = True
                self.pulse_remaining = 0
            elif self.armed and self.previous is not None and self.previous < effect.end <= position:
                self.armed = False
                self.pulse_remaining = 16
            if self.pulse_remaining:
                strength, frequency = effect.strength / 8, 110
            elif effect.start <= position < effect.end:
                strength = effect.strength / 24
        self.previous = position
        amplitude = min(90., max(0., strength * 70 * gain))
        values = []
        for _ in range(8):
            if effect.kind == "weapon" and self.pulse_remaining:
                self.pulse_remaining -= 1
            self.phase = (self.phase + min(255, frequency) / 1000) % 1
            values.append(round(math.sin(2 * math.pi * self.phase) * amplitude))
        return tuple(values)


class TriggerRouter:
    """The pad has one trigger waveform column: alternate when lanes differ."""
    def __init__(self):
        self.next_lane = 0
    def choose(self, left, right):
        left_active, right_active = any(left), any(right)
        if not left_active and not right_active:
            return 2, (0,) * 8, False  # Clear both previously latched routes.
        if left == right:
            return 2, left, True
        if not left_active:
            return 1, right, True
        if not right_active:
            return 0, left, True
        lane = self.next_lane
        self.next_lane ^= 1
        return lane, (left, right)[lane], True
