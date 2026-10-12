"""Explicit Bluetooth model bindings; raw Windows device IDs are not stored."""
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import re
import threading
import uuid


APEX6PRO_MODEL = "flydigi.apex6pro"


def bluetooth_key(connection):
    if connection.kind != "bluetooth" or not isinstance(connection.device_id, str) or not connection.device_id.strip():
        raise ValueError("请先检测并选择可唯一识别的蓝牙手柄。")
    return hashlib.sha256(connection.device_id.casefold().encode("utf-8")).hexdigest()


class ControllerProfiles:
    def __init__(self, state):
        self.path = Path(state) / "controller-profiles.json"
        self.load_error = None
        self._bluetooth = {}
        self._lock = threading.RLock()
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(value, dict) or type(value.get("version")) is not int or value["version"] != 1:
                raise ValueError("不支持的手柄绑定设置格式")
            rows = value.get("bluetooth")
            if not isinstance(rows, dict) or any(not re.fullmatch(r"[0-9a-f]{64}", key)
                    or model != APEX6PRO_MODEL for key, model in rows.items()):
                raise ValueError("手柄绑定设置内容无效")
            self._bluetooth = dict(rows)
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError, UnicodeError) as exc:
            self.load_error = str(exc)

    def resolve(self, connection):
        # Native model detection is authoritative on every transport. Model-only
        # legacy adapters also count as native; only our explicit user source
        # may be re-resolved or cleared from the saved Bluetooth binding.
        if (connection.kind != "bluetooth" or not connection.device_id
                or (connection.model and connection.model_source != "user")):
            return connection
        with self._lock:
            model = self._bluetooth.get(bluetooth_key(connection))
        if model or connection.model_source == "user":
            return replace(connection, model=model or "", model_source="user" if model else "")
        return connection

    def set_bluetooth_pro(self, connection, enabled):
        key = bluetooth_key(connection)
        if connection.model and connection.model_source != "user":
            raise ValueError("手柄型号已自动识别，不能通过蓝牙绑定覆盖。")
        with self._lock:
            updated = dict(self._bluetooth)
            if enabled:
                updated[key] = APEX6PRO_MODEL
            else:
                updated.pop(key, None)
            if updated != self._bluetooth or self.load_error:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                temporary = self.path.with_name(".controller-profiles-" + uuid.uuid4().hex + ".tmp")
                try:
                    with temporary.open("x", encoding="utf-8") as stream:
                        json.dump({"version": 1, "bluetooth": updated}, stream, ensure_ascii=False)
                        stream.write("\n")
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(temporary, self.path)
                finally:
                    # This is the single temporary file owned by this save.
                    try:
                        temporary.unlink(missing_ok=True)
                    except OSError:
                        pass
                # Do not activate a new binding when its durable write failed.
                self._bluetooth = updated
                self.load_error = None
            return replace(connection, model=APEX6PRO_MODEL if enabled else "",
                           model_source="user" if enabled else "")
