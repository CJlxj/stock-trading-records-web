/* 首屏主题引导：必须在样式生效前同步执行，避免白闪或黑闪。
   webapp/server.py 的 CSP 是 script-src 'self'，内联脚本会被拦截，所以单独成文件。
   只做一件事：把已保存的手动选择写回 <html data-theme>。
   未手动选择（auto）时不写属性，由 CSS 的 prefers-color-scheme 跟随系统。 */
(() => {
  "use strict";
  const KEY = "stockRules.web.v1.theme";
  let mode = "";
  try {
    mode = window.localStorage.getItem(KEY) || "";
  } catch (error) {
    mode = "";
  }
  if (mode === "light" || mode === "dark") {
    document.documentElement.dataset.theme = mode;
  }
})();
