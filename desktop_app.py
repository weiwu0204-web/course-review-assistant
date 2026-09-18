"""桌面应用壳（pywebview + 内嵌 FastAPI）——打包版入口。

双击运行（或 `uv run python desktop_app.py`）后弹出原生窗口，
加载项目现有的 Web 界面，内部起一个仅本机可访问的服务。
关闭窗口即退出。

参数：
    --serve-only    只启动后台服务不弹窗（用于无头环境验证/调试）
    --debug         打开 WebView 开发者工具

技术要点：
- FastAPI 跑在后台 daemon 线程，只监听 127.0.0.1，不对外暴露
- pywebview 用 EdgeChromium(WebView2) 渲染，Windows 10/11 自带运行时
- 将来 PyInstaller 打包时，本文件就是入口
"""
import os
import socket
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

import webview
from uvicorn import Config, Server

from app.config import get_settings

settings = get_settings()


class SaveApi:
    """暴露给前端页面的保存能力（js_api 桥）。

    为什么需要：pywebview 的 WebView2 默认禁用下载（settings["ALLOW_DOWNLOADS"]
    为 False），页面里 <a download> 点击会被静默吞掉，用户毫无反馈；即便启用，
    WebView2 控件也没有浏览器那种"已下载"提示。所以桌面壳里点"下载"改走这里：
    把文件写到系统"下载"目录，并把完整路径返回给前端做明确提示。
    刻意不用 create_file_dialog 弹"另存为"框——从 js_api 线程（非 GUI 线程）
    调它会触发已知死锁（pywebview issue #1823）。
    """

    def save_document(self, filename: str, content: str) -> dict:
        try:
            path = _unique_path(_downloads_dir(), filename)
            path.write_text(content, encoding="utf-8")
            return {"ok": True, "path": str(path)}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def _downloads_dir() -> Path:
    """系统"下载"目录；不存在或不可用时逐级退到用户目录、临时目录。"""
    home = Path.home()
    for cand in (home / "Downloads", home / "下载", home):
        if cand.is_dir():
            return cand
    return Path(tempfile.gettempdir())


def _unique_path(directory: Path, filename: str) -> Path:
    """同名时自动追加 (1)、(2)…，避免覆盖已有文件。"""
    candidate = directory / filename
    if not candidate.exists():
        return candidate
    stem, suffix = os.path.splitext(filename)
    for i in range(1, 100):
        candidate = directory / f"{stem} ({i}){suffix}"
        if not candidate.exists():
            return candidate
    return directory / f"{stem}_{int(time.time())}{suffix}"


def _find_free_port(preferred: int, tries: int = 10) -> int:
    """从 preferred 起探测 tries 个端口，返回第一个空闲的；全被占用返回 0（由系统分配）。

    桌面软件不应写死固定端口——用户机器上 8000 很可能被别的程序占着，
    写死会让壳直接启动失败、窗口弹不出来。
    """
    for port in range(preferred, preferred + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            # 注意：不要设 SO_REUSEADDR——Windows 上它会让 bind() 对已被占用的
            # 端口静默"成功"（Unix 语义不同），导致探测误判端口空闲
            try:
                sock.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    return 0


def _run_server(server: Server) -> None:
    server.run()


def _wait_until_up(port: int, tries: int = 40) -> bool:
    for _ in range(tries):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=0.5)
            return True
        except Exception:  # noqa: BLE001
            time.sleep(0.25)
    return False


def main() -> None:
    serve_only = "--serve-only" in sys.argv
    debug = "--debug" in sys.argv

    port = _find_free_port(settings.port)
    if port == 0:
        print(f"[desktop] {settings.port}~{settings.port + 9} 端口均被占用，请释放后重试")
        sys.exit(1)
    if port != settings.port:
        print(f"[desktop] 端口 {settings.port} 被占用，改用 {port}")

    server = Server(
        Config(
            "app.main:app",
            host="127.0.0.1",
            port=port,
            log_level="warning",
        )
    )
    threading.Thread(target=_run_server, args=(server,), daemon=True).start()

    if not _wait_until_up(port):
        print(f"[desktop] 后台服务未能在 {port} 端口启动，退出")
        sys.exit(1)
    print(f"[desktop] 后台服务已就绪：http://127.0.0.1:{port}")

    if serve_only:
        print("[desktop] --serve-only：服务保持运行，Ctrl+C 退出")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
        server.should_exit = True
        return

    try:
        webview.create_window(
            "PPT 复习文档生成器",
            f"http://127.0.0.1:{port}",
            width=1120,
            height=800,
            min_size=(900, 640),
            js_api=SaveApi(),
        )
        webview.start(debug=debug)
        print("[desktop] 窗口已关闭，退出")
    except Exception as exc:  # noqa: BLE001
        # 无头/远程会话里 GUI 可能弹不出来，把真实原因打出来而不是静默退出
        print(f"[desktop] 窗口启动失败：{type(exc).__name__}: {exc}")
        print(f"[desktop] 服务仍在 http://127.0.0.1:{port}，浏览器可直接访问")
        input("按回车退出…")
    finally:
        server.should_exit = True


if __name__ == "__main__":
    main()
