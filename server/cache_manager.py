"""
cache_manager.py
断点续传：用 JSON 文件记录每个视频的处理进度及精细状态。
"""

import json
import os
from typing import Optional, Dict, Any

from config import CACHE_DIR

# ── 任务状态常量 ──────────────────────────────
STATUS_INITIALIZED = "initialized"   # 已创建，尚未开始处理
STATUS_PROCESSING  = "processing"    # 正在处理（截图 / 上传中）
STATUS_COMPLETED   = "completed"     # 全部完成
STATUS_FAILED      = "failed"        # 处理失败


def _ensure_dir():
    os.makedirs(CACHE_DIR, exist_ok=True)


def _cache_path(video_id: str) -> str:
    return os.path.join(CACHE_DIR, f"{video_id}.json")


def save_progress(video_id: str, data: Dict[str, Any]):
    """覆盖写入整条进度记录"""
    _ensure_dir()
    with open(_cache_path(video_id), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def update_status(video_id: str, status: str, **extra):
    """仅更新状态及附加字段，保留其余字段不变"""
    data = load_progress(video_id) or {}
    data["status"] = status
    data.update(extra)
    save_progress(video_id, data)


def load_progress(video_id: str) -> Optional[Dict[str, Any]]:
    """读取进度记录，文件不存在或损坏时返回 None"""
    path = _cache_path(video_id)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def clear_progress(video_id: str):
    """删除进度缓存文件"""
    path = _cache_path(video_id)
    if os.path.exists(path):
        os.remove(path)


def has_progress(video_id: str) -> bool:
    return os.path.exists(_cache_path(video_id))


def get_any_in_progress() -> Optional[str]:
    """扫描缓存目录，返回第一个处于 processing 状态的 video_id，无则返回 None。"""
    _ensure_dir()
    try:
        for fname in os.listdir(CACHE_DIR):
            if not fname.endswith(".json"):
                continue
            path = os.path.join(CACHE_DIR, fname)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if data.get("status") == STATUS_PROCESSING:
                    return fname[:-5]  # 去掉 ".json"
            except Exception:
                continue
    except Exception:
        pass
    return None
