"""Read connection status through both XInput versions, without outputs or raw input logging."""
import argparse
import ctypes
import json
import os
from pathlib import Path

from dsbridge.platform.windows.controller_types import XINPUT_STATE
from dsbridge.platform.windows.xinput import _ComApartment


def main(argv=None, *, quiet=False):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', type=Path)
    args = parser.parse_args(argv)
    system_dir = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32'
    result = {'libraries': {}, 'motor_commands': 0}
    apartment = _ComApartment()
    try:
        for name in ('xinput1_4.dll', 'xinput9_1_0.dll'):
            native = ctypes.WinDLL(str(system_dir / name))
            get_state = native.XInputGetState
            get_state.argtypes = [ctypes.c_uint32, ctypes.POINTER(XINPUT_STATE)]
            get_state.restype = ctypes.c_uint32
            slots = []
            for index in range(4):
                state = XINPUT_STATE()
                code = int(get_state(index, ctypes.byref(state)))
                slots.append({'slot': index, 'connected': code == 0, 'win32_status': code})
            result['libraries'][name] = slots
    finally:
        apartment.close()
    output = json.dumps(result, ensure_ascii=False, indent=2)
    if args.report:
        args.report.write_text(output, encoding='utf-8')
    if not quiet:
        print(output)
    return result


if __name__ == '__main__':
    main()
