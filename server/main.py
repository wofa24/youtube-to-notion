"""
main.py
FastAPI 服务入口。
提供 REST API 供 Chrome 扩展调用，SSE 推送实时进度。

异步流水线（生产者-消费者 + Semaphore）：
  生产者：截帧线程（process_subtitles）每凑满 COMPOSITE_SIZE 帧，
          同步生成合成图，然后通过 asyncio.run_coroutine_threadsafe
          立即在事件循环中创建上传 Task，无需等待后续截帧完成。
  并发控制：asyncio.Semaphore(UPLOAD_CONCURRENCY=3) 保证最多 3 个
          上传请求同时在途，恰好打满 Notion 3 req/s 配额。
  顺序保证：results[gi] 按组编号写入，所有 Task 完成后按序合并 blocks。
"""

import asyncio
import json
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, Any

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
from uploader import (
    create_notion_page, get_page_url,
    make_group_composite, upload_and_build, append_blocks,
    COMPOSITE_SIZE, UPLOAD_WORKERS, UPLOAD_CONCURRENCY,
)

app = FastAPI(title="YouTube to Notion")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

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
    resume: bool = True


# ─────────────────────────────────────────────
# 工具函数
# ─────────────────────────────────────────────

def _extract_video_id(url: str) -> str:
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
# 异步流水线
# ─────────────────────────────────────────────

async def _async_pipeline(task_id: str, req: ProcessRequest):
    """
    核心流水线：
      • 截帧在 screen_exec 线程池执行（不阻塞事件循环）
      • 每凑满一组，_on_segment 回调同步生成合成图，
        然后用 run_coroutine_threadsafe 即刻向事件循环投递上传 Task
      • Semaphore(UPLOAD_CONCURRENCY) 确保恰好 3 个上传同时在途
      • 截帧期间上传已经在并发执行，两者完全重叠
    """
    token = req.notion_token or NOTION_TOKEN
    db_id = req.database_id  or NOTION_DATABASE_ID
    loop  = asyncio.get_event_loop()

    def progress(current: int, total: int, msg: str):
        _update_task(task_id, progress_current=current, progress_total=total, message=msg)

    try:
        video_id = _extract_video_id(req.url)
        _update_task(task_id, status="running", message="获取视频信息...")
        cache_manager.update_status(video_id, cache_manager.STATUS_PROCESSING)

        # ── 断点续传 ────────────────────────────────────
        cached = cache_manager.load_progress(video_id) if req.resume else None
        resume_subtitle_index = 0
        page_id          = None
        cached_subtitles = None
        cached_segments: list = []

        if cached and cached.get("status") == cache_manager.STATUS_PROCESSING:
            page_id               = cached.get("page_id")
            resume_subtitle_index = cached.get("subtitle_index", 0)
            cached_subtitles      = cached.get("subtitles")
            cached_segments       = cached.get("segments", [])
            _update_task(task_id, message=f"断点续传，从第 {resume_subtitle_index} 条字幕继续...")

        # ── 获取视频信息 & 字幕 ──────────────────────────
        info  = await loop.run_in_executor(None, get_video_info, req.url)
        title = info["title"]
        _update_task(task_id, title=title, message="获取字幕...")

        if cached_subtitles:
            subtitles = cached_subtitles
        else:
            subtitles = await loop.run_in_executor(None, get_subtitles, req.url)
            if not subtitles:
                raise RuntimeError("未能获取到字幕，请检查视频是否有字幕或 Whisper 是否安装")

        # ── 下载视频 ─────────────────────────────────────
        _update_task(task_id, message="下载视频（用于截图）...")
        local_video_path = await loop.run_in_executor(
            None, get_video_local_path, req.url, video_id
        )

        # ── 创建 Notion 页面（首次）──────────────────────
        if not page_id:
            page_id = await loop.run_in_executor(
                None, create_notion_page, title, req.url, token, db_id
            )

        cache_manager.save_progress(video_id, {
            "status":         cache_manager.STATUS_PROCESSING,
            "page_id":        page_id,
            "subtitle_index": resume_subtitle_index,
            "subtitles":      subtitles,
            "segments":       cached_segments,
            "url":            req.url,
            "title":          title,
        })

        # ── 并发控制与任务容器 ───────────────────────────
        # Semaphore 限制同时在途的上传请求数，对齐 Notion 3 req/s 配额
        sem = asyncio.Semaphore(UPLOAD_CONCURRENCY)
        results: Dict[int, list] = {}   # gi → blocks，供最终按序合并
        task_refs: list = []            # asyncio.Task 引用，用于 gather
        upload_exec = ThreadPoolExecutor(max_workers=UPLOAD_WORKERS)

        # ── 上传协程（每组一个 Task）────────────────────
        async def _upload_one(gi: int, group: list, comp_path: str):
            """
            获取 Semaphore 后在线程池中执行实际上传。
            Semaphore 控制并发，确保 ≤ UPLOAD_CONCURRENCY 个请求同时在途。
            """
            async with sem:
                try:
                    blocks = await loop.run_in_executor(
                        upload_exec,
                        upload_and_build,
                        comp_path, group, token, gi, req.url,
                    )
                except Exception as e:
                    print(f"[main] 上传组 {gi} 失败: {e}")
                    blocks = []
            results[gi] = blocks
            done = len(results)
            _update_task(task_id, message=f"上传进度 {done}/{_gi[0]} 组...")

        # 在事件循环中创建 Task 并记录引用（供 gather 等待）
        async def _schedule_upload(gi: int, group: list, comp_path: str):
            task = asyncio.create_task(_upload_one(gi, group, comp_path))
            task_refs.append(task)

        # ── 生产者回调（运行于 screen_exec 截帧线程）────
        _gi    = [0]   # mutable int，跨线程共享组计数
        _batch: list = []

        def _on_segment(seg: dict):
            """
            每产生一个 segment 时被调用。
            凑满 COMPOSITE_SIZE 帧后：
              1. 在当前线程同步生成合成图（CPU 密集，约 30–80ms）
              2. 立即向事件循环投递上传 Task（网络 I/O，与截帧并发执行）
            future.result() 确保 Task 已入 task_refs 后再继续下一帧。
            """
            _batch.append(seg)
            if len(_batch) >= COMPOSITE_SIZE:
                gi    = _gi[0]; _gi[0] += 1
                group = _batch.copy(); _batch.clear()
                # 同步生成合成图（截帧线程，非阻塞事件循环）
                comp_path = make_group_composite(group, gi, page_id[:8])
                if comp_path:
                    # 立即在事件循环中调度上传 Task
                    future = asyncio.run_coroutine_threadsafe(
                        _schedule_upload(gi, group, comp_path), loop
                    )
                    future.result()  # 等待 Task 创建完成（微秒级），保证 task_refs 完整

        # 续传：缓存 segments 先走回调（复用分组逻辑）
        for seg in cached_segments:
            _on_segment(seg)

        _update_task(task_id, message=f"共 {len(subtitles)} 条字幕，截图并上传中...")

        # 截帧在独立线程池运行，上传 Task 在事件循环并发执行，两者完全重叠
        screen_exec = ThreadPoolExecutor(max_workers=2)
        new_segments = await loop.run_in_executor(
            screen_exec,
            lambda: process_subtitles(
                subtitles=subtitles,
                stream_url=local_video_path,
                video_id=video_id,
                hash_threshold=req.hash_threshold,
                max_sentences=req.max_sentences,
                max_seconds=req.max_seconds,
                progress_callback=progress,
                resume_index=resume_subtitle_index,
                segment_callback=_on_segment,
            ),
        )

        # 截图完成，删除本地视频文件
        if os.path.exists(local_video_path):
            os.remove(local_video_path)

        # 冲刷不足一组的剩余 segments（已在事件循环中，直接创建 Task）
        if _batch:
            gi = _gi[0]; _gi[0] += 1
            comp_path = make_group_composite(_batch.copy(), gi, page_id[:8])
            if comp_path:
                task = asyncio.create_task(_upload_one(gi, _batch.copy(), comp_path))
                task_refs.append(task)

        # 等待所有上传 Task 完成（包括截帧期间已启动的并发上传）
        if task_refs:
            await asyncio.gather(*task_refs)

        upload_exec.shutdown(wait=False)
        screen_exec.shutdown(wait=False)

        # ── 保存最终进度 ─────────────────────────────────
        all_segments = cached_segments + new_segments
        cache_manager.save_progress(video_id, {
            "status":         cache_manager.STATUS_PROCESSING,
            "page_id":        page_id,
            "subtitle_index": len(subtitles),
            "subtitles":      subtitles,
            "segments":       all_segments,
            "url":            req.url,
            "title":          title,
        })

        # ── 按组序号合并 blocks，一次性写入 Notion ───────
        total_segs = len(all_segments)
        _update_task(task_id, message="写入 Notion...", progress_total=total_segs)

        all_blocks: list = []
        for gi in sorted(results.keys()):
            all_blocks.extend(results[gi])

        await loop.run_in_executor(None, append_blocks, page_id, all_blocks, token)

        page_url = get_page_url(page_id)
        cache_manager.save_progress(video_id, {
            "status":   cache_manager.STATUS_COMPLETED,
            "page_id":  page_id,
            "page_url": page_url,
            "title":    title,
            "url":      req.url,
        })

        _update_task(task_id,
                     status="done",
                     message="完成！",
                     page_url=page_url,
                     progress_current=total_segs,
                     progress_total=total_segs)

    except Exception as e:
        import traceback
        print(f"[main] 任务 {task_id} 失败: {e}")
        traceback.print_exc()
        _update_task(task_id, status="error", message=f"错误: {e}")
        try:
            cache_manager.update_status(video_id, cache_manager.STATUS_FAILED)
        except Exception:
            pass


def _run_pipeline(task_id: str, req: ProcessRequest):
    """在独立后台线程中创建新的 asyncio 事件循环，运行异步流水线。"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_async_pipeline(task_id, req))
    finally:
        loop.close()


# ─────────────────────────────────────────────
# API 路由
# ─────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/process")
def start_process(req: ProcessRequest):
    """
    启动处理任务，返回 task_id。
    三层冲突检测（内存 → 其他视频缓存 → 当前视频缓存）。
    """
    try:
        video_id = _extract_video_id(req.url)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # 层1：内存状态（同进程生命周期）
    running = next(
        (tid for tid, t in _tasks.items() if t.get("status") in ("pending", "running")),
        None,
    )
    if running:
        title = _tasks[running].get("title") or running
        raise HTTPException(
            status_code=409,
            detail=f"已有任务正在运行（{title}），请等待完成后再提交",
        )

    # 层2：其他视频的持久化 processing 状态
    other = cache_manager.get_any_in_progress()
    if other and other != video_id:
        raise HTTPException(
            status_code=409,
            detail=f"另一个视频（{other}）有未完成的任务，请先清除缓存或等待完成",
        )

    # 层3：当前视频 processing 但未开启续传
    cached = cache_manager.load_progress(video_id)
    if cached and cached.get("status") == cache_manager.STATUS_PROCESSING and not req.resume:
        raise HTTPException(
            status_code=409,
            detail="该视频有未完成的任务，请开启断点续传或先清除缓存",
        )

    task_id = video_id
    _tasks[task_id] = {
        "status":           "pending",
        "message":          "任务已创建",
        "progress_current": 0,
        "progress_total":   0,
        "page_url":         None,
        "title":            "",
    }

    thread = threading.Thread(target=_run_pipeline, args=(task_id, req), daemon=True)
    thread.start()
    return {"task_id": task_id}


@app.get("/progress/{task_id}")
async def progress_stream(task_id: str):
    """SSE 实时进度推送。服务重启后任务不在内存中时，返回中断提示。"""
    if task_id not in _tasks:
        cached = cache_manager.load_progress(task_id)
        if cached and cached.get("status") == cache_manager.STATUS_PROCESSING:
            _tasks[task_id] = {
                "status":           "error",
                "message":          "服务已重启，任务中断。断点续传数据已保留，请重新提交。",
                "progress_current": cached.get("subtitle_index", 0),
                "progress_total":   0,
                "page_url":         None,
                "title":            cached.get("title", ""),
            }
        else:
            raise HTTPException(status_code=404, detail="任务不存在")

    async def event_generator():
        while True:
            task = _tasks.get(task_id, {})
            yield {"data": json.dumps(task, ensure_ascii=False)}
            if task.get("status") in ("done", "error"):
                break
            await asyncio.sleep(0.5)

    return EventSourceResponse(event_generator())


@app.get("/status/{task_id}")
def get_status(task_id: str):
    """轮询接口（SSE 降级备用）"""
    if task_id not in _tasks:
        raise HTTPException(status_code=404, detail="任务不存在")
    return _tasks[task_id]


@app.get("/check_resume/{video_id}")
def check_resume(video_id: str):
    cached = cache_manager.load_progress(video_id)
    if cached:
        return {
            "has_cache":      True,
            "title":          cached.get("title", ""),
            "subtitle_index": cached.get("subtitle_index", 0),
            "status":         cached.get("status", ""),
        }
    return {"has_cache": False}


@app.delete("/cache/{video_id}")
def delete_cache(video_id: str):
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
