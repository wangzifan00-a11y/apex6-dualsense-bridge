"""Build/pure-test/local-pipe-test the helper; never install or touch PnP."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def main():
    if os.name != "nt":
        raise RuntimeError("The helper requires Windows .NET Framework 4.x")
    source = ROOT / "native" / "hotswitch"
    compiler = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "Microsoft.NET" / "Framework64" / "v4.0.30319" / "csc.exe"
    if not compiler.is_file():
        raise RuntimeError("Windows .NET Framework 4.x x64 compiler is unavailable")
    output = source / "bin"
    output.mkdir(exist_ok=True)
    target = output / "DSBridgeHotSwitch.exe"
    files = sorted(source.glob("*.cs"))
    sources = {str(file.relative_to(ROOT)).replace("\\", "/"): hashlib.sha256(file.read_bytes()).hexdigest() for file in files}
    manifest_path = output / "build-manifest.json"
    reusable = False
    if target.is_file() and manifest_path.is_file():
        try:
            previous = json.loads(manifest_path.read_text(encoding="utf-8"))
            reusable = previous.get("sources") == sources and previous.get("sha256") == hashlib.sha256(target.read_bytes()).hexdigest()
        except (OSError, ValueError):
            pass
    # Framework csc embeds build timestamps. Preserve the exact verified artifact
    # when sources have not changed, rather than creating same-version conflicts.
    if not reusable:
        subprocess.run([str(compiler), "/nologo", "/target:winexe", "/platform:x64", "/optimize+", "/warnaserror+",
                        "/reference:System.ServiceProcess.dll", "/reference:System.Web.Extensions.dll",
                        "/out:" + str(target), *map(str, files)], check=True)
    result = subprocess.run([str(target), "--self-test"], check=True, capture_output=True, text=True,
                            creationflags=subprocess.CREATE_NO_WINDOW, timeout=20)
    report = json.loads(result.stdout)
    if report.get("ok") is not True or report.get("code") != "self_test_passed":
        raise RuntimeError("Helper self-test did not pass")
    smoke_result = subprocess.run([str(target), "--pipe-self-test"], check=True, capture_output=True, text=True,
                                  creationflags=subprocess.CREATE_NO_WINDOW, timeout=40)
    pipe_smoke = json.loads(smoke_result.stdout)
    if pipe_smoke.get("ok") is not True or pipe_smoke.get("code") != "pipe_self_test_passed":
        raise RuntimeError("Helper local pipe self-test did not pass")
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    target.with_suffix(".exe.sha256").write_text(digest + "  " + target.name + "\n", encoding="ascii")
    manifest = {"version": report["version"], "api_version": 1, "sha256": digest, "self_test": report, "pipe_self_test": pipe_smoke,
                "sources": sources}
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"binary": str(target), "sha256": digest, "self_test": report, "pipe_self_test": pipe_smoke}, ensure_ascii=False))


if __name__ == "__main__":
    main()
