/**
 * popup.js
 * Chrome 扩展弹窗逻辑：
 * - 检测本地服务状态
 * - 读取/保存配置
 * - 发起处理请求
 * - SSE 实时进度展示
 */

const API = "http://127.0.0.1:8000";

// ── DOM 引用 ──
const dot = document.getElementById("dot");
const serverStatusText = document.getElementById("server-status-text");
const btnStart = document.getElementById("btn-start");
const statusBox = document.getElementById("status-box");
const statusMsg = document.getElementById("status-msg");
const progressBar = document.getElementById("progress-bar");
const resultLink = document.getElementById("result-link");
const errorMsg = document.getElementById("error-msg");
const resumeBanner = document.getElementById("resume-banner");
const resumeText = document.getElementById("resume-text");
const btnClearCache = document.getElementById("btn-clear-cache");
const advancedToggle = document.getElementById("advanced-toggle");
const advancedPanel = document.getElementById("advanced-panel");
const toggleArrow = document.getElementById("toggle-arrow");

// ── 当前视频 URL ──
let currentVideoUrl = "";
let currentVideoId = "";
let activeEventSource = null;

// ─────────────────────────────────────────────
// 初始化
// ─────────────────────────────────────────────

document.addEventListener("DOMContentLoaded", async () => {
  loadSavedConfig();
  await checkServerHealth();
  await detectVideoUrl();
  setupEventListeners();
});

// ─────────────────────────────────────────────
// 配置持久化
// ─────────────────────────────────────────────

function loadSavedConfig() {
  chrome.storage.local.get(
    ["notionToken", "databaseId", "hashThreshold", "maxSentences", "maxSeconds"],
    (data) => {
      if (data.notionToken) document.getElementById("notion-token").value = data.notionToken;
      if (data.databaseId) document.getElementById("database-id").value = data.databaseId;
      if (data.hashThreshold != null) document.getElementById("hash-threshold").value = data.hashThreshold;
      if (data.maxSentences != null) document.getElementById("max-sentences").value = data.maxSentences;
      if (data.maxSeconds != null) document.getElementById("max-seconds").value = data.maxSeconds;
    }
  );
}

function cleanDatabaseId(raw) {
  // 去掉前缀（如 CC-、https://notion.so/ 等）和后缀参数（?pvs=xx）
  let id = raw.trim();
  id = id.replace(/^.*\//, "");       // 去掉 URL 路径部分
  id = id.replace(/\?.*$/, "");       // 去掉 ?pvs=xx 等参数
  id = id.replace(/^[A-Z]+-/, "");    // 去掉 CC- 等前缀
  id = id.replace(/-/g, "");          // 去掉连字符
  return id;
}

function saveConfig() {
  const rawDbId = document.getElementById("database-id").value;
  const cleanId = cleanDatabaseId(rawDbId);
  // 如果清理后不同，自动更新输入框
  if (cleanId !== rawDbId.trim()) {
    document.getElementById("database-id").value = cleanId;
  }
  chrome.storage.local.set({
    notionToken: document.getElementById("notion-token").value.trim(),
    databaseId: cleanId,
    hashThreshold: parseInt(document.getElementById("hash-threshold").value),
    maxSentences: parseInt(document.getElementById("max-sentences").value),
    maxSeconds: parseFloat(document.getElementById("max-seconds").value),
  });
}

// ─────────────────────────────────────────────
// 服务健康检查
// ─────────────────────────────────────────────

async function checkServerHealth() {
  try {
    const resp = await fetch(`${API}/health`, { signal: AbortSignal.timeout(3000) });
    if (resp.ok) {
      dot.className = "dot online";
      serverStatusText.textContent = "本地服务已连接";
      btnStart.disabled = false;  // 服务在线就启用按钮
      return true;
    }
  } catch (_) {}
  dot.className = "dot offline";
  serverStatusText.textContent = "本地服务未启动（请运行 python main.py）";
  btnStart.disabled = true;
  return false;
}

// ─────────────────────────────────────────────
// 获取当前 YouTube 视频 URL
// ─────────────────────────────────────────────

async function detectVideoUrl() {
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!tab || !tab.url || !tab.url.includes("youtube.com/watch")) {
      serverStatusText.textContent += " · 请在 YouTube 视频页面使用";
      return;
    }

    const response = await chrome.tabs.sendMessage(tab.id, { type: "GET_VIDEO_URL" });
    currentVideoUrl = response?.url || tab.url;
    currentVideoId = extractVideoId(currentVideoUrl);

    if (currentVideoId) {
      await checkResume(currentVideoId);
    }
  } catch (e) {
    console.warn("获取视频 URL 失败:", e);
  }
}

function extractVideoId(url) {
  const m = url.match(/(?:v=|youtu\.be\/)([A-Za-z0-9_-]{11})/);
  return m ? m[1] : "";
}

// ─────────────────────────────────────────────
// 断点续传检查
// ─────────────────────────────────────────────

async function checkResume(videoId) {
  try {
    const resp = await fetch(`${API}/check_resume/${videoId}`);
    const data = await resp.json();
    if (data.has_cache) {
      resumeText.textContent = `检测到未完成任务：${data.title || videoId}（已处理 ${data.subtitle_index} 条字幕）`;
      resumeBanner.classList.add("visible");
    }
  } catch (_) {}
}

// ─────────────────────────────────────────────
// 事件绑定
// ─────────────────────────────────────────────

function setupEventListeners() {
  btnStart.addEventListener("click", onStartClick);

  btnClearCache.addEventListener("click", async () => {
    if (currentVideoId) {
      await fetch(`${API}/cache/${currentVideoId}`, { method: "DELETE" });
      resumeBanner.classList.remove("visible");
    }
  });

  advancedToggle.addEventListener("click", () => {
    const isOpen = advancedPanel.classList.toggle("open");
    toggleArrow.textContent = isOpen ? "▼" : "▶";
  });
}

// ─────────────────────────────────────────────
// 开始处理
// ─────────────────────────────────────────────

async function onStartClick() {
  const token = document.getElementById("notion-token").value.trim();
  const dbId = cleanDatabaseId(document.getElementById("database-id").value);

  if (!token) { showError("请填写 Notion Token"); return; }
  if (!dbId) { showError("请填写 Database ID"); return; }
  if (!currentVideoUrl) { showError("未检测到 YouTube 视频，请在视频页面使用"); return; }

  saveConfig();
  hideError();
  resultLink.classList.remove("visible");

  const payload = {
    url: currentVideoUrl,
    notion_token: token,
    database_id: dbId,
    hash_threshold: parseInt(document.getElementById("hash-threshold").value),
    max_sentences: parseInt(document.getElementById("max-sentences").value),
    max_seconds: parseFloat(document.getElementById("max-seconds").value),
    resume: true,
  };

  btnStart.disabled = true;
  showStatus("正在提交任务...", 0);

  try {
    const resp = await fetch(`${API}/process`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });

    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      if (resp.status === 409) {
        showError(`⏳ ${err.detail || "已有任务正在运行，请等待完成后再提交"}`);
        btnStart.disabled = false;
        return;
      }
      throw new Error(err.detail || "提交失败");
    }

    const { task_id } = await resp.json();
    listenProgress(task_id);
  } catch (e) {
    showError(`提交失败: ${e.message}`);
    btnStart.disabled = false;
  }
}

// ─────────────────────────────────────────────
// SSE 进度监听
// ─────────────────────────────────────────────

function listenProgress(taskId) {
  if (activeEventSource) {
    activeEventSource.close();
  }

  const es = new EventSource(`${API}/progress/${taskId}`);
  activeEventSource = es;

  es.onmessage = (event) => {
    try {
      const data = JSON.parse(event.data);
      const { status, message, progress_current, progress_total, page_url } = data;

      const pct = progress_total > 0
        ? Math.round((progress_current / progress_total) * 100)
        : 0;

      showStatus(message || "处理中...", pct);

      if (status === "done") {
        es.close();
        activeEventSource = null;
        showStatus("完成！", 100);
        if (page_url) {
          resultLink.href = page_url;
          resultLink.classList.add("visible");
        }
        btnStart.disabled = false;
        resumeBanner.classList.remove("visible");
      } else if (status === "error") {
        es.close();
        activeEventSource = null;
        showError(message || "处理失败");
        btnStart.disabled = false;
      }
    } catch (e) {
      console.error("SSE 解析失败:", e);
    }
  };

  es.onerror = () => {
    // SSE 断开时 fallback 到轮询
    es.close();
    activeEventSource = null;
    pollStatus(taskId);
  };
}

// ─────────────────────────────────────────────
// 轮询 fallback（SSE 不可用时）
// ─────────────────────────────────────────────

async function pollStatus(taskId) {
  const interval = setInterval(async () => {
    try {
      const resp = await fetch(`${API}/status/${taskId}`);
      const data = await resp.json();
      const { status, message, progress_current, progress_total, page_url } = data;

      const pct = progress_total > 0
        ? Math.round((progress_current / progress_total) * 100)
        : 0;

      showStatus(message || "处理中...", pct);

      if (status === "done") {
        clearInterval(interval);
        showStatus("完成！", 100);
        if (page_url) {
          resultLink.href = page_url;
          resultLink.classList.add("visible");
        }
        btnStart.disabled = false;
      } else if (status === "error") {
        clearInterval(interval);
        showError(message || "处理失败");
        btnStart.disabled = false;
      }
    } catch (e) {
      clearInterval(interval);
      showError("无法连接到本地服务");
      btnStart.disabled = false;
    }
  }, 1000);
}

// ─────────────────────────────────────────────
// UI 工具函数
// ─────────────────────────────────────────────

function showStatus(msg, pct) {
  statusBox.classList.add("visible");
  statusMsg.textContent = msg;
  progressBar.style.width = `${Math.min(100, Math.max(0, pct))}%`;
}

function showError(msg) {
  errorMsg.textContent = msg;
  errorMsg.classList.add("visible");
}

function hideError() {
  errorMsg.classList.remove("visible");
  errorMsg.textContent = "";
}
