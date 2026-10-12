"""Read-only check for repeated info replies across detection/session handles."""
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dsbridge.controllers.flydigi.apex6.connection import query_connection
from dsbridge.controllers.flydigi.apex6.hid import ReceiverHID, receiver_interfaces, USAGE_PAGE
from dsbridge.controllers.flydigi.apex6.protocol import ReceiverProtocol
from dsbridge.controllers.flydigi.apex6.session import ReceiverClaim


def main():
    result = {"at": datetime.now(timezone.utc).isoformat(), "motor_commands": 0,
              "configuration_commands": 0, "connection": query_connection(None), "queries": []}
    claim = ReceiverClaim()
    try:
        candidates = [r for r in receiver_interfaces() if r.get("usage_page") == USAGE_PAGE
                      and r.get("input_length") == r.get("output_length") == 33]
        if len(candidates) != 1:
            raise RuntimeError("需要唯一接收器专用接口")
        with ReceiverHID(candidates[0]["path"]) as transport:
            protocol = ReceiverProtocol(transport)
            for label, command, length in (("after_detection", 1, 25),
                                            ("different_reply", 4, 16),
                                            ("info_after_uid", 1, 25),
                                            ("identical_info", 1, 25),
                                            ("different_reply_again", 4, 16),
                                            ("info_after_uid_again", 1, 25)):
                start = time.monotonic()
                row = {"label": label, "command": command}
                try:
                    data = protocol.ask(command, length)
                    row.update(ok=True, reply_length=len(data))
                except RuntimeError as exc:
                    row.update(ok=False, error=str(exc))
                row["elapsed_ms"] = round((time.monotonic() - start) * 1000)
                result["queries"].append(row)
                print(json.dumps(row, ensure_ascii=False), flush=True)
    finally:
        claim.close()
        folder = ROOT / "测试记录/接收器重复应答"
        folder.mkdir(parents=True, exist_ok=True)
        output = folder / (datetime.now().strftime("%Y%m%d-%H%M%S") + ".json")
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"connection": result["connection"], "report": str(output)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
