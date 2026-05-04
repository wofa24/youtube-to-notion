# YouTube to Notion

将 YouTube 视频的字幕和截图自动整理，上传到 Notion 数据库。每个"画面段"= 一张截图 + 该画面期间所有字幕合并的文本，自动去重相似画面，支持断点续传。

---

## 效果预览

```
[截图1 - 800px宽]
字幕文本：Hello, today we're going to talk about...
This is the second sentence with same screen.

[截图2 - 画面切换后]
字幕文本：Now let's look at the code...
```

---

## 环境要求

| 工具 | 说明 |
|------|------|
| Python 3.9+ | 运行后端服务 |
| ffmpeg | 视频截图（已通过 winget 安装） |
| Chrome 浏览器 | 运行扩展 |

---

## 快速开始

### 第一步：安装 Python 依赖

打开 PowerShell，运行：

```powershell
cd D:\CC\server
pip install fastapi uvicorn yt-dlp imagehash Pillow notion-client sse-starlette httpx openai-whisper
```

> **说明：** `openai-whisper` 用于视频没有字幕时自动转录，安装时间较长（需要下载 PyTorch），请耐心等待。

---

### 第二步：配置 ffmpeg 路径

ffmpeg 已通过 winget 安装，但路径需要在代码里指定。打开 `server/config.py`，找到 `FFMPEG_PATH` 这一行，确认路径正确：

```python
FFMPEG_PATH = os.environ.get(
    "FFMPEG_PATH",
    r"C:\Users\air\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-8.1-full_build\bin\ffmpeg.exe"
)
```

如果你的路径不同，可以在 PowerShell 里查找：

```powershell
Get-ChildItem -Path "C:\Users\$env:USERNAME\AppData\Local\Microsoft\WinGet\Packages" -Recurse -Filter "ffmpeg.exe" | ForEach-Object { $_.FullName }
```

把找到的路径替换进 `config.py`。

---

### 第三步：配置 Notion

**3.1 创建 Integration（获取 Token）**

1. 打开 [https://www.notion.so/my-integrations](https://www.notion.so/my-integrations)
2. 点击 **"+ New integration"**
3. 填写名称（如 "YouTube Clipper"），选择你的 Workspace
4. 点击 Submit
5. 在 **"安装访问令牌"** 处点击 **"显示"**，复制 `ntn_xxxxxxxxxx` 格式的 Token

**3.2 创建 Database**

1. 在 Notion 里新建一个页面
2. 输入 `/table`，选择 **"Table - Full page"**（全页表格）
3. 表格默认有 `Name` 列，再添加一列：点 `+` → 选择 **URL** 类型 → 命名为 `URL`
4. 点击右上角 **`⤢`** 按钮，把表格单独打开为全页
5. 复制浏览器地址栏的 URL，格式类似：
   ```
   https://www.notion.so/xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
   ```
   `notion.so/` 后面那串 32 位字符就是 **Database ID**

**3.3 连接 Integration 到 Database**

1. 在 Database 页面右上角点 **`...`**
2. 点 **"Add connections"**（连接）
3. 找到你创建的 Integration，点击确认

---

### 第四步：启动本地服务

```powershell
cd D:\CC\server
python main.py
```

看到以下输出说明启动成功：

```
[main] 服务启动于 http://127.0.0.1:8000
INFO:     Started server process
INFO:     Uvicorn running on http://127.0.0.1:8000
```

**保持这个终端窗口开着，不要关闭。**

---

### 第五步：安装 Chrome 扩展

1. 打开 Chrome，地址栏输入 `chrome://extensions/`
2. 右上角开启 **"开发者模式"**（Developer mode）
3. 点击 **"加载已解压的扩展程序"**（Load unpacked）
4. 选择 `D:\CC\extension` 文件夹
5. 扩展图标出现在 Chrome 工具栏

---

### 第六步：使用

1. 打开任意 YouTube 视频页面（URL 必须包含 `watch?v=`）
2. 点击工具栏中的 **YouTube to Notion** 扩展图标
3. 弹窗顶部显示绿点 **"本地服务已连接"**
4. 填入 **Notion Token**（`ntn_xxxxxxxxxx` 格式）
5. 填入 **Database ID**（32 位字符，扩展会自动清理多余的前缀和参数）
6. 点击 **"开始处理"**
7. 等待进度条完成（视频越长耗时越久，1 小时视频约 10-15 分钟）
8. 完成后点击绿色链接跳转到 Notion 页面

> **首次使用提示：** Token 和 Database ID 填写一次后会自动保存，下次打开扩展无需重新填写。

---

## 高级设置

点击扩展弹窗中的 **"高级设置"** 展开：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| 去重阈值 | 10 | imagehash 差异值（0=完全相同，64=完全不同）。值越小，对画面变化越敏感 |
| 最大句数 | 8 | 同一画面最多累积几句字幕后强制分段 |
| 强制分段秒数 | 30 | 同一画面超过多少秒后强制分段（防止静止画面堆积太多字幕） |

---

## 断点续传

处理过程中如果中断（关闭终端、网络断开等），重新打开同一个视频页面点击扩展，会自动检测到上次进度并显示提示横幅。

- 点 **"开始处理"** → 从上次中断处继续
- 点 **"清除重来"** → 删除缓存，从头开始

---

## 项目结构

```
youtube-to-notion/
├── extension/              # Chrome 扩展
│   ├── manifest.json       # 扩展配置
│   ├── popup.html          # 弹窗界面
│   ├── popup.js            # 弹窗逻辑（配置保存、进度展示）
│   ├── content.js          # 注入 YouTube 页面，获取视频 URL
│   └── icons/              # 扩展图标
├── server/                 # Python 后端
│   ├── main.py             # FastAPI 服务入口（端口 8000）
│   ├── fetcher.py          # yt-dlp 获取字幕 + 视频流地址
│   ├── processor.py        # ffmpeg 截图 + imagehash 去重
│   ├── uploader.py         # Notion API 上传图片和文本块
│   ├── cache_manager.py    # 断点续传（JSON 缓存）
│   ├── config.py           # 所有配置项（ffmpeg 路径、阈值等）
│   └── requirements.txt    # Python 依赖列表
└── README.md
```

---

## 常见问题

**Q: 扩展显示"本地服务未启动"**
A: 确保已在 `server/` 目录运行 `python main.py`，且终端没有报错。服务必须保持运行。

**Q: 错误 `[WinError 2] 系统找不到指定的文件`**
A: ffmpeg 路径配置问题。在 PowerShell 运行以下命令找到 ffmpeg 位置，然后更新 `server/config.py` 中的 `FFMPEG_PATH`：
```powershell
Get-ChildItem -Path "C:\Users\$env:USERNAME" -Recurse -Filter "ffmpeg.exe" -ErrorAction SilentlyContinue | ForEach-Object { $_.FullName }
```

**Q: 错误 `No module named 'whisper'`**
A: 视频没有字幕，触发了 Whisper 转录但未安装。运行：`pip install openai-whisper`

**Q: 错误 `is a page, not a database`**
A: Database ID 填的是普通页面的 ID。需要在 Notion 里创建一个 Table（表格），然后把表格单独打开为全页，复制 URL 里的 ID。

**Q: 错误 `401 Unauthorized`**
A: Notion Token 错误，或者 Integration 没有被邀请进 Database 所在页面。检查 Token 格式（应为 `ntn_` 开头），并确认已在 Database 页面的 `...` → `Add connections` 里添加了 Integration。

**Q: 按钮是灰色的**
A: 本地服务未启动，或者当前不在 YouTube 视频页面（URL 需要包含 `watch?v=`）。

**Q: 处理速度很慢**
A: 正常现象。每条字幕需要截一张图，1 小时视频约有 300-500 条字幕，截图 + 上传合计约 10-20 分钟。可以让它在后台跑，不需要盯着看。
