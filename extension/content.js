/**
 * content.js
 * 注入 YouTube 页面，响应 popup 的消息请求，返回当前视频 URL。
 */

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message.type === "GET_VIDEO_URL") {
    const url = window.location.href;
    sendResponse({ url });
  }
  return true;
});
