"""
cache_manager.py
断点续传：用 JSON 文件记录每个视频的处理进度。
"""

import json
import os
from typing import Optional, Dict, Any

from config import CACHE_DIR


def _ensure_dir():
    os.makedirs(CACHE_DIR, exist_ok=True)


def _cache_path(video_id: str) -> str:
    return os.path.join(CACHE_DIR, f"{video_id}.json")


def save_progress(video_id: str, data: Dict[str, Any]):
    """保存处理进度"""
    _ensure_dir()
    with open(_cache_path(video_id), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_progress(video_id: str) -> Optional[Dict[str, Any]]:
    """加载处理进度，不存在返回 None"""
    path = _cache_path(video_id)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def clear_progress(video_id: str):
    """清除进度缓存"""
    path = _cache_path(video_id)
    if os.path.exists(path):
        os.remove(path)


def has_progress(video_id: str) -> bool:
    return os.path.exists(_cache_path(video_id))
