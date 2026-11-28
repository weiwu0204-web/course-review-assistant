"""仓库文档健康检查门禁：验证现行文档的状态标签（frontmatter）与相对链接有效性。

用法：
    python -m scripts.check_docs
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS_DIR = ROOT / "docs"
VALID_STATUS = {"current", "draft", "archived"}
HEX_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
INLINE_CODE_RE = re.compile(r"`[^`\n]*`")
LINK_RE = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
SKIP_SCHEMES = ("http://", "https://", "mailto:", "tel:", "data:", "javascript:")


def is_excluded_doc(path: Path) -> bool:
    rel = path.relative_to(ROOT).as_posix()
    return rel.startswith("docs/archive/") or rel.startswith("docs/templates/")


def parse_frontmatter(text: str) -> dict[str, str] | None:
    cleaned = text.lstrip("\ufeff")
    if not cleaned.startswith("---\n") and not cleaned.startswith("---\r\n"):
        return None
    lines = cleaned.splitlines()
    end_idx = None
    for idx in range(1, min(len(lines), 20)):
        if lines[idx].strip() == "---":
            end_idx = idx
            break
    if end_idx is None:
        return None
    meta: dict[str, str] = {}
    for line in lines[1:end_idx]:
        if ":" in line:
            key, val = line.split(":", 1)
            meta[key.strip()] = val.strip()
    return meta


def check_frontmatter(md_file: Path) -> list[str]:
    rel = md_file.relative_to(ROOT).as_posix()
    meta = parse_frontmatter(md_file.read_text(encoding="utf-8"))
    if meta is None:
        return [f"{rel}: 缺少顶部 YAML 状态头（status / verified）"]
    errors: list[str] = []
    status = meta.get("status", "")
    verified = meta.get("verified", "")
    if status not in VALID_STATUS:
        errors.append(f"{rel}: 非法 status='{status}'，允许值 {sorted(VALID_STATUS)}")
    if not HEX_SHA_RE.match(verified):
        errors.append(f"{rel}: 非法 verified='{verified}'，需为 7-40 位提交哈希")
    return errors


def iter_markdown_links(text: str):
    """逐行产出非代码块内的 Markdown 链接目标与行号。"""
    in_fence = False
    fence_marker = ""
    for lineno, raw_line in enumerate(text.splitlines(), 1):
        m = FENCE_RE.match(raw_line)
        if m:
            token = m.group(1)
            if not in_fence:
                in_fence = True
                fence_marker = token[0] * len(token)
            elif raw_line.lstrip().startswith(fence_marker):
                in_fence = False
                fence_marker = ""
            continue
        if in_fence:
            continue
        scrubbed = INLINE_CODE_RE.sub("", raw_line)
        for match in LINK_RE.finditer(scrubbed):
            yield lineno, match.group(1).strip()


def check_links(md_file: Path, repo_root: Path = ROOT) -> list[str]:
    rel = md_file.relative_to(repo_root).as_posix()
    text = md_file.read_text(encoding="utf-8")
    root_resolved = repo_root.resolve()
    errors: list[str] = []
    for lineno, raw_target in iter_markdown_links(text):
        target = raw_target.strip("<>").split()[0] if raw_target else ""
        if not target or target.startswith("#") or target.startswith(SKIP_SCHEMES):
            continue
        path_part = target.split("#", 1)[0]
        if not path_part:
            continue
        candidate = (md_file.parent / path_part).resolve()
        try:
            candidate.relative_to(root_resolved)
        except ValueError:
            errors.append(f"{rel}:{lineno}: 链接越出仓库根目录 -> {raw_target}")
            continue
        if not candidate.exists():
            errors.append(f"{rel}:{lineno}: 链接目标不存在 -> {raw_target}")
    return errors


def collect_errors(repo_root: Path = ROOT) -> list[str]:
    errors: list[str] = []
    docs_dir = repo_root / "docs"
    if docs_dir.exists():
        for md_file in sorted(docs_dir.rglob("*.md")):
            rel = md_file.relative_to(repo_root).as_posix()
            if rel.startswith("docs/archive/") or rel.startswith("docs/templates/"):
                continue
            errors.extend(check_frontmatter(md_file) if repo_root == ROOT else [])
            # Skip link scan on huge historical CHANGELOG while still checking its frontmatter
            if rel != "docs/CHANGELOG.md":
                errors.extend(check_links(md_file, repo_root))

    extra_files = [
        repo_root / "README.md",
        repo_root / "AGENTS.md",
        repo_root / "eval" / "README.md",
    ]
    agents_dir = repo_root / ".agents"
    if agents_dir.exists():
        extra_files.extend(sorted(agents_dir.rglob("*.md")))

    for md_file in extra_files:
        if md_file.exists():
            errors.extend(check_links(md_file, repo_root))
    return errors


def main() -> int:
    errors = collect_errors(ROOT)
    if errors:
        for err in errors:
            print(f"[DOC ERROR] {err}", file=sys.stderr)
        print(f"文档检查失败：共 {len(errors)} 处问题。", file=sys.stderr)
        return 1
    print("文档状态标签与相对链接检查全部通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
