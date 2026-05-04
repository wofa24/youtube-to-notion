"""
uploader.py
将处理好的图文段上传到 Notion。
每 COMPOSITE_SIZE 帧合成一张图片，减少上传次数；并发上传，批量 append blocks。
"""

import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Dict, Optional

import httpx
from PIL import Image as _PILImage
from notion_client import Client

from config import TEMP_DIR

COMPOSITE_SIZE = 5    # 每组合并几帧截图
UPLOAD_WORKERS = 3    # 并发上传 worker 数（受 Notion 3 req/s 限制）
BLOCK_BATCH    = 100  # Notion API 每批最多 100 个 block
COMPOSITE_W    = 640  # 合成图宽度（px）；缩小以减少上传体积
COMPOSITE_Q    = 72   # 合成图 JPEG 质量


def _get_client(token: str) -> Client:
    return Client(auth=token)


def create_notion_page(
    title: str,
    video_url: str,
    token: str,
    database_id: str,
) -> str:
    """在 Notion Database 中创建新页面，返回 page_id。"""
    client = _get_client(token)
    response = client.pages.create(
        parent={"database_id": database_id},
        properties={
            "title": {"title": [{"text": {"content": title}}]},
            "URL":   {"url": video_url},
        },
    )
    return response["id"]


def upload_image_to_notion(image_path: str, token: str) -> Optional[str]:
    """
    将本地图片上传到 Notion，返回 file_upload_id。
    两步流程：
      1. POST /v1/file_uploads          → 创建会话，拿到 upload_url 和 id
      2. POST upload_url (multipart)    → 上传文件内容
    step1 遇 429 最多重试 3 次（指数退避）。
    """
    base_headers = {
        "Authorization": f"Bearer {token}",
        "Notion-Version": "2022-06-28",
    }
    filename = os.path.basename(image_path)

    try:
        with httpx.Client(timeout=60) as client:
            # step1：创建上传会话（受 Notion 3 req/s 限速，加重试）
            session = None
            for attempt in range(4):
                r = client.post(
                    "https://api.notion.com/v1/file_uploads",
                    headers={**base_headers, "Content-Type": "application/json"},
                    json={"mode": "single_part"},
                )
                if r.status_code == 429:
                    wait = 2 ** attempt
                    print(f"[uploader] step1 限速，{wait}s 后重试 (第 {attempt + 1} 次)...")
                    time.sleep(wait)
                    continue
                if r.status_code not in (200, 201):
                    print(f"[uploader] 创建上传会话失败 {r.status_code}: {r.text[:300]}")
                    return None
                session = r.json()
                break

            if session is None:
                print("[uploader] 创建上传会话多次限速，放弃")
                return None

            file_id    = session.get("id")
            upload_url = session.get("upload_url")
            if not file_id or not upload_url:
                print(f"[uploader] 上传会话响应缺少字段: {session}")
                return None

            # step2：上传文件内容（走 S3/CDN，不受 Notion 限速）
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


def _make_composite(image_paths: List[str], out_path: str) -> bool:
    """垂直拼接多张截图为一张合成图，统一缩至 COMPOSITE_W 宽，帧间 4px 灰线。"""
    try:
        imgs = []
        for p in image_paths:
            if os.path.exists(p):
                imgs.append(_PILImage.open(p).convert("RGB"))
        if not imgs:
            return False
        w   = COMPOSITE_W
        sep = 4
        resized = []
        for img in imgs:
            h = round(img.height * w / img.width)
            resized.append(img.resize((w, h), _PILImage.LANCZOS))
        total_h = sum(img.height for img in resized) + sep * (len(resized) - 1)
        composite = _PILImage.new("RGB", (w, total_h), (180, 180, 180))
        y = 0
        for img in resized:
            composite.paste(img, (0, y))
            y += img.height + sep
        composite.save(out_path, "JPEG", quality=COMPOSITE_Q)
        return True
    except Exception as e:
        print(f"[uploader] 合成图片失败: {e}")
        return False


def _build_text_blocks(text: str, start_time: float, video_url: str) -> List[Dict]:
    """构建带时间戳链接前缀的段落块（不含图片块）。"""
    import re as _re
    base_url = _re.sub(r"[&?]t=\d+", "", video_url)
    sep      = "&" if "?" in base_url else "?"
    ts_url   = f"{base_url}{sep}t={int(start_time)}"
    ts_text  = f"▶ {_format_timestamp(start_time)}  "

    chunks = [text[i:i + 2000] for i in range(0, max(len(text), 1), 2000)]
    blocks = []
    for idx, chunk in enumerate(chunks):
        rich_text = []
        if idx == 0:
            rich_text.append({
                "type": "text",
                "text": {"content": ts_text, "link": {"url": ts_url}},
                "annotations": {"color": "blue"},
            })
        rich_text.append({"type": "text", "text": {"content": chunk}})
        blocks.append({
            "object": "block",
            "type": "paragraph",
            "paragraph": {"rich_text": rich_text, "color": "gray_background"},
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
      1. 每 COMPOSITE_SIZE 帧合成一张截图 → 大幅减少 Notion 文件上传次数
      2. 并发上传合成图（UPLOAD_WORKERS 个 worker）
      3. 批量追加 blocks（每批最多 100 个，遇 429 指数退避重试）
    返回最后成功上传的段索引。
    """
    to_process = segments[start_index:]
    total      = len(segments)

    if not to_process:
        return start_index - 1

    # ── 第一步：生成合成图 ──
    groups: List[List[Dict]] = [
        to_process[i:i + COMPOSITE_SIZE]
        for i in range(0, len(to_process), COMPOSITE_SIZE)
    ]
    comp_paths: List[Optional[str]] = []
    for gi, group in enumerate(groups):
        img_paths = [seg["image_path"] for seg in group]
        out = os.path.join(TEMP_DIR, f"_comp_{page_id[:8]}_{gi:04d}.jpg")
        if _make_composite(img_paths, out):
            comp_paths.append(out)
        else:
            comp_paths.append(next((p for p in img_paths if os.path.exists(p)), None))

    # ── 第二步：并发上传合成图 ──
    if progress_callback:
        progress_callback(start_index, total, f"上传图片组 (0/{len(groups)})...")

    file_ids: List[Optional[str]] = [None] * len(groups)
    done_count = 0

    with ThreadPoolExecutor(max_workers=UPLOAD_WORKERS) as executor:
        future_to_gi = {
            executor.submit(upload_image_to_notion, path, token): gi
            for gi, path in enumerate(comp_paths) if path
        }
        for future in as_completed(future_to_gi):
            gi = future_to_gi[future]
            try:
                file_ids[gi] = future.result()
            except Exception as e:
                print(f"[uploader] 图片组 {gi} 上传异常: {e}")
            done_count += 1
            if progress_callback:
                progress_callback(
                    start_index + done_count * COMPOSITE_SIZE, total,
                    f"上传图片组 ({done_count}/{len(groups)})...",
                )

    # 清理合成图临时文件
    for path in comp_paths:
        if path and "_comp_" in path and os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass

    # ── 第三步：构建所有 blocks ──
    all_blocks: List[Dict] = []
    last_success = start_index - 1

    for gi, (group, file_id) in enumerate(zip(groups, file_ids)):
        if file_id is None:
            print(f"[uploader] 图片组 {gi} 上传失败，跳过")
            continue
        all_blocks.append({
            "object": "block",
            "type": "image",
            "image": {"type": "file_upload", "file_upload": {"id": file_id}},
        })
        for seg in group:
            all_blocks.extend(
                _build_text_blocks(seg["text"], seg.get("start", 0.0), video_url)
            )
        last_success = start_index + gi * COMPOSITE_SIZE + len(group) - 1

    # ── 第四步：批量追加 blocks（遇 429 指数退避重试）──
    client = _get_client(token)
    if progress_callback:
        progress_callback(total, total, "写入 Notion 页面...")

    for batch_start in range(0, len(all_blocks), BLOCK_BATCH):
        batch = all_blocks[batch_start:batch_start + BLOCK_BATCH]
        for attempt in range(4):
            try:
                client.blocks.children.append(block_id=page_id, children=batch)
                break
            except Exception as e:
                is_rate_limit = "rate_limited" in str(e).lower() or "429" in str(e)
                if is_rate_limit and attempt < 3:
                    wait = 2 ** attempt
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
