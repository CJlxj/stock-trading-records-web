import {
  API,
  CUSTOM_RULE_DRAFT_STORAGE_KEY,
  PanelCore,
  RULE_DRAFT_STORAGE_KEY,
  askConfirm,
  byId,
  clean,
  dataMutationBusy,
  escapeHtml,
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
  writeReceiptMayBeUncertain,
} from "../platform.js";
import { renderDataSummary } from "./data.js";
import { renderScreenStatus } from "./screening.js";

  function standaloneRuleDraftPayload() {
    return {
      schemaVersion: 1,
      requestId: state.customRuleCreateRequestId,
      pendingReceipt: state.customRuleCreatePendingReceipt,
      name: clean(byId("ruleName")?.value),
      expression: clean(byId("ruleExpression")?.value),
      description: clean(byId("ruleDescription")?.value),
      tagId: clean(byId("ruleTag")?.value),
    };
  }

  function saveStandaloneRuleDraftCache() {
    if (!state.customRuleCreateRequestId) return;
    safeStorageSet(CUSTOM_RULE_DRAFT_STORAGE_KEY, JSON.stringify(standaloneRuleDraftPayload()));
  }

  function clearStandaloneRuleDraftCache() {
    safeStorageRemove(CUSTOM_RULE_DRAFT_STORAGE_KEY);
  }

  function restoreStandaloneRuleDraftCache() {
    const raw = safeStorageGet(CUSTOM_RULE_DRAFT_STORAGE_KEY);
    if (!raw || byId("ruleDialog").open) return false;
    try {
      const payload = JSON.parse(raw);
      if (payload.schemaVersion !== 1 || !clean(payload.requestId)) throw new Error("invalid cache");
      openRuleDialog();
      if (!byId("ruleDialog").open) return false;
      state.customRuleCreateRequestId = clean(payload.requestId);
      state.customRuleCreatePendingReceipt = payload.pendingReceipt === true;
      byId("ruleName").value = clean(payload.name);
      byId("ruleExpression").value = clean(payload.expression);
      byId("ruleDescription").value = clean(payload.description);
      // 旧缓存没有分类字段时按默认「我的规则」恢复，不丢弃待核对的草稿。
      renderRuleTagOptions(clean(payload.tagId));
      if (state.customRuleCreatePendingReceipt) {
        byId("confirmRuleDefinitionButton").textContent = "重新核对保存";
        setFeedback(
          "ruleDefinitionFeedback",
          "上次保存结果待核对；内容和请求编号已恢复，请直接点击“重新核对保存”。",
          false,
          false,
          true,
        );
        setFormLocked(
          byId("ruleDefinitionForm"),
          true,
          ["confirmRuleDefinitionButton", "closeRuleDialogButton"],
        );
      }
      saveStandaloneRuleDraftCache();
      byId("confirmRuleDefinitionButton").focus({ preventScroll: true });
      return true;
    } catch (_error) {
      clearStandaloneRuleDraftCache();
      return false;
    }
  }


  function activeRuleSet() {
    return state.rules?.rule_sets?.active || null;
  }


  function registryRule(ref) {
    const [id, versionText] = clean(ref).split("@");
    const version = Number(versionText);
    return (state.rules?.registry?.items || []).find((item) => (
      item.id === id && Number(item.version) === version
    )) || null;
  }

  // 规则库里的每条规则都只有一个来源：注册表。组合引用的是 `id@version`，
  // 规则库自己按 `id` 显示当前版本。
  function libraryRules() {
    return (state.rules?.registry?.items || [])
      .filter((item) => item.status === "ACTIVE" && item.kind === "scored")
      .filter((item, index, all) => (
        all.findIndex((candidate) => candidate.id === item.id) === index
      ));
  }

  function libraryRule(ruleId) {
    const id = clean(ruleId).split("@")[0];
    return libraryRules().find((item) => item.id === id) || null;
  }

  function renewRuleApplyRequest() {
    state.ruleApplyRequestId = newRequestId();
  }

  function ruleDescription(item) {
    let text = clean(item?.plain_template || item?.description || item?.group_label || "收盘规则");
    const schema = item?.params_schema || {};
    return text.replace(/\{([a-zA-Z0-9_]+)\}/g, (_match, key) => {
      const value = schema[key]?.default;
      return value === undefined || value === null ? "设定值" : String(value);
    });
  }

  function ruleExpression(item) {
    let expression = clean(
      item?.implementation?.normalized_expression
        || item?.implementation?.expression
        || "",
    );
    Object.entries(item?.params_schema || {}).forEach(([name, spec]) => {
      if (spec?.default === undefined || spec?.default === null) return;
      const escapedName = name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
      expression = expression.replace(
        new RegExp(`\\b${escapedName}\\b`, "g"),
        String(spec.default),
      );
    });
    return expression;
  }

  function renderRuleEditorContract() {
    const contract = state.rules?.editor_contract || {};
    byId("ruleExpressionNotice").textContent = contract.notice || "只支持本地日线行情字段和安全函数。";
    byId("ruleFieldList").innerHTML = (contract.fields || []).map((item) => (
      `<span title="${escapeHtml(item.label)}">${escapeHtml(item.name)}</span>`
    )).join("");
    byId("ruleFunctionList").textContent = (contract.functions || []).length
      ? `可用函数：${contract.functions.join("、")}`
      : "";
    byId("ruleExpressionExamples").innerHTML = (contract.examples || []).map((item) => (
      `<button type="button" data-rule-example="${escapeHtml(item.expression)}">${escapeHtml(item.label)}</button>`
    )).join("");
  }

  const TAG_LABEL_MAXIMUM = 12;

  function ruleTags() {
    return Array.isArray(state.rules?.rule_tags?.items)
      ? state.rules.rule_tags.items
      : [];
  }

  function renderRuleTagOptions(selected) {
    const target = clean(selected);
    const options = [
      { id: "", label: "未分类" },
      ...ruleTags().map((tag) => ({
        id: clean(tag.id),
        label: clean(tag.label) || clean(tag.id),
      })),
    ];
    byId("ruleTag").innerHTML = options.map((tag) => (
      `<option value="${escapeHtml(tag.id)}">${escapeHtml(tag.label)}</option>`
    )).join("");
    // 标签可能已被删除；那条规则就落回「未分类」，和列表里看到的一致。
    byId("ruleTag").value = options.some((tag) => tag.id === target) ? target : "";
  }

  // 规则弹窗只做一件事：在规则库里新增或保存一条规则。
  // 组合编辑器不再内联建规则，所以这里没有「草稿」这条分支。
  function openRuleDialog(ruleId = "") {
    if (ruleInteractionLocked() || ruleDraftBlocked() || state.customRuleCreateBusy) return;
    const item = ruleId ? libraryRule(ruleId) : null;
    if (ruleId && !item) return;
    state.ruleDialogOpener = document.activeElement;
    state.ruleEditingRef = item ? `${item.id}@${item.version}` : "";
    state.customRuleCreateRequestId = newRequestId();
    state.customRuleCreatePendingReceipt = false;
    byId("ruleDialogTitle").textContent = item ? "编辑规则" : "新增规则";
    byId("ruleName").value = item?.name || "";
    byId("ruleExpression").value = ruleExpression(item);
    byId("ruleDescription").value = item?.description || "";
    renderRuleTagOptions(item?.tag_id);
    // 正常态不放流程解释；这个位置留给字段错误与回执不确定。
    setFeedback("ruleDefinitionFeedback", "");
    byId("confirmRuleDefinitionButton").textContent = item ? "保存规则" : "新增规则";
    state.ruleDialogInitialSignature = ruleDialogSignature();
    byId("ruleDialog").showModal();
    saveStandaloneRuleDraftCache();
    byId(item ? "ruleExpression" : "ruleName").focus();
  }

  function ruleDialogSignature() {
    return JSON.stringify({
      name: clean(byId("ruleName").value),
      expression: clean(byId("ruleExpression").value),
      description: clean(byId("ruleDescription").value),
      tagId: clean(byId("ruleTag").value),
    });
  }

  async function closeRuleDialog(options = {}) {
    const force = options?.force === true;
    if (!force && state.customRuleCreateBusy) return false;
    if (!force && state.customRuleCreatePendingReceipt) {
      setFeedback(
        "ruleDefinitionFeedback",
        "保存结果尚未核对，请恢复连接后点击“重新核对保存”。为避免重复规则，核对前不能关闭。",
        false,
        false,
        true,
      );
      byId("confirmRuleDefinitionButton").focus({ preventScroll: true });
      return false;
    }
    const dirty = ruleDialogSignature() !== state.ruleDialogInitialSignature;
    if (!force && dirty && !await askConfirm({
      title: "放弃这条规则？",
      message: "还没有保存，关闭后填写的内容会丢失。",
      confirmLabel: "放弃",
      cancelLabel: "继续编辑",
      danger: true,
    })) return false;
    byId("ruleDialog").close();
    clearStandaloneRuleDraftCache();
    state.ruleEditingRef = "";
    state.customRuleCreateRequestId = "";
    state.customRuleCreatePendingReceipt = false;
    state.ruleDialogInitialSignature = "";
    const opener = state.ruleDialogOpener;
    state.ruleDialogOpener = null;
    if (opener?.isConnected) opener.focus({ preventScroll: true });
    return true;
  }

  function applyRuleExample(event) {
    const button = event.target.closest("[data-rule-example]");
    if (!button) return;
    byId("ruleExpression").value = button.dataset.ruleExample;
    saveStandaloneRuleDraftCache();
    byId("ruleExpression").focus();
  }

  function workspaceRuleSetItems() {
    return PanelCore.ruleSetSummaries(state.rules?.rule_sets || {});
  }

  function workspaceRuleSetItem(id = state.selectedRuleSetId) {
    return PanelCore.ruleSetSummaries(state.rules?.rule_sets || {})
      .find((item) => item.id === id) || null;
  }

  function workspaceIsExactCurrent(id, version) {
    return PanelCore.ruleSetExactTargetOutcome(
      { active: activeRuleSet() },
      id,
      version,
    ).isExactCurrent;
  }

  function ruleInteractionLocked() {
    return ["selecting", "saving", "activating", "reconciling"].includes(state.ruleOperation.phase)
      || state.rulesSavingBusy
      || state.screeningBusy
      || dataMutationBusy();
  }

  function ruleDraftBlocked() {
    return ["conflict", "uncertain"].includes(state.ruleOperation.phase)
      || Boolean(state.ruleSavePendingVerification)
      || state.ruleInputsUncertain;
  }

  function beginRuleOperation(phase) {
    const token = state.ruleOperation.token + 1;
    state.ruleOperation = { phase, token };
    return token;
  }

  function ruleOperationIsCurrent(token) {
    return state.ruleOperation.token === token;
  }

  function setRuleOperationPhase(token, phase) {
    if (!ruleOperationIsCurrent(token)) return false;
    state.ruleOperation = { phase, token };
    return true;
  }

  function resetRuleOperation() {
    state.ruleOperation = { phase: "idle", token: state.ruleOperation.token + 1 };
    state.ruleSavePendingVerification = null;
  }

  function workspaceEditorMode() {
    return PanelCore.ruleSetEditorMode(
      state.selectedRuleSetExact || workspaceRuleSetItem() || {},
    );
  }

  function workspaceIsAdvanced() {
    return state.ruleSetDraftKind === "existing"
      && workspaceEditorMode() === "legacy_advanced"
      && state.ruleDetailMode !== "edit";
  }

  function workspaceCapability(item, name, fallback = true) {
    const nested = item?.capabilities?.[name];
    if (typeof nested === "boolean") return nested;
    const flat = item?.[`can_${name}`];
    if (typeof flat === "boolean") return flat;
    return fallback;
  }

  function workspaceIsReadOnly() {
    const item = workspaceRuleSetItem();
    return state.ruleDetailMode !== "edit"
      || (state.ruleSetDraftKind === "existing" && !workspaceCapability(item, "edit", true));
  }

  function workspaceName() {
    return clean(byId("ruleSetName")?.value ?? state.ruleSetDraftName);
  }

  function workspaceSignature() {
    return JSON.stringify({
      kind: state.ruleSetDraftKind,
      id: state.selectedRuleSetId,
      version: state.selectedRuleSetVersion,
      name: workspaceName(),
      refs: Array.from(state.ruleDraftRefs),
      secondaryRefs: Array.from(state.secondaryRuleDraftRefs),
    });
  }

  function workspaceDirty() {
    if (["new", "copy"].includes(state.ruleSetDraftKind)) return state.ruleEditorOpen;
    return Boolean(state.rulePristineSignature)
      && workspaceSignature() !== state.rulePristineSignature;
  }

  function workspaceTargetExists(id) {
    return Boolean(clean(id)) && workspaceRuleSetItems().some((item) => clean(item.id) === clean(id));
  }

  function preserveWorkspaceAsNewDraft(payload = null) {
    if (payload) {
      state.ruleSetDraftName = clean(payload.name) || state.ruleSetDraftName;
    }
    state.selectedRuleSetId = "";
    state.selectedRuleSetVersion = null;
    state.selectedRuleSetExact = null;
    state.ruleSetDraftKind = "new";
    state.ruleEditorOpen = true;
    state.ruleDetailMode = "edit";
    state.ruleSavePendingVerification = null;
    state.ruleInputsUncertain = false;
    state.ruleOperation = { phase: "idle", token: state.ruleOperation.token + 1 };
    state.rulePristineSignature = "";
    renewRuleApplyRequest();
    safeStorageRemove(RULE_DRAFT_STORAGE_KEY);
    saveWorkspaceDraftCache();
  }

  function targetDeletedError() {
    const error = new Error("原组合已被删除，本次目标不再可用。");
    error.code = "RULE_SET_TARGET_DELETED";
    return error;
  }

  function saveWorkspaceDraftCache() {
    if (!workspaceDirty() && !state.ruleSavePendingVerification) {
      safeStorageRemove(RULE_DRAFT_STORAGE_KEY);
      return;
    }
    safeStorageSet(RULE_DRAFT_STORAGE_KEY, JSON.stringify({
      schemaVersion: 5,
      kind: state.ruleSetDraftKind,
      id: state.selectedRuleSetId,
      version: state.selectedRuleSetVersion,
      ruleSetHash: clean(state.selectedRuleSetExact?.rule_set_hash || workspaceRuleSetItem()?.rule_set_hash),
      name: workspaceName(),
      refs: Array.from(state.ruleDraftRefs),
      secondaryRefs: Array.from(state.secondaryRuleDraftRefs),
      editingStage: state.ruleEditingStage,
      requestId: state.ruleApplyRequestId,
      operationPhase: ["conflict", "uncertain"].includes(state.ruleOperation.phase)
        ? state.ruleOperation.phase
        : "idle",
      pendingVerification: state.ruleSavePendingVerification,
    }));
  }

  function restoreWorkspaceDraftCache() {
    const raw = safeStorageGet(RULE_DRAFT_STORAGE_KEY);
    if (!raw) return false;
    try {
      const payload = JSON.parse(raw);
      if (![3, 4, 5].includes(payload.schemaVersion) || !Array.isArray(payload.refs) || !Array.isArray(payload.definitions)) {
        return false;
      }
      const cachedPending = payload.pendingVerification || null;
      const recoverablePending = new Set([
        "delete_receipt",
        "activation_receipt",
        "save_receipt",
        "saved_target",
      ]).has(cachedPending?.stage);
      const listOnlyRecovery = ["activation_receipt", "delete_receipt"]
        .includes(cachedPending?.stage);
      let cachedConflict = false;
      if (payload.kind === "existing") {
        const item = workspaceRuleSetItem(payload.id);
        cachedConflict = !item
          ? !recoverablePending
          : clean(payload.ruleSetHash) !== clean(item.rule_set_hash);
      }
      state.ruleSetDraftKind = ["new", "copy"].includes(payload.kind) ? payload.kind : "existing";
      state.selectedRuleSetId = clean(payload.id);
      state.selectedRuleSetVersion = Number(payload.version) || null;
      state.selectedRuleSetExact = null;
      state.ruleSetDraftName = clean(payload.name);
      state.ruleDraftRefs = new Set(payload.refs);
      state.secondaryRuleDraftRefs = new Set(Array.isArray(payload.secondaryRefs) ? payload.secondaryRefs : []);
      state.ruleEditingStage = payload.editingStage === "secondary" ? "secondary" : "primary";
      state.ruleApplyRequestId = clean(payload.requestId) || newRequestId();
      const cachedPhase = cachedConflict
        ? "conflict"
        : (recoverablePending
          ? "uncertain"
          : (["conflict", "uncertain"].includes(payload.operationPhase) ? payload.operationPhase : "idle"));
      state.ruleOperation = { phase: cachedPhase, token: state.ruleOperation.token + 1 };
      state.ruleEditorOpen = !listOnlyRecovery;
      state.ruleDetailMode = listOnlyRecovery ? "view" : "edit";
      const { activate: _ignoredActivate, ...normalizedPending } = cachedPending || {};
      state.ruleSavePendingVerification = cachedPending && !cachedPending.stage
        ? {
          ...normalizedPending,
          stage: (
            clean(cachedPending.id)
            && Number(cachedPending.version) > 0
            && Number(cachedPending.version) !== Number(payload.version)
          ) ? "saved_target" : "save_receipt",
          existing: cachedPending.existing ?? payload.kind === "existing",
        }
        : (cachedPending ? normalizedPending : null);
      if (["conflict", "uncertain"].includes(cachedPhase)) state.ruleInputsUncertain = true;
      return true;
    } catch (_error) {
      return false;
    }
  }

  function workspaceExactFromPayload(payload = {}) {
    if (payload.version?.scored) return payload.version;
    if (payload.rule_set_version?.scored) return payload.rule_set_version;
    if (payload.scored) return payload;
    return null;
  }

  async function fetchWorkspaceRuleSet(item, requestedVersion = null) {
    if (!item) return null;
    const version = Number(
      requestedVersion || item.latest_version || item.usable_version || item.active_version || item.version,
    );
    const active = activeRuleSet();
    if (
      active?.id === item.id
      && (!version || Number(active.version) === version)
    ) return active;
    if (item.scored && (!version || Number(item.version) === version)) return item;
    if (item.latest?.scored && (!version || Number(item.latest.version) === version)) return item.latest;
    const suffix = version ? `?version=${encodeURIComponent(version)}` : "";
    const payload = await requestJson(`${API.ruleSets}/${encodeURIComponent(item.id)}${suffix}`);
    return workspaceExactFromPayload(payload) || payload.active || null;
  }

  function workspaceSelectedRuleMarkup(token, index, total, { readOnly = false, legacyLabel = "", stage = "primary" } = {}) {
    const item = registryRule(token);
    if (!item) {
      return `<article class="rule-option-row rule-selected-row is-missing" data-selected-rule="${escapeHtml(token)}"><span class="rule-order-number">${index + 1}</span><p>规则 ${escapeHtml(token)} 暂时无法读取</p></article>`;
    }
    const formula = ruleExpression(item);
    const controlsDisabled = workspaceIsReadOnly() || ruleInteractionLocked() || ruleDraftBlocked();
    return `<article class="rule-option-row rule-selected-row" data-selected-rule="${escapeHtml(token)}">
      <span class="rule-order-number" aria-label="顺序 ${index + 1}">${index + 1}</span>
      <div class="rule-copy">
        <strong>${escapeHtml(item.name || token)}</strong>
        ${formula ? `<code>${escapeHtml(formula)}</code>` : ""}
        ${legacyLabel ? `<span class="rule-card-tag">${escapeHtml(legacyLabel)}</span>` : ""}
      </div>
      ${readOnly ? "" : `<div class="rule-row-actions">
        <div class="rule-order-actions" aria-label="调整${escapeHtml(item.name || token)}的顺序">
          <button type="button" data-move-rule="up" data-rule-stage="${stage}" data-rule-token="${escapeHtml(token)}" aria-label="上移${escapeHtml(item.name || token)}" ${controlsDisabled ? "disabled" : (index === 0 ? 'aria-disabled="true"' : "")}>↑</button>
          <button type="button" data-move-rule="down" data-rule-stage="${stage}" data-rule-token="${escapeHtml(token)}" aria-label="下移${escapeHtml(item.name || token)}" ${controlsDisabled ? "disabled" : (index === total - 1 ? 'aria-disabled="true"' : "")}>↓</button>
        </div>
        <button class="rule-remove-button" type="button" data-remove-rule="${escapeHtml(token)}" data-rule-stage="${stage}" ${controlsDisabled ? "disabled" : ""}>移除</button>
      </div>`}
    </article>`;
  }

  function renderWorkspaceSelectedRules() {
    const advanced = workspaceIsAdvanced();
    if (advanced) {
      const flow = PanelCore.ruleSetFlowSummary(state.selectedRuleSetExact || {});
      const required = flow.groups.required;
      const veto = flow.groups.veto;
      const scored = flow.groups.scored;
      const secondary = flow.groups.secondary;
      const legacyPrimarySummary = [
        required.count ? `额外前置 ${required.count} 条` : "",
        veto.count ? `触发排除 ${veto.count} 条` : "",
        scored.count ? `${scored.count} 条中至少满足 ${scored.minimum} 条` : "未设置候选规则",
      ].filter(Boolean).join(" · ");
      byId("primaryRulesSummary").textContent = `旧版规则：${legacyPrimarySummary}`;
      byId("secondaryRulesSummary").textContent = secondary.count
        ? `${secondary.count} 条中至少满足 ${secondary.minimum} 条`
        : "该历史组合未启用";
      const scoredRows = scored.refs.length
        ? scored.refs.map((ref, index) => workspaceSelectedRuleMarkup(ref, index, scored.refs.length, {
          readOnly: true,
          legacyLabel: "旧版候选",
        })).join("")
        : '<p class="empty-inline">该历史组合未设置候选规则。</p>';
      const legacyConditions = [
        ...required.refs.map((ref) => ({ ref, label: "额外前置" })),
        ...veto.refs.map((ref) => ({ ref, label: "触发后不入选" })),
      ];
      const legacyConditionDetails = legacyConditions.length
        ? `<details class="legacy-extra-conditions">
          <summary><span>旧版附加条件</span><strong>${legacyConditions.length} 条</strong></summary>
          <div>${legacyConditions.map((entry, index) => workspaceSelectedRuleMarkup(entry.ref, index, legacyConditions.length, {
            readOnly: true,
            legacyLabel: entry.label,
          })).join("")}</div>
        </details>`
        : "";
      byId("ruleList").innerHTML = scoredRows + legacyConditionDetails;
      byId("secondaryRuleList").innerHTML = secondary.refs.length
        ? secondary.refs.map((ref, index) => workspaceSelectedRuleMarkup(ref, index, secondary.refs.length, { readOnly: true, legacyLabel: "旧版二级", stage: "secondary" })).join("")
        : '<p class="empty-inline">该历史组合未启用二级筛选。</p>';
      const total = required.count + veto.count + scored.count + secondary.count;
      byId("primaryRuleCount").textContent = `${scored.count} 条`;
      byId("secondaryRuleCount").textContent = `${secondary.count} 条`;
      return;
    }
    byId("primaryRulesSummary").textContent = "全部满足才进入候选；顺序决定排序";
    byId("secondaryRulesSummary").textContent = "一级通过后，所选规则全部满足";
    const readOnly = workspaceIsReadOnly();
    const primaryRows = Array.from(state.ruleDraftRefs);
    const secondaryRows = Array.from(state.secondaryRuleDraftRefs);
    byId("ruleList").innerHTML = primaryRows.length
      ? primaryRows.map((ref, index) => workspaceSelectedRuleMarkup(ref, index, primaryRows.length, { stage: "primary", readOnly })).join("")
      : `<p class="empty-inline">${readOnly ? "该组合没有一级规则。" : "还没有一级规则。点右上角「+ 添加规则」从规则库加入。"}</p>`;
    byId("secondaryRuleList").innerHTML = secondaryRows.length
      ? secondaryRows.map((ref, index) => workspaceSelectedRuleMarkup(ref, index, secondaryRows.length, { stage: "secondary", readOnly })).join("")
      : `<p class="empty-inline">${readOnly ? "该组合没有二级规则，一级通过后直接进入排序。" : "还没有二级规则。点右上角「+ 添加规则」加入；不加则一级通过后直接进入排序。"}</p>`;
    byId("primaryRuleCount").textContent = `${primaryRows.length} 条`;
    byId("secondaryRuleCount").textContent = `${secondaryRows.length} 条`;
  }

  // 规则页四个互斥主状态：组合管理 / 组合编辑器 / 独立规则库 / 上下文规则选择器。
  // 主内容区同一时间只渲染一个；宽度变化绝不参与状态转换。
  const RULES_SURFACES = ["sets", "editor", "library", "picker"];

  function rulesSurface() {
    return RULES_SURFACES.includes(state.rulesSurface) ? state.rulesSurface : "sets";
  }

  // 只有在选择器里才允许勾选规则；独立规则库永远不改任何组合。
  function workspaceLibraryCanSelect() {
    return rulesSurface() === "picker"
      && Boolean(state.pickerSession)
      && state.ruleEditorOpen
      && !workspaceIsReadOnly()
      && !workspaceIsAdvanced();
  }

  function renderRulesSurface() {
    const surface = rulesSurface();
    const editing = surface === "editor";
    const picking = surface === "picker";
    const session = state.pickerSession;
    byId("ruleSetsView").hidden = surface !== "sets";
    byId("ruleEditorView").hidden = !editing;
    byId("ruleLibraryView").hidden = !(surface === "library" || picking);
    byId("rulePickerCrumb").hidden = !picking;
    byId("rulePickerBar").hidden = !picking;
    // 编辑或选择期间两个子入口不作为主要切换控件，避免未保存草稿被切走。
    byId("rulesSurfaceTabs").hidden = editing || picking;
    [["setsSurfaceTab", "sets"], ["librarySurfaceTab", "library"]].forEach(([id, key]) => {
      byId(id).setAttribute("aria-selected", String(surface === key));
      byId(id).classList.toggle("is-active", surface === key);
    });
    const stageLabel = session?.stage === "secondary" ? "二级" : "一级";
    if (picking && session) {
      byId("rulePickerTitle").textContent = `选择${stageLabel}规则`;
      byId("rulePickerCount").textContent = `已选择 ${session.pending.size} 条`;
      const confirm = byId("rulePickerConfirmButton");
      confirm.textContent = `加入${stageLabel}（${session.pending.size}）`;
      confirm.disabled = session.pending.size === 0;
    }
    byId("ruleLibraryIntro").textContent = picking
      ? "本次选择只加入组合草稿，保存组合后才生成新版本。"
      : "浏览或新增规则不会改变任何组合。";
  }

  async function setRulesSurface(next, { focus = true } = {}) {
    if (!RULES_SURFACES.includes(next) || next === rulesSurface()) return;
    // 有脏草稿时不能被子入口静默切走。
    if (next !== "editor" && state.ruleEditorOpen && workspaceDirty()) {
      if (!await askConfirm({
        title: "放弃未保存的修改？",
        message: "离开后当前组合的改动不会保留。",
        confirmLabel: "放弃并离开",
        cancelLabel: "继续编辑",
        danger: true,
      })) return;
      await cancelWorkspaceRuleEdit({ skipConfirm: true });
    }
    if (next !== "picker") state.pickerSession = null;
    state.rulesSurface = next;
    renderRuleSetWorkspace({ announce: false, preserveInputs: true });
    if (!focus) return;
    const heading = {
      sets: "ruleSetPickerHeading",
      editor: "enabledRulesHeading",
      library: "ruleLibraryHeading",
      picker: "rulePickerTitle",
    }[next];
    byId(heading)?.focus({ preventScroll: true });
  }

  async function leaveRuleEditor() {
    const opener = state.ruleEditorOpener;
    if (state.ruleEditorOpen && workspaceDirty()) {
      if (!await askConfirm({
        title: "放弃未保存的修改？",
        message: "返回组合列表后，当前改动不会保留。",
        confirmLabel: "放弃并返回",
        cancelLabel: "继续编辑",
        danger: true,
      })) return;
    }
    await cancelWorkspaceRuleEdit({ skipConfirm: true });
    state.pickerSession = null;
    state.rulesSurface = "sets";
    renderRuleSetWorkspace({ announce: false, preserveInputs: true });
    const restore = opener && document.contains(opener) ? opener : byId("newRuleSetButton");
    restore?.focus({ preventScroll: true });
  }

  // 选择器是一次性事务：进入时记录目标级别与已有引用，
  // 新选择先进临时集合，只有确认才追加到组合草稿；取消则草稿完全不变。
  function openRulePicker(stage, opener) {
    if (!state.ruleEditorOpen || workspaceIsReadOnly() || workspaceIsAdvanced()) return;
    if (ruleInteractionLocked() || ruleDraftBlocked()) return;
    const target = stage === "secondary" ? "secondary" : "primary";
    state.ruleEditingStage = target;
    state.pickerSession = {
      stage: target,
      pending: new Set(),
      order: [],
      opener: opener || null,
      scrollY: window.scrollY,
    };
    state.rulesSurface = "picker";
    renderRuleSetWorkspace({ announce: false, preserveInputs: true });
    byId("rulePickerTitle").focus({ preventScroll: true });
  }

  function toggleRulePickerSelection(token) {
    const session = state.pickerSession;
    if (!token || !session || !workspaceLibraryCanSelect()) return;
    // 已在一级或二级的同源规则不可再次选择。
    if (workspaceHasRuleSource(token)) return;
    if (session.pending.has(token)) {
      session.pending.delete(token);
      session.order = session.order.filter((item) => item !== token);
    } else {
      session.pending.add(token);
      session.order.push(token);
    }
    renderRuleSetWorkspace({ announce: false, preserveInputs: true });
  }

  function closeRulePicker({ apply }) {
    const session = state.pickerSession;
    if (!session) return;
    const added = apply ? [...session.order] : [];
    if (apply && added.length) {
      const targetRefs = session.stage === "secondary"
        ? state.secondaryRuleDraftRefs
        : state.ruleDraftRefs;
      // 已有顺序不变；新规则按本次选择顺序追加。
      added.forEach((token) => targetRefs.add(token));
      renewRuleApplyRequest();
    }
    const opener = session.opener;
    state.pickerSession = null;
    state.rulesSurface = "editor";
    renderRuleSetWorkspace({ preserveInputs: true });
    if (apply && added.length) saveWorkspaceDraftCache();
    setFeedback(
      "ruleFeedback",
      apply && added.length
        ? `已加入 ${added.length} 条${session.stage === "secondary" ? "二级" : "一级"}规则；保存组合后才生成新版本。`
        : "",
    );
    const restore = opener && document.contains(opener) ? opener : byId("enabledRulesHeading");
    restore?.focus({ preventScroll: true });
    window.scrollTo({ top: session.scrollY || 0, behavior: "auto" });
  }

  function workspaceRuleFocusTarget(token) {
    const item = registryRule(token) || {};
    return {
      groupId: clean(item.tag_id),
      name: clean(item.name || token || "规则"),
    };
  }

  // 一条规则只能属于一个筛选级别；同一条规则的不同版本也算同一条。
  function workspaceRuleSourceStage(token) {
    const source = clean(token).split("@")[0];
    const matches = (ref) => clean(ref).split("@")[0] === source;
    if ([...state.ruleDraftRefs].some(matches)) return "primary";
    if ([...state.secondaryRuleDraftRefs].some(matches)) return "secondary";
    return "";
  }

  function workspaceHasRuleSource(token) {
    return Boolean(workspaceRuleSourceStage(token));
  }

  function focusWorkspaceLibrarySummary(groupId) {
    const group = Array.from(byId("ruleLibraryGroups").querySelectorAll("[data-rule-library-group]"))
      .find((entry) => entry.dataset.ruleLibraryGroup === groupId);
    group?.querySelector("summary")?.focus({ preventScroll: true });
    state.ruleLibraryFocusGroupId = "";
  }

  function focusLibraryRule(ref) {
    window.requestAnimationFrame(() => {
      const row = Array.from(byId("ruleLibraryGroups").querySelectorAll("[data-library-rule-ref]"))
        .find((entry) => entry.dataset.libraryRuleRef === ref);
      (row?.querySelector("button") || row)?.focus({ preventScroll: true });
      state.customRuleFocusRef = "";
    });
  }

  function focusWorkspaceMoveControl(token, direction) {
    const row = Array.from(document.querySelectorAll("#ruleList [data-selected-rule], #secondaryRuleList [data-selected-rule]"))
      .find((entry) => entry.dataset.selectedRule === token);
    row?.querySelector(`[data-move-rule="${direction}"]`)?.focus({ preventScroll: true });
  }

  // 每条规则一视同仁：都能编辑（生成新版本）和删除（无组合引用时）。
  // 一条规则只占一行：名称 → 表达式 → 说明，靠字重、等宽底色和弱化色区分，
  // 不再各自成行，表达式也不用「规则表达式」标签再解释一遍自己。三种态：
  //   浏览 —— 没有任何按钮，规则名本身是编辑入口；
  //   管理 —— 追加「删除规则」，与分类的「删除标签」区分开；
  //   选择 —— 只有一个选择框，任何改动入口都不出现。
  function workspaceLibraryItemMarkup(item, { canSelect, manage, busy } = {}) {
    const ref = `${item.id}@${item.version}`;
    const formula = ruleExpression(item);
    const name = item.name || ref;
    const targetLabel = state.ruleEditingStage === "secondary" ? "二级" : "一级";
    const usedStage = canSelect ? workspaceRuleSourceStage(ref) : "";
    const usedLabel = usedStage === "secondary" ? "二级" : "一级";
    const picked = canSelect && Boolean(state.pickerSession?.pending.has(ref));
    const ruleId = escapeHtml(clean(item.id));
    const title = canSelect
      ? `<strong>${escapeHtml(name)}</strong>`
      : `<button class="record-open" type="button" data-edit-library-rule="${ruleId}" title="编辑${escapeHtml(name)}" ${busy ? "disabled" : ""}>${escapeHtml(name)}</button>`;
    const remove = manage && !canSelect
      ? `<button class="row-action is-ghost is-danger" type="button" data-delete-library-rule="${ruleId}" aria-label="删除规则：${escapeHtml(name)}" ${busy ? "disabled" : ""}>删除规则</button>`
      : "";
    const action = canSelect
      ? `<label class="rule-library-add-button${usedStage ? " is-used" : ""}" title="${usedStage ? `同一条规则只能属于一个筛选级别，当前在${usedLabel}筛选。` : ""}"><input type="checkbox" data-add-library-rule="${escapeHtml(ref)}" aria-label="${usedStage ? `已在${usedLabel}筛选，不能重复选择` : `选择加入${targetLabel}筛选`}：${escapeHtml(name)}" ${picked ? "checked" : ""} ${busy || usedStage ? "disabled" : ""}><span>${usedStage ? `已在${usedLabel}` : "选择"}</span></label>`
      : "";
    // 表达式是规则的附属技术信息：等宽浅底的一枚小片，紧跟在规则名后面。
    const expression = formula
      ? `<code class="rule-expression">${escapeHtml(formula)}</code>`
      : "";
    return `<article class="record-row rule-library-item" role="listitem" data-library-rule-ref="${escapeHtml(ref)}" tabindex="-1">
      <span class="record-row-main">
        ${title}
        ${expression}
        <small>${escapeHtml(ruleDescription(item))}</small>
      </span>
      <span class="record-row-actions">${remove}${action}</span>
    </article>`;
  }

  function renderWorkspaceLibrary() {
    const canSelect = workspaceLibraryCanSelect();
    const groups = PanelCore.ruleTagGroups(
      libraryRules(),
      ruleTags(),
      state.ruleSearchQuery,
      new Set(),
    );
    const total = groups.reduce((sum, group) => sum + group.count, 0);
    const queryActive = Boolean(clean(state.ruleSearchQuery));
    const libraryBusy = ruleInteractionLocked()
      || ruleDraftBlocked()
      || state.customRuleCreateBusy
      || state.customRuleDeleteBusy;
    // 管理态只在规则库本体里存在；选择器永远是只读的挑选界面。
    const manage = !canSelect && state.libraryManageMode;
    byId("availableRuleCount").textContent = `${total} 条`;
    // 选择规则时整条工具栏都不出现：这里不能、也不该改动规则库。
    document.querySelector(".rule-library-tools").hidden = canSelect;
    byId("manageLibraryButton").setAttribute("aria-pressed", String(manage));
    byId("manageLibraryButton").classList.toggle("is-active", manage);
    byId("manageLibraryButton").textContent = manage ? "完成" : "管理";
    byId("manageLibraryButton").disabled = libraryBusy;
    // 建标签和补默认规则都是维护动作，收进管理态，浏览时不占位置。
    byId("addRuleTagButton").hidden = !manage;
    byId("importDefaultRulesButton").hidden = !manage;
    byId("addRuleTagButton").disabled = libraryBusy;
    byId("importDefaultRulesButton").disabled = libraryBusy;
    // 常态不解释显而易见的操作；只有搜索结果和选择器的层级互斥需要说一句。
    byId("ruleSearchSummary").textContent = queryActive
      ? `找到 ${total} 条规则`
      : (canSelect ? "同一条规则只能属于一个筛选级别" : "");
    byId("ruleLibraryGroups").innerHTML = groups.map((group) => {
      const open = queryActive || state.ruleLibraryOpenGroups.has(group.id);
      const rows = group.items.map((item) => workspaceLibraryItemMarkup(item, {
        canSelect,
        manage,
        busy: libraryBusy,
      })).join("");
      // 「未分类」是系统兜底分组，不能改名也不能删除；
      // 其余分类的管理动作只在管理态出现，浏览时标题行是干净的。
      // 破坏性动作写明作用对象：分类删的是标签，规则行删的是规则。
      const tagActions = group.unassigned || !manage ? "" : `<span class="rule-tag-actions">
        <button class="row-action is-ghost" type="button" data-rename-rule-tag="${escapeHtml(group.id)}" aria-label="重命名分类：${escapeHtml(group.label)}" ${libraryBusy ? "disabled" : ""}>重命名</button>
        <button class="row-action is-ghost is-danger" type="button" data-delete-rule-tag="${escapeHtml(group.id)}" aria-label="删除标签：${escapeHtml(group.label)}" ${libraryBusy ? "disabled" : ""}>删除标签</button>
      </span>`;
      // 分类名与条数同属一个信息区，条数不再被推到行中间。
      return `<details class="rule-library-group" data-rule-library-group="${escapeHtml(group.id)}" ${open ? "open" : ""}>
        <summary><span class="rule-tag-info"><span class="rule-tag-name">${escapeHtml(group.label)}</span><small>${canSelect ? `可加 ${group.count}` : `${group.count} 条`}</small></span>${tagActions}</summary>
        <div class="rule-library-items">${rows || '<p class="empty-inline">此分类没有匹配规则。</p>'}</div>
      </details>`;
    }).join("");
    if (state.customRuleFocusRef) focusLibraryRule(state.customRuleFocusRef);
  }

  function renderWorkspaceRuleSetList() {
    const items = workspaceRuleSetItems();
    const selectionLocked = ruleInteractionLocked() || ruleDraftBlocked();
    const deleteLocked = selectionLocked || workspaceDirty();
    const editor = byId("ruleEditorForm");
    byId("ruleSetList").after(editor);
    const rows = items.map((item) => {
      const selected = state.ruleEditorOpen
        && item.id === state.selectedRuleSetId
        && state.ruleSetDraftKind === "existing";
      const flow = PanelCore.ruleSetFlowSummary(item.latest || item);
      const activation = PanelCore.ruleSetActivationState(
        item,
        activeRuleSet(),
        state.ruleActivationTargetId,
      );
      const targetVersion = activation.targetVersion || 0;
      const currentLineage = activation.currentLineage;
      const exactCurrent = activation.exactCurrent;
      const secondaryCopy = `二级 ${flow.secondary.count} 条`;
      const legacyPrimaryCount = flow.groups.required.count + flow.groups.veto.count + flow.groups.scored.count;
      const primaryCopy = flow.mode === "simple_all"
        ? `一级 ${flow.primary.count} 条`
        : `一级 ${legacyPrimaryCount} 条`;
      const canActivate = workspaceCapability(item, "activate") && targetVersion > 0;
      const activateDisabled = selectionLocked || workspaceDirty() || !canActivate;
      const canDelete = workspaceCapability(item, "delete", !currentLineage) && !currentLineage;
      const deleteDisabled = deleteLocked || !canDelete;
      const deleteHint = currentLineage
        ? "正在用于筛选，请先使用其他组合"
        : (deleteLocked ? "请先保存当前修改" : `删除${item.name}`);
      const disclosureDisabled = selectionLocked && !selected;
      const disclosureHint = disclosureDisabled
        ? "请先完成当前规则操作或重新载入核对"
        : `${selected ? "收起" : "展开"}${item.name}`;
      return `<article class="rule-set-card${selected ? " is-selected" : ""}${currentLineage ? " is-current" : ""}" role="listitem" data-rule-set-row="${escapeHtml(item.id)}">
        <button class="rule-set-disclosure" type="button" data-toggle-rule-set="${escapeHtml(item.id)}" data-rule-set-version="${targetVersion}" aria-expanded="${selected}" aria-controls="rule-set-detail-${escapeHtml(item.id)}" aria-label="${escapeHtml(disclosureHint)}" title="${escapeHtml(disclosureHint)}" ${disclosureDisabled ? "disabled" : ""}>
          <span class="rule-set-chevron" aria-hidden="true">⌄</span>
          <span class="record-row-main">
            <span class="rule-set-row-main">
              <strong>${escapeHtml(item.name)}</strong>
              ${activation.statusLabel ? `<span class="rule-set-current-badge${exactCurrent ? " is-exact" : " is-previous"}" ${exactCurrent ? 'aria-current="true"' : ""} title="${exactCurrent ? "当前筛选依据，无需重复操作" : "该组合的旧版本正在使用；点击使用可切换到当前版本"}">${escapeHtml(activation.statusLabel)}</span>` : ""}
            </span>
            <small>${escapeHtml(`第 ${targetVersion} 版`)}</small>
          </span>
          <span class="record-row-facts">
            <span class="stage-count">${primaryCopy}</span>
            <span class="stage-count">${secondaryCopy}</span>
          </span>
        </button>
        <div class="record-row-actions" aria-label="${escapeHtml(item.name)}操作">
          ${exactCurrent
            ? ""
            : `<button class="row-action is-primary" type="button" data-activate-rule-set="${escapeHtml(item.id)}" data-rule-set-version="${targetVersion}" title="使用${escapeHtml(item.name)}作为筛选依据" ${activation.activating ? 'aria-busy="true"' : ""} ${activateDisabled ? "disabled" : ""}>${escapeHtml(activation.actionLabel)}</button>`}
          <button class="row-action" type="button" data-edit-rule-set="${escapeHtml(item.id)}" data-rule-set-version="${targetVersion}" ${selectionLocked ? "disabled" : ""}>编辑</button>
          <button class="row-action is-danger" type="button" data-delete-rule-set="${escapeHtml(item.id)}" aria-label="${escapeHtml(deleteHint)}" title="${escapeHtml(deleteHint)}" ${deleteDisabled ? "disabled" : ""}>删除</button>
        </div>
        ${selected ? `<div class="rule-set-detail-mount" id="rule-set-detail-${escapeHtml(item.id)}"></div>` : ""}
      </article>`;
    });
    if (state.ruleEditorOpen && state.ruleSetDraftKind === "new") {
      rows.unshift(`<article class="rule-set-card is-selected is-new" role="listitem" data-rule-set-row="new"><div class="record-row-main"><strong>新建组合</strong><small>尚未保存</small></div><div class="rule-set-detail-mount" id="rule-set-detail-new"></div></article>`);
    }
    byId("ruleSetCount").textContent = `共 ${items.length} 个组合`;
    byId("ruleSetList").innerHTML = rows.length ? rows.join("") : '<p class="empty-inline">还没有规则组合，请新建一个。</p>';
    const mount = state.ruleEditorOpen
      ? byId(state.ruleSetDraftKind === "new" ? "rule-set-detail-new" : `rule-set-detail-${state.selectedRuleSetId}`)
      : null;
    // 编辑态：编辑器是独立主视图，不再放回组合列表行内。
    // 查看态：详情仍在展开行下方就地显示。
    if (rulesSurface() === "editor") byId("ruleEditorView").append(editor);
    else if (mount) mount.append(editor);
    byId("ruleSetList").setAttribute(
      "aria-busy",
      String(state.ruleOperation.phase === "selecting"),
    );
  }

  function focusWorkspaceRuleSetRow(id) {
    window.requestAnimationFrame(() => {
      const button = Array.from(document.querySelectorAll("#ruleSetList [data-toggle-rule-set]"))
        .find((candidate) => candidate.dataset.toggleRuleSet === id);
      const fallback = document.querySelector("#ruleSetList [data-toggle-rule-set]:not(:disabled)")
        || byId("newRuleSetButton");
      (button && !button.disabled ? button : fallback)?.focus({ preventScroll: true });
    });
  }

  function focusWorkspaceRuleEditor() {
    window.requestAnimationFrame(() => {
      const target = workspaceIsReadOnly() ? byId("enabledRulesHeading") : byId("ruleSetName");
      byId("ruleEditorForm")?.scrollIntoView({ behavior: "smooth", block: "start" });
      target?.focus({ preventScroll: true });
      if (target === byId("ruleSetName")) target.select();
    });
  }

  function updateWorkspaceState({ announce = true } = {}) {
    const advanced = workspaceIsAdvanced();
    const readOnly = workspaceIsReadOnly();
    const count = state.ruleDraftRefs.size;
    const dirty = workspaceDirty();
    const busy = ruleInteractionLocked();
    const draftBlocked = ruleDraftBlocked();
    const canSelectFromLibrary = workspaceLibraryCanSelect();
    // 「从规则库选择」属于编辑器，不能用「是否处于选择器」来判断可见性。
    const canPickRules = state.ruleEditorOpen
      && !workspaceIsReadOnly()
      && !workspaceIsAdvanced();
    const listOnlyRecovery = ["activation_receipt", "delete_receipt"]
      .includes(state.ruleSavePendingVerification?.stage);
    if (draftBlocked && !listOnlyRecovery && !state.ruleEditorOpen) {
      state.ruleEditorOpen = true;
      byId("ruleEditorForm").hidden = false;
    }
    const nameInput = byId("ruleSetName");
    nameInput.disabled = readOnly || busy || draftBlocked;
    byId("ruleSearchInput").disabled = busy || draftBlocked || state.customRuleCreateBusy;
    byId("addRuleButton").disabled = busy || draftBlocked || state.customRuleCreateBusy;
    byId("newRuleSetButton").disabled = busy || draftBlocked;
    nameInput.closest(".rule-set-fields").hidden = readOnly;
    byId("enabledRulesHeading").textContent = readOnly
      ? "组合详情"
      : (state.ruleSetDraftKind === "existing" ? "编辑组合" : "新建组合");
    byId("ruleEditorForm").classList.toggle("is-readonly", readOnly);
    const valid = Boolean(workspaceName()) && count > 0 && !readOnly;
    const recoveryRequired = draftBlocked;
    byId("saveRulesButton").disabled = !valid || !dirty || busy || draftBlocked;
    byId("resetRulesButton").hidden = !recoveryRequired;
    byId("resetRulesButton").textContent = "重新载入并核对";
    byId("resetRulesButton").disabled = busy;
    byId("saveRulesButton").hidden = readOnly || recoveryRequired;
    byId("cancelRuleEditButton").hidden = readOnly || recoveryRequired;
    byId("saveRulesButton").closest(".rule-save-panel").hidden = readOnly;
    document.querySelectorAll("[data-rule-stage-panel]").forEach((panel) => {
      panel.classList.toggle("is-active", panel.dataset.ruleStagePanel === state.ruleEditingStage);
    });
    document.querySelectorAll("[data-stage-add]").forEach((button) => {
      button.hidden = !canPickRules;
      button.disabled = busy || draftBlocked;
    });
    renderRulesSurface();
    if (!announce) return;
    if (state.ruleOperation.phase === "conflict") {
      setFeedback("ruleFeedback", "方案已被另一端更新；草稿已保留。请重新载入并核对。", false, false, true);
    } else if (state.ruleOperation.phase === "uncertain") {
      setFeedback("ruleFeedback", "当前状态待核对；草稿和请求编号已保留。请重新载入并核对。", false, false, true);
    } else if (state.ruleInputsUncertain) {
      setFeedback("ruleFeedback", "规则状态待核对；请先重新载入并核对。", false, false, true);
    } else {
      setFeedback("ruleFeedback", "");
    }
  }

  function renderRuleSetWorkspace({ announce = true, preserveInputs = false } = {}) {
    byId("ruleEditorForm").hidden = !state.ruleEditorOpen;
    if (!preserveInputs) {
      byId("ruleSetName").value = state.ruleSetDraftName;
    }
    byId("ruleSearchInput").value = state.ruleSearchQuery;
    renderWorkspaceRuleSetList();
    renderWorkspaceSelectedRules();
    renderWorkspaceLibrary();
    updateWorkspaceState({ announce });
  }

  function setWorkspaceFromExact(item, exact) {
    state.selectedRuleSetId = clean(item?.id || exact?.id);
    state.selectedRuleSetVersion = Number(exact?.version || item?.latest_version || item?.version) || null;
    state.selectedRuleSetExact = exact || null;
    state.ruleSetDraftKind = "existing";
    state.ruleSetDraftName = clean(exact?.name || item?.name);
    const legacy = PanelCore.ruleSetEditorMode(exact || item || {}) === "legacy_advanced";
    state.ruleDraftRefs = new Set(legacy
      ? [...(exact?.scored?.rules || []), ...(exact?.required?.rules || [])]
      : (exact?.scored?.rules || []));
    state.secondaryRuleDraftRefs = new Set(exact?.secondary?.rules || []);
    state.ruleSearchQuery = "";
    renewRuleApplyRequest();
    state.rulePristineSignature = "";
    byId("ruleSetName").value = state.ruleSetDraftName;
    state.rulePristineSignature = workspaceSignature();
  }

  async function selectWorkspaceRuleSet(id, { force = false, version = null, openEditor = false, detailMode = "view" } = {}) {
    if (!force && (ruleInteractionLocked() || ruleDraftBlocked())) return false;
    const sameOpenTarget = openEditor
      && state.ruleEditorOpen
      && state.ruleSetDraftKind === "existing"
      && clean(state.selectedRuleSetId) === clean(id)
      && state.ruleDetailMode === (detailMode === "edit" ? "edit" : "view")
      && (!Number(version) || Number(version) === Number(state.selectedRuleSetVersion));
    if (!force && sameOpenTarget) {
      focusWorkspaceRuleEditor();
      return true;
    }
    if (!force && workspaceDirty() && !await askConfirm({
      title: "放弃未保存的修改？",
      message: "切换到另一个组合后，当前改动不会保留。",
      confirmLabel: "放弃并切换",
      cancelLabel: "继续编辑",
      danger: true,
    })) {
      return false;
    }
    const item = PanelCore.ruleSetSummaries(state.rules?.rule_sets || {})
      .find((candidate) => candidate.id === id);
    if (!item) return false;
    const previousEditorOpen = state.ruleEditorOpen;
    const previousDetailMode = state.ruleDetailMode;
    state.ruleEditorOpen = Boolean(openEditor);
    state.ruleDetailMode = openEditor && detailMode === "edit" ? "edit" : "view";
    const operationToken = beginRuleOperation("selecting");
    renderRuleSetWorkspace({ announce: false, preserveInputs: true });
    try {
      const exact = await fetchWorkspaceRuleSet(item, version);
      if (!ruleOperationIsCurrent(operationToken)) return false;
      if (!exact) throw new Error("该组合版本暂时无法读取。");
      setWorkspaceFromExact(item, exact);
      state.ruleSavePendingVerification = null;
      safeStorageRemove(RULE_DRAFT_STORAGE_KEY);
      setRuleOperationPhase(operationToken, "idle");
      renderRuleSetWorkspace();
      if (openEditor) focusWorkspaceRuleEditor();
      else focusWorkspaceRuleSetRow(id);
      return true;
    } catch (error) {
      if (!ruleOperationIsCurrent(operationToken)) return false;
      state.ruleEditorOpen = previousEditorOpen;
      state.ruleDetailMode = previousDetailMode;
      setRuleOperationPhase(operationToken, "idle");
      renderRuleSetWorkspace({ announce: false, preserveInputs: true });
      focusWorkspaceRuleSetRow(state.selectedRuleSetId);
      setFeedback("ruleFeedback", error.message, true);
      showToast(error.message, true);
      return false;
    }
  }

  async function startWorkspaceRuleSet() {
    if (ruleInteractionLocked() || ruleDraftBlocked()) return false;
    if (workspaceDirty() && !await askConfirm({
      title: "放弃未保存的修改？",
      message: "新建组合会丢掉当前未保存的改动。",
      confirmLabel: "放弃并新建",
      cancelLabel: "继续编辑",
      danger: true,
    })) return false;
    state.ruleEditorOpen = true;
    state.ruleDetailMode = "edit";
    state.selectedRuleSetId = "";
    state.selectedRuleSetVersion = null;
    state.selectedRuleSetExact = null;
    state.ruleSetDraftKind = "new";
    state.ruleSetDraftName = "新规则组合";
    state.ruleDraftRefs = new Set();
    state.secondaryRuleDraftRefs = new Set();
    state.ruleSearchQuery = "";
    renewRuleApplyRequest();
    state.rulePristineSignature = "";
    renderRuleSetWorkspace();
    saveWorkspaceDraftCache();
    focusWorkspaceRuleEditor();
    return true;
  }

  async function cancelWorkspaceRuleEdit({ skipConfirm = false } = {}) {
    if (ruleInteractionLocked() || ruleDraftBlocked()) return false;
    if (!skipConfirm && workspaceDirty() && !await askConfirm({
      title: "放弃未保存的修改？",
      message: "取消编辑后，当前改动不会保留。",
      confirmLabel: "放弃修改",
      cancelLabel: "继续编辑",
      danger: true,
    })) return false;
    if (state.ruleSetDraftKind === "new") {
      state.ruleEditorOpen = false;
      state.ruleDetailMode = "view";
      state.selectedRuleSetId = "";
      state.selectedRuleSetVersion = null;
      state.selectedRuleSetExact = null;
      state.ruleSetDraftName = "新规则组合";
      state.ruleDraftRefs = new Set();
      state.secondaryRuleDraftRefs = new Set();
      state.rulePristineSignature = "";
      renewRuleApplyRequest();
      safeStorageRemove(RULE_DRAFT_STORAGE_KEY);
      // 放弃新建后没有任何组合可展示，必须退回组合管理，
      // 否则留在编辑器视图上就是一个空白页。
      state.pickerSession = null;
      state.rulesSurface = "sets";
    } else {
      const item = workspaceRuleSetItem();
      setWorkspaceFromExact(item, state.selectedRuleSetExact || item?.latest || item);
      state.ruleEditorOpen = true;
      state.ruleDetailMode = "view";
      safeStorageRemove(RULE_DRAFT_STORAGE_KEY);
    }
    renderRuleSetWorkspace();
    focusWorkspaceRuleSetRow(state.selectedRuleSetId);
    return true;
  }

  async function refreshWorkspaceRules({ selectId = "", version = null } = {}) {
    state.rules = await requestJson(API.rules);
    state.ruleInputsUncertain = false;
    const visibleItems = workspaceRuleSetItems();
    const visibleIds = new Set(visibleItems.map((item) => clean(item.id)));
    const requestedId = visibleIds.has(clean(selectId)) ? clean(selectId) : "";
    const activeId = clean(state.rules?.rule_sets?.active?.id);
    const targetId = requestedId || (visibleIds.has(activeId) ? activeId : "") || visibleItems[0]?.id;
    const targetVersion = requestedId ? version : null;
    if (targetId) {
      const selected = await selectWorkspaceRuleSet(targetId, { force: true, version: targetVersion });
      if (!selected) throw new Error("规则已读取，但目标组合版本无法载入。");
    } else {
      state.selectedRuleSetId = "";
      state.selectedRuleSetVersion = null;
      state.selectedRuleSetExact = null;
      state.ruleSetDraftKind = "new";
      state.ruleSetDraftName = "新规则组合";
      state.ruleDraftRefs = new Set();
      state.secondaryRuleDraftRefs = new Set();
      state.ruleEditingStage = "primary";
      state.ruleSearchQuery = "";
      state.ruleSavePendingVerification = null;
      renewRuleApplyRequest();
      state.rulePristineSignature = "";
      safeStorageRemove(RULE_DRAFT_STORAGE_KEY);
      renderRuleSetWorkspace();
      window.requestAnimationFrame(() => byId("newRuleSetButton")?.focus({ preventScroll: true }));
    }
    renderScreenStatus();
  }

  function savedWorkspaceTarget(result = {}) {
    const item = result.rule_set || {};
    const exact = result.version || result.rule_set_version || null;
    return {
      id: clean(exact?.id || item.id),
      version: Number(exact?.version || item.latest_version),
      item,
      exact,
    };
  }

  function mergeWorkspaceMutationSnapshot(saved = null, active = null) {
    const ruleSets = state.rules?.rule_sets;
    if (!ruleSets) return;
    if (saved?.id) {
      const items = Array.isArray(ruleSets.items) ? [...ruleSets.items] : [];
      const index = items.findIndex((item) => clean(item.id) === saved.id);
      const nextItem = {
        ...(index >= 0 ? items[index] : {}),
        ...saved.item,
        id: saved.id,
        latest_version: saved.version,
        latest: saved.exact || saved.item?.latest,
      };
      if (index >= 0) items.splice(index, 1, nextItem);
      else items.push(nextItem);
      ruleSets.items = items;
    }
    if (active) ruleSets.active = active;
  }

  function commitSavedWorkspace(saved) {
    let item = workspaceRuleSetItem(saved.id);
    if (!item) {
      mergeWorkspaceMutationSnapshot(saved);
      item = workspaceRuleSetItem(saved.id) || saved.item;
    }
    if (item && saved.exact) setWorkspaceFromExact(item, saved.exact);
    state.ruleSavePendingVerification = null;
    safeStorageRemove(RULE_DRAFT_STORAGE_KEY);
  }

  function currentRuleSetCopy(outcome, targetId) {
    if (!outcome.activeId || !outcome.activeVersion) return "当前仍没有已启用组合";
    if (clean(outcome.activeId) === clean(targetId)) return `当前仍为 v${outcome.activeVersion}`;
    const item = PanelCore.ruleSetSummaries(state.rules?.rule_sets || {})
      .find((candidate) => candidate.id === outcome.activeId);
    return `当前仍为“${clean(item?.name || outcome.activeId)}” v${outcome.activeVersion}`;
  }

  async function reconcileWorkspaceTarget(id, version, { saved = null, preserveEditor = false } = {}) {
    state.rules = await requestJson(API.rules);
    if (!workspaceTargetExists(id)) throw targetDeletedError();
    const outcome = PanelCore.ruleSetExactTargetOutcome(state.rules, id, version);
    if (outcome.status === "UNKNOWN") {
      const error = new Error("当前规则版本无法精确核对。");
      error.code = "RULE_SET_TARGET_UNKNOWN";
      throw error;
    }
    state.ruleInputsUncertain = false;
    if (saved) commitSavedWorkspace(saved);
    else if (!preserveEditor) {
      const item = workspaceRuleSetItem(id);
      const exact = workspaceIsExactCurrent(id, version)
        ? activeRuleSet()
        : (Number(item?.latest?.version) === Number(version) ? item.latest : state.selectedRuleSetExact);
      if (
        item
        && exact
        && Number(exact.version) === Number(version)
        && (!state.ruleEditorOpen || clean(state.selectedRuleSetId) === clean(id))
      ) setWorkspaceFromExact(item, exact);
    }
    renderScreenStatus();
    return outcome;
  }

  function confirmWorkspaceActivation(id, version, active = null) {
    if (active) mergeWorkspaceMutationSnapshot(null, active);
    state.ruleSavePendingVerification = null;
    state.ruleInputsUncertain = false;
  }

  function workspaceActiveExpectation() {
    const active = activeRuleSet();
    if (!clean(active?.id)) return { expected_active_absent: true };
    return {
      expected_active_absent: false,
      expected_active_rule_set_id: clean(active.id),
      expected_active_rule_set_version: Number(active.version),
      expected_active_rule_set_hash: clean(active.rule_set_hash),
    };
  }

  async function activateWorkspaceRuleSet(id = state.selectedRuleSetId, version = null) {
    if (ruleInteractionLocked() || ruleDraftBlocked()) return;
    if (workspaceDirty()) {
      setFeedback("ruleFeedback", "请先保存当前修改，再使用该组合。", false, false, true);
      showToast("请先保存当前修改。", true);
      return;
    }
    const item = PanelCore.ruleSetSummaries(state.rules?.rule_sets || {})
      .find((candidate) => candidate.id === id);
    if (!item) return;
    const viewedVersion = clean(id) === clean(state.selectedRuleSetId)
      ? state.selectedRuleSetVersion
      : null;
    const targetVersion = Number(version || viewedVersion || item.latest_version || item.usable_version || item.version);
    if (workspaceIsExactCurrent(id, targetVersion)) return;
    const operationToken = beginRuleOperation("activating");
    const activeExpectation = workspaceActiveExpectation();
    state.ruleActivationTargetId = clean(id);
    state.rulesSavingBusy = true;
    renderRuleSetWorkspace({ announce: false, preserveInputs: true });
    setFeedback("ruleFeedback", "");
    setFeedback("ruleSetListFeedback", `正在切换到“${item.name}”…`);
    let feedback = null;
    try {
      let activationResult = null;
      let activationRejected = null;
      try {
        activationResult = await requestJson(`${API.ruleSets}/${encodeURIComponent(id)}/activate`, {
          method: "POST",
          body: JSON.stringify({
            version: targetVersion,
            ...activeExpectation,
          }),
        });
      } catch (activationError) {
        const status = Number(activationError.status);
        const resultMayBeUncertain = writeReceiptMayBeUncertain(activationError)
          || !Number.isFinite(status);
        if (!resultMayBeUncertain) activationRejected = activationError;
        // 只有连接中断或服务端异常可能发生“已写入但回执丢失”，才继续 GET 对账；绝不重试 POST。
      }
      if (!ruleOperationIsCurrent(operationToken)) return;
      if (activationRejected) {
        setRuleOperationPhase(operationToken, "reconciling");
        let refreshed = false;
        try {
          state.rules = await requestJson(API.rules);
          state.ruleInputsUncertain = false;
          refreshed = true;
          renderScreenStatus();
        } catch (_refreshError) {
          // POST 已明确拒绝，不把随后的读取失败误报为写入结果不确定。
        }
        if (!ruleOperationIsCurrent(operationToken)) return;
        const changedElsewhere = ["RULE_SET_CHANGED", "ACTIVE_RULE_SET_CHANGED"]
          .includes(activationRejected.code);
        const changedSubject = activationRejected.code === "ACTIVE_RULE_SET_CHANGED"
          ? "筛选依据已被另一端切换"
          : "规则组合已被另一端更新";
        feedback = {
          message: changedElsewhere
            ? `${changedSubject}；${refreshed ? "页面已更新，请重新确认后再使用" : "请求已被拒绝，请恢复连接后刷新页面再确认"}。（${activationRejected.message}）`
            : activationRejected.message,
          warning: true,
          persistent: true,
        };
        setRuleOperationPhase(operationToken, "idle");
        return;
      }
      setRuleOperationPhase(operationToken, "reconciling");
      const responseOutcome = PanelCore.ruleSetExactTargetOutcome(activationResult || {}, id, targetVersion);
      try {
        const outcome = await reconcileWorkspaceTarget(id, targetVersion, { preserveEditor: true });
        if (!ruleOperationIsCurrent(operationToken)) return;
        if (outcome.isExactCurrent) {
          confirmWorkspaceActivation(id, targetVersion);
          feedback = { message: `v${targetVersion} 已开始使用；下一次筛选会使用这个版本。`, success: true };
        } else {
          feedback = {
            message: `未能使用；${currentRuleSetCopy(outcome, id)}。请核对后重试。`,
            warning: true,
            persistent: true,
          };
        }
        setRuleOperationPhase(operationToken, "idle");
      } catch (refreshError) {
        if (refreshError.code === "RULE_SET_TARGET_DELETED") {
          state.ruleSavePendingVerification = null;
          state.ruleInputsUncertain = false;
          resetRuleOperation();
          safeStorageRemove(RULE_DRAFT_STORAGE_KEY);
          feedback = {
            message: "原组合已被删除，无法使用。",
            warning: true,
            persistent: true,
            resolved: true,
          };
          try {
            await refreshWorkspaceRules();
          } catch (_fallbackError) {
            // The deletion is already certain; a fallback read must not restore an uncertain write state.
          }
        } else if (responseOutcome.isExactCurrent) {
          mergeWorkspaceMutationSnapshot(null, activationResult.active);
          confirmWorkspaceActivation(id, targetVersion, activationResult.active);
          setRuleOperationPhase(operationToken, "idle");
          feedback = { message: `v${targetVersion} 已开始使用；下一次筛选会使用这个版本。`, success: true };
        } else {
          state.ruleSavePendingVerification = {
            stage: "activation_receipt",
            id: clean(id),
            version: targetVersion,
            activeExpectation,
          };
          state.ruleInputsUncertain = true;
          setRuleOperationPhase(operationToken, "uncertain");
          saveWorkspaceDraftCache();
          feedback = {
            message: "设置结果待核对，请勿重复操作；恢复连接后点击“重新载入并核对”。",
            warning: true,
            persistent: true,
            recovery: true,
          };
        }
      }
    } finally {
      state.rulesSavingBusy = false;
      if (clean(state.ruleActivationTargetId) === clean(id)) state.ruleActivationTargetId = "";
      renderRuleSetWorkspace({ announce: false, preserveInputs: true });
      renderScreenStatus();
      if (feedback) {
        if (feedback.success) {
          setFeedback("ruleFeedback", "");
          setFeedback("ruleSetListFeedback", "");
          showToast("已开始使用该组合。");
        } else {
          setFeedback(
            "ruleFeedback",
            feedback.persistent ? feedback.message : "",
            false,
            false,
            Boolean(feedback.persistent && feedback.warning),
          );
          setFeedback("ruleSetListFeedback", feedback.message, false, false, true);
          showToast(feedback.message, true);
        }
      }
      if (feedback?.recovery) window.requestAnimationFrame(() => byId("resetRulesButton")?.focus());
      else if (feedback) focusWorkspaceRuleSetRow(id);
    }
  }

  async function deleteWorkspaceRuleSet(id) {
    if (ruleInteractionLocked() || ruleDraftBlocked() || workspaceDirty()) return;
    const item = PanelCore.ruleSetSummaries(state.rules?.rule_sets || {})
      .find((candidate) => candidate.id === id);
    const currentLineage = clean(activeRuleSet()?.id) === clean(id);
    if (
      !item
      || currentLineage
      || !workspaceCapability(item, "delete", !currentLineage)
    ) return;
    if (!await askConfirm({
      title: `删除组合“${item.name}”？`,
      message: "删除后不能恢复；历史筛选记录仍会保留。",
      confirmLabel: "删除组合",
      danger: true,
    })) return;
    const deletingSelected = state.ruleSetDraftKind === "existing"
      && clean(state.selectedRuleSetId) === clean(id);
    const selectedBeforeDelete = state.selectedRuleSetId;
    let deleteAcknowledged = false;
    setFeedback("ruleFeedback", "");
    state.rulesSavingBusy = true;
    renderRuleSetWorkspace({ announce: false, preserveInputs: true });
    try {
      await requestJson(`${API.ruleSets}/${encodeURIComponent(id)}`, {
        method: "DELETE",
        body: JSON.stringify({ expected_rule_set_hash: clean(item.rule_set_hash) }),
      });
      deleteAcknowledged = true;
      if (deletingSelected) await refreshWorkspaceRules();
      else {
        state.rules = await requestJson(API.rules);
        renderRuleSetWorkspace({ announce: false, preserveInputs: true });
        renderScreenStatus();
        focusWorkspaceRuleSetRow(selectedBeforeDelete);
      }
      showToast("组合已删除。");
    } catch (error) {
      if (deleteAcknowledged || writeReceiptMayBeUncertain(error)) {
        state.ruleSavePendingVerification = {
          stage: "delete_receipt",
          id: clean(id),
          name: clean(item.name),
          hash: clean(item.rule_set_hash),
          selected: deletingSelected,
          previousSelectedId: clean(selectedBeforeDelete),
        };
        state.ruleInputsUncertain = true;
        state.ruleOperation = { phase: "uncertain", token: state.ruleOperation.token + 1 };
        saveWorkspaceDraftCache();
        renderRuleSetWorkspace({ announce: false, preserveInputs: true });
        setFeedback("ruleFeedback", "删除结果待核对，请勿重复操作；恢复连接后点击“重新载入并核对”。", false, false, true);
        showToast("删除结果待核对。", true);
      } else {
        setFeedback("ruleFeedback", error.message, true);
        showToast(error.message, true);
      }
    } finally {
      state.rulesSavingBusy = false;
      renderRuleSetWorkspace({ announce: false, preserveInputs: true });
      if (state.ruleSavePendingVerification?.stage === "delete_receipt") {
        window.requestAnimationFrame(() => byId("resetRulesButton")?.focus());
      }
    }
  }

  async function legacyConversionForSave() {
    const source = state.selectedRuleSetExact || {};
    if (PanelCore.ruleSetEditorMode(source) !== "legacy_advanced") return null;
    const scored = source.scored?.rules || [];
    const required = source.required?.rules || [];
    const veto = source.veto?.rules || [];
    const secondary = source.secondary?.rules || [];
    const oldPrimaryMinimum = scored.length
      ? Math.max(1, Math.min(scored.length, Math.ceil(Number(source.scored?.pass_ratio || 0) * scored.length)))
      : 0;
    const oldSecondaryMinimum = secondary.length
      ? Math.max(1, Math.min(secondary.length, Number(source.secondary?.minimum_match || 0)))
      : 0;
    const lines = [
      "该组合是旧版规则。首次保存会生成同一组合的新版本，旧版本保持不变。",
      `一级门槛：原候选 ${oldPrimaryMinimum}/${scored.length}${required.length ? `，另有 ${required.length} 条前置` : ""} → 新版 ${state.ruleDraftRefs.size}/${state.ruleDraftRefs.size} 全部满足。`,
      `二级门槛：${oldSecondaryMinimum}/${secondary.length} → ${state.secondaryRuleDraftRefs.size}/${state.secondaryRuleDraftRefs.size} 全部满足。`,
      ...(veto.length ? [`原有 ${veto.length} 条命中即排除规则不会带入新版。`] : []),
      "旧版规则组门槛不再保留。",
      "是否确认以上变化并保存？",
    ];
    if (!await askConfirm({
      title: "确认转换这个旧版组合？",
      message: lines.join(" "),
      confirmLabel: "确认并保存",
    })) return false;
    return {
      policy: "legacy_to_two_stage_all_v1",
      confirmed: true,
      source_version: Number(state.selectedRuleSetVersion),
      source_rule_set_hash: clean(source.rule_set_hash),
      dropped_veto_refs: [...veto],
    };
  }

  async function saveWorkspaceRuleSet({ receiptRecovery = null } = {}) {
    const recoveringReceipt = receiptRecovery?.stage === "save_receipt";
    if (ruleInteractionLocked() || ruleDraftBlocked()) return;
    if (recoveringReceipt) {
      state.ruleSavePendingVerification = receiptRecovery;
    }
    const selected = Array.from(state.ruleDraftRefs);
    const secondarySelected = Array.from(state.secondaryRuleDraftRefs);
    const name = workspaceName();
    if (workspaceIsAdvanced()) {
      setFeedback("ruleFeedback", "此组合只能查看，不能直接编辑。", true);
      return;
    }
    if (!name) {
      setFeedback("ruleFeedback", "请填写组合名称。", true);
      byId("ruleSetName").focus();
      return;
    }
    if (!selected.length) {
      setFeedback("ruleFeedback", "至少加入一条规则。", true);
      return;
    }
    const legacyConversion = recoveringReceipt
      ? receiptRecovery?.payload?.legacy_conversion || null
      : legacyConversionForSave();
    if (legacyConversion === false) return;
    const operationToken = beginRuleOperation("saving");
    state.rulesSavingBusy = true;
    setFormLocked(byId("ruleEditorForm"), true, ["saveRulesButton"]);
    renderRuleSetWorkspace({ announce: false, preserveInputs: true });
    renderDataSummary();
    renderScreenStatus();
    setFeedback("ruleFeedback", "");
    let feedback = null;
    let toastCopy = "";
    try {
      const existing = recoveringReceipt
        ? Boolean(receiptRecovery.existing)
        : state.ruleSetDraftKind === "existing";
      const targetId = recoveringReceipt
        ? clean(receiptRecovery.id)
        : state.selectedRuleSetId;
      const item = workspaceRuleSetItem();
      const path = existing
        ? `${API.ruleSets}/${encodeURIComponent(targetId)}/versions`
        : API.ruleSets;
      const requestId = clean(receiptRecovery?.requestId) || state.ruleApplyRequestId || newRequestId();
      state.ruleApplyRequestId = requestId;
      const frozenRecoveryPayload = recoveringReceipt && receiptRecovery?.payload
        ? { ...receiptRecovery.payload }
        : null;
      const payload = frozenRecoveryPayload || {
          request_id: requestId,
          name,
          editor_mode: "simple_all",
          rules: selected.map((ref) => ({ ref })),
          secondary_rules: secondarySelected.map((ref) => ({ ref })),
          ...(legacyConversion ? { legacy_conversion: legacyConversion } : {}),
        };
      if (existing && !frozenRecoveryPayload) {
        payload.base_version = state.selectedRuleSetVersion;
        payload.expected_rule_set_hash = clean(state.selectedRuleSetExact?.rule_set_hash || item?.rule_set_hash);
      }
      saveWorkspaceDraftCache();
      let result = null;
      try {
        result = await requestJson(path, {
          method: "POST",
          body: JSON.stringify(payload),
        });
      } catch (error) {
        if (["RULE_SET_CHANGED", "ACTIVE_RULE_SET_CHANGED"].includes(error.code)) {
          state.ruleSavePendingVerification = null;
          try {
            state.rules = await requestJson(API.rules);
          } catch (_refreshError) {
            state.ruleInputsUncertain = true;
          }
          state.ruleInputsUncertain = true;
          setRuleOperationPhase(operationToken, "conflict");
          saveWorkspaceDraftCache();
          feedback = {
            message: "另一端刚更新了组合；当前草稿仍保留。请重新载入并核对后再编辑。",
            warning: true,
          };
        } else if (existing && ["RULE_SET_NOT_FOUND", "RULE_SET_DELETED"].includes(error.code)) {
          preserveWorkspaceAsNewDraft(payload);
          feedback = {
            message: "原组合已被删除；当前规则已保留为新组合草稿，请确认后保存。",
            warning: true,
            resolved: true,
          };
        } else if (recoveringReceipt) {
          state.ruleSavePendingVerification = {
            ...receiptRecovery,
            stage: "save_receipt",
            version: null,
            requestId,
          };
          state.ruleInputsUncertain = true;
          setRuleOperationPhase(operationToken, "uncertain");
          saveWorkspaceDraftCache();
          feedback = {
            message: "仍无法核对保存回执；草稿和请求编号已保留，请勿重复保存。",
            warning: true,
          };
        } else if (writeReceiptMayBeUncertain(error)) {
          state.ruleSavePendingVerification = {
            stage: "save_receipt",
            id: existing ? targetId : "",
            version: null,
            requestId,
            existing,
            payload,
          };
          state.ruleInputsUncertain = true;
          setRuleOperationPhase(operationToken, "uncertain");
          saveWorkspaceDraftCache();
          feedback = {
            message: "未收到保存回执，当前状态待核对，请勿重复保存。恢复连接后请先重新读取规则。",
            warning: true,
          };
        } else {
          setRuleOperationPhase(operationToken, "idle");
          feedback = { message: error.message, error: true };
        }
        return;
      }

      const saved = savedWorkspaceTarget(result);
      if (!saved.id || !Number.isFinite(saved.version) || saved.version <= 0 || !saved.exact) {
        const targetKnown = Boolean(saved.exact && saved.id)
          && Number.isFinite(saved.version)
          && saved.version > 0;
        state.ruleSavePendingVerification = {
          stage: targetKnown ? "saved_target" : "save_receipt",
          id: targetKnown ? saved.id : (existing ? targetId : ""),
          version: targetKnown ? saved.version : null,
          requestId,
          ...(targetKnown ? {} : { existing, payload }),
        };
        state.ruleInputsUncertain = true;
        setRuleOperationPhase(operationToken, "uncertain");
        saveWorkspaceDraftCache();
        feedback = {
          message: "组合已保存，但返回的版本无法核对；请勿重复保存，恢复连接后先重新读取规则。",
          warning: true,
        };
        return;
      }

      if (!ruleOperationIsCurrent(operationToken)) return;
      setRuleOperationPhase(operationToken, "reconciling");
      try {
        await reconcileWorkspaceTarget(saved.id, saved.version, { saved });
        if (!ruleOperationIsCurrent(operationToken)) return;
        state.ruleDetailMode = "view";
        feedback = { message: "", success: true };
        toastCopy = "组合已保存。";
        setRuleOperationPhase(operationToken, "idle");
      } catch (refreshError) {
        if (refreshError.code === "RULE_SET_TARGET_DELETED") {
          preserveWorkspaceAsNewDraft(payload);
          feedback = {
            message: "原组合已被删除；当前规则已保留为新组合草稿，请确认后保存。",
            warning: true,
            resolved: true,
          };
        } else {
          state.ruleSavePendingVerification = {
            stage: "saved_target",
            id: saved.id,
            version: saved.version,
            requestId,
          };
          state.ruleInputsUncertain = true;
          setRuleOperationPhase(operationToken, "uncertain");
          saveWorkspaceDraftCache();
          feedback = {
            message: `v${saved.version}已保存，当前状态待核对，请勿重复保存。`,
            warning: true,
          };
        }
      }
    } finally {
      state.rulesSavingBusy = false;
      setFormLocked(byId("ruleEditorForm"), false, ["saveRulesButton"]);
      renderRuleSetWorkspace({ announce: false, preserveInputs: true });
      renderDataSummary();
      renderScreenStatus();
      if (feedback) {
        if (feedback.success) setFeedback("ruleFeedback", "");
        else setFeedback("ruleFeedback", feedback.message, Boolean(feedback.error), false, Boolean(feedback.warning));
      }
      if (feedback?.resolved) {
        setFeedback("ruleSetListFeedback", feedback.message, false, false, true);
        showToast("原组合已删除，规则已保留为新草稿。", true);
      }
      if (toastCopy) showToast(toastCopy);
    }
  }

  async function initializeRuleSetWorkspace({ restore = false } = {}) {
    const active = activeRuleSet();
    const summaries = PanelCore.ruleSetSummaries(state.rules?.rule_sets || {});
    const item = summaries.find((candidate) => candidate.id === active?.id) || summaries[0] || null;
    if (item) {
      const exact = await fetchWorkspaceRuleSet(item, item.latest_version || item.usable_version || item.version);
      setWorkspaceFromExact(item, exact || item);
    } else {
      state.ruleSetDraftKind = "new";
      state.ruleSetDraftName = "新规则组合";
      state.ruleDraftRefs = new Set();
      state.secondaryRuleDraftRefs = new Set();
      state.rulePristineSignature = "";
    }
    if (restore && restoreWorkspaceDraftCache()) {
      const cachedItem = workspaceRuleSetItem();
      if (cachedItem && state.ruleSetDraftKind === "existing") {
        state.selectedRuleSetExact = await fetchWorkspaceRuleSet(cachedItem, state.selectedRuleSetVersion);
      }
    }
    renderRuleSetWorkspace();
    renderRuleEditorContract();
  }

  function addWorkspaceLibraryRule(token) {
    const targetRefs = state.ruleEditingStage === "secondary"
      ? state.secondaryRuleDraftRefs
      : state.ruleDraftRefs;
    if (
      !token
      || !workspaceLibraryCanSelect()
      || workspaceHasRuleSource(token)
      || ruleInteractionLocked()
      || ruleDraftBlocked()
    ) return;
    const target = workspaceRuleFocusTarget(token);
    targetRefs.add(token);
    state.ruleLibraryOpenGroups.add(target.groupId);
    state.ruleLibraryFocusGroupId = target.groupId;
    renewRuleApplyRequest();
    renderRuleSetWorkspace({ preserveInputs: true });
    saveWorkspaceDraftCache();
    setFeedback("ruleFeedback", "");
    focusWorkspaceLibrarySummary(target.groupId);
  }

  function moveWorkspaceRule(token, direction, stage = "primary") {
    const source = stage === "secondary" ? state.secondaryRuleDraftRefs : state.ruleDraftRefs;
    const refs = Array.from(source);
    const index = refs.indexOf(token);
    const nextIndex = direction === "up" ? index - 1 : index + 1;
    if (
      workspaceIsReadOnly()
      || ruleInteractionLocked()
      || ruleDraftBlocked()
      || index < 0
      || nextIndex < 0
      || nextIndex >= refs.length
    ) return;
    const target = workspaceRuleFocusTarget(token);
    [refs[index], refs[nextIndex]] = [refs[nextIndex], refs[index]];
    if (stage === "secondary") state.secondaryRuleDraftRefs = new Set(refs);
    else state.ruleDraftRefs = new Set(refs);
    renewRuleApplyRequest();
    renderRuleSetWorkspace({ preserveInputs: true });
    saveWorkspaceDraftCache();
    setFeedback("ruleFeedback", "");
    focusWorkspaceMoveControl(token, direction);
  }

  function removeWorkspaceRule(token, stage = "primary") {
    if (
      workspaceIsReadOnly()
      || ruleInteractionLocked()
      || ruleDraftBlocked()
    ) return;
    const target = workspaceRuleFocusTarget(token);
    const source = stage === "secondary" ? state.secondaryRuleDraftRefs : state.ruleDraftRefs;
    source.delete(token);
    state.ruleLibraryOpenGroups.add(target.groupId);
    state.ruleLibraryFocusGroupId = target.groupId;
    renewRuleApplyRequest();
    renderRuleSetWorkspace({ preserveInputs: true });
    saveWorkspaceDraftCache();
    setFeedback("ruleFeedback", "");
    focusWorkspaceLibrarySummary(target.groupId);
  }

  function mergeCustomRuleSnapshot(rule) {
    if (!rule || !clean(rule.id) || !Number(rule.version)) return;
    const registry = state.rules?.registry;
    if (!registry) return;
    const items = Array.isArray(registry.items) ? [...registry.items] : [];
    const index = items.findIndex((item) => (
      clean(item.id) === clean(rule.id) && Number(item.version) === Number(rule.version)
    ));
    if (index >= 0) items.splice(index, 1, rule);
    else items.push(rule);
    registry.items = items;
    registry.count = items.length;
  }

  async function saveLibraryRule() {
    if (state.customRuleCreateBusy || ruleInteractionLocked() || ruleDraftBlocked()) return;
    const recoveringReceipt = state.customRuleCreatePendingReceipt;
    if (recoveringReceipt) {
      setFormLocked(byId("ruleDefinitionForm"), false, ["confirmRuleDefinitionButton", "closeRuleDialogButton"]);
    }
    const name = clean(byId("ruleName").value);
    const expression = clean(byId("ruleExpression").value);
    const description = clean(byId("ruleDescription").value);
    if (!name || !expression) {
      setFeedback("ruleDefinitionFeedback", !name ? "请填写规则名称。" : "请填写确定的执行条件。", true);
      byId(!name ? "ruleName" : "ruleExpression").focus();
      return;
    }
    const tagId = clean(byId("ruleTag").value);
    const requestId = state.customRuleCreateRequestId || newRequestId();
    state.customRuleCreateRequestId = requestId;
    state.customRuleCreateBusy = true;
    saveStandaloneRuleDraftCache();
    const form = byId("ruleDefinitionForm");
    const button = byId("confirmRuleDefinitionButton");
    setBusy(button, true, recoveringReceipt ? "正在核对…" : "正在保存…");
    setFormLocked(form, true, ["confirmRuleDefinitionButton"]);
    updateWorkspaceState({ announce: false });
    try {
      const result = await requestJson(API.customRuleCreate, {
        method: "POST",
        body: JSON.stringify({
          request_id: requestId,
          name,
          description,
          expression,
          tag_id: tagId,
          // 有来源就是「编辑」：后端在同一条规则下生成新版本，旧版本不动。
          base_ref: state.ruleEditingRef,
        }),
      });
      const rule = result.rule || null;
      const ref = clean(result.ref) || (rule ? `${rule.id}@${rule.version}` : "");
      let refreshWarning = "";
      try {
        state.rules = await requestJson(API.rules);
      } catch (_refreshError) {
        mergeCustomRuleSnapshot(rule);
        refreshWarning = "规则已保存，列表已按保存回执更新；恢复连接后会再次同步。";
      }
      state.customRuleFocusRef = ref;
      state.customRuleCreatePendingReceipt = false;
      await closeRuleDialog({ force: true });
      renderRuleSetWorkspace({ announce: false, preserveInputs: true });
      renderScreenStatus();
      setFeedback(
        "customRuleFeedback",
        refreshWarning || "规则已保存；需要时可再加入一级或二级组合。",
        false,
        !refreshWarning,
        Boolean(refreshWarning),
      );
      showToast("规则已保存。", Boolean(refreshWarning));
    } catch (error) {
      if (writeReceiptMayBeUncertain(error)) {
        state.customRuleCreatePendingReceipt = true;
        saveStandaloneRuleDraftCache();
        setFeedback(
          "ruleDefinitionFeedback",
          "保存结果待核对，请勿重复新建或修改内容；恢复连接后点击“重新核对保存”。",
          false,
          false,
          true,
        );
      } else {
        state.customRuleCreatePendingReceipt = false;
        saveStandaloneRuleDraftCache();
        setFeedback("ruleDefinitionFeedback", error.message, true);
      }
    } finally {
      state.customRuleCreateBusy = false;
      setFormLocked(form, false, ["confirmRuleDefinitionButton"]);
      setBusy(button, false);
      if (state.customRuleCreatePendingReceipt) {
        button.textContent = "重新核对保存";
        setFormLocked(form, true, ["confirmRuleDefinitionButton", "closeRuleDialogButton"]);
      }
      updateWorkspaceState({ announce: false });
      renderWorkspaceLibrary();
    }
  }

  // 分类标签的三个动作都很轻：只改 rules/tags.yaml，规则文件一律不动。
  async function withLibraryWrite(run, { failureMessage } = {}) {
    if (state.customRuleDeleteBusy || state.customRuleCreateBusy) return false;
    if (ruleInteractionLocked() || ruleDraftBlocked()) return false;
    state.customRuleDeleteBusy = true;
    setFeedback("customRuleFeedback", "");
    renderWorkspaceLibrary();
    try {
      const message = await run();
      state.rules = await requestJson(API.rules);
      renderRuleSetWorkspace({ announce: false, preserveInputs: true });
      renderScreenStatus();
      if (message) {
        setFeedback("customRuleFeedback", message, false, true);
        showToast(message);
      }
      return true;
    } catch (error) {
      const text = error.message || failureMessage || "操作失败。";
      setFeedback("customRuleFeedback", text, true);
      showToast(text, true);
      return false;
    } finally {
      state.customRuleDeleteBusy = false;
      updateWorkspaceState({ announce: false });
      renderWorkspaceLibrary();
    }
  }

  // 名称校验就地在弹窗里做：不合法时提示留在输入框下方，弹窗不关，
  // 也不会把请求发出去换一条来自后端的错误。取消返回 null，保存返回合法名称。
  async function askTagLabel(title, initial = "") {
    const raw = await askConfirm({
      title,
      message: `最多 ${TAG_LABEL_MAXIMUM} 个字，不能包含空格。`,
      confirmLabel: "保存",
      input: initial,
      validate: (value) => (
        /\s/u.test(value) || value.length > TAG_LABEL_MAXIMUM
          ? `分类名称不能含空格，且不超过 ${TAG_LABEL_MAXIMUM} 个字。`
          : ""
      ),
    });
    return raw === null ? null : clean(raw);
  }

  async function createRuleTag() {
    const label = await askTagLabel("新分类名称");
    if (!label) return;
    await withLibraryWrite(async () => {
      await requestJson(API.ruleTags, {
        method: "POST",
        body: JSON.stringify({ label }),
      });
      return `分类“${label}”已创建。`;
    });
  }

  async function renameRuleTag(tagId) {
    const id = clean(tagId);
    const current = ruleTags().find((tag) => clean(tag.id) === id);
    if (!id || !current) return;
    const label = await askTagLabel("新的分类名称", clean(current.label));
    if (!label || label === clean(current.label)) return;
    await withLibraryWrite(async () => {
      await requestJson(`${API.ruleTags}/${encodeURIComponent(id)}`, {
        method: "POST",
        body: JSON.stringify({ label }),
      });
      return `分类已改名为“${label}”。`;
    });
  }

  async function deleteRuleTag(tagId) {
    const id = clean(tagId);
    const current = ruleTags().find((tag) => clean(tag.id) === id);
    if (!id || !current) return;
    const label = clean(current.label) || id;
    const count = Number(current.count) || 0;
    if (!await askConfirm({
      title: `删除分类“${label}”？`,
      message: count
        ? `${count} 条规则会落到「未分类」，规则本身不会被删除。`
        : "这个分类下还没有规则。",
      confirmLabel: "删除分类",
      danger: true,
    })) return;
    await withLibraryWrite(async () => {
      const result = await requestJson(`${API.ruleTags}/${encodeURIComponent(id)}`, {
        method: "DELETE",
        body: JSON.stringify({}),
      });
      const released = (result.released_rule_ids || []).length;
      return released
        ? `分类“${label}”已删除，${released} 条规则已落到「未分类」。`
        : `分类“${label}”已删除。`;
    });
  }

  async function importDefaultRules() {
    if (!await askConfirm({
      title: "导入默认规则？",
      message: "会把当前缺失的默认规则和对应分类补回来；已有的规则不会被覆盖。",
      confirmLabel: "导入",
    })) return;
    await withLibraryWrite(async () => {
      const result = await requestJson(API.ruleDefaults, {
        method: "POST",
        body: JSON.stringify({}),
      });
      const count = Number(result.imported_count) || 0;
      const labels = (result.tag_labels || []).join("、");
      if (!count) return "默认规则都在，没有需要补的。";
      return labels
        ? `已补回 ${count} 条默认规则，分类：${labels}。`
        : `已补回 ${count} 条默认规则。`;
    });
  }

  async function deleteLibraryRule(ruleId) {
    const id = clean(ruleId);
    if (!id || state.customRuleDeleteBusy) return;
    if (ruleInteractionLocked() || ruleDraftBlocked() || state.customRuleCreateBusy) return;
    const name = clean(libraryRule(id)?.name) || id;
    if (!await askConfirm({
      title: `删除规则“${name}”？`,
      message: "删除后不能恢复；已保存的组合和历史批次不受影响。",
      confirmLabel: "删除规则",
      danger: true,
    })) return;
    state.customRuleDeleteBusy = true;
    setFeedback("customRuleFeedback", "");
    renderWorkspaceLibrary();
    try {
      await requestJson(`${API.customRuleCreate}/${encodeURIComponent(id)}`, {
        method: "DELETE",
        body: JSON.stringify({}),
      });
      state.rules = await requestJson(API.rules);
      renderRuleSetWorkspace({ announce: false, preserveInputs: true });
      renderScreenStatus();
      setFeedback("customRuleFeedback", `规则“${name}”已删除。`, false, true);
      showToast("规则已删除。");
    } catch (error) {
      // 被引用时后端会连引用位置一起返回，直接读给用户，不让他去猜在哪里用过。
      const references = Array.isArray(error.references) ? error.references : [];
      const message = error.code === "RULE_IN_USE" && references.length
        ? `${error.message}请先把它从这些位置移除。`
        : error.message;
      setFeedback("customRuleFeedback", message, true);
      showToast(message, true);
      if (writeReceiptMayBeUncertain(error)) return;
      try {
        state.rules = await requestJson(API.rules);
        renderRuleSetWorkspace({ announce: false, preserveInputs: true });
      } catch (_refreshError) {
        // 刷新失败时保留现有列表；连接恢复后照常同步。
      }
    } finally {
      state.customRuleDeleteBusy = false;
      updateWorkspaceState({ announce: false });
      renderWorkspaceLibrary();
    }
  }

  function saveWorkspaceRuleDefinition(event) {
    event.preventDefault();
    void saveLibraryRule();
  }


  function bindRuleEvents() {
    byId("ruleEditorForm").addEventListener("submit", (event) => {
      event.preventDefault();
      saveWorkspaceRuleSet();
    });
    byId("ruleList").addEventListener("click", (event) => {
      const moveButton = event.target.closest("[data-move-rule]");
      if (moveButton) {
        moveWorkspaceRule(moveButton.dataset.ruleToken, moveButton.dataset.moveRule, moveButton.dataset.ruleStage);
        return;
      }
      const removeButton = event.target.closest("[data-remove-rule]");
      if (removeButton) {
        removeWorkspaceRule(removeButton.dataset.removeRule, removeButton.dataset.ruleStage);
        return;
      }
    });
    byId("secondaryRuleList").addEventListener("click", (event) => {
      const moveButton = event.target.closest("[data-move-rule]");
      if (moveButton) return moveWorkspaceRule(moveButton.dataset.ruleToken, moveButton.dataset.moveRule, "secondary");
      const removeButton = event.target.closest("[data-remove-rule]");
      if (removeButton) return removeWorkspaceRule(removeButton.dataset.removeRule, "secondary");
    });
    document.querySelectorAll("[data-stage-add]").forEach((button) => {
      button.addEventListener("click", () => {
        openRulePicker(button.dataset.stageAdd === "secondary" ? "secondary" : "primary", button);
      });
    });
    byId("rulesSurfaceTabs").addEventListener("click", (event) => {
      const button = event.target.closest("[data-rules-surface-tab]");
      if (button) void setRulesSurface(button.dataset.rulesSurfaceTab);
    });
    byId("ruleEditorBackButton").addEventListener("click", () => void leaveRuleEditor());
    byId("rulePickerCancelButton").addEventListener("click", () => closeRulePicker({ apply: false }));
    byId("rulePickerConfirmButton").addEventListener("click", () => closeRulePicker({ apply: true }));
    byId("ruleLibraryGroups").addEventListener("click", (event) => {
      const renameTagButton = event.target.closest("[data-rename-rule-tag]");
      if (renameTagButton) {
        // summary 里的按钮不能顺带把分组折叠了。
        event.preventDefault();
        void renameRuleTag(renameTagButton.dataset.renameRuleTag);
        return;
      }
      const deleteTagButton = event.target.closest("[data-delete-rule-tag]");
      if (deleteTagButton) {
        event.preventDefault();
        void deleteRuleTag(deleteTagButton.dataset.deleteRuleTag);
        return;
      }
      const editButton = event.target.closest("[data-edit-library-rule]");
      if (editButton) {
        openRuleDialog(editButton.dataset.editLibraryRule);
        return;
      }
      const deleteButton = event.target.closest("[data-delete-library-rule]");
      if (deleteButton) {
        void deleteLibraryRule(deleteButton.dataset.deleteLibraryRule);
        return;
      }
      const button = event.target.closest("[data-add-library-rule]");
      if (button) toggleRulePickerSelection(button.dataset.addLibraryRule);
    });
    byId("ruleLibraryGroups").addEventListener("toggle", (event) => {
      const details = event.target.closest("[data-rule-library-group]");
      if (!details) return;
      if (details.open) state.ruleLibraryOpenGroups.add(details.dataset.ruleLibraryGroup);
      else state.ruleLibraryOpenGroups.delete(details.dataset.ruleLibraryGroup);
    }, true);
    byId("ruleSearchInput").addEventListener("input", () => {
      state.ruleSearchQuery = byId("ruleSearchInput").value;
      renderWorkspaceLibrary();
    });
    byId("ruleSetName").addEventListener("input", () => {
      if (workspaceIsReadOnly() || ruleInteractionLocked() || ruleDraftBlocked()) return;
      state.ruleSetDraftName = byId("ruleSetName").value;
      renewRuleApplyRequest();
      updateWorkspaceState();
      saveWorkspaceDraftCache();
    });
    byId("ruleSetList").addEventListener("click", async (event) => {
      const deleteButton = event.target.closest("[data-delete-rule-set]");
      if (deleteButton) {
        await deleteWorkspaceRuleSet(deleteButton.dataset.deleteRuleSet);
        return;
      }
      const activateButton = event.target.closest("[data-activate-rule-set]");
      if (activateButton) {
        await activateWorkspaceRuleSet(
          activateButton.dataset.activateRuleSet,
          Number(activateButton.dataset.ruleSetVersion),
        );
        return;
      }
      const editButton = event.target.closest("[data-edit-rule-set]");
      if (editButton) {
        state.ruleEditorOpener = editButton;
        state.rulesSurface = "editor";
        await selectWorkspaceRuleSet(editButton.dataset.editRuleSet, {
          version: Number(editButton.dataset.ruleSetVersion),
          openEditor: true,
          detailMode: "edit",
        });
        return;
      }
      const toggleButton = event.target.closest("[data-toggle-rule-set]");
      if (!toggleButton) return;
      const id = toggleButton.dataset.toggleRuleSet;
      if (state.ruleEditorOpen && state.ruleSetDraftKind === "existing" && clean(state.selectedRuleSetId) === clean(id)) {
        if (workspaceDirty() && !await askConfirm({
          title: "放弃未保存的修改？",
          message: "收起详情后，当前改动不会保留。",
          confirmLabel: "放弃并收起",
          cancelLabel: "继续编辑",
          danger: true,
        })) return;
        const item = workspaceRuleSetItem(id);
        if (item && state.selectedRuleSetExact) {
          setWorkspaceFromExact(item, state.selectedRuleSetExact);
        }
        state.ruleEditorOpen = false;
        state.ruleDetailMode = "view";
        safeStorageRemove(RULE_DRAFT_STORAGE_KEY);
        renderRuleSetWorkspace();
        toggleButton.focus({ preventScroll: true });
        return;
      }
      await selectWorkspaceRuleSet(id, {
        version: Number(toggleButton.dataset.ruleSetVersion),
        openEditor: true,
        detailMode: "view",
      });
    });
    byId("newRuleSetButton").addEventListener("click", () => {
      state.ruleEditorOpener = byId("newRuleSetButton");
      state.rulesSurface = "editor";
      void startWorkspaceRuleSet();
    });
    byId("cancelRuleEditButton").addEventListener("click", cancelWorkspaceRuleEdit);
    byId("resetRulesButton").addEventListener("click", async () => {
      const reloadRequired = ruleDraftBlocked();
      const pendingTarget = state.ruleSavePendingVerification;
      const selectedId = state.selectedRuleSetId;
      const missingExistingDraft = !pendingTarget
        && state.ruleSetDraftKind === "existing"
        && Boolean(clean(selectedId))
        && !workspaceTargetExists(selectedId);
      resetRuleOperation();
      if (missingExistingDraft) {
        preserveWorkspaceAsNewDraft();
        renderRuleSetWorkspace({ announce: false, preserveInputs: true });
        const message = "原组合已被删除；当前修改已保留为新组合草稿，请确认后保存。";
        setFeedback("ruleFeedback", message, false, false, true);
        setFeedback("ruleSetListFeedback", message, false, false, true);
        showToast("当前修改已保留为新草稿。", true);
        focusWorkspaceRuleEditor();
        return;
      }
      if (pendingTarget?.stage === "activation_receipt") {
        state.ruleInputsUncertain = false;
        try {
          const outcome = await reconcileWorkspaceTarget(
            pendingTarget.id,
            pendingTarget.version,
            { preserveEditor: true },
          );
          state.ruleSavePendingVerification = null;
          state.ruleOperation = { phase: "idle", token: state.ruleOperation.token + 1 };
          safeStorageRemove(RULE_DRAFT_STORAGE_KEY);
          renderRuleSetWorkspace({ announce: false, preserveInputs: true });
          setFeedback("ruleFeedback", "");
          if (outcome.isExactCurrent) {
            confirmWorkspaceActivation(pendingTarget.id, pendingTarget.version);
            setFeedback("ruleSetListFeedback", "");
            showToast("已核对：设置已生效。");
          } else {
            setFeedback("ruleSetListFeedback", "已核对：设置未生效，可重新操作。", false, false, true);
            showToast("已核对：设置未生效，可重新操作。", true);
          }
          focusWorkspaceRuleSetRow(pendingTarget.id);
        } catch (error) {
          if (error.code === "RULE_SET_TARGET_DELETED") {
            state.ruleSavePendingVerification = null;
            state.ruleInputsUncertain = false;
            safeStorageRemove(RULE_DRAFT_STORAGE_KEY);
            const message = "原组合已被删除，本次设置目标不再可用。";
            setFeedback("ruleSetListFeedback", message, false, false, true);
            showToast("原组合已被删除。", true);
            try {
              await refreshWorkspaceRules();
            } catch (_fallbackError) {
              setFeedback("ruleSetListFeedback", message, false, false, true);
            }
          } else {
            state.ruleSavePendingVerification = pendingTarget;
            state.ruleOperation = { phase: "uncertain", token: state.ruleOperation.token + 1 };
            state.ruleInputsUncertain = true;
            saveWorkspaceDraftCache();
            renderRuleSetWorkspace({ announce: false, preserveInputs: true });
            const message = "仍无法核对设置结果，请勿重复操作；恢复连接后再次重新载入。";
            setFeedback("ruleFeedback", message, false, false, true);
            setFeedback("ruleSetListFeedback", message, false, false, true);
            showToast("仍无法核对设置结果。", true);
          }
        }
        return;
      }
      if (pendingTarget?.stage === "delete_receipt") {
        state.ruleInputsUncertain = false;
        try {
          await refreshWorkspaceRules({
            selectId: pendingTarget.selected ? pendingTarget.id : (pendingTarget.previousSelectedId || selectedId),
          });
          const stillExists = workspaceRuleSetItems().some((item) => clean(item.id) === clean(pendingTarget.id));
          if (stillExists) {
            const message = `“${pendingTarget.name || "该组合"}”仍存在，本次未删除；你可以重新点击删除。`;
            setFeedback("ruleFeedback", message, false, false, true);
            setFeedback("ruleSetListFeedback", message, false, false, true);
            focusWorkspaceRuleSetRow(pendingTarget.id);
          } else {
            setFeedback("ruleFeedback", "");
            setFeedback("ruleSetListFeedback", "");
            showToast("组合已确认删除。");
          }
        } catch (_error) {
          state.ruleSavePendingVerification = pendingTarget;
          state.ruleOperation = { phase: "uncertain", token: state.ruleOperation.token + 1 };
          state.ruleInputsUncertain = true;
          saveWorkspaceDraftCache();
          renderRuleSetWorkspace({ announce: false, preserveInputs: true });
          const message = "仍无法核对删除结果，请勿重复操作；恢复连接后再次重新载入。";
          setFeedback("ruleFeedback", message, false, false, true);
          setFeedback("ruleSetListFeedback", message, false, false, true);
          showToast("仍无法核对删除结果。", true);
        }
        return;
      }
      if (pendingTarget?.stage === "save_receipt") {
        state.ruleInputsUncertain = false;
        state.ruleApplyRequestId = clean(pendingTarget.requestId) || state.ruleApplyRequestId;
        await saveWorkspaceRuleSet({
          receiptRecovery: pendingTarget,
        });
        return;
      }
      if (pendingTarget?.stage === "saved_target") {
        state.ruleInputsUncertain = false;
        try {
          state.rules = await requestJson(API.rules);
          if (!workspaceTargetExists(pendingTarget.id)) {
            preserveWorkspaceAsNewDraft();
            renderRuleSetWorkspace({ announce: false, preserveInputs: true });
            const message = "原组合已被删除；当前规则已保留为新组合草稿，请确认后保存。";
            setFeedback("ruleFeedback", message, false, false, true);
            setFeedback("ruleSetListFeedback", message, false, false, true);
            showToast("原组合已删除，规则已保留为新草稿。", true);
            focusWorkspaceRuleEditor();
          } else {
            const selected = await selectWorkspaceRuleSet(pendingTarget.id, {
              force: true,
              version: pendingTarget.version,
              openEditor: true,
            });
            if (!selected) throw new Error("已读取组合列表，但保存版本仍无法核对。");
            safeStorageRemove(RULE_DRAFT_STORAGE_KEY);
            setFeedback("ruleFeedback", "");
            setFeedback("ruleSetListFeedback", "");
            showToast("组合已核对并保存。");
          }
        } catch (_error) {
          state.ruleSavePendingVerification = pendingTarget;
          state.ruleOperation = { phase: "uncertain", token: state.ruleOperation.token + 1 };
          state.ruleInputsUncertain = true;
          saveWorkspaceDraftCache();
          renderRuleSetWorkspace({ announce: false, preserveInputs: true });
          setFeedback("ruleFeedback", "仍无法核对已保存版本；草稿继续保留，请勿重复提交。", false, false, true);
        }
        return;
      }
      if (reloadRequired) {
        try {
          await refreshWorkspaceRules({
            selectId: pendingTarget?.id || selectedId,
            version: pendingTarget?.version || null,
          });
        } catch (_error) {
          state.ruleSavePendingVerification = pendingTarget;
          state.ruleOperation = { phase: "uncertain", token: state.ruleOperation.token + 1 };
          state.ruleInputsUncertain = true;
          saveWorkspaceDraftCache();
          renderRuleSetWorkspace({ announce: false, preserveInputs: true });
          setFeedback("ruleFeedback", "仍无法重新读取规则；草稿继续保留，请勿重复提交。", false, false, true);
        }
        return;
      }
      if (state.ruleSetDraftKind === "existing") {
        await selectWorkspaceRuleSet(state.selectedRuleSetId, {
          force: true,
          version: state.selectedRuleSetVersion,
        });
      } else {
        const activeId = activeRuleSet()?.id;
        if (activeId) await selectWorkspaceRuleSet(activeId, { force: true });
        else await startWorkspaceRuleSet();
      }
    });
    byId("addRuleButton").addEventListener("click", () => openRuleDialog());
    byId("manageLibraryButton").addEventListener("click", () => {
      state.libraryManageMode = !state.libraryManageMode;
      renderWorkspaceLibrary();
      byId("manageLibraryButton").focus({ preventScroll: true });
    });
    byId("addRuleTagButton").addEventListener("click", () => void createRuleTag());
    byId("importDefaultRulesButton").addEventListener("click", () => void importDefaultRules());
    byId("closeRuleDialogButton").addEventListener("click", () => void closeRuleDialog());
    byId("ruleDialog").addEventListener("cancel", (event) => {
      event.preventDefault();
      void closeRuleDialog();
    });
    byId("ruleDefinitionForm").addEventListener("submit", saveWorkspaceRuleDefinition);
    ["ruleName", "ruleExpression", "ruleDescription"].forEach((id) => {
      byId(id).addEventListener("input", () => saveStandaloneRuleDraftCache());
    });
    byId("ruleTag").addEventListener("change", () => saveStandaloneRuleDraftCache());
    byId("ruleExpressionExamples").addEventListener("click", applyRuleExample);
  }

export {
  activeRuleSet,
  bindRuleEvents,
  initializeRuleSetWorkspace,
  renderRuleSetWorkspace,
  restoreStandaloneRuleDraftCache,
  ruleDialogSignature,
  workspaceDirty,
};
