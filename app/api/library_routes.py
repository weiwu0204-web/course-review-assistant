"""历史记录路由：按课程归组的资料库。

与 `/api/batches` 的分工：
- `/api/batches` 是**批量任务**视角（跑没跑完、能不能续跑），启动时恢复进度用它
- `/api/library` 是**课程**视角（这门课有哪些文档、问过什么），历史面板用它

两个都保留是有意的：同一批数据，两种回忆方式。
"""
from __future__ import annotations

from fastapi import APIRouter, Query

from app.services import library

router = APIRouter()


@router.get("/library")
def library_list(
    with_answers: bool = Query(default=True, description="是否一并返回问答答案全文"),
) -> dict:
    """按课程归组的历史记录（组间按最近活动倒序）。

    **不检查知识库缓存是否还在**：问答历史落在 `output/qa/`，与
    `output/kb/` 的 72 小时缓存无关。用户一周后回来翻记录是正常用法，
    这时缓存早已过期，但历史必须还在——所以这里只读日志文件。
    """
    courses = library.collect(with_answers=with_answers)
    return {"courses": courses, **library.stats()}
