"""
fetcher.py
负责从 YouTube 获取字幕和视频直链。
优先使用 yt-dlp 拉取字幕，失败时 fallback 到本地 Whisper 转录。
"""

import json
import os
import re
import subprocess
import tempfile
from typing import List, Dict, Optional

import yt_dlp

from config import TEMP_DIR, FFMPEG_PATH

# ffmpeg 所在目录（yt-dlp 需要目录，不是完整路径）
_FFMPEG_DIR = os.path.dirname(FFMPEG_PATH)


def _ensure_dir(path: str):
    os.makedirs(path, exist_ok=True)


# ─────────────────────────────────────────────
# 字幕结构：[{"start": 0.0, "end": 5.2, "text": "Hello"}]
# ─────────────────────────────────────────────

def _parse_json3(data: dict) -> List[Dict]:
    """解析 yt-dlp json3 格式字幕"""
    result = []
    for event in data.get("events", []):
        start_ms = event.get("tStartMs", 0)
        dur_ms = event.get("dDurationMs", 0)
        segs = event.get("segs", [])
        text = "".join(s.get("utf8", "") for s in segs).strip()
        text = re.sub(r"\s+", " ", text)
        if not text or text == "\n":
            continue
        result.append({
            "start": start_ms / 1000.0,
            "end": (start_ms + dur_ms) / 1000.0,
            "text": text,
        })
    return result


def _parse_vtt(content: str) -> List[Dict]:
    """解析 WebVTT 格式字幕"""
    result = []
    blocks = re.split(r"\n\n+", content.strip())
    time_re = re.compile(
        r"(\d{2}:\d{2}:\d{2}[.,]\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}[.,]\d{3})"
    )

    def to_sec(ts: str) -> float:
        ts = ts.replace(",", ".")
        parts = ts.split(":")
        return float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])

    for block in blocks:
        lines = block.strip().splitlines()
        for i, line in enumerate(lines):
            m = time_re.match(line)
            if m:
                text_lines = lines[i + 1:]
                text = " ".join(
                    re.sub(r"<[^>]+>", "", l).strip() for l in text_lines if l.strip()
                )
                text = re.sub(r"\s+", " ", text).strip()
                if text:
                    result.append({
                        "start": to_sec(m.group(1)),
                        "end": to_sec(m.group(2)),
                        "text": text,
                    })
                break
    return result


def get_subtitles_ytdlp(url: str) -> Optional[List[Dict]]:
    """
    用 yt-dlp 下载字幕，返回字幕列表。
    优先 json3，其次 vtt。失败返回 None。
    """
    _ensure_dir(TEMP_DIR)
    out_tmpl = os.path.join(TEMP_DIR, "subtitle_%(id)s")

    ydl_opts = {
        "skip_download": True,
        "writesubtitles": True,
        "writeautomaticsub": True,
        "subtitlesformat": "json3/vtt",
        "outtmpl": out_tmpl,
        "quiet": True,
        "no_warnings": True,
        "ffmpeg_location": _FFMPEG_DIR,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            info = ydl.extract_info(url, download=True)
        except Exception as e:
            print(f"[fetcher] yt-dlp 字幕下载失败: {e}")
            return None

    video_id = info.get("id", "")
    # 查找下载的字幕文件
    for fname in os.listdir(TEMP_DIR):
        if video_id in fname:
            fpath = os.path.join(TEMP_DIR, fname)
            if fname.endswith(".json3"):
                with open(fpath, "r", encoding="utf-8") as f:
                    data = json.load(f)
                return _parse_json3(data)
            elif fname.endswith(".vtt"):
                with open(fpath, "r", encoding="utf-8") as f:
                    content = f.read()
                return _parse_vtt(content)
    return None


def get_subtitles_whisper(url: str) -> List[Dict]:
    """
    用本地 Whisper 转录音频，返回字幕列表。
    需要先安装 openai-whisper 和 ffmpeg。
    """
    import whisper

    _ensure_dir(TEMP_DIR)
    audio_path = os.path.join(TEMP_DIR, "audio_whisper.m4a")

    # 用 yt-dlp 下载音频
    ydl_opts = {
        "format": "bestaudio[ext=m4a]/bestaudio",
        "outtmpl": audio_path,
        "quiet": True,
        "no_warnings": True,
        "ffmpeg_location": _FFMPEG_DIR,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])

    print("[fetcher] 开始 Whisper 转录，请稍候...")
    model = whisper.load_model("base")
    result = model.transcribe(audio_path, word_timestamps=False)

    subtitles = []
    for seg in result.get("segments", []):
        subtitles.append({
            "start": float(seg["start"]),
            "end": float(seg["end"]),
            "text": seg["text"].strip(),
        })

    # 清理音频文件
    if os.path.exists(audio_path):
        os.remove(audio_path)

    return subtitles


def get_subtitles(url: str) -> List[Dict]:
    """
    获取字幕的统一入口。
    优先 yt-dlp，失败时 fallback 到 Whisper。
    """
    print("[fetcher] 尝试用 yt-dlp 获取字幕...")
    subs = get_subtitles_ytdlp(url)
    if subs and len(subs) > 0:
        print(f"[fetcher] 获取到 {len(subs)} 条字幕")
        return subs

    print("[fetcher] yt-dlp 未获取到字幕，fallback 到 Whisper 转录...")
    subs = get_subtitles_whisper(url)
    print(f"[fetcher] Whisper 转录完成，共 {len(subs)} 条")
    return subs


def get_video_local_path(url: str, video_id: str) -> str:
    """
    将视频下载到本地文件并返回路径（用于 ffmpeg 截图）。
    使用 yt-dlp 下载，自动走系统代理，避免 ffmpeg 直连被墙。
    优先选 720p 以内的 H.264 MP4，次选其他 MP4。
    """
    _ensure_dir(TEMP_DIR)
    local_path = os.path.join(TEMP_DIR, f"{video_id}_video.mp4")
    if os.path.exists(local_path):
        print(f"[fetcher] 使用已缓存视频: {local_path}")
        return local_path

    print("[fetcher] 开始下载视频（用于截图）...")
    ydl_opts = {
        "format": (
            "bestvideo[vcodec^=avc1][ext=mp4][height<=720]"
            "/bestvideo[ext=mp4][height<=720]"
            "/bestvideo[ext=mp4]"
            "/bestvideo"
        ),
        "outtmpl": local_path,
        "quiet": True,
        "no_warnings": True,
        "ffmpeg_location": _FFMPEG_DIR,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])

    if not os.path.exists(local_path):
        raise RuntimeError(f"视频下载失败，找不到文件: {local_path}")
    print(f"[fetcher] 视频下载完成: {local_path} ({os.path.getsize(local_path)//1024//1024}MB)")
    return local_path


def get_video_info(url: str) -> Dict:
    """获取视频基本信息（标题、时长等）"""
    ydl_opts = {
        "skip_download": True,
        "quiet": True,
        "no_warnings": True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
        return {
            "id": info.get("id", ""),
            "title": info.get("title", "Untitled"),
            "duration": info.get("duration", 0),
            "thumbnail": info.get("thumbnail", ""),
        }
