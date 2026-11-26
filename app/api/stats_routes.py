"""可观测路由：把磁盘上的作业记录汇成统计页要的数据。

**只读**。没有任何写入路径，也不碰 `output/` 以外的目录——
统计是"看账本"，账本本身由 batch_runner / qa 在业务流程里写。
"""
from __future__ import annotations

from fastapi import APIRouter

from app.services import stats

router = APIRouter()


@router.get("/stats")
def stats_overview() -> dict:
    """生成与问答的汇总指标（成本 / token / 覆盖率分布 / 延迟分位）。

    每次请求都重新读盘：几百个小文件几十毫秒，缓存反而会让人怀疑
    "数字怎么没跟着更新"。数据量真的到万级时再加缓存。
    """
    return stats.collect()
