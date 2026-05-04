"""
main.py
FastAPI 服务入口。
提供 REST API 供 Chrome 扩展调用，SSE 推送实时进度。

异步流水线架构（生产者-消费者）：
  生产者：在独立线程中执行截帧，每凑满 COMPOSITE_SIZE 帧，
          通过 asyncio.run_coroutine_threadsafe 将任务投入 asyncio.Queue。
  消费者：UPLOAD_WORKERS 个 async 协程并发从队列取任务，
          在 ThreadPoolExecutor 中调用上传函数，完全与截帧解耦。
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
    upload_group_and_build, append_blocks,
    COMPOSITE_SIZE, UPLOAD_WORKERS,
)

app = FastAPI(title="YouTube to Notion")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# 内存任务表（单进程生命周期内有效）
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
# 异步消费者协程
# ─────────────────────────────────────────────

async def _upload_worker(
    queue: asyncio.Queue,
    page_id: str,
    token: str,
    video_url: str,
    results: Dict[int, list],
    executor: ThreadPoolExecutor,
    task_id: str,
):
    """
    消费者：持续从队列取出 (gi, group) 并上传。
    队列中放入 None 作为哨兵信号，收到后退出。
    上传结果按 gi 存入 results，供主协程按序合并。
    """
    loop = asyncio.get_event_loop()
    while True:
        item = await queue.get()
        if item is None:          # 哨兵：生产者已完成
            queue.task_done()
            break
        gi, group = item
        try:
            blocks = await loop.run_in_executor(
                executor,
                upload_group_and_build,
                group, page_id, token, gi, video_url,
            )
        except Exception as e:
            print(f"[main] 上传组 {gi} 异常: {e}")
            blocks = []
        results[gi] = blocks
        queue.task_done()
        _update_task(task_id, message=f"已上传第 {gi + 1} 组图片...")


# ─────────────────────────────────────────────
# 异步流水线主体
# ─────────────────────────────────────────────

async def _async_pipeline(task_id: str, req: ProcessRequest):
    """
    异步流水线：
      1. 生产者在线程池中运行 process_subtitles（截帧 + 去重）。
         每凑满 COMPOSITE_SIZE 帧，通过 run_coroutine_threadsafe 投入队列。
      2. UPLOAD_WORKERS 个消费者协程并发上传，
         在独立 ThreadPoolExecutor 中调用 upload_group_and_build。
      3. 截帧与上传完全并发，显著缩短总耗时。
    """
    token  = req.notion_token or NOTION_TOKEN
    db_id  = req.database_id  or NOTION_DATABASE_ID
    loop   = asyncio.get_event_loop()

    def progress(current: int, total: int, msg: str):
        _update_task(task_id, progress_current=current, progress_total=total, message=msg)

    try:
        video_id = _extract_video_id(req.url)
        _update_task(task_id, status="running", message="获取视频信息...")
        cache_manager.update_status(video_id, cache_manager.STATUS_PROCESSING)

        # ── 断点续传 ──────────────────────────────────────
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

        # ── 获取视频信息 & 字幕 ───────────────────────────
        info  = await loop.run_in_executor(None, get_video_info, req.url)
        title = info["title"]
        _update_task(task_id, title=title, message="获取字幕...")

        if cached_subtitles:
            subtitles = cached_subtitles
        else:
            subtitles = await loop.run_in_executor(None, get_subtitles, req.url)
            if not subtitles:
                raise RuntimeError("未能获取到字幕，请检查视频是否有字幕或 Whisper 是否安装")

        # ── 下载视频（供 ffmpeg 本地截帧）────────────────
        _update_task(task_id, message="下载视频（用于截图）...")
        local_video_path = await loop.run_in_executor(
            None, get_video_local_path, req.url, video_id
        )

        # ── 创建 Notion 页面（首次）──────────────────────
        if not page_id:
            page_id = await loop.run_in_executor(
                None, create_notion_page, title, req.url, token, db_id
            )

        # 持久化初始进度
        cache_manager.save_progress(video_id, {
            "status":         cache_manager.STATUS_PROCESSING,
            "page_id":        page_id,
            "subtitle_index": resume_subtitle_index,
            "subtitles":      subtitles,
            "segments":       cached_segments,
            "url":            req.url,
            "title":          title,
        })

        # ── 构建生产者-消费者管道 ─────────────────────────
        # 队列容量 = 消费者数 × 2，超出时生产者自动等待（背压）
        queue: asyncio.Queue = asyncio.Queue(maxsize=UPLOAD_WORKERS * 2)
        results: Dict[int, list] = {}
        upload_exec = ThreadPoolExecutor(max_workers=UPLOAD_WORKERS)

        # 启动消费者
        workers = [
            asyncio.create_task(
                _upload_worker(queue, page_id, token, req.url, results, upload_exec, task_id)
            )
            for _ in range(UPLOAD_WORKERS)
        ]

        # ── 生产者回调（在截帧线程中被调用）─────────────
        _gi    = [0]
        _batch: list = []

        def _on_segment(seg: dict):
            """每产生一个 segment 时被调用；凑满一组后投入队列（有背压）。"""
            _batch.append(seg)
            if len(_batch) >= COMPOSITE_SIZE:
                gi    = _gi[0]; _gi[0] += 1
                group = _batch.copy(); _batch.clear()
                # 从子线程向事件循环所在线程安全地投递，并等待入队完成（背压）
                future = asyncio.run_coroutine_threadsafe(queue.put((gi, group)), loop)
                future.result()

        # 续传：把缓存 segments 先走一遍回调，保持分组逻辑一致
        for seg in cached_segments:
            _on_segment(seg)

        _update_task(task_id, message=f"共 {len(subtitles)} 条字幕，截图并上传中...")

        # 截帧在独立线程池中运行，不阻塞事件循环，上传与截帧同时进行
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

        # 截帧完成，删除本地视频文件
        if os.path.exists(local_video_path):
            os.remove(local_video_path)

        # 冲刷不足一组的剩余 segments
        if _batch:
            gi = _gi[0]
            future = asyncio.run_coroutine_threadsafe(
                queue.put((gi, _batch.copy())), loop
            )
            future.result()

        # 等待所有已入队任务处理完毕
        await queue.join()

        # 发送哨兵，通知每个消费者退出
        for _ in range(UPLOAD_WORKERS):
            await queue.put(None)
        await asyncio.gather(*workers)

        upload_exec.shutdown(wait=False)
        screen_exec.shutdown(wait=False)

        # ── 保存最终进度（含完整 segments，供续传恢复）──
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

        # ── 按组序号合并 blocks，一次性写入 Notion ──────
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


# ─────────────────────────────────────────────
# 后台线程入口
# ─────────────────────────────────────────────

def _run_pipeline(task_id: str, req: ProcessRequest):
    """
    在独立后台线程中创建新的 asyncio 事件循环，运行异步流水线。
    FastAPI 的事件循环与这里完全隔离，互不干扰。
    """
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
    """健康检查"""
    return {"status": "ok"}


@app.post("/process")
def start_process(req: ProcessRequest):
    """
    启动处理任务，返回 task_id。
    冲突检测（三层）：
      1. 内存中有 pending / running 任务 → 409
      2. 缓存中有 processing 状态的其他视频 → 409
      3. 当前视频缓存状态为 processing 且未开启断点续传 → 409
    """
    try:
        video_id = _extract_video_id(req.url)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # 层1：内存状态
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
        # 服务重启场景：缓存显示 processing，但内存已清空
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
    """检查断点续传缓存，返回状态供前端决策"""
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
