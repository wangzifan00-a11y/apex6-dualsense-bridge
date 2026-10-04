"""Read only the APEX 6 architecture/info replies, without changing its mode.

Field interpretation follows SDL_hidapi_flydigi.c InitControllerV2: architecture
query 0x07 distinguishes new 0=USB/1=receiver from old 1=USB/2=receiver.
"""
import time


def connection_kind(new_architecture, raw):
    return ({0: "usb", 1: "receiver"} if new_architecture else {1: "usb", 2: "receiver"}).get(raw, "unknown")


def query_connection(selected):
    from dsbridge.controllers.flydigi.apex6.hid import ReceiverHID, receiver_interfaces, USAGE_PAGE
    from dsbridge.controllers.flydigi.apex6.protocol import ReceiverProtocol, command_report
    from dsbridge.controllers.flydigi.apex6.session import ReceiverClaim
    claim = None
    try:
        claim = ReceiverClaim()
        candidates = [row for row in receiver_interfaces() if row.get("usage_page") == USAGE_PAGE
                      and row.get("input_length") == row.get("output_length") == 33]
        # The existing private protocol supports one receiver; never query an
        # arbitrary second device when the user's selection is ambiguous.
        if len(candidates) != 1:
            return {"kind": "unknown", "reason": "vendor interface is not unique"}
        with ReceiverHID(candidates[0]["path"]) as transport:
            transport.flush_queue()
            transport.write(command_report(7))
            deadline = time.monotonic() + .8
            architecture = None
            while time.monotonic() < deadline:
                packet = transport.read(max(1, round((deadline - time.monotonic()) * 1000)))
                if packet and len(packet) == 33 and packet[:4] == b"\0\x5a\xa5\x07":
                    architecture = packet[4:6] == b"\1\0"
                    break
            if architecture is None:
                return {"kind": "unknown", "reason": "architecture query timed out"}
            data = ReceiverProtocol(transport).ask(1, 25)
            if data[0] not in (0x95, 0x96, 0x98):
                return {"kind": "unknown", "reason": "unverified controller model"}
            return {"kind": connection_kind(architecture, data[1]), "new_architecture": architecture,
                    "raw_connection": data[1], "model": data[0]}
    except (OSError, RuntimeError) as exc:
        return {"kind": "unknown", "reason": str(exc)}
    finally:
        if claim:
            claim.close()
