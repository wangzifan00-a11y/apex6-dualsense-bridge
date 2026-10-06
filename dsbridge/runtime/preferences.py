"""Small user preferences, stored atomically in the writable state directory."""
import json
import math
import os
from pathlib import Path
import uuid


DEFAULT_GAIN_PERCENT = 70
MIN_GAIN_PERCENT = 0
MAX_GAIN_PERCENT = 150


def gain_percent(value):
    """Use the same whole percentage for the label, output and saved setting."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("转换强度必须是数字")
    if not MIN_GAIN_PERCENT <= value <= MAX_GAIN_PERCENT or not math.isfinite(value):
        raise ValueError("转换强度必须介于 0% 和 150%")
    return round(value)


class GainPreferences:
    def __init__(self, state):
        self.path = Path(state) / "ui-settings.json"
        self.percent = DEFAULT_GAIN_PERCENT
        self.load_error = None
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(value, dict) or type(value.get("version")) is not int or value["version"] != 1:
                raise ValueError("不支持的界面设置格式")
            self.percent = gain_percent(value["gain_percent"])
        except FileNotFoundError:
            pass
        except (OSError, ValueError, KeyError, TypeError, UnicodeError) as exc:
            self.load_error = str(exc)
        self._saved_percent = self.percent if self.load_error is None else None

    def save(self, value):
        percent = gain_percent(value)
        if percent == self._saved_percent:
            return False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(".ui-settings-" + uuid.uuid4().hex + ".tmp")
        try:
            with temporary.open("x", encoding="utf-8") as stream:
                json.dump({"version": 1, "gain_percent": percent}, stream, ensure_ascii=False)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            # Remove only the exact temporary file owned by this save attempt.
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        self.percent = self._saved_percent = percent
        return True
