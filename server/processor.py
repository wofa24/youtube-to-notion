"""
processor.py
核心决策引擎：ffmpeg 截图 + imagehash 去重 + 强制分段逻辑。
"""

import os
import subprocess
from typing import List, Dict, Callable, Optional

import imagehash
from PIL import Image

from config import (
    TEMP_DIR,
    SCREENSHOT_WIDTH,
    SCREENSHOT_OFFSET,
    DEFAULT_HASH_THRESHOLD,
    DEFAULT_MAX_SENTENCES,
    DEFAULT_MAX_SECONDS,
    FFMPEG_PATH,
)


def _ensure_dir(path: str):
    os.makedirs(path, exist_ok=True)


def take_screenshot(stream_url: str, timestamp: float, out_path: str) -> bool:
    """
    用 ffmpeg 在指定时间戳截取一帧。
    timestamp: 秒（浮点）
    返回是否成功。
    """
    cmd = [
        FFMPEG_PATH,
        "-ss", f"{timestamp:.3f}",
        "-i", stream_url,
        "-frames:v", "1",
        "-vf", f"scale={SCREENSHOT_WIDTH}:-1",
        "-q:v", "80",   # WebP quality: 0-100, higher = better
        "-y",
        out_path,
    ]
    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
        return result.returncode == 0 and os.path.exists(out_path)
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        print(f"[processor] ffmpeg 截图失败 @ {timestamp:.2f}s: {e}")
        return False


def compute_hash(img_path: str) -> Optional[imagehash.ImageHash]:
    """计算图片感知哈希"""
    try:
        return imagehash.phash(Image.open(img_path))
    except Exception as e:
        print(f"[processor] 哈希计算失败: {e}")
        return None


def process_subtitles(
    subtitles: List[Dict],
    stream_url: str,
    video_id: str,
    hash_threshold: int = DEFAULT_HASH_THRESHOLD,
    max_sentences: int = DEFAULT_MAX_SENTENCES,
    max_seconds: float = DEFAULT_MAX_SECONDS,
    progress_callback: Optional[Callable[[int, int, str], None]] = None,
    resume_index: int = 0,
    segment_callback: Optional[Callable[[Dict], None]] = None,
) -> List[Dict]:
    """
    主处理循环：逐句截图、去重、强制分段。

    返回 segments 列表，每个元素：
    {
        "image_path": "/path/to/img.jpg",
        "text": "合并后的字幕文本",
        "start": 0.0,
        "end": 5.2,
        "subtitle_index": 3,   # 对应最后一条字幕的索引（用于断点续传）
    }
    """
    _ensure_dir(TEMP_DIR)

    segments: List[Dict] = []
    total = len(subtitles)

    # 当前段的状态
    current_image_path: Optional[str] = None
    current_hash: Optional[imagehash.ImageHash] = None
    text_buffer: List[str] = []
    seg_start: float = 0.0
    seg_sentence_count: int = 0
    seg_start_time: float = 0.0

    # 上一个已保存段的图片路径（用于哈希对比）
    last_saved_hash: Optional[imagehash.ImageHash] = None

    for i in range(resume_index, total):
        sub = subtitles[i]
        ts = max(0.0, sub["end"] - SCREENSHOT_OFFSET)

        if progress_callback:
            progress_callback(i + 1, total, f"截图中 ({i + 1}/{total})...")

        # 截图
        img_path = os.path.join(TEMP_DIR, f"{video_id}_frame_{i:05d}.webp")
        success = take_screenshot(stream_url, ts, img_path)

        if not success:
            # 截图失败，仅累积文本
            text_buffer.append(sub["text"])
            continue

        current_hash = compute_hash(img_path)

        # 判断是否需要强制分段
        force_split = False
        if text_buffer:
            elapsed = sub["end"] - seg_start_time
            if seg_sentence_count >= max_sentences or elapsed >= max_seconds:
                force_split = True

        # 判断画面是否切换
        scene_changed = False
        if last_saved_hash is not None and current_hash is not None:
            diff = last_saved_hash - current_hash
            if diff >= hash_threshold:
                scene_changed = True
        elif last_saved_hash is None:
            # 第一帧，直接开始新段
            scene_changed = True

        if scene_changed or force_split:
            # 保存上一段（如果有内容）
            if text_buffer and current_image_path:
                seg = {
                    "image_path": current_image_path,
                    "text": " ".join(text_buffer),
                    "start": seg_start,
                    "end": sub["start"],
                    "subtitle_index": i - 1,
                }
                segments.append(seg)
                if segment_callback:
                    segment_callback(seg)

            # 开始新段
            current_image_path = img_path
            last_saved_hash = current_hash
            text_buffer = [sub["text"]]
            seg_start = sub["start"]
            seg_start_time = sub["start"]
            seg_sentence_count = 1
        else:
            # 画面未变，累积文本
            text_buffer.append(sub["text"])
            seg_sentence_count += 1
            # 删除重复截图节省磁盘
            if img_path != current_image_path and os.path.exists(img_path):
                os.remove(img_path)

        # 更新当前段图片（首次或场景切换后）
        if current_image_path is None:
            current_image_path = img_path

    # 处理最后一段
    if text_buffer and current_image_path:
        last_sub = subtitles[-1] if subtitles else {"end": 0}
        seg = {
            "image_path": current_image_path,
            "text": " ".join(text_buffer),
            "start": seg_start,
            "end": last_sub["end"],
            "subtitle_index": total - 1,
        }
        segments.append(seg)
        if segment_callback:
            segment_callback(seg)

    return segments
