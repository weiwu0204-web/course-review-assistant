"""用户级配置存储：API Key 等个人设置的读写。

打包成 exe 后没有 .env，同学的 API Key 写到这里，与程序目录隔离：
  - onedir 分发目录可能被放只读位置，exe 旁的文件也容易在更新/清理时被误删
  - 用户配置放在自己的数据目录，多个版本共存也不互相覆盖

路径：%APPDATA%/PPT复习助手/settings.json
（可用环境变量 PPT_REVIEWER_CONFIG_DIR 覆盖，供测试与便携版使用）
"""
import json
import os
from datetime import datetime
from pathlib import Path

CONFIG_FILE_NAME = "settings.json"
APP_DIR_NAME = "PPT复习助手"


def user_config_dir() -> Path:
    """用户配置目录：优先环境变量覆盖（测试用），否则走 %APPDATA%。"""
    override = os.environ.get("PPT_REVIEWER_CONFIG_DIR")
    if override:
        return Path(override)
    base = os.environ.get("APPDATA")
    if not base:
        base = str(Path.home())
    return Path(base) / APP_DIR_NAME


def config_path() -> Path:
    return user_config_dir() / CONFIG_FILE_NAME


def load_user_settings() -> dict:
    """读取用户配置，返回 {provider_id, api_key, tier, updated_at}。

    文件不存在或损坏时返回 {}（当作未配置处理，不抛错）。
    旧版配置没有 tier 字段属正常（读取方按服务商默认档回退）。
    """
    try:
        data = json.loads(config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def save_user_settings(provider_id: str, api_key: str, tier: str = "") -> dict:
    """保存用户配置（api_key 去除首尾空白），返回写入的完整内容。

    tier：服务商档位 id（如智谱 paid/free）；单档服务商传空字符串即可。
    """
    payload = {
        "provider_id": provider_id.strip(),
        "api_key": api_key.strip(),
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    if tier:
        payload["tier"] = tier.strip()
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return payload


def clear_user_settings() -> None:
    """删除用户配置（设置页「清除」用），失败时静默（文件可能不存在）。"""
    try:
        config_path().unlink(missing_ok=True)
    except OSError:
        pass
