"""
main.py
FastAPI 服务入口。
提供 REST API 供 Chrome 扩展调用，SSE 推送实时进度。
"""

import asyncio
import json
import os
import re
import threading
from typing import Dict, Any, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

import cache_manager
from config import (
    NOTION_TOKEN,
    NOTION_DATABASE_ID,
    DEFAULT_HASH_THRESHOLD,
    DEFAULT_MAX_SENTENCES,
    DEFAULT_MAX_SECONDS,
    TEMP_DIR,
)
from fetcher import get_video_info, get_subtitles, get_video_local_path
from processor import process_subtitles
from uploader import create_notion_page, upload_segments, get_page_url

app = FastAPI(title="YouTube to Notion")

# 允许 Chrome 扩展跨域访问
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# 全局任务状态存储（内存，单进程够用）
_tasks: Dict[str, Dict[str, Any]] = {}


# ─────────────────────────────────────────────
# 请求模型
# ─────────────────────────────────────────────

class ProcessRequest(BaseModel):
    url: str
    notion_token: str = ""
    database_id: str = ""
    hash_threshold: int = DEFAULT_HASH_THRESHOLD
    max_sentences: int = DEFAULT_MAX_SENTENCES
    max_seconds: float = DEFAULT_MAX_SECONDS
    resume: bool = True  # 是否启用断点续传


class ConfigRequest(BaseModel):
    notion_token: str
    database_id: str


# ─────────────────────────────────────────────
# 工具函数
# ─────────────────────────────────────────────

def _extract_video_id(url: str) -> str:
    """从 YouTube URL 提取 video_id"""
    patterns = [
        r"(?:v=|youtu\.be/)([A-Za-z0-9_-]{11})",
        r"(?:embed/)([A-Za-z0-9_-]{11})",
    ]
    for p in patterns:
        m = re.search(p, url)
        if m:
            return m.group(1)
    raise ValueError(f"无法从 URL 提取 video_id: {url}")


def _update_task(task_id: str, **kwargs):
    if task_id in _tasks:
        _tasks[task_id].update(kwargs)


# ─────────────────────────────────────────────
# 后台处理线程
# ─────────────────────────────────────────────

def _run_pipeline(task_id: str, req: ProcessRequest):
    """在后台线程中执行完整处理流程"""
    token = req.notion_token or NOTION_TOKEN
    db_id = req.database_id or NOTION_DATABASE_ID

    def progress(current: int, total: int, msg: str):
        _update_task(task_id,
                     progress_current=current,
                     progress_total=total,
                     message=msg)

    try:
        video_id = _extract_video_id(req.url)
        _update_task(task_id, status="running", message="获取视频信息...")

        # ── 断点续传检查 ──
        cached = cache_manager.load_progress(video_id) if req.resume else None
        resume_subtitle_index = 0
        resume_segment_index = 0
        page_id = None
        cached_subtitles = None
        cached_segments = []

        if cached:
            page_id = cached.get("page_id")
            resume_subtitle_index = cached.get("subtitle_index", 0)
            resume_segment_index = cached.get("segment_index", 0)
            cached_subtitles = cached.get("subtitles")
            cached_segments = cached.get("segments", [])
            _update_task(task_id, message=f"检测到断点，从第 {resume_subtitle_index} 条字幕继续...")

        # ── 获取视频信息 ──
        info = get_video_info(req.url)
        title = info["title"]
        _update_task(task_id, title=title, message="获取字幕...")

        # ── 获取字幕 ──
        if cached_subtitles:
            subtitles = cached_subtitles
            _update_task(task_id, message=f"使用缓存字幕，共 {len(subtitles)} 条")
        else:
            subtitles = get_subtitles(req.url)
            if not subtitles:
                raise RuntimeError("未能获取到字幕，请检查视频是否有字幕或 Whisper 是否安装")

        _update_task(task_id, message=f"共 {len(subtitles)} 条字幕，开始截图...")

        # ── 下载视频到本地（ffmpeg 从本地截图，避免直连被墙）──
        _update_task(task_id, message="下载视频（用于截图）...")
        local_video_path = get_video_local_path(req.url, video_id)

        # ── 截图 + 去重 ──
        new_segments = process_subtitles(
            subtitles=subtitles,
            stream_url=local_video_path,
            video_id=video_id,
            hash_threshold=req.hash_threshold,
            max_sentences=req.max_sentences,
            max_seconds=req.max_seconds,
            progress_callback=progress,
            resume_index=resume_subtitle_index,
        )

        all_segments = cached_segments + new_segments

        # 截图完成，删除本地视频文件释放磁盘
        if os.path.exists(local_video_path):
            os.remove(local_video_path)

        _update_task(task_id, message=f"截图完成，共 {len(all_segments)} 个画面段，开始上传 Notion...")

        # ── 创建 Notion 页面（首次）──
        if not page_id:
            page_id = create_notion_page(title, req.url, token, db_id)

        # 保存进度（截图完成，上传前）
        # 保留 image_path，断点续传时需要重新上传图片
        cache_manager.save_progress(video_id, {
            "page_id": page_id,
            "subtitle_index": len(subtitles),
            "segment_index": resume_segment_index,    # 上传起始索引（本次运行开始前已上传的段数）
            "subtitles": subtitles,
            "segments": all_segments,               # 保留完整 segments（含 image_path）
            "url": req.url,
            "title": title,
        })

        # ── 上传到 Notion ──
        # 只上传新截图的段，cached_segments 已经在上次上传过了
        upload_segments(
            page_id=page_id,
            segments=all_segments,
            token=token,
            progress_callback=progress,
            start_index=resume_segment_index,
            video_url=req.url,
        )

        page_url = get_page_url(page_id)

        # 清除缓存（完成）
        cache_manager.clear_progress(video_id)

        _update_task(task_id,
                     status="done",
                     message="完成！",
                     page_url=page_url,
                     progress_current=len(all_segments),
                     progress_total=len(all_segments))

    except Exception as e:
        import traceback
        err = str(e)
        print(f"[main] 任务 {task_id} 失败: {err}")
        traceback.print_exc()
        _update_task(task_id, status="error", message=f"错误: {err}")


# ─────────────────────────────────────────────
# API 路由
# ─────────────────────────────────────────────

@app.get("/health")
def health():
    """健康检查，扩展启动时用于检测服务是否在线"""
    return {"status": "ok"}


@app.post("/process")
def start_process(req: ProcessRequest):
    """
    启动处理任务，返回 task_id。
    扩展通过 /progress/{task_id} SSE 接口获取进度。
    """
    try:
        video_id = _extract_video_id(req.url)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # 拒绝并发：若已有任务在运行，返回 409
    running = next(
        (tid for tid, t in _tasks.items() if t.get("status") == "running"),
        None,
    )
    if running:
        running_title = _tasks[running].get("title") or running
        raise HTTPException(
            status_code=409,
            detail=f"已有任务正在运行（{running_title}），请等待完成后再提交",
        )

    task_id = video_id
    _tasks[task_id] = {
        "status": "pending",
        "message": "任务已创建",
        "progress_current": 0,
        "progress_total": 0,
        "page_url": None,
        "title": "",
    }

    thread = threading.Thread(target=_run_pipeline, args=(task_id, req), daemon=True)
    thread.start()

    return {"task_id": task_id}


@app.get("/progress/{task_id}")
async def progress_stream(task_id: str):
    """SSE 接口，实时推送任务进度"""
    if task_id not in _tasks:
        raise HTTPException(status_code=404, detail="任务不存在")

    async def event_generator():
        while True:
            task = _tasks.get(task_id, {})
            yield {
                "data": json.dumps(task, ensure_ascii=False),
            }
            if task.get("status") in ("done", "error"):
                break
            await asyncio.sleep(0.5)

    return EventSourceResponse(event_generator())


@app.get("/status/{task_id}")
def get_status(task_id: str):
    """轮询接口（SSE 的备选方案）"""
    if task_id not in _tasks:
        raise HTTPException(status_code=404, detail="任务不存在")
    return _tasks[task_id]


@app.get("/check_resume/{video_id}")
def check_resume(video_id: str):
    """检查是否有断点续传缓存"""
    cached = cache_manager.load_progress(video_id)
    if cached:
        return {
            "has_cache": True,
            "title": cached.get("title", ""),
            "subtitle_index": cached.get("subtitle_index", 0),
        }
    return {"has_cache": False}


@app.delete("/cache/{video_id}")
def delete_cache(video_id: str):
    """清除断点续传缓存"""
    cache_manager.clear_progress(video_id)
    return {"ok": True}


# ─────────────────────────────────────────────
# 启动入口
# ─────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    from config import SERVER_HOST, SERVER_PORT

    os.makedirs(TEMP_DIR, exist_ok=True)
    print(f"[main] 服务启动于 http://{SERVER_HOST}:{SERVER_PORT}")
    uvicorn.run(app, host=SERVER_HOST, port=SERVER_PORT)
