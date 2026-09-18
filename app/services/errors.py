"""AI 调用异常的「用户可行动」翻译：把底层异常转成知道下一步干什么的文案。

0.7.0 前该逻辑是 app/api/routes.py 的私有函数 _friendly_ai_error，批任务（batch_runner）
与单份生成共用同一份异常翻译，故提升为公共模块；routes.py 以
`from app.services.errors import friendly_ai_error as _friendly_ai_error` 引用，
对外路径不变（旧测试不受影响）。

设计约束：
- 文案必须透出当前服务商名与充值入口 URL（前端渲染成可点链接）；
- 识别不到的异常回退到原始信息，绝不二次猜测平台语义；
- 分类依据是异常文本里的稳定关键字；余额/限流/鉴权/网络的措辞与单份时代保持一致，
  这样单份页与批量页给用户的指引是同一套话术。
"""
from app.config import current_provider


def friendly_ai_error(exc: Exception) -> str:
    """把生成阶段的 AI 调用异常翻译成「用户知道下一步干什么」的文案。"""
    low = str(exc).lower()
    provider = current_provider()
    name = provider.get("name", "当前服务商")
    url = provider.get("topup_url", "")

    if any(k in low for k in ("402", "balance", "insufficient", "quota", "额度", "余额", "欠费")):
        return (
            f"AI 生成失败：余额不足或额度用尽（当前使用 {name}）。"
            f"充值入口：{url} ；也可以在右上角「设置」里换一家有免费额度的服务商再试。"
        )
    if any(k in low for k in ("429", "rate limit", "rate_limit", "限流")):
        return (
            f"AI 生成失败：请求被限流（多为额度不足或短时间请求过多，当前使用 {name}）。"
            f"可稍后重试，或在右上角「设置」里换一家服务商。"
        )
    if any(k in low for k in ("401", "403", "authentication", "invalid api key", "api key")):
        return "AI 生成失败：Key 无效或已失效，请在右上角「设置」里重新粘贴完整 Key。"
    if any(k in low for k in ("connection", "network", "timed out", "timeout", "无法连接")):
        return "AI 生成失败：连不上服务商服务器，请检查网络后重试。"
    return f"AI 生成失败：{exc}"


def classify_error(exc: Exception) -> str:
    """把异常归成批任务可决策的类别：balance / network / other。

    batch_runner 据此决定整批行为：
      - balance：剩余 job 全部熔断（继续打只会一直失败）；
      - network：连续失败达到阈值自动暂停批；
      - other：仅当前 job 记错，不影响后续。
    """
    low = str(exc).lower()
    if any(k in low for k in ("402", "balance", "insufficient", "quota", "额度", "余额", "欠费")):
        return "balance"
    if any(k in low for k in ("connection", "network", "timed out", "timeout", "无法连接", "failed to fetch")):
        return "network"
    return "other"
