"""
uploader.py
将处理好的图文段上传到 Notion。

图片优先上传到 catbox.moe（匿名外部图床，无速率限制），失败时降级到
Notion 自带文件上传。外部图床方式可绕开 Notion 3 req/s 限制，速度约
快 5–10 倍。
"""

import os
import time
from typing import List, Dict, Optional, Tuple

import httpx
from PIL import Image as _PILImage
from notion_client import Client

from config import TEMP_DIR

COMPOSITE_SIZE     = 5    # 每组合并几帧截图
UPLOAD_WORKERS     = 6    # 线程池 worker 数（略大于 UPLOAD_CONCURRENCY，保证 CPU/IO 流水线满载）
UPLOAD_CONCURRENCY = 3    # asyncio.Semaphore 限制的最大并发上传数（对齐 Notion 3 req/s）
BLOCK_BATCH        = 100  # Notion API 每批最多 100 个 block
COMPOSITE_W        = 1280 # 合成图宽度（px）；1280 在清晰度与体积间取最优平衡
COMPOSITE_Q        = 80   # WebP 质量（0-100，80 ≈ JPEG 85，体积缩减约 30%）


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


# ─────────────────────────────────────────────
# 图片上传（外部图床优先，Notion 降级）
# ─────────────────────────────────────────────

def _upload_to_catbox(image_path: str) -> Optional[str]:
    """
    匿名上传图片到 catbox.moe，返回公开 HTTPS URL。
    无需账号、无速率限制，适合批量并发上传。
    """
    try:
        with httpx.Client(timeout=30) as c:
            with open(image_path, "rb") as f:
                r = c.post(
                    "https://catbox.moe/user/api.php",
                    data={"reqtype": "fileupload"},
                    files={"fileToUpload": (os.path.basename(image_path), f, "image/webp")},
                )
            url = r.text.strip()
            if r.status_code == 200 and url.startswith("https://"):
                return url
            print(f"[uploader] catbox 失败: {r.status_code} {r.text[:100]}")
    except Exception as e:
        print(f"[uploader] catbox 异常: {e}")
    return None


def _upload_to_notion(image_path: str, token: str) -> Optional[str]:
    """
    将本地图片上传到 Notion，返回 file_upload_id（降级方案）。
    step1 遇 429 最多重试 3 次（指数退避）。
    """
    base_headers = {
        "Authorization": f"Bearer {token}",
        "Notion-Version": "2022-06-28",
    }
    filename = os.path.basename(image_path)

    try:
        with httpx.Client(timeout=60) as client:
            session = None
            for attempt in range(4):
                r = client.post(
                    "https://api.notion.com/v1/file_uploads",
                    headers={**base_headers, "Content-Type": "application/json"},
                    json={"mode": "single_part"},
                )
                if r.status_code == 429:
                    wait = 2 ** attempt
                    print(f"[uploader] Notion step1 限速，{wait}s 后重试...")
                    time.sleep(wait)
                    continue
                if r.status_code not in (200, 201):
                    print(f"[uploader] 创建上传会话失败 {r.status_code}: {r.text[:300]}")
                    return None
                session = r.json()
                break

            if session is None:
                return None

            file_id    = session.get("id")
            upload_url = session.get("upload_url")
            if not file_id or not upload_url:
                return None

            with open(image_path, "rb") as f:
                step2 = client.post(
                    upload_url,
                    headers=base_headers,
                    files={"file": (filename, f, "image/webp")},
                )
            if step2.status_code not in (200, 201):
                print(f"[uploader] Notion 文件上传失败 {step2.status_code}: {step2.text[:300]}")
                return None

        return file_id

    except Exception as e:
        print(f"[uploader] Notion 上传异常: {e}")
        return None


def _upload_image(image_path: str, token: str) -> Tuple[Optional[str], Optional[str]]:
    """
    上传图片，优先 catbox（快），失败降级 Notion。
    返回 (kind, value)：
      kind='external'    value=URL
      kind='file_upload' value=file_id
      kind=None          失败
    """
    url = _upload_to_catbox(image_path)
    if url:
        return ("external", url)
    print("[uploader] catbox 不可用，降级到 Notion 上传...")
    fid = _upload_to_notion(image_path, token)
    if fid:
        return ("file_upload", fid)
    return (None, None)


def _image_block(kind: Optional[str], value: Optional[str]) -> Optional[Dict]:
    """根据上传结果构建 Notion image block。"""
    if kind == "external" and value:
        return {
            "object": "block", "type": "image",
            "image": {"type": "external", "external": {"url": value}},
        }
    if kind == "file_upload" and value:
        return {
            "object": "block", "type": "image",
            "image": {"type": "file_upload", "file_upload": {"id": value}},
        }
    return None


# ─────────────────────────────────────────────
# 合成图 & 文本块
# ─────────────────────────────────────────────

def _make_composite(image_paths: List[str], out_path: str) -> bool:
    """垂直拼接多张截图，统一缩至 COMPOSITE_W 宽，帧间 4px 灰线，输出 WebP。"""
    try:
        imgs = []
        for p in image_paths:
            if os.path.exists(p):
                imgs.append(_PILImage.open(p).convert("RGB"))
        if not imgs:
            return False
        w, sep = COMPOSITE_W, 4
        resized = [img.resize((w, round(img.height * w / img.width)), _PILImage.LANCZOS)
                   for img in imgs]
        total_h = sum(img.height for img in resized) + sep * (len(resized) - 1)
        canvas = _PILImage.new("RGB", (w, total_h), (180, 180, 180))
        y = 0
        for img in resized:
            canvas.paste(img, (0, y))
            y += img.height + sep
        canvas.save(out_path, "WEBP", quality=COMPOSITE_Q, method=4)
        return True
    except Exception as e:
        print(f"[uploader] 合成图片失败: {e}")
        return False


def _format_timestamp(seconds: float) -> str:
    s = int(seconds)
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _build_text_blocks(text: str, start_time: float, video_url: str) -> List[Dict]:
    """构建带时间戳链接前缀的段落块。"""
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


# ─────────────────────────────────────────────
# 公开上传入口（两步解耦：生成合图 → 上传）
# ─────────────────────────────────────────────

def make_group_composite(group: List[Dict], gi: int, page_id_prefix: str) -> Optional[str]:
    """
    为一组 segment 生成合成图（WebP）。
    调用方（通常是截帧线程）完成后立即将路径投入上传队列。
    失败时 fallback 到组内第一张有效截图路径。
    """
    img_paths = [seg["image_path"] for seg in group]
    out = os.path.join(TEMP_DIR, f"_comp_{page_id_prefix}_{gi:04d}.webp")
    if _make_composite(img_paths, out):
        return out
    return next((p for p in img_paths if os.path.exists(p)), None)


def upload_and_build(
    comp_path: str,
    group: List[Dict],
    token: str,
    gi: int,
    video_url: str,
) -> List[Dict]:
    """
    上传合成图并返回对应的 Notion blocks 列表。
    在 ThreadPoolExecutor 中被 asyncio 协程调用；
    Semaphore 在调用方控制，本函数只负责单次上传。
    上传完成后自动清理合成图临时文件。
    """
    kind, value = _upload_image(comp_path, token)

    if "_comp_" in comp_path and os.path.exists(comp_path):
        try:
            os.remove(comp_path)
        except OSError:
            pass

    blk = _image_block(kind, value)
    if blk is None:
        print(f"[uploader] 组 {gi}: 图片上传失败，跳过")
        return []

    blocks: List[Dict] = [blk]
    for seg in group:
        blocks.extend(_build_text_blocks(seg["text"], seg.get("start", 0.0), video_url))
    return blocks


def upload_group_and_build(
    group: List[Dict],
    page_id: str,
    token: str,
    gi: int,
    video_url: str,
) -> List[Dict]:
    """合图 + 上传的一体化入口（供旧调用路径兼容）。"""
    comp_path = make_group_composite(group, gi, page_id[:8])
    if not comp_path:
        print(f"[uploader] 组 {gi}: 无有效图片，跳过")
        return []
    return upload_and_build(comp_path, group, token, gi, video_url)


def append_blocks(page_id: str, blocks: List[Dict], token: str):
    """批量追加 blocks 到 Notion 页面，遇 429 指数退避重试。"""
    if not blocks:
        return
    client = _get_client(token)
    for batch_start in range(0, len(blocks), BLOCK_BATCH):
        batch = blocks[batch_start:batch_start + BLOCK_BATCH]
        for attempt in range(4):
            try:
                client.blocks.children.append(block_id=page_id, children=batch)
                break
            except Exception as e:
                if ("rate_limited" in str(e).lower() or "429" in str(e)) and attempt < 3:
                    wait = 2 ** attempt
                    print(f"[uploader] Notion 限速，{wait}s 后重试...")
                    time.sleep(wait)
                else:
                    print(f"[uploader] 批量追加失败: {e}")
                    break


def get_page_url(page_id: str) -> str:
    clean_id = page_id.replace("-", "")
    return f"https://notion.so/{clean_id}"
