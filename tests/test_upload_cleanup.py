"""上传目录 TTL 清理逻辑的单元测试。

不调真实 API、不碰项目真实 uploads/：用 monkeypatch 把 UPLOAD_DIR/EXAM_DIR
指向临时目录，再伪造旧文件的 mtime 触发清理（真实 TTL 默认 24h）。
"""
import os
import time
from pathlib import Path

from app.api import routes

OLD_SECONDS = 25 * 3600  # 比默认 TTL（24h）旧 1 小时


def _make_tree(root: Path) -> None:
    """在临时目录里构造：旧课件目录 / 新课件目录 / 旧试题 / 新试题 / 干扰文件。"""
    root.mkdir(parents=True, exist_ok=True)  # pytest tmp_path 已存在

    old_ppt = root / "aaaa1111" / "课件.pptx"
    old_ppt.parent.mkdir(parents=True)
    old_ppt.write_bytes(b"old ppt")

    fresh_ppt = root / "bbbb2222" / "课件.pptx"
    fresh_ppt.parent.mkdir(parents=True)
    fresh_ppt.write_bytes(b"fresh ppt")

    exams = root / "exams"
    exams.mkdir()
    (exams / "cccc3333.txt").write_text("old exam", encoding="utf-8")
    (exams / "dddd4444.txt").write_text("fresh exam", encoding="utf-8")

    # 顶层干扰文件（preview.png 一类），也应被 TTL 清理
    (root / "preview.png").write_bytes(b"png")

    old = time.time() - OLD_SECONDS
    os.utime(old_ppt.parent, (old, old))          # 旧课件目录 → 过期
    os.utime(root / "preview.png", (old, old))    # 旧顶层文件 → 过期
    os.utime(exams / "cccc3333.txt", (old, old))  # 旧试题 → 过期


def test_stale_uploads_removed_fresh_kept(monkeypatch, tmp_path):
    """超期课件目录/试题/顶层文件被清掉；新上传与 exams 目录本身保留。"""
    monkeypatch.setattr(routes, "UPLOAD_DIR", tmp_path)
    monkeypatch.setattr(routes, "EXAM_DIR", tmp_path / "exams")
    _make_tree(tmp_path)

    routes._cleanup_stale_uploads()

    assert not (tmp_path / "aaaa1111").exists(), "超期课件目录应被删除"
    assert (tmp_path / "bbbb2222").exists(), "新课件目录应保留"
    assert not (tmp_path / "preview.png").exists(), "超期顶层文件应被删除"
    assert not (tmp_path / "exams" / "cccc3333.txt").exists(), "超期试题应被删除"
    assert (tmp_path / "exams" / "dddd4444.txt").exists(), "新试题应保留"
    assert (tmp_path / "exams").is_dir(), "exams 目录本身不应被删"


def test_cleanup_noop_on_missing_dirs(monkeypatch, tmp_path):
    """upload 目录尚不存在时调用不应报错（首次运行场景）。"""
    missing = tmp_path / "not_exist_yet"
    monkeypatch.setattr(routes, "UPLOAD_DIR", missing)
    monkeypatch.setattr(routes, "EXAM_DIR", missing / "exams")

    routes._cleanup_stale_uploads()  # 不应抛异常
