"""Optional browser smoke test for the isolated service preview; uses synthetic files."""

import argparse
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from pptx import Presentation


def document(path: Path, text: str) -> None:
    slides = Presentation()
    slides.slides.add_slide(slides.slide_layouts[0]).shapes.title.text = text
    slides.save(path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("port", type=int)
    parser.add_argument("user")
    parser.add_argument("other_user")
    parser.add_argument("password_file", type=Path)
    parser.add_argument("browser", type=Path, help="Path to Chrome or Chromium (no browser download needed)")
    parser.add_argument("--screenshot", type=Path, help="Optional path for the synthetic workspace")
    args = parser.parse_args()

    from playwright.sync_api import expect, sync_playwright

    password = args.password_file.read_text(encoding="utf-8").strip()
    base = f"http://127.0.0.1:{args.port}"
    with TemporaryDirectory(prefix="ppt-preview-browser-") as folder, sync_playwright() as playwright:
        work = Path(folder)
        first, second = work / "lesson.pptx", work / "updated.pptx"
        document(first, "Cache AlphaRevision")
        document(second, "Cache BetaRevision")
        exam = work / "exam.txt"
        exam.write_text("示例题：什么是缓存？", encoding="utf-8")
        browser = playwright.chromium.launch(headless=True, executable_path=str(args.browser))
        try:
            page = browser.new_page(base_url=base, viewport={"width": 1180, "height": 900})
            page.set_default_timeout(12_000)
            js_errors = []
            page.on("pageerror", lambda error: js_errors.append(str(error)))

            def login(name):
                page.get_by_label("账号").fill(name)
                page.get_by_label("密码").fill(password)
                page.get_by_role("button", name="进入工作台").click()
                expect(page.locator("#account-name")).to_have_text(name)

            page.goto("/")
            login(args.user)
            title = "浏览器验收 " + uuid4().hex[:8]
            page.get_by_label("课程名称").fill(title)
            page.get_by_role("button", name="创建课程").click()
            expect(page.locator("#course option:checked")).to_have_text(title)
            expect(page.locator("#repair-option")).to_be_hidden()

            def upload(path, kind="courseware", material=None):
                form = page.locator("#upload-form")
                form.locator("[name=kind]").select_option(kind)
                if material:
                    form.locator("[name=material]").select_option(material)
                form.locator("[name=file]").set_input_files(str(path))
                form.get_by_role("button", name="上传").click()
                expect(page.locator("#notice")).to_contain_text(path.name + " 已发布")

            upload(first)
            selected = page.get_by_role("checkbox", name="lesson.pptx · v1")
            expect(selected).to_be_checked()
            first_id = selected.get_attribute("value")
            upload(exam, "exam")
            page.locator("#batch-form [name=exam]").select_option(label="exam.txt v1")
            page.get_by_role("button", name="生成手册").click()
            expect(page.locator("#batch-state")).to_have_text("已完成", timeout=30_000)
            expect(page.locator("#jobs")).to_contain_text("lesson.pptx（v1）")
            with page.expect_download() as capture:
                page.get_by_role("button", name="下载 Markdown").click()
            artifact = work / "review.md"
            capture.value.save_as(str(artifact))
            assert "试题：有" in artifact.read_text(encoding="utf-8")

            page.get_by_role("button", name="按所选课件建库").click()
            expect(page.locator("#kb-summary")).to_contain_text("lesson.pptx v1")
            old_kb = page.locator("#kb").input_value()
            upload(second, material=first_id)
            page.get_by_role("button", name="按所选课件建库").click()
            expect(page.locator("#kb-summary")).to_contain_text("updated.pptx v2")

            def ask(text, expected, filename):
                page.get_by_label("问题").fill(text)
                page.get_by_role("button", name="提问", exact=True).click()
                expect(page.locator("#answer")).to_contain_text(expected)
                expect(page.locator("#sources")).to_contain_text(filename + " · 第 1 页")
                expect(page.locator("#history")).to_contain_text(text)

            ask("Cache beta?", "BetaRevision", second.name)
            page.locator("#kb").select_option(old_kb)
            expect(page.locator("#kb-summary")).to_contain_text("lesson.pptx v1")
            ask("Cache alpha?", "AlphaRevision", first.name)
            if args.screenshot:
                page.screenshot(path=str(args.screenshot), full_page=True)
            page.get_by_role("button", name="退出").click()
            expect(page.locator("#login-panel")).to_be_visible()
            login(args.other_user)
            expect(page.locator("#course option").filter(has_text=title)).to_have_count(0)
            assert not js_errors, js_errors
        finally:
            browser.close()
    print("Browser preview passed: upload and revision, generation and download, question and history, account separation.")


if __name__ == "__main__":
    main()
