import {
  API,
  PanelCore,
  TRADE_DRAFT_SCHEMA,
  TRADE_DRAFT_STORAGE_KEY,
  askConfirm,
  byId,
  clean,
  escapeHtml,
  formatMoney,
  navigate,
  newRequestId,
  requestJson,
  safeStorageGet,
  safeStorageRemove,
  safeStorageSet,
  setBusy,
  setFeedback,
  setFormLocked,
  showToast,
  state,
  stockBySymbol,
  symbolItems,
  writeReceiptMayBeUncertain,
} from "../platform.js";
import { ruleDialogSignature } from "./rules.js";
import { screeningFreshness } from "./screening.js";

  function resolveTradeStockInput() {
    const input = byId("tradeSymbol");
    const query = clean(input.value).toLowerCase();
    let stock = stockBySymbol(query);
    if (!stock && query) {
      const matches = symbolItems().filter((item) => (
        clean(item.symbol).toLowerCase().includes(query)
        || clean(item.stock_name).toLowerCase().includes(query)
      ));
      if (matches.length === 1) stock = matches[0];
    }
    if (!stock) {
      input.setCustomValidity("请从股票列表中选择一个明确的代码或名称。");
      input.reportValidity();
      input.focus();
      setFeedback("tradeFeedback", "请先从下拉列表中确认要记录的股票。", true);
      return null;
    }
    input.setCustomValidity("");
    input.value = stock.symbol;
    closeTradeSymbolPicker();
    return stock;
  }

  function sortedSymbolItems() {
    return [...symbolItems()].sort((left, right) => (
      clean(left.symbol).localeCompare(clean(right.symbol))
    ));
  }

  function visibleTradeSymbolOptions() {
    return Array.from(byId("tradeSymbolOptions").querySelectorAll("[data-trade-symbol]"));
  }

  function setActiveTradeSymbolOption(index, { scroll = false } = {}) {
    const options = visibleTradeSymbolOptions();
    if (!options.length) {
      state.tradeSymbolActiveIndex = -1;
      byId("tradeSymbol").removeAttribute("aria-activedescendant");
      return;
    }
    const nextIndex = ((index % options.length) + options.length) % options.length;
    state.tradeSymbolActiveIndex = nextIndex;
    options.forEach((option, optionIndex) => {
      option.classList.toggle("is-active", optionIndex === nextIndex);
    });
    const active = options[nextIndex];
    byId("tradeSymbol").setAttribute("aria-activedescendant", active.id);
    if (scroll) active.scrollIntoView({ block: "nearest" });
  }

  function renderTradeSymbolOptions({ showAll = false } = {}) {
    const input = byId("tradeSymbol");
    const list = byId("tradeSymbolOptions");
    if (!input || !list) return;
    const query = showAll ? "" : clean(input.value).toLowerCase();
    const selected = stockBySymbol(input.value);
    const items = sortedSymbolItems().filter((item) => (
      !query
      || clean(item.symbol).toLowerCase().includes(query)
      || clean(item.stock_name).toLowerCase().includes(query)
    ));
    list.innerHTML = items.length
      ? items.map((item, index) => {
        const isSelected = selected?.symbol === item.symbol;
        return `<button
          class="stock-option${isSelected ? " is-selected" : ""}"
          id="tradeSymbolOption${index}"
          type="button"
          role="option"
          tabindex="-1"
          aria-selected="${isSelected ? "true" : "false"}"
          data-trade-symbol="${escapeHtml(item.symbol)}"
        >
          <span>${escapeHtml(item.stock_name || item.symbol)}</span>
          <small>${escapeHtml(item.symbol)}</small>
          <strong aria-hidden="true">${isSelected ? "当前" : ""}</strong>
        </button>`;
      }).join("")
      : `<div class="stock-option-empty" role="status">
        <strong>没有匹配的股票</strong>
        <small>请尝试其他代码或名称。</small>
      </div>`;
    state.tradeSymbolActiveIndex = -1;
    input.removeAttribute("aria-activedescendant");
  }

  function shouldShowAllTradeSymbols() {
    const value = clean(byId("tradeSymbol").value).toUpperCase();
    if (!value) return true;
    const selected = stockBySymbol(value);
    if (!selected) return false;
    const fullSymbol = clean(selected.symbol).toUpperCase();
    return value === fullSymbol || value === fullSymbol.slice(0, 6);
  }

  function openTradeSymbolPicker({ showAll = true } = {}) {
    const input = byId("tradeSymbol");
    const list = byId("tradeSymbolOptions");
    renderTradeSymbolOptions({ showAll });
    list.hidden = false;
    input.setAttribute("aria-expanded", "true");
    const toggle = byId("tradeSymbolToggle");
    toggle.setAttribute("aria-expanded", "true");
    toggle.setAttribute("aria-label", "收起股票列表");
  }

  function closeTradeSymbolPicker() {
    const input = byId("tradeSymbol");
    const list = byId("tradeSymbolOptions");
    list.hidden = true;
    input.setAttribute("aria-expanded", "false");
    input.removeAttribute("aria-activedescendant");
    const toggle = byId("tradeSymbolToggle");
    toggle.setAttribute("aria-expanded", "false");
    toggle.setAttribute("aria-label", "展开全部股票");
    state.tradeSymbolActiveIndex = -1;
  }

  function chooseTradeSymbol(symbol) {
    byId("tradeSymbol").value = symbol;
    closeTradeSymbolPicker();
    byId("tradeSymbol").focus();
  }

  function moveTradeSymbolSelection(step) {
    if (byId("tradeSymbolOptions").hidden) {
      openTradeSymbolPicker({ showAll: shouldShowAllTradeSymbols() });
    }
    const options = visibleTradeSymbolOptions();
    if (!options.length) return;
    const nextIndex = state.tradeSymbolActiveIndex < 0
      ? (step > 0 ? 0 : options.length - 1)
      : state.tradeSymbolActiveIndex + step;
    setActiveTradeSymbolOption(nextIndex, { scroll: true });
  }

  function handleTradeSymbolKeydown(event) {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      moveTradeSymbolSelection(event.key === "ArrowDown" ? 1 : -1);
      return;
    }
    if (event.key === "Enter" && !byId("tradeSymbolOptions").hidden) {
      event.preventDefault();
      const options = visibleTradeSymbolOptions();
      const active = options[state.tradeSymbolActiveIndex];
      if (active) {
        chooseTradeSymbol(active.dataset.tradeSymbol);
      } else if (options.length === 1) {
        chooseTradeSymbol(options[0].dataset.tradeSymbol);
      } else if (options.length > 1) {
        setActiveTradeSymbolOption(0, { scroll: true });
      }
      return;
    }
    if (event.key === "Escape") {
      closeTradeSymbolPicker();
      return;
    }
    if (event.key === "Tab") closeTradeSymbolPicker();
  }

  function populateSymbolChoices() {
    if (!byId("tradeSymbolOptions").hidden) {
      renderTradeSymbolOptions({ showAll: true });
    }
  }


  function resetTradeForm(symbol = "") {
    byId("tradeForm").reset();
    byId("tradeRequestId").value = newRequestId();
    byId("tradeDate").value = state.bootstrap?.today || new Date().toISOString().slice(0, 10);
    byId("tradeDate").max = state.bootstrap?.today || new Date().toISOString().slice(0, 10);
    byId("tradeSide").value = "BUY";
    byId("tradeSymbol").value = symbol;
    closeTradeSymbolPicker();
    updateTradeChecklistLabels();
    updateTradeAmount();
    updateTradeDisciplineStatus();
    setFeedback("tradeFeedback", "记录只保存自报事实和勾选结果，不代表规则允许操作。");
  }

  function updateTradeAmount() {
    const price = Number(byId("tradePrice").value);
    const shares = Number(byId("tradeShares").value);
    // 金额和它的标签在同一个文字节点里，读屏和肉眼都不会看到孤立数字。
    byId("tradeAmount").textContent = `成交金额 ${formatMoney(
      Number.isFinite(price) && Number.isFinite(shares) ? price * shares : 0,
    )}`;
  }

  function checkedTradeValues(name) {
    return Array.from(document.querySelectorAll(`input[name="${name}"]:checked`))
      .map((input) => input.value);
  }

  function tradeChecklistSnapshot() {
    return {
      reasonTags: checkedTradeValues("reason_tag"),
      disciplineChecks: checkedTradeValues("discipline_check"),
      emotionFlags: checkedTradeValues("emotion_flag"),
      emotionClear: byId("tradeEmotionClear").checked,
    };
  }

  function updateTradeChecklistLabels() {
    const side = byId("tradeSide").value === "SELL" ? "SELL" : "BUY";
    setSectionHeadingText(
      byId("tradeReasonHeading"),
      side === "BUY" ? "为什么买入" : "为什么卖出",
    );
    document.querySelectorAll("[data-buy-label][data-sell-label]").forEach((element) => {
      element.textContent = side === "BUY"
        ? element.dataset.buyLabel
        : element.dataset.sellLabel;
    });
  }

  function updateTradeDisciplineStatus() {
    const status = byId("tradeDisciplineStatus");
    const {
      reasonTags,
      disciplineChecks,
      emotionFlags,
      emotionClear,
    } = tradeChecklistSnapshot();
    const review = PanelCore.tradeDisciplineStatus({
      reasonTags,
      disciplineChecks,
      emotionFlags,
      emotionClear,
    });
    status.classList.remove("is-ready", "is-warning", "is-risk");
    status.classList.add(review.className);
    status.textContent = review.message;
  }

  // 操作记录页主从：历史记录是固定首页，新增是从首页进入的独立子页面。
  // 视口宽度绝不参与 home/editor 转换。
  function renderRecordsSurface() {
    const editing = state.recordsSurface === "editor";
    byId("recordsHomeView").hidden = editing;
    byId("recordEditorView").hidden = !editing;
    renderRecordDraftNotice();
  }

  function renderRecordDraftNotice() {
    const meta = state.tradeDraftMeta;
    const notice = byId("recordDraftNotice");
    const show = Boolean(meta) && state.recordsSurface === "home";
    notice.hidden = !show;
    if (show) {
      const who = clean(meta.symbol) || "未选择股票";
      byId("recordDraftSummary").textContent = `未完成草稿 · ${who} · 更新于 ${meta.updatedAt || "刚刚"}`;
    }
    const status = byId("recordDraftStatus");
    const inEditor = state.recordsSurface === "editor" && Boolean(meta);
    status.hidden = !inEditor;
    if (inEditor) status.textContent = `草稿已自动保存于本机 · ${meta.updatedAt || "刚刚"}`;
  }

  // 只替换标题里的文字部分，保留前面的章节序号。
  function setSectionHeadingText(heading, text) {
    if (!heading) return;
    const textNode = [...heading.childNodes]
      .find((node) => node.nodeType === Node.TEXT_NODE && clean(node.textContent));
    if (textNode) textNode.textContent = text;
    else heading.append(text);
  }

  function tradeDraftSnapshot() {
    return {
      schemaVersion: TRADE_DRAFT_SCHEMA,
      requestId: byId("tradeRequestId").value,
      tradeDate: byId("tradeDate").value,
      side: byId("tradeSide").value,
      symbol: byId("tradeSymbol").value,
      price: byId("tradePrice").value,
      shares: byId("tradeShares").value,
      notes: byId("tradeNotes").value,
      reasonTags: checkedTradeValues("reason_tag"),
      disciplineChecks: checkedTradeValues("discipline_check"),
      emotionFlags: checkedTradeValues("emotion_flag"),
      emotionClear: byId("tradeEmotionClear").checked,
      sourceContext: state.tradeDraftMeta?.sourceContext || "",
      createdAt: state.tradeDraftMeta?.createdAt || new Date().toISOString(),
      updatedAt: new Date().toTimeString().slice(0, 5),
    };
  }

  // 只有 tradeDraftDirty() 成立才持久化；未改动的空白表单不产生草稿。
  function saveTradeDraftCache() {
    window.clearTimeout(state.tradeDraftTimer);
    state.tradeDraftTimer = window.setTimeout(() => {
      if (state.recordsSurface !== "editor" || !tradeDraftDirty()) return;
      const payload = tradeDraftSnapshot();
      state.tradeDraftMeta = payload;
      const stored = safeStorageSet(TRADE_DRAFT_STORAGE_KEY, JSON.stringify(payload));
      state.tradeDraftPersisted = stored;
      renderRecordDraftNotice();
    }, 400);
  }

  function clearTradeDraftCache() {
    window.clearTimeout(state.tradeDraftTimer);
    state.tradeDraftMeta = null;
    state.tradeDraftPersisted = false;
    safeStorageRemove(TRADE_DRAFT_STORAGE_KEY);
    renderRecordDraftNotice();
  }

  // 损坏或旧结构草稿被安全忽略，不能阻止历史首页加载。
  function restoreTradeDraftCache() {
    let payload = null;
    try {
      payload = JSON.parse(safeStorageGet(TRADE_DRAFT_STORAGE_KEY) || "null");
    } catch (_error) {
      payload = null;
    }
    if (!payload || typeof payload !== "object") return;
    if (Number(payload.schemaVersion) !== TRADE_DRAFT_SCHEMA) {
      safeStorageRemove(TRADE_DRAFT_STORAGE_KEY);
      return;
    }
    if (!/^[A-Za-z0-9_-]{8,64}$/.test(clean(payload.requestId))) {
      safeStorageRemove(TRADE_DRAFT_STORAGE_KEY);
      return;
    }
    state.tradeDraftMeta = payload;
    state.tradeDraftPersisted = true;
    renderRecordDraftNotice();
  }

  function applyTradeDraft(payload) {
    // 恢复时 request_id 保持不变，直到正式保存确认成功或用户明确放弃。
    byId("tradeRequestId").value = clean(payload.requestId);
    byId("tradeDate").value = clean(payload.tradeDate);
    byId("tradeSide").value = payload.side === "SELL" ? "SELL" : "BUY";
    byId("tradeSymbol").value = clean(payload.symbol);
    byId("tradePrice").value = clean(payload.price);
    byId("tradeShares").value = clean(payload.shares);
    byId("tradeNotes").value = clean(payload.notes);
    const restore = (name, values) => {
      document.querySelectorAll(`#tradeForm input[name="${name}"]`).forEach((input) => {
        input.checked = (values || []).includes(input.value);
      });
    };
    restore("reason_tag", payload.reasonTags);
    restore("discipline_check", payload.disciplineChecks);
    restore("emotion_flag", payload.emotionFlags);
    byId("tradeEmotionClear").checked = Boolean(payload.emotionClear);
    updateTradeChecklistLabels();
    updateTradeAmount();
    updateTradeDisciplineStatus();
  }

  function openRecordEditor({ symbol = "", resume = false, fromCandidate = false } = {}) {
    if (resume && state.tradeDraftMeta) {
      state.recordsSurface = "editor";
      applyTradeDraft(state.tradeDraftMeta);
    } else {
      state.recordsSurface = "editor";
      resetTradeForm(symbol);
      state.tradeDraftMeta = fromCandidate
        ? { sourceContext: "screening_candidate", symbol }
        : null;
      if (fromCandidate) {
        // 只带入股票与来源项，不替用户勾选纪律、仓位或情绪。
        const source = document.querySelector('#tradeReasonList input[value="RULE_TRIGGER"]');
        if (source) source.checked = true;
        updateTradeDisciplineStatus();
        saveTradeDraftCache();
      }
    }
    renderRecordsSurface();
    byId(resume ? "recordEditorHeading" : (symbol ? "tradePrice" : "tradeDate"))
      ?.focus({ preventScroll: true });
  }

  async function leaveRecordEditor() {
    // 返回首页不要求先提交；草稿已成功写入本机就直接返回。
    if (tradeDraftDirty()) {
      window.clearTimeout(state.tradeDraftTimer);
      const payload = tradeDraftSnapshot();
      state.tradeDraftMeta = payload;
      state.tradeDraftPersisted = safeStorageSet(
        TRADE_DRAFT_STORAGE_KEY,
        JSON.stringify(payload),
      );
      if (!state.tradeDraftPersisted && !await askConfirm({
        title: "草稿没能保存到本机",
        message: "返回后当前填写的内容会丢失。",
        confirmLabel: "仍要返回",
        cancelLabel: "留在这里",
        danger: true,
      })) return;
    }
    state.recordsSurface = "home";
    renderRecordsSurface();
    byId(state.tradeDraftMeta ? "resumeDraftButton" : "newRecordButton")
      ?.focus({ preventScroll: true });
  }

  async function discardTradeDraft() {
    if (!await askConfirm({
      title: "放弃这条未完成记录？",
      message: "放弃后无法恢复。",
      confirmLabel: "放弃草稿",
      danger: true,
    })) return;
    clearTradeDraftCache();
    resetTradeForm();
    state.recordsSurface = "home";
    renderRecordsSurface();
    byId("newRecordButton")?.focus({ preventScroll: true });
    showToast("草稿已放弃。");
  }

  function tradeDraftDirty() {
    const defaultDate = state.bootstrap?.today || new Date().toISOString().slice(0, 10);
    return Boolean(
      clean(byId("tradeSymbol").value)
      || clean(byId("tradePrice").value)
      || clean(byId("tradeShares").value)
      || clean(byId("tradeNotes").value)
      || byId("tradeSide").value !== "BUY"
      || (clean(byId("tradeDate").value) && byId("tradeDate").value !== defaultDate)
      || document.querySelector('#tradeForm input[type="checkbox"]:checked'),
    );
  }

  function unsavedDialogDraft() {
    const ruleDialogDirty = byId("ruleDialog").open
      && ruleDialogSignature() !== state.ruleDialogInitialSignature;
    const csvFileSelected = Boolean(byId("marketCsvFile").files?.length);
    return ruleDialogDirty || csvFileSelected;
  }

  function recordStatusClass(value) {
    return PanelCore.recordStatusClass(value);
  }

  function renderTrades() {
    const payload = state.trades || { records: [], count: 0 };
    const records = payload.records || [];
    byId("recordCount").textContent = String(payload.count || 0);
    byId("recordList").innerHTML = records.length
      ? records.map((record) => `<article class="record-card${clean(record.request_id) && clean(record.request_id) === clean(state.highlightedRecordId) ? " is-highlighted" : ""}" role="listitem">
        <div class="record-side ${record.side === "BUY" ? "is-buy" : "is-sell"}">${record.side === "BUY" ? "买" : "卖"}</div>
        <div class="record-stock">
          <strong>${escapeHtml(record.stock_name || record.symbol)}</strong>
          <small>${escapeHtml(record.operation || (record.side === "BUY" ? "买入" : "卖出"))} · ${escapeHtml(record.symbol)} · ${escapeHtml(record.trade_date)} ${escapeHtml(clean(record.trade_time).slice(0, 5))}</small>
        </div>
        <div class="record-value">
          <strong>${record.price == null ? "—" : Number(record.price).toFixed(2)} × ${record.shares ?? "—"}</strong>
          <small>${formatMoney(record.gross_amount)}</small>
        </div>
        <span class="record-status ${recordStatusClass(record.rule_status)}">${escapeHtml(record.rule_status || "历史未审查")}</span>
        <div class="record-evidence">
          <p><strong>操作依据</strong>${escapeHtml(record.notes || "未记录")}</p>
          <p><strong>情绪</strong>${escapeHtml(record.emotion || "未记录")}</p>
        </div>
      </article>`).join("")
      : `<div class="empty-state compact">
        <strong>还没有操作记录</strong>
        <p>保存后会显示在这里。</p>
      </div>`;
  }

  async function loadTrades(force = false) {
    if ((state.tradesLoaded && !force) || state.tradesLoading) return;
    state.tradesLoading = true;
    byId("recordList").setAttribute("aria-busy", "true");
    if (!state.trades?.records?.length) {
      byId("recordList").innerHTML = '<p class="loading-copy">正在读取最近记录…</p>';
    }
    try {
      state.trades = await requestJson(`${API.trades}?limit=50`);
      state.tradesLoaded = true;
      renderTrades();
    } catch (error) {
      state.tradesLoaded = false;
      byId("recordList").innerHTML = `<div class="empty-state compact">
        <strong>最近记录暂时无法读取</strong>
        <p>${escapeHtml(error.message)}</p>
      </div>`;
      setFeedback("tradeFeedback", error.message, true);
      showToast(error.message, true);
    } finally {
      state.tradesLoading = false;
      byId("recordList").setAttribute("aria-busy", "false");
    }
  }

  // 回执不确定时唯一可信的判断方式：重新读取最近记录，按 request_id 核对是否已落库。
  // 服务端每条记录都回传 request_id（source 去前缀），因此不需要新增接口或字段。
  async function reconcileTradeReceipt(requestId) {
    const payload = await requestJson(`${API.trades}?limit=50`);
    const saved = (payload.records || []).some(
      (record) => clean(record.request_id) === clean(requestId),
    );
    return { payload, saved };
  }

  // 5xx / 断连时记录可能已经写入。绝不能按“失败”处理后让用户换新编号重录，
  // 那会绕过 request_id 去重、在 records/my_trades.csv 里追加第二条相同事实。
  async function settleUncertainTradeReceipt(error, requestId) {
    setFeedback("tradeFeedback", "连接中断，正在核对这条记录是否已经保存…");
    try {
      const { payload, saved } = await reconcileTradeReceipt(requestId);
      state.trades = payload;
      state.tradesLoaded = true;
      renderTrades();
      if (saved) {
        clearTradeDraftCache();
        resetTradeForm();
        state.recordsSurface = "home";
        renderRecordsSurface();
        setFeedback(
          "tradeFeedback",
          "已核对：这条记录其实已经保存，没有重复写入。",
          false,
          true,
        );
        showToast("记录已保存，无需重复提交。");
        return;
      }
      setFeedback(
        "tradeFeedback",
        `${error.message} 已核对：服务器没有这条记录，可以直接重试保存。`,
        true,
      );
      showToast("未写入，可以重试。", true);
    } catch (_checkError) {
      // 核对本身也失败：保留同一 request_id 和草稿，重试仍会被服务端去重。
      state.tradesLoaded = false;
      setFeedback(
        "tradeFeedback",
        `${error.message} 暂时无法确认是否已保存，请恢复连接后重新进入本页核对，不要重新录入同一笔操作。`,
        false,
        false,
        true,
      );
      showToast("无法确认保存结果，请恢复连接后核对。", true);
    }
  }

  async function saveTrade(event) {
    event.preventDefault();
    if (state.tradeSavingBusy) return;
    const button = byId("saveTradeButton");
    const stock = resolveTradeStockInput();
    if (!stock) return;
    const checklist = tradeChecklistSnapshot();
    const requestId = byId("tradeRequestId").value;
    state.tradeSavingBusy = true;
    setBusy(button, true, "正在保存…");
    setFormLocked(byId("tradeForm"), true, ["saveTradeButton"]);
    setFeedback("tradeFeedback", "正在保存这条操作记录。");
    try {
      const result = await requestJson(API.trades, {
        method: "POST",
        body: JSON.stringify({
          request_id: requestId,
          trade_date: byId("tradeDate").value,
          symbol: stock.symbol,
          stock_name: stock?.stock_name || "",
          side: byId("tradeSide").value,
          price: byId("tradePrice").value,
          shares: byId("tradeShares").value,
          notes: byId("tradeNotes").value,
          reason_tags: checklist.reasonTags,
          discipline_checks: checklist.disciplineChecks,
          emotion_flags: checklist.emotionFlags,
          emotion_clear: checklist.emotionClear,
          checklist_version: "1",
        }),
      });
      resetTradeForm();
      try {
        state.trades = await requestJson(`${API.trades}?limit=50`);
        state.tradesLoaded = true;
        renderTrades();
      } catch (refreshError) {
        state.tradesLoaded = false;
        setFeedback(
          "tradeFeedback",
          `操作记录已经保存，但最近记录刷新失败：${refreshError.message} 重新进入本页会自动重试。`,
          false,
          false,
          true,
        );
        showToast("操作记录已保存，列表稍后重试刷新。");
        clearTradeDraftCache();
        state.recordsSurface = "home";
        renderRecordsSurface();
        return;
      }
      // 正式写入已确认，草稿使命结束；返回首页核对刚保存的事实。
      clearTradeDraftCache();
      state.highlightedRecordId = clean(result.record?.request_id) || requestId;
      state.recordsSurface = "home";
      renderRecordsSurface();
      renderTrades();
      setFeedback(
        "tradeFeedback",
        result.deduplicated ? "这条记录已保存过，没有重复写入。" : "操作记录已保存到本地。",
        false,
        true,
      );
      showToast(result.deduplicated ? "没有重复写入。" : "操作记录已保存。");
      byId("recentRecordsHeading")?.focus({ preventScroll: true });
    } catch (error) {
      if (writeReceiptMayBeUncertain(error)) {
        await settleUncertainTradeReceipt(error, requestId);
      } else {
        setFeedback("tradeFeedback", error.message, true);
        showToast(error.message, true);
      }
    } finally {
      state.tradeSavingBusy = false;
      setFormLocked(byId("tradeForm"), false, ["saveTradeButton"]);
      setBusy(button, false);
    }
  }

  async function prepareTradeForCandidate(symbol) {
    if (screeningFreshness().status !== "current") {
      navigate("screen");
      setFeedback(
        "screenFeedback",
        "这条候选来自旧批次，请先按当前数据和规则重新筛选。",
        false,
        false,
        true,
      );
      return;
    }
    navigate("records");
    // 已存在未完成草稿时不能被候选内容静默覆盖。
    if (state.tradeDraftMeta) {
      const keep = await askConfirm({
        title: "已有一条未完成草稿",
        message: `草稿股票：${clean(state.tradeDraftMeta.symbol) || "未选择"}。`,
        confirmLabel: "继续填写原草稿",
        cancelLabel: `改记录 ${symbol}`,
      });
      if (keep) {
        openRecordEditor({ resume: true });
        setFeedback("tradeFeedback", "已恢复原来的未完成草稿。");
        return;
      }
      if (!await askConfirm({
        title: "放弃原草稿？",
        message: `放弃后无法恢复，将改为记录 ${symbol}。`,
        confirmLabel: "放弃并改记录",
        danger: true,
      })) {
        state.recordsSurface = "home";
        renderRecordsSurface();
        return;
      }
      clearTradeDraftCache();
    }
    openRecordEditor({ symbol, fromCandidate: true });
    setFeedback("tradeFeedback", `已带入候选 ${symbol}；请按实际情况完成纪律和情绪核对。`);
  }


  function bindRecordEvents() {
    byId("tradeForm").addEventListener("submit", saveTrade);
    byId("newRecordButton").addEventListener("click", async () => {
      // 已有草稿时不静默覆盖：先让用户选择继续还是放弃。
      if (state.tradeDraftMeta && !await askConfirm({
        title: "已有一条未完成草稿",
        message: "继续填写会回到那条草稿；留在首页则原样保留。",
        confirmLabel: "继续填写",
        cancelLabel: "留在首页",
      })) return;
      openRecordEditor({ resume: Boolean(state.tradeDraftMeta) });
    });
    byId("recordEditorBackButton").addEventListener("click", () => void leaveRecordEditor());
    byId("resumeDraftButton").addEventListener("click", () => openRecordEditor({ resume: true }));
    byId("discardDraftButton").addEventListener("click", () => void discardTradeDraft());
    byId("tradeForm").addEventListener("input", saveTradeDraftCache);
    byId("tradeForm").addEventListener("change", saveTradeDraftCache);
    byId("tradeSymbol").addEventListener("focus", () => {
      if (byId("tradeSymbolOptions").hidden) {
        openTradeSymbolPicker({ showAll: shouldShowAllTradeSymbols() });
      }
    });
    byId("tradeSymbol").addEventListener("click", () => {
      if (byId("tradeSymbolOptions").hidden) {
        openTradeSymbolPicker({ showAll: shouldShowAllTradeSymbols() });
      }
    });
    byId("tradeSymbol").addEventListener("input", () => {
      byId("tradeSymbol").setCustomValidity("");
      openTradeSymbolPicker({ showAll: false });
    });
    byId("tradeSymbol").addEventListener("keydown", handleTradeSymbolKeydown);
    byId("tradeSymbolToggle").addEventListener("click", () => {
      if (byId("tradeSymbolOptions").hidden) {
        openTradeSymbolPicker({ showAll: true });
        byId("tradeSymbol").focus({ preventScroll: true });
      } else {
        closeTradeSymbolPicker();
      }
    });
    byId("tradeSymbolOptions").addEventListener("pointerdown", (event) => {
      if (event.target.closest("[data-trade-symbol]")) event.preventDefault();
    });
    byId("tradeSymbolOptions").addEventListener("click", (event) => {
      const option = event.target.closest("[data-trade-symbol]");
      if (option) chooseTradeSymbol(option.dataset.tradeSymbol);
    });
    document.addEventListener("pointerdown", (event) => {
      if (!event.target.closest("#tradeSymbolPicker")) closeTradeSymbolPicker();
    });
    document.addEventListener("focusin", (event) => {
      if (!event.target.closest("#tradeSymbolPicker")) closeTradeSymbolPicker();
    });
    byId("tradeSide").addEventListener("change", () => {
      document.querySelectorAll('input[name="reason_tag"], input[name="discipline_check"]').forEach((input) => {
        input.checked = false;
      });
      updateTradeChecklistLabels();
      updateTradeDisciplineStatus();
      setFeedback("tradeFeedback", "操作方向已切换，请重新核对操作依据和纪律项。");
    });
    byId("tradeForm").addEventListener("change", (event) => {
      if (!event.target.matches('input[type="checkbox"]')) return;
      if (event.target.name === "emotion_clear" && event.target.checked) {
        document.querySelectorAll('input[name="emotion_flag"]').forEach((input) => {
          input.checked = false;
        });
      }
      if (event.target.name === "emotion_flag" && event.target.checked) {
        byId("tradeEmotionClear").checked = false;
      }
      updateTradeDisciplineStatus();
    });
    byId("tradePrice").addEventListener("input", updateTradeAmount);
    byId("tradeShares").addEventListener("input", updateTradeAmount);
    byId("candidateList").addEventListener("click", (event) => {
      const button = event.target.closest("[data-record-symbol]");
      if (button) prepareTradeForCandidate(button.dataset.recordSymbol);
    });
  }

export {
  bindRecordEvents,
  loadTrades,
  populateSymbolChoices,
  renderRecordsSurface,
  resetTradeForm,
  restoreTradeDraftCache,
  tradeDraftDirty,
  unsavedDialogDraft,
};
