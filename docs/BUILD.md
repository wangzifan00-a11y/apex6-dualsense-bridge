# 从源码运行与构建

已验证环境：Windows x64、CPython 3.14.5（含 tkinter）。依赖版本固定在 `requirements-build.txt`。

1. 下载并完整解压本仓库的 2.1.0 Windows Release，将其中的 `vendor` 和 `installers` 两个目录复制到源码根目录。它们是已校验的第三方运行资源，构建不会自动执行驱动安装包。
2. 在源码根目录打开 PowerShell：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-build.txt
.\.venv\Scripts\python.exe app.py
```

构建新版本：

```powershell
.\.venv\Scripts\python.exe build.py
```

输出位于 `发布/<时间戳>/`，包含完整目录、ZIP 和 SHA-256 校验文件。

不发送真实马达反馈的自动测试：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -q
.\.venv\Scripts\python.exe -m unittest test_viiper_backend test_sony_writer -q
```

测试产生的 `测试记录`、缓存及本机运行状态均已加入 `.gitignore`。实际运行桥接仍需安装 USBip 和 HidHide。
