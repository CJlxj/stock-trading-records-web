// Web platform primitives shared by the four feature modules.
  const PanelCore = globalThis.StockPanelCore;
  if (!PanelCore) throw new Error("共享面板合同未加载，请刷新页面。");
  const API = PanelCore.apiFor("web");
  const RULE_DRAFT_STORAGE_KEY = "stockRules.web.v1.ruleDraft";
  const CUSTOM_RULE_DRAFT_STORAGE_KEY = "stockRules.web.v1.customRuleDraft";
  const TRADE_DRAFT_STORAGE_KEY = "stockRules.web.v1.tradeDraft";
  const TRADE_DRAFT_SCHEMA = 1;
  const THEME_STORAGE_KEY = "stockRules.web.v1.theme";
  const THEME_MODES = [
    { mode: "auto", label: "跟随系统", glyph: "◐" },
    { mode: "light", label: "白天", glyph: "☀" },
    { mode: "dark", label: "夜晚", glyph: "☾" },
  ];
  // 与 styles.css 的 --bg 两套取值保持一致。
  const THEME_COLORS = { light: "#f7f4ed", dark: "#1e1c19" };

  const state = {
    csrfToken: "",
    bootstrap: null,
    dataset: null,
    rules: null,
    screening: null,
    trades: null,
    catalog: null,
    catalogSearch: null,
    tradesLoaded: false,
    activeView: "screen",
    ruleDraftRefs: new Set(),
    secondaryRuleDraftRefs: new Set(),
    rulePristineSignature: "",
    ruleApplyRequestId: "",
    ruleEditingRef: "",
    ruleEditingStage: "primary",
    rulesSurface: "sets",
    recordsSurface: "home",
    tradeDraftMeta: null,
    tradeDraftTimer: 0,
    highlightedRecordId: "",
    ruleEditorOpener: null,
    pickerSession: null,
    screeningBusy: false,
    screeningInputsUncertain: false,
    ruleInputsUncertain: false,
    dataUpdateBusy: false,
    marketImportBusy: false,
    catalogSyncBusy: false,
    catalogSearchSerial: 0,
    stockAddBusy: new Set(),
    tradeSymbolActiveIndex: -1,
    rulesSavingBusy: false,
    tradeSavingBusy: false,
    tradesLoading: false,
    dataDialogOpener: null,
    ruleDialogOpener: null,
    ruleDialogInitialSignature: "",
    confirmDialogOpener: null,
    libraryManageMode: false,
    customRuleCreateBusy: false,
    customRuleDeleteBusy: false,
    customRuleCreateRequestId: "",
    customRuleCreatePendingReceipt: false,
    customRuleFocusRef: "",
    selectedRuleSetId: "",
    selectedRuleSetVersion: null,
    selectedRuleSetExact: null,
    ruleEditorOpen: false,
    ruleDetailMode: "view",
    ruleSetDraftKind: "existing",
    ruleSetDraftName: "",
    ruleSearchQuery: "",
    ruleLibraryOpenGroups: new Set(),
    ruleLibraryFocusGroupId: "",
    dataStatusRefreshSerial: 0,
    dataStatusLastRequestedAt: 0,
    ruleSavePendingVerification: null,
    ruleOperation: { phase: "idle", token: 0 },
    ruleActivationTargetId: "",
    themeMode: "auto",
  };

  let toastTimer = null;

  const byId = (id) => document.getElementById(id);
  const clean = (value) => String(value ?? "").trim();
  const escapeHtml = (value) => clean(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");

  function safeStorageGet(key) {
    try {
      return localStorage.getItem(key);
    } catch (_error) {
      return null;
    }
  }

  function safeStorageSet(key, value) {
    try {
      localStorage.setItem(key, value);
      return true;
    } catch (_error) {
      return false;
    }
  }

  function safeStorageRemove(key) {
    try {
      localStorage.removeItem(key);
    } catch (_error) {
      // 当前会话仍可继续编辑。
    }
  }

  function darkMediaQuery() {
    return window.matchMedia?.("(prefers-color-scheme: dark)") || null;
  }

  function resolvedTheme() {
    if (state.themeMode === "light" || state.themeMode === "dark") return state.themeMode;
    return darkMediaQuery()?.matches ? "dark" : "light";
  }

  function applyTheme() {
    const root = document.documentElement;
    if (state.themeMode === "auto") delete root.dataset.theme;
    else root.dataset.theme = state.themeMode;
    document.querySelector('meta[name="theme-color"]')
      ?.setAttribute("content", THEME_COLORS[resolvedTheme()]);
    const current = THEME_MODES.find((entry) => entry.mode === state.themeMode) || THEME_MODES[0];
    const button = byId("themeToggleButton");
    if (!button) return;
    const label = `显示模式：${current.label}`;
    button.setAttribute("aria-label", label);
    button.title = label;
    button.querySelector(".theme-toggle-glyph").textContent = current.glyph;
  }

  function cycleTheme() {
    const index = THEME_MODES.findIndex((entry) => entry.mode === state.themeMode);
    const next = THEME_MODES[(index + 1) % THEME_MODES.length];
    state.themeMode = next.mode;
    if (next.mode === "auto") safeStorageRemove(THEME_STORAGE_KEY);
    else safeStorageSet(THEME_STORAGE_KEY, next.mode);
    applyTheme();
    showToast(`显示模式：${next.label}`);
  }

  function initializeTheme() {
    const saved = clean(safeStorageGet(THEME_STORAGE_KEY));
    state.themeMode = saved === "light" || saved === "dark" ? saved : "auto";
    applyTheme();
    darkMediaQuery()?.addEventListener("change", () => {
      if (state.themeMode === "auto") applyTheme();
    });
  }


  function formatMoney(value) {
    const number = Number(value);
    return Number.isFinite(number)
      ? new Intl.NumberFormat("zh-CN", {
        style: "currency",
        currency: "CNY",
        minimumFractionDigits: 2,
      }).format(number)
      : "—";
  }

  // 成本价按台账落盘的 4 位小数原样显示：四舍五入成 2 位会让屏幕上的数字
  // 对不上 records/my_trades.csv 里的那一笔。
  function formatCost(value) {
    const number = Number(value);
    return Number.isFinite(number)
      ? new Intl.NumberFormat("zh-CN", {
        style: "currency",
        currency: "CNY",
        minimumFractionDigits: 2,
        maximumFractionDigits: 4,
      }).format(number)
      : "—";
  }

  function formatDateTime(value) {
    if (!value) return "—";
    const parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) return clean(value);
    return new Intl.DateTimeFormat("zh-CN", {
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    }).format(parsed);
  }

  function newRequestId() {
    if (window.crypto?.randomUUID) return window.crypto.randomUUID().replaceAll("-", "");
    return `entry_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 10)}`;
  }

  function showToast(message, isError = false) {
    if (document.querySelector("dialog[open]")) return;
    const toast = byId("toast");
    window.clearTimeout(toastTimer);
    toast.textContent = message;
    toast.classList.toggle("is-error", isError);
    toast.classList.add("is-visible");
    toastTimer = window.setTimeout(() => toast.classList.remove("is-visible"), 3200);
  }

  // 所有确认与改名共用这一个居中弹窗，替掉浏览器原生 confirm/prompt——
  // 原生弹窗贴在窗口顶端，位置和样式都不受页面控制。
  // 返回 false 表示取消；需要输入时返回填好的字符串，取消返回 null。
  function askConfirm({
    title = "确认操作",
    message = "",
    confirmLabel = "确定",
    cancelLabel = "取消",
    danger = false,
    input = null,
    validate = null,
  } = {}) {
    const dialog = byId("confirmDialog");
    const field = byId("confirmDialogField");
    const inputEl = byId("confirmDialogInput");
    const errorEl = byId("confirmDialogError");
    const accept = byId("confirmDialogAccept");
    byId("confirmDialogTitle").textContent = title;
    byId("confirmDialogMessage").textContent = message;
    byId("confirmDialogMessage").hidden = !message;
    accept.textContent = confirmLabel;
    accept.classList.toggle("is-danger", Boolean(danger));
    byId("confirmDialogCancel").textContent = cancelLabel;
    const wantsInput = input !== null;
    field.hidden = !wantsInput;
    inputEl.value = wantsInput ? clean(input) : "";
    // 校验提示属于输入框，开合时先清空，别把上一次的错误留在弹窗里。
    const showError = (text) => {
      errorEl.textContent = text;
      errorEl.hidden = !text;
      inputEl.setAttribute("aria-invalid", text ? "true" : "false");
    };
    showError("");

    return new Promise((resolve) => {
      let settled = false;
      const finish = (value) => {
        if (settled) return;
        settled = true;
        dialog.removeEventListener("cancel", onCancel);
        dialog.removeEventListener("close", onClose);
        accept.removeEventListener("click", onAccept);
        byId("confirmDialogCancel").removeEventListener("click", onCancel);
        inputEl.removeEventListener("keydown", onKey);
        inputEl.removeEventListener("input", onInput);
        showError("");
        if (dialog.open) dialog.close();
        const opener = state.confirmDialogOpener;
        state.confirmDialogOpener = null;
        if (opener?.isConnected) opener.focus({ preventScroll: true });
        resolve(value);
      };
      const onAccept = () => {
        if (!wantsInput) return finish(true);
        const value = clean(inputEl.value);
        // 只有点「保存」才校验：留空或不合法就地提示，弹窗不关，请求也不发。
        const problem = value
          ? (validate ? clean(validate(value)) : "")
          : "名称不能为空。";
        if (problem) {
          showError(problem);
          inputEl.focus();
          return;
        }
        finish(value);
      };
      const onInput = () => showError("");
      const onCancel = (event) => {
        event?.preventDefault?.();
        finish(wantsInput ? null : false);
      };
      const onClose = () => finish(wantsInput ? null : false);
      const onKey = (event) => {
        if (event.key === "Enter") {
          event.preventDefault();
          onAccept();
        }
      };
      state.confirmDialogOpener = document.activeElement;
      accept.addEventListener("click", onAccept);
      byId("confirmDialogCancel").addEventListener("click", onCancel);
      dialog.addEventListener("cancel", onCancel);
      dialog.addEventListener("close", onClose);
      inputEl.addEventListener("keydown", onKey);
      inputEl.addEventListener("input", onInput);
      dialog.showModal();
      (wantsInput ? inputEl : accept).focus({ preventScroll: true });
      if (wantsInput) inputEl.select();
    });
  }

  function setFeedback(id, message, isError = false, isSuccess = false, isWarning = false) {
    const element = byId(id);
    element.textContent = message;
    if (["screenFeedback", "ruleFeedback", "ruleSetListFeedback", "customRuleFeedback"].includes(id)) {
      element.hidden = !clean(message);
    }
    element.classList.toggle("is-error", isError);
    element.classList.toggle("is-success", isSuccess && !isError);
    element.classList.toggle("is-warning", isWarning && !isError && !isSuccess);
  }

  function setFormLocked(form, locked, excludedIds = []) {
    if (!form) return;
    if (locked) form.setAttribute("aria-busy", "true");
    else form.removeAttribute("aria-busy");
    const excluded = new Set(excludedIds);
    form.querySelectorAll("button, input, select, textarea").forEach((control) => {
      if (excluded.has(control.id)) return;
      if (locked) {
        control.dataset.wasDisabledBeforeLock = control.disabled ? "true" : "false";
        control.disabled = true;
      } else {
        control.disabled = control.dataset.wasDisabledBeforeLock === "true";
        delete control.dataset.wasDisabledBeforeLock;
      }
    });
  }

  function setBusy(button, busy, busyLabel = "处理中…") {
    if (!button) return;
    if (busy) {
      if (!button.dataset.originalLabel) button.dataset.originalLabel = button.textContent;
      button.disabled = true;
      button.setAttribute("aria-busy", "true");
      button.textContent = busyLabel;
      return;
    }
    button.removeAttribute("aria-busy");
    if (button.dataset.originalLabel) button.textContent = button.dataset.originalLabel;
    delete button.dataset.originalLabel;
    button.disabled = false;
  }

  function dataMutationBusy() {
    return state.dataUpdateBusy
      || state.marketImportBusy
      || state.catalogSyncBusy
      || state.stockAddBusy.size > 0;
  }

  function screeningConfigurationBusy() {
    return dataMutationBusy() || state.rulesSavingBusy;
  }


  async function requestJson(path, options = {}) {
    const method = clean(options.method || "GET").toUpperCase();
    const headers = new Headers(options.headers || {});
    if (!["GET", "HEAD"].includes(method)) {
      headers.set("Content-Type", "application/json");
      if (state.csrfToken) headers.set("X-Panel-CSRF", state.csrfToken);
    }
    let response;
    let text;
    try {
      response = await fetch(path, {
        ...options,
        method,
        headers,
        credentials: "same-origin",
      });
      text = await response.text();
    } catch (_error) {
      setConnection(false, "连接中断");
      const error = new Error("无法连接本地面板服务，请确认服务仍在运行。");
      error.code = "CONNECTION_FAILED";
      throw error;
    }
    setConnection(true, "本地数据已连接");
    let payload = {};
    if (text) {
      try {
        payload = JSON.parse(text);
      } catch (_error) {
        payload = {};
      }
    }
    if (!response.ok) {
      const error = new Error(payload.error || `请求失败（${response.status}）`);
      error.status = response.status;
      error.code = payload.error_code;
      error.fieldErrors = payload.field_errors || [];
      error.duplicateRunId = payload.duplicate_run_id;
      error.references = payload.references || [];
      throw error;
    }
    return payload;
  }

  function writeReceiptMayBeUncertain(error) {
    return error?.code === "CONNECTION_FAILED" || Number(error?.status) >= 500;
  }

  async function optionalJson(path) {
    try {
      return await requestJson(path);
    } catch (error) {
      if (error.status === 404) return null;
      throw error;
    }
  }

  function setConnection(connected, message) {
    const element = byId("connectionStatus");
    if (!element) return;
    element.classList.toggle("is-ready", connected);
    element.classList.toggle("is-error", !connected);
    element.querySelector("strong").textContent = message;
  }

  const viewLifecycle = {
    tradeDraftDirty: () => false,
    loadTrades: () => {},
    refreshDatasetStatus: () => {},
  };

  function configureViewLifecycle(handlers = {}) {
    Object.assign(viewLifecycle, handlers);
  }

  function navigate(view, { focus = true } = {}) {
    // 从顶部导航进入操作记录页时固定回到 RECORDS_HOME。
    if (view === "records" && state.recordsSurface === "editor" && !viewLifecycle.tradeDraftDirty()) {
      state.recordsSurface = "home";
    }
    const target = ["screen", "rules", "records"].includes(view) ? view : "screen";
    state.activeView = target;
    document.querySelectorAll("[data-view-panel]").forEach((panel) => {
      panel.classList.toggle("is-hidden", panel.dataset.viewPanel !== target);
    });
    document.querySelectorAll(".nav-button").forEach((button) => {
      const current = button.dataset.view === target;
      button.classList.toggle("is-active", current);
      if (current) button.setAttribute("aria-current", "page");
      else button.removeAttribute("aria-current");
    });
    if (window.location.hash !== `#${target}`) {
      window.history.replaceState(null, "", `#${target}`);
    }
    if (target === "records" && !state.tradesLoaded) viewLifecycle.loadTrades();
    if (target === "screen" && state.dataset) void viewLifecycle.refreshDatasetStatus();
    if (focus) byId(`${target}Heading`)?.focus({ preventScroll: true });
    window.scrollTo({ top: 0, behavior: "instant" });
  }

  function symbolItems() {
    return state.bootstrap?.symbols || [];
  }

  function stockBySymbol(rawSymbol) {
    const query = clean(rawSymbol).toLowerCase();
    if (!query) return null;
    const digits = query.match(/\d{6}/)?.[0];
    if (digits) {
      const symbolMatch = symbolItems().find((item) => clean(item.symbol).startsWith(digits));
      if (symbolMatch) return symbolMatch;
    }
    const exactNameMatches = symbolItems().filter(
      (item) => clean(item.stock_name).toLowerCase() === query,
    );
    if (exactNameMatches.length === 1) return exactNameMatches[0];
    return null;
  }


export {
  API,
  CUSTOM_RULE_DRAFT_STORAGE_KEY,
  PanelCore,
  RULE_DRAFT_STORAGE_KEY,
  TRADE_DRAFT_SCHEMA,
  TRADE_DRAFT_STORAGE_KEY,
  askConfirm,
  byId,
  clean,
  configureViewLifecycle,
  cycleTheme,
  dataMutationBusy,
  escapeHtml,
  formatCost,
  formatDateTime,
  formatMoney,
  initializeTheme,
  navigate,
  newRequestId,
  optionalJson,
  requestJson,
  safeStorageGet,
  safeStorageRemove,
  safeStorageSet,
  screeningConfigurationBusy,
  setBusy,
  setConnection,
  setFeedback,
  setFormLocked,
  showToast,
  state,
  stockBySymbol,
  symbolItems,
  writeReceiptMayBeUncertain,
};
