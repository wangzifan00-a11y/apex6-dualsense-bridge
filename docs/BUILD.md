# 从源码运行与构建

已验证环境：Windows x64、CPython 3.14.5（含 tkinter）。依赖版本固定在 `requirements-build.txt`。

1. 下载并完整解压本仓库对应版本的 Windows Release，将其中的 `vendor`、`installers`、`native` 目录及 `release-manifest.json` 复制到源码根目录（保留源码中的 `.cs` 文件）。前两项提供第三方运行资源；`native` 与清单提供匹配该 Release 的自动切换组件及完整性校验。从源码运行时可直接使用它们，不会自动执行驱动安装包。
2. 在源码根目录打开 PowerShell：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-build.txt
.\.venv\Scripts\python.exe app.py
```

构建新版本还需要 Windows 自带的 .NET Framework 4.x x64 编译器：`%SystemRoot%\Microsoft.NET\Framework64\v4.0.30319\csc.exe`。构建流程会编译原生组件，执行纯逻辑测试和隔离的本地管道测试；这些测试不安装服务、不操作手柄。

构建命令：

```powershell
.\.venv\Scripts\python.exe build.py
```

输出位于 `发布/<时间戳>/`，包含完整目录、ZIP 和 SHA-256 校验文件。

单独检查原生组件可运行 `python tools/build_hotswitch_helper.py`，输出在 `native/hotswitch/bin/`。编译器会嵌入构建时间，因此重新编译的二进制不一定与 Release 的 SHA-256 相同。系统服务拒绝用不同内容覆盖已安装的同版本组件；日常从源码运行请使用匹配 Release 的组件。开发修改原生组件时，应同步更新组件及客户端要求的版本，不要强行覆盖已安装文件。接口与恢复机制见 [原生组件说明](../native/hotswitch/README.md)。

不发送真实马达反馈的自动测试：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -q
.\.venv\Scripts\python.exe -m unittest test_viiper_backend test_sony_writer -q
```

测试产生的 `测试记录`、缓存及本机运行状态均已加入 `.gitignore`。实际运行桥接仍需安装 USBip 和 HidHide。
