import {
  API,
  PanelCore,
  byId,
  clean,
  dataMutationBusy,
  escapeHtml,
  newRequestId,
  requestJson,
  setBusy,
  setFeedback,
  setFormLocked,
  showToast,
  state,
  stockBySymbol,
} from "../platform.js";
import { populateSymbolChoices } from "./records.js";
import {
  renderScreenStatus,
  renderScreeningAction,
} from "./screening.js";

  let catalogSearchTimer = null;

  function setDataActionsBusy(activeButton, busy, busyLabel) {
    const buttons = [
      byId("updateAllDataButton"),
      byId("syncStockCatalogButton"),
      byId("importMarketCsvButton"),
    ];
    buttons.forEach((button) => {
      if (button === activeButton) {
        setBusy(button, busy, busyLabel);
      } else {
        button.disabled = busy;
      }
    });
    byId("dataStockSearch").disabled = busy;
    byId("closeDataDialogButton").disabled = busy;
  }


  function renderDataSummary() {
    const dataset = state.dataset || {};
    const alignment = PanelCore.datasetStatusSummary(dataset);
    const { aligned, total, unaligned } = alignment;
    const dataBusy = dataMutationBusy();
    const blockedByScreening = state.screeningBusy || state.rulesSavingBusy;
    byId("dataDate").textContent = alignment.dateLine;
    byId("dataSummary").textContent = alignment.inlineLine;
    byId("dialogDataCounts").textContent = [
      `本地 ${total} 只`,
      total === 0 ? "暂无可对齐数据" : (unaligned > 0 ? `已对齐 ${aligned} 只` : "全部已对齐"),
      ...(unaligned > 0 ? [`未对齐 ${unaligned} 只`] : []),
    ].join(" · ");
    byId("dialogCurrentDate").textContent = alignment.currentDate;
    byId("dialogTargetDate").textContent = alignment.targetDate;
    byId("dialogActualDate").textContent = alignment.actualDate;
    const alignmentStatus = byId("dialogAlignmentStatus");
    alignmentStatus.textContent = alignment.label;
    alignmentStatus.className = `data-alignment-pill ${alignment.className}`;
    alignmentStatus.title = alignment.message;
    byId("dialogAlignmentRelation").textContent = `与当前日期对齐：${alignment.relationLabel}`;
    byId("updateAllDataButton").disabled = dataBusy || blockedByScreening || total === 0;
    byId("syncStockCatalogButton").disabled = dataBusy || blockedByScreening;
    byId("importMarketCsvButton").disabled = dataBusy || blockedByScreening;
    byId("dataStockSearch").disabled = dataBusy;
    byId("closeDataDialogButton").disabled = dataBusy;
    renderDataStockList();
  }

  function dataEntries() {
    return [...(state.dataset?.entries || [])]
      .sort((left, right) => clean(left.symbol).localeCompare(clean(right.symbol)));
  }

  function dataStockName(symbol) {
    return stockBySymbol(symbol)?.stock_name || symbol;
  }

  function dataAlignmentStatus(entry) {
    return PanelCore.dataAlignmentStatus(entry);
  }

  function renderDataCatalogMeta() {
    const catalog = state.catalog || {};
    const meta = byId("dataCatalogMeta");
    if (catalog.ready) {
      meta.textContent = `完整目录 ${catalog.rows || 0} 只 · ${catalog.snapshot_date || "日期未知"}；输入后搜索，未输入时显示本地库。`;
      return;
    }
    meta.textContent = catalog.message || "完整股票目录尚未准备，请先更新目录。";
  }

  function renderLocalDataStockList() {
    const list = byId("dataStockList");
    if (!list) return;
    const entries = dataEntries();
    list.setAttribute("aria-busy", "false");
    byId("dataStockListTitle").textContent = "本地股票库";
    byId("dataStockListCount").textContent = String(entries.length);
    const alignment = PanelCore.datasetAlignmentSummary(state.dataset || {});
    byId("dataStockListMeta").textContent = `共 ${entries.length} 只 · 目标 ${alignment.targetDate} · 实际共同 ${alignment.actualDate}`;
    list.innerHTML = entries.length
      ? entries.map((entry) => {
        const status = dataAlignmentStatus(entry);
        const latestDate = entry.last_used_trade_date || entry.last_trade_date || "无";
        return `<article class="data-stock-row" role="listitem">
          <div class="data-stock-identity">
            <strong>${escapeHtml(dataStockName(entry.symbol))}</strong>
            <small>${escapeHtml(entry.symbol)}</small>
          </div>
          <div class="data-stock-date">
            <span>最新数据</span>
            <strong>${escapeHtml(latestDate)}</strong>
          </div>
          <span class="data-alignment-status ${status.className}">${status.label}</span>
        </article>`;
      }).join("")
      : `<div class="empty-state compact">
        <strong>本地股票库还没有股票</strong>
        <p>在上方输入股票代码或名称，加入第一只股票。</p>
      </div>`;
  }

  function renderCatalogSearchResults() {
    const list = byId("dataStockList");
    const query = clean(byId("dataStockSearch").value);
    const search = state.catalogSearch || {};
    byId("dataStockListTitle").textContent = "完整目录搜索";
    if (!state.catalog?.ready) {
      list.setAttribute("aria-busy", "false");
      byId("dataStockListCount").textContent = "0";
      byId("dataStockListMeta").textContent = "目录尚未准备";
      list.innerHTML = `<div class="empty-state compact">
        <strong>请先更新完整股票目录</strong>
        <p>目录只保存代码和名称，不会把全部股票加入本地库。</p>
      </div>`;
      return;
    }
    if (search.loading || search.query !== query) {
      list.setAttribute("aria-busy", "true");
      byId("dataStockListCount").textContent = "…";
      byId("dataStockListMeta").textContent = "正在搜索";
      list.innerHTML = '<p class="loading-copy">正在搜索完整股票目录…</p>';
      return;
    }
    if (search.error) {
      list.setAttribute("aria-busy", "false");
      byId("dataStockListCount").textContent = "0";
      byId("dataStockListMeta").textContent = "搜索失败";
      list.innerHTML = `<div class="empty-state compact">
        <strong>暂时无法搜索</strong>
        <p>${escapeHtml(search.error)}</p>
      </div>`;
      return;
    }

    const results = search.results || [];
    const totalMatches = Number(search.total_matches || 0);
    list.setAttribute("aria-busy", "false");
    byId("dataStockListCount").textContent = String(results.length);
    byId("dataStockListMeta").textContent = `显示 ${results.length} / ${totalMatches} 条`;
    list.innerHTML = results.length
      ? results.map((item) => {
        const catalogAction = PanelCore.catalogAction(item);
        const localStatus = catalogAction.localStatus || null;
        const adding = state.stockAddBusy.has(item.symbol);
        const allImportsLocked = dataMutationBusy()
          || state.screeningBusy
          || state.rulesSavingBusy;
        let action = "";
        if (catalogAction.type !== "none") {
          const actionAttribute = catalogAction.type === "update"
            ? "data-update-stock"
            : "data-add-stock";
          action = `<button
            class="button button-secondary button-compact data-stock-action"
            type="button"
            ${actionAttribute}="${escapeHtml(item.symbol)}"
            ${allImportsLocked ? "disabled" : ""}
            ${adding ? 'aria-busy="true"' : ""}
          >${adding ? "正在处理…" : catalogAction.label}</button>`;
        } else {
          action = `<span class="data-alignment-status ${localStatus.className}">${localStatus.label}</span>`;
        }
        return `<article class="data-stock-row data-catalog-row" role="listitem">
          <div class="data-stock-identity">
            <strong>${escapeHtml(item.stock_name || item.symbol)}</strong>
            <small>${escapeHtml(item.symbol)}</small>
          </div>
          <div class="data-stock-date">
            <span>${item.in_local_library ? "本地状态" : "市场"}</span>
            <strong>${escapeHtml(
              item.in_local_library
                ? (item.local_latest_date || localStatus.label)
                : item.market,
            )}</strong>
          </div>
          ${action}
        </article>`;
      }).join("")
      : `<div class="empty-state compact">
        <strong>没有找到匹配的股票</strong>
        <p>请核对股票代码或名称。</p>
      </div>`;
  }

  function renderDataStockList() {
    renderDataCatalogMeta();
    if (clean(byId("dataStockSearch")?.value)) {
      renderCatalogSearchResults();
    } else {
      renderLocalDataStockList();
    }
  }

  async function searchCatalog(query, serial) {
    try {
      const result = await requestJson(
        `${API.catalogSearch}?q=${encodeURIComponent(query)}&limit=20`,
      );
      if (
        serial !== state.catalogSearchSerial
        || clean(byId("dataStockSearch").value) !== query
      ) return;
      state.catalog = result.catalog || state.catalog;
      state.catalogSearch = { ...result, loading: false, error: "" };
    } catch (error) {
      if (serial !== state.catalogSearchSerial) return;
      state.catalogSearch = {
        query,
        total_matches: 0,
        results: [],
        loading: false,
        error: error.message,
      };
    }
    renderDataStockList();
  }

  function scheduleCatalogSearch() {
    window.clearTimeout(catalogSearchTimer);
    state.catalogSearchSerial += 1;
    const serial = state.catalogSearchSerial;
    const query = clean(byId("dataStockSearch").value);
    if (!query) {
      state.catalogSearch = null;
      renderDataStockList();
      return;
    }
    if (!state.catalog?.ready) {
      state.catalogSearch = {
        query,
        total_matches: 0,
        results: [],
        loading: false,
        error: "",
      };
      renderDataStockList();
      return;
    }
    state.catalogSearch = {
      query,
      total_matches: 0,
      results: [],
      loading: true,
      error: "",
    };
    renderDataStockList();
    catalogSearchTimer = window.setTimeout(
      () => searchCatalog(query, serial),
      250,
    );
  }


  function openDataDialog() {
    byId("toast").classList.remove("is-visible");
    state.dataDialogOpener = document.activeElement;
    window.clearTimeout(catalogSearchTimer);
    state.catalogSearchSerial += 1;
    state.catalogSearch = null;
    byId("dataStockSearch").value = "";
    if (!byId("marketCsvRequestId").value) {
      byId("marketCsvRequestId").value = newRequestId();
    }
    renderDataSummary();
    setFeedback("dataFeedback", "");
    byId("dataDialog").showModal();
    byId("dataStockSearch").focus();
    void refreshDatasetStatus({ force: true });
  }

  function closeDataDialog() {
    if (dataMutationBusy()) {
      setFeedback(
        "dataFeedback",
        "数据任务仍在进行，请等待完成后再关闭。",
        false,
        false,
        true,
      );
      return false;
    }
    byId("dataDialog").close();
    const opener = state.dataDialogOpener;
    state.dataDialogOpener = null;
    (opener?.isConnected ? opener : byId("openDataButton")).focus({ preventScroll: true });
    return true;
  }

  async function refreshCoreData() {
    const serial = state.dataStatusRefreshSerial + 1;
    state.dataStatusRefreshSerial = serial;
    const [bootstrap, dataset] = await Promise.all([
      requestJson(API.bootstrap),
      requestJson(API.dataStatus),
    ]);
    if (serial !== state.dataStatusRefreshSerial) return false;
    state.bootstrap = bootstrap;
    state.csrfToken = bootstrap.csrf_token || state.csrfToken;
    state.dataset = dataset;
    state.screeningInputsUncertain = false;
    populateSymbolChoices();
    renderScreenStatus();
    return true;
  }

  async function refreshDatasetStatus({ force = false } = {}) {
    if (
      !state.dataset
      || document.visibilityState === "hidden"
      || dataMutationBusy()
      || state.screeningBusy
      || state.rulesSavingBusy
    ) return false;
    const now = Date.now();
    if (!force && now - state.dataStatusLastRequestedAt < 1500) return false;
    state.dataStatusLastRequestedAt = now;
    const serial = state.dataStatusRefreshSerial + 1;
    state.dataStatusRefreshSerial = serial;
    try {
      const dataset = await requestJson(API.dataStatus);
      if (serial !== state.dataStatusRefreshSerial) return false;
      state.dataset = dataset;
      state.screeningInputsUncertain = false;
      renderScreenStatus();
      return true;
    } catch (_error) {
      if (serial !== state.dataStatusRefreshSerial) return false;
      state.screeningInputsUncertain = true;
      renderScreenStatus();
      return false;
    }
  }

  async function syncFullStockCatalog() {
    if (
      state.catalogSyncBusy
      || state.dataUpdateBusy
      || state.marketImportBusy
      || state.stockAddBusy.size > 0
      || state.screeningBusy
      || state.rulesSavingBusy
    ) return;
    const button = byId("syncStockCatalogButton");
    state.catalogSyncBusy = true;
    setDataActionsBusy(button, true, "正在更新目录…");
    setFeedback(
      "dataFeedback",
      "正在从 Web 更新完整沪深北股票目录；这不会自动加入或下载全部股票。",
    );
    try {
      const result = await requestJson(API.catalogSync, {
        method: "POST",
        body: JSON.stringify({}),
      });
      state.catalog = result;
      const unchangedCopy = result.unchanged ? "，内容与上次一致" : "";
      setFeedback(
        "dataFeedback",
        `完整股票目录已就绪：${result.rows || 0} 只，目录日期 ${result.snapshot_date || "未知"}${unchangedCopy}。`,
        false,
        true,
      );
      byId("dataFeedback").scrollIntoView({ block: "nearest" });
    } catch (error) {
      setFeedback(
        "dataFeedback",
        `${error.message} 已有本地股票和日线不会被删除。`,
        true,
      );
    } finally {
      state.catalogSyncBusy = false;
      setDataActionsBusy(button, false);
      renderDataSummary();
      if (clean(byId("dataStockSearch").value)) scheduleCatalogSearch();
    }
  }

  async function addCatalogStock(symbol) {
    if (
      !symbol
      || state.stockAddBusy.has(symbol)
      || state.stockAddBusy.size > 0
      || state.dataUpdateBusy
      || state.marketImportBusy
      || state.catalogSyncBusy
      || state.screeningBusy
      || state.rulesSavingBusy
    ) return;
    state.stockAddBusy.add(symbol);
    state.dataStatusRefreshSerial += 1;
    renderDataSummary();
    setFeedback(
      "dataFeedback",
      `正在加入 ${symbol} 并从 Web 导入三年前复权日线，请保持面板打开。`,
    );
    try {
      const result = await requestJson(API.stocks, {
        method: "POST",
        body: JSON.stringify({ symbol }),
      });
      try {
        if (result.dataset) {
          state.dataset = result.dataset;
          state.screeningInputsUncertain = false;
          const bootstrap = await requestJson(API.bootstrap);
          state.bootstrap = bootstrap;
          state.csrfToken = bootstrap.csrf_token || state.csrfToken;
          populateSymbolChoices();
          renderScreenStatus();
        } else {
          await refreshCoreData();
        }
      } catch (refreshError) {
        setFeedback(
          "dataFeedback",
          `${result.stock_name || symbol} 已加入本地库，但页面清单刷新失败：${refreshError.message} 重新打开面板即可读取最新状态。`,
          false,
          false,
          true,
        );
        return;
      }

      if (result.import_status === "FAILED") {
        setFeedback(
          "dataFeedback",
          `${result.stock_name || symbol} 已加入本地库，但日线暂未导入成功；可在搜索结果中点击“重试导入”。`,
          false,
          false,
          true,
        );
      } else if (result.import_status === "SAVED_WITH_WARNING") {
        setFeedback(
          "dataFeedback",
          `${result.stock_name || symbol} 已加入，本地已有日线；远端更新暂时失败，请稍后重试。`,
          false,
          false,
          true,
        );
      } else if (result.import_status === "ALREADY_PRESENT") {
        setFeedback(
          "dataFeedback",
          `${result.stock_name || symbol} 已在本地股票库中，没有重复加入。`,
          false,
          true,
        );
      } else {
        setFeedback(
          "dataFeedback",
          `${result.stock_name || symbol} 已加入，本地日线已导入。`,
          false,
          true,
        );
      }
      byId("dataFeedback").scrollIntoView({ block: "nearest" });
    } catch (error) {
      setFeedback(
        "dataFeedback",
        `${error.message} 本地原有数据未被删除。`,
        true,
      );
    } finally {
      state.stockAddBusy.delete(symbol);
      renderScreenStatus();
      if (clean(byId("dataStockSearch").value)) scheduleCatalogSearch();
    }
  }

  async function updateCatalogStock(symbol) {
    if (
      !symbol
      || state.stockAddBusy.has(symbol)
      || dataMutationBusy()
      || state.screeningBusy
      || state.rulesSavingBusy
    ) return;
    state.stockAddBusy.add(symbol);
    renderDataSummary();
    setFeedback("dataFeedback", `正在补齐 ${symbol} 的近三年前复权日线。`);
    try {
      const result = await requestJson(API.dataUpdate, {
        method: "POST",
        body: JSON.stringify({
          symbol,
          timeframe: "day",
          adjust: "qfq",
          history_years: 3,
        }),
      });
      try {
        await refreshCoreData();
      } catch (refreshError) {
        state.screeningInputsUncertain = true;
        setFeedback(
          "dataFeedback",
          `${symbol} 的数据已写入，但页面清单待同步：${refreshError.message} 请刷新面板读取最新状态。`,
          false,
          false,
          true,
        );
        return;
      }
      setFeedback(
        "dataFeedback",
        `${result.stock_name || symbol} 的本地日线已更新并重新对齐。`,
        false,
        true,
      );
    } catch (error) {
      setFeedback("dataFeedback", `${error.message} 原有本地数据未被删除。`, true);
    } finally {
      state.stockAddBusy.delete(symbol);
      renderScreenStatus();
      if (clean(byId("dataStockSearch").value)) scheduleCatalogSearch();
    }
  }

  async function updateAllMarketData() {
    if (
      state.dataUpdateBusy
      || state.marketImportBusy
      || state.catalogSyncBusy
      || state.stockAddBusy.size > 0
      || state.screeningBusy
      || state.rulesSavingBusy
    ) return;
    const entries = dataEntries();
    const button = byId("updateAllDataButton");
    if (!entries.length) {
      setFeedback("dataFeedback", "本地股票库还没有可更新的股票。", true);
      return;
    }
    state.dataUpdateBusy = true;
    state.dataStatusRefreshSerial += 1;
    setDataActionsBusy(button, true, `正在更新 0 / ${entries.length}`);
    const failures = [];
    let successCount = 0;
    try {
      for (let index = 0; index < entries.length; index += 1) {
        const symbol = entries[index].symbol;
        const position = index + 1;
        setBusy(button, true, `正在更新 ${position} / ${entries.length}`);
        setFeedback("dataFeedback", `正在更新 ${symbol}（${position} / ${entries.length}），请保持面板打开。`);
        try {
          await requestJson(API.dataUpdate, {
            method: "POST",
            body: JSON.stringify({
              symbol,
              timeframe: "day",
              adjust: "qfq",
              history_years: 3,
            }),
          });
          successCount += 1;
        } catch (error) {
          failures.push({ symbol, message: error.message });
        }
      }
      try {
        state.dataset = await requestJson(API.dataScan, {
          method: "POST",
          body: JSON.stringify({}),
        });
        state.screeningInputsUncertain = false;
        const bootstrap = await requestJson(API.bootstrap);
        state.bootstrap = bootstrap;
        state.csrfToken = bootstrap.csrf_token || state.csrfToken;
        populateSymbolChoices();
        renderScreenStatus();
      } catch (refreshError) {
        state.screeningInputsUncertain = true;
        renderScreeningAction();
        setFeedback(
          "dataFeedback",
          `行情更新已全部尝试（${successCount} 只成功，${failures.length} 只失败），但清单重建或页面刷新失败：${refreshError.message} 原有数据不会被删除。`,
          false,
          false,
          true,
        );
        showToast("行情已更新，但页面清单暂未刷新。");
        return;
      }
      const failedSymbols = failures.slice(0, 6).map((item) => item.symbol).join("、");
      const moreFailures = failures.length > 6 ? ` 等 ${failures.length} 只` : "";
      if (!failures.length) {
        setFeedback(
          "dataFeedback",
          `全部更新完成：${successCount} 只成功，当前 ${state.dataset.computable_count || 0} / ${state.dataset.discovered_count || 0} 只已对齐。`,
          false,
          true,
        );
      } else if (successCount) {
        setFeedback(
          "dataFeedback",
          `更新完成：${successCount} 只成功，${failures.length} 只失败（${failedSymbols}${moreFailures}）。失败股票已保留在未对齐清单。`,
          false,
          false,
          true,
        );
      } else {
        setFeedback(
          "dataFeedback",
          `本次全部更新失败：${failedSymbols}${moreFailures}。原有本地数据未被删除。`,
          true,
        );
      }
      byId("dataFeedback").scrollIntoView({ block: "nearest" });
      showToast(failures.length ? "全部股票已检查，部分更新失败。" : "全部股票数据已更新。");
    } catch (error) {
      setFeedback("dataFeedback", error.message, true);
      showToast(error.message, true);
    } finally {
      state.dataUpdateBusy = false;
      setDataActionsBusy(button, false);
      renderScreenStatus();
    }
  }

  function arrayBufferToBase64(buffer) {
    const bytes = new Uint8Array(buffer);
    const chunkSize = 0x8000;
    let binary = "";
    for (let offset = 0; offset < bytes.length; offset += chunkSize) {
      binary += String.fromCharCode(...bytes.subarray(offset, offset + chunkSize));
    }
    return window.btoa(binary);
  }

  function updateMarketCsvFileName() {
    const file = byId("marketCsvFile").files?.[0];
    byId("marketCsvFileName").textContent = file
      ? `${file.name} · ${(file.size / 1024 / 1024).toFixed(2)} MB`
      : "尚未选择";
    byId("marketCsvFile").setCustomValidity("");
  }

  async function importManualMarketData(event) {
    event.preventDefault();
    if (
      state.marketImportBusy
      || state.dataUpdateBusy
      || state.catalogSyncBusy
      || state.stockAddBusy.size > 0
      || state.screeningBusy
      || state.rulesSavingBusy
    ) return;
    const form = byId("marketCsvImportForm");
    const fileInput = byId("marketCsvFile");
    const file = fileInput.files?.[0];
    if (!form.reportValidity()) return;
    if (!file || !file.name.toLowerCase().endsWith(".csv")) {
      fileInput.setCustomValidity("请选择一个 CSV 文件。");
      fileInput.reportValidity();
      return;
    }
    if (file.size > 8 * 1024 * 1024) {
      fileInput.setCustomValidity("CSV 文件不能超过 8 MB。");
      fileInput.reportValidity();
      return;
    }

    const button = byId("importMarketCsvButton");
    state.marketImportBusy = true;
    state.dataStatusRefreshSerial += 1;
    setDataActionsBusy(button, true, "正在校验并导入…");
    setFormLocked(form, true, ["importMarketCsvButton"]);
    renderScreenStatus();
    setFeedback(
      "marketCsvFeedback",
      "正在校验列名、日期、价格关系和成交量单位，请稍候。",
    );
    try {
      const contentBase64 = arrayBufferToBase64(await file.arrayBuffer());
      const result = await requestJson(API.dataImport, {
        method: "POST",
        body: JSON.stringify({
          request_id: byId("marketCsvRequestId").value,
          symbol: byId("marketCsvSymbol").value,
          filename: file.name,
          content_base64: contentBase64,
          volume_unit: byId("marketCsvVolumeUnit").value,
          adjust: byId("marketCsvAdjust").value,
        }),
      });
      if (result.dataset) {
        state.dataset = result.dataset;
        state.screeningInputsUncertain = false;
      } else {
        state.screeningInputsUncertain = true;
      }
      let refreshWarning = "";
      try {
        const bootstrap = await requestJson(API.bootstrap);
        state.bootstrap = bootstrap;
        state.csrfToken = bootstrap.csrf_token || state.csrfToken;
        populateSymbolChoices();
      } catch (refreshError) {
        refreshWarning = ` 页面股票名称清单暂未刷新：${refreshError.message}`;
      }
      renderScreenStatus();
      const duplicates = Number(
        result.normalization?.exact_duplicates_removed || 0,
      );
      const membership = result.membership_status === "ADDED"
        ? "，并已加入本地股票库"
        : "";
      const duplicateCopy = duplicates
        ? `，合并 ${duplicates} 条完全重复日期`
        : "";
      setFeedback(
        "marketCsvFeedback",
        `${result.stock_name || result.symbol} 已标准化为 ${result.rows} 行（${result.start_date} 至 ${result.latest_date}）${duplicateCopy}${membership}。${refreshWarning}`,
        false,
        !refreshWarning,
        Boolean(refreshWarning),
      );
      setFeedback(
        "dataFeedback",
        `手动数据已导入；当前 ${state.dataset.computable_count || 0} / ${state.dataset.discovered_count || 0} 只已对齐。`,
        false,
        true,
      );
      byId("marketCsvRequestId").value = newRequestId();
      fileInput.value = "";
      updateMarketCsvFileName();
    } catch (error) {
      setFeedback(
        "marketCsvFeedback",
        `${error.message} 原有本地数据没有被删除。`,
        true,
      );
    } finally {
      state.marketImportBusy = false;
      setFormLocked(form, false, ["importMarketCsvButton"]);
      setDataActionsBusy(button, false);
      renderScreenStatus();
    }
  }


  function bindDataEvents() {
    byId("openDataButton").addEventListener("click", openDataDialog);
    byId("closeDataDialogButton").addEventListener("click", closeDataDialog);
    byId("dataDialog").addEventListener("cancel", (event) => {
      event.preventDefault();
      closeDataDialog();
    });
    byId("updateAllDataButton").addEventListener("click", updateAllMarketData);
    byId("syncStockCatalogButton").addEventListener("click", syncFullStockCatalog);
    byId("marketCsvFile").addEventListener("change", updateMarketCsvFileName);
    byId("marketCsvImportForm").addEventListener("submit", importManualMarketData);
    byId("dataStockSearch").addEventListener("input", scheduleCatalogSearch);
    byId("dataStockList").addEventListener("click", (event) => {
      const button = event.target.closest("[data-add-stock]");
      if (button) addCatalogStock(button.dataset.addStock);
      const updateButton = event.target.closest("[data-update-stock]");
      if (updateButton) updateCatalogStock(updateButton.dataset.updateStock);
    });
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible") void refreshDatasetStatus();
    });
    window.addEventListener("focus", () => void refreshDatasetStatus());
  }

export {
  bindDataEvents,
  refreshDatasetStatus,
  renderDataSummary,
};
