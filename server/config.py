import os

# Notion 配置（可通过环境变量覆盖）
NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "")
NOTION_DATABASE_ID = os.environ.get("NOTION_DATABASE_ID", "")

# 截图配置
SCREENSHOT_WIDTH = 800          # 截图宽度（px）
SCREENSHOT_OFFSET = 0.2         # 截图时间偏移（秒），取字幕结束前 N 秒

# 去重配置
DEFAULT_HASH_THRESHOLD = 10     # imagehash 差异阈值，越小越严格

# 强制分段配置
DEFAULT_MAX_SENTENCES = 8       # 同一画面最多累积句数
DEFAULT_MAX_SECONDS = 30        # 同一画面最多累积秒数

# 服务配置
SERVER_HOST = "127.0.0.1"
SERVER_PORT = 8000

# 临时文件目录
TEMP_DIR = os.path.join(os.path.dirname(__file__), "temp")
CACHE_DIR = os.path.join(os.path.dirname(__file__), "cache")

# ffmpeg 路径（如果 PATH 中没有，填写完整路径）
FFMPEG_PATH = os.environ.get(
    "FFMPEG_PATH",
    r"C:\Users\air\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-8.1-full_build\bin\ffmpeg.exe"
)
