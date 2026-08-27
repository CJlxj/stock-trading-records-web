import {
  API,
  PanelCore,
  byId,
  configureViewLifecycle,
  cycleTheme,
  dataMutationBusy,
  initializeTheme,
  navigate,
  optionalJson,
  requestJson,
  setConnection,
  setFeedback,
  showToast,
  state,
} from "./platform.js";
import {
  bindDataEvents,
  refreshDatasetStatus,
} from "./modules/data.js";
import {
  bindRecordEvents,
  loadTrades,
  populateSymbolChoices,
  renderRecordsSurface,
  resetTradeForm,
  restoreTradeDraftCache,
  tradeDraftDirty,
  unsavedDialogDraft,
} from "./modules/records.js";
import {
  bindRuleEvents,
  initializeRuleSetWorkspace,
  restoreStandaloneRuleDraftCache,
  workspaceDirty,
} from "./modules/rules.js";
import {
  bindScreeningEvents,
  renderScreening,
  renderScreenStatus,
} from "./modules/screening.js";

  function bindPlatformEvents() {
    document.querySelectorAll("[data-view]").forEach((button) => {
      button.addEventListener("click", () => navigate(button.dataset.view));
    });
    document.querySelectorAll("[data-view-link]").forEach((link) => {
      link.addEventListener("click", (event) => {
        event.preventDefault();
        navigate(link.dataset.viewLink);
      });
    });
    byId("themeToggleButton").addEventListener("click", cycleTheme);
    window.addEventListener("hashchange", () => navigate(window.location.hash.slice(1), { focus: false }));
    window.addEventListener("beforeunload", (event) => {
      const hasRuleDraft = workspaceDirty();
      if (
        !dataMutationBusy()
        && !state.screeningBusy
        && !state.rulesSavingBusy
        && !state.tradeSavingBusy
        && !hasRuleDraft
        && !tradeDraftDirty()
        && !unsavedDialogDraft()
      ) return;
      event.preventDefault();
      event.returnValue = "";
    });
  }

  function bindEvents() {
    bindPlatformEvents();
    bindScreeningEvents();
    bindDataEvents();
    bindRuleEvents();
    bindRecordEvents();
  }

  async function loadCore() {
    try {
      const health = await requestJson(API.health);
      PanelCore.assertCompatibleContract(health);
      state.csrfToken = health.csrf_token || "";
      const [bootstrap, dataset, rules] = await Promise.all([
        requestJson(API.bootstrap),
        requestJson(API.dataStatus),
        requestJson(API.rules),
      ]);
      const [screeningResult, catalogResult] = await Promise.allSettled([
        optionalJson(API.screeningLatest),
        requestJson(API.catalogStatus),
      ]);
      state.bootstrap = bootstrap;
      state.csrfToken = bootstrap.csrf_token || state.csrfToken;
      state.dataset = dataset;
      state.rules = rules;
      state.screeningInputsUncertain = false;
      state.ruleInputsUncertain = false;
      state.screening = screeningResult.status === "fulfilled"
        ? screeningResult.value
        : null;
      state.catalog = catalogResult.status === "fulfilled"
        ? catalogResult.value
        : {
          ready: false,
          rows: 0,
          message: "完整股票目录暂时无法读取；本地数据、规则和筛选仍可使用。",
        };
      populateSymbolChoices();
      await initializeRuleSetWorkspace({ restore: true });
      resetTradeForm();
      renderScreenStatus();
      renderScreening();
      setConnection(true, "本地数据已连接");
      navigate(window.location.hash.slice(1) || "screen", { focus: false });
      restoreStandaloneRuleDraftCache();
      restoreTradeDraftCache();
      renderRecordsSurface();
    } catch (error) {
      setConnection(false, "连接失败");
      setFeedback("screenFeedback", error.message, true);
      showToast(error.message, true);
    }
  }

  configureViewLifecycle({
    tradeDraftDirty,
    loadTrades,
    refreshDatasetStatus,
  });
  bindEvents();
  initializeTheme();
  loadCore();
