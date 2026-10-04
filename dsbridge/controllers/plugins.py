"""Load only user-enabled local adapter packages, never scan/import every file."""
import importlib
import json
from pathlib import Path
import re
import sys


def load_enabled(registry, paths):
    config = paths.state / "adapters.json"
    if not config.is_file():
        return
    entries = json.loads(config.read_text(encoding="utf-8"))
    if not isinstance(entries, list):
        raise ValueError("adapters.json 必须是扩展模块名称列表")
    root = (paths.state / "plugins").resolve()
    for module in entries:
        if not isinstance(module, str) or not re.fullmatch(r"dsbridge_ext_[a-z][a-z0-9_]*", module):
            raise ValueError("扩展包名称必须以 dsbridge_ext_ 开头")
        package = (root / module / "__init__.py").resolve()
        if not package.is_relative_to(root) or not package.is_file():
            raise ValueError("未找到本地手柄扩展包：" + module)
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        extension = importlib.import_module(module)
        if Path(extension.__file__).resolve() != package:
            raise ValueError("手柄扩展名称被其他模块占用：" + module)
        # Each registration validates API, capabilities and unique profile IDs.
        extension.register(registry)
