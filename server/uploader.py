"""
uploader.py
将处理好的图文段上传到 Notion。
使用 Notion files.upload API 上传图片，然后 append blocks。
"""

import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Dict, Optional

import httpx
from notion_client import Client

from config import NOTION_TOKEN, NOTION_DATABASE_ID


def _get_client(token: str) -> Client:
    return Client(auth=token)


def create_notion_page(
    title: str,
    video_url: str,
    token: str,
    database_id: str,
) -> str:
    """
    在 Notion Database 中创建新页面，返回 page_id。
    """
    client = _get_client(token)
    response = client.pages.create(
        parent={"database_id": database_id},
        properties={
            "title": {
                "title": [{"text": {"content": title}}]
            },
            "URL": {
                "url": video_url
            },
        },
    )
    return response["id"]


def upload_image_to_notion(
    image_path: str,
    token: str,
) -> Optional[str]:
    """
    将本地图片上传到 Notion，返回 file_upload_id。
    Notion files upload 两步流程：
      1. POST /v1/file_uploads          → 创建上传会话，拿到 upload_url 和 id
      2. POST upload_url (multipart)    → 把文件内容上传上去
    """
    base_headers = {
        "Authorization": f"Bearer {token}",
        "Notion-Version": "2022-06-28",
    }
    filename = os.path.basename(image_path)

    try:
        with httpx.Client(timeout=60) as client:
            # ── 第一步：创建上传会话 ──
            step1 = client.post(
                "https://api.notion.com/v1/file_uploads",
                headers={**base_headers, "Content-Type": "application/json"},
                json={"mode": "single_part"},
            )
            if step1.status_code not in (200, 201):
                print(f"[uploader] 创建上传会话失败 {step1.status_code}: {step1.text[:300]}")
                return None

            session = step1.json()
            file_id = session.get("id")
            upload_url = session.get("upload_url")

            if not file_id or not upload_url:
                print(f"[uploader] 上传会话响应缺少字段: {session}")
                return None

            # ── 第二步：上传文件内容（multipart/form-data，由 httpx 自动设置 Content-Type）──
            with open(image_path, "rb") as f:
                step2 = client.post(
                    upload_url,
                    headers=base_headers,
                    files={"file": (filename, f, "image/jpeg")},
                )
            if step2.status_code not in (200, 201):
                print(f"[uploader] 文件上传失败 {step2.status_code}: {step2.text[:300]}")
                return None

        return file_id

    except Exception as e:
        print(f"[uploader] 图片上传异常: {e}")
        return None


def _format_timestamp(seconds: float) -> str:
    """将秒数转为 m:ss 或 h:mm:ss 格式。"""
    s = int(seconds)
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _build_blocks(text: str, file_upload_id: str, start_time: float, video_url: str) -> List[Dict]:
    """构建一个段的图片块 + 文本块列表（文本超 2000 字符自动拆分）。
    第一个文本块前缀为可点击的时间戳链接。
    """
    # YouTube 时间戳链接：移除已有的 t= 参数再追加新的
    import re as _re
    base_url = _re.sub(r"[&?]t=\d+", "", video_url)
    sep = "&" if "?" in base_url else "?"
    ts_url = f"{base_url}{sep}t={int(start_time)}"
    ts_text = f"▶ {_format_timestamp(start_time)}  "

    blocks = [
        {
            "object": "block",
            "type": "image",
            "image": {
                "type": "file_upload",
                "file_upload": {"id": file_upload_id},
            },
        },
    ]
    chunks = [text[i:i + 2000] for i in range(0, max(len(text), 1), 2000)]
    for idx, chunk in enumerate(chunks):
        rich_text = []
        if idx == 0:
            # 第一段前缀加时间戳链接
            rich_text.append({
                "type": "text",
                "text": {"content": ts_text, "link": {"url": ts_url}},
                "annotations": {"color": "blue"},
            })
        rich_text.append({"type": "text", "text": {"content": chunk}})
        blocks.append({
            "object": "block",
            "type": "paragraph",
            "paragraph": {
                "rich_text": rich_text,
                "color": "gray_background",
            },
        })
    return blocks


def upload_segments(
    page_id: str,
    segments: List[Dict],
    token: str,
    progress_callback=None,
    start_index: int = 0,
    video_url: str = "",
) -> int:
    """
    上传所有段到 Notion：
      1. 并发上传图片（4 个 worker）
      2. 批量追加 blocks（每批最多 100 个，Notion API 上限）
    返回最后成功上传的段索引。
    """
    UPLOAD_WORKERS = 4
    BLOCK_BATCH   = 100

    to_process = segments[start_index:]
    total = len(segments)

    if not to_process:
        return start_index - 1

    # ── 第一步：并发上传图片 ──
    if progress_callback:
        progress_callback(start_index, total, f"并发上传图片 (0/{len(to_process)})...")

    file_ids: List[Optional[str]] = [None] * len(to_process)
    done_count = 0

    with ThreadPoolExecutor(max_workers=UPLOAD_WORKERS) as executor:
        future_to_idx = {
            executor.submit(upload_image_to_notion, seg["image_path"], token): i
            for i, seg in enumerate(to_process)
        }
        for future in as_completed(future_to_idx):
            i = future_to_idx[future]
            try:
                file_ids[i] = future.result()
            except Exception as e:
                print(f"[uploader] 段 {start_index + i} 图片上传异常: {e}")
            done_count += 1
            if progress_callback:
                progress_callback(
                    start_index + done_count, total,
                    f"并发上传图片 ({done_count}/{len(to_process)})...",
                )

    # ── 第二步：构建所有 blocks ──
    all_blocks: List[Dict] = []
    last_success = start_index - 1

    for i, (seg, file_id) in enumerate(zip(to_process, file_ids)):
        abs_i = start_index + i
        if file_id is None:
            print(f"[uploader] 段 {abs_i} 图片上传失败，跳过")
            continue
        all_blocks.extend(_build_blocks(seg["text"], file_id, seg.get("start", 0.0), video_url))
        last_success = abs_i

    # ── 第三步：批量追加 blocks（遇 429 指数退避重试）──
    client = _get_client(token)
    if progress_callback:
        progress_callback(total, total, "写入 Notion 页面...")

    for batch_start in range(0, len(all_blocks), BLOCK_BATCH):
        batch = all_blocks[batch_start:batch_start + BLOCK_BATCH]
        for attempt in range(4):  # 最多重试 3 次：等待 1s / 2s / 4s
            try:
                client.blocks.children.append(block_id=page_id, children=batch)
                break
            except Exception as e:
                is_rate_limit = "rate_limited" in str(e).lower() or "429" in str(e)
                if is_rate_limit and attempt < 3:
                    wait = 2 ** attempt  # 1s, 2s, 4s
                    print(f"[uploader] Notion 限速，{wait}s 后重试 (第 {attempt + 1} 次)...")
                    time.sleep(wait)
                else:
                    print(f"[uploader] 批量追加失败 (block {batch_start}~{batch_start + len(batch)}): {e}")
                    break

    return last_success


def get_page_url(page_id: str) -> str:
    """将 page_id 转换为 Notion 页面 URL"""
    clean_id = page_id.replace("-", "")
    return f"https://notion.so/{clean_id}"
