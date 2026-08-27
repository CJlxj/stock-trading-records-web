((global) => {
  "use strict";

  const DOMAIN_CONTRACT_VERSION = "panel-domain-v1.8";
  const CORE_TASKS = Object.freeze(["data", "rules", "screening", "records"]);

  const API = Object.freeze({
    web: Object.freeze({
      health: "/api/health",
      contract: "/api/panel-contract",
      bootstrap: "/api/bootstrap",
      dataStatus: "/api/datasets/status",
      dataScan: "/api/datasets/scan",
      dataUpdate: "/api/market-data/update",
      dataImport: "/api/market-data/import",
      catalogStatus: "/api/stock-catalog/status",
      catalogSearch: "/api/stock-catalog/search",
      catalogSync: "/api/stock-catalog/sync",
      stocks: "/api/local-stocks",
      rules: "/api/rules",
      customRuleCreate: "/api/custom-rules",
      ruleTags: "/api/rule-tags",
      ruleDefaults: "/api/rule-defaults",
      ruleSets: "/api/rule-sets",
      screeningLatest: "/api/screenings/latest",
      screeningPreflight: "/api/screening/preflight",
      screeningRun: "/api/screening/run",
      screenings: "/api/screenings",
      trades: "/api/trades",
    }),
  });

  const RESULT_STATUS = Object.freeze({
    CANDIDATE: Object.freeze({ label: "候选", className: "is-candidate", priority: 0 }),
    SECONDARY_FILTERED: Object.freeze({ label: "二级未通过", className: "is-secondary-filtered", priority: 1 }),
    SECONDARY_DATA_GAP: Object.freeze({ label: "二级待补", className: "is-secondary-gap", priority: 2 }),
    NEAR_MISS: Object.freeze({ label: "接近门槛", className: "is-watch", priority: 3 }),
    DATA_GAP: Object.freeze({ label: "数据待补", className: "is-data-gap", priority: 4 }),
    NOT_SELECTED: Object.freeze({ label: "未入选", className: "is-not-selected", priority: 5 }),
    REJECTED: Object.freeze({ label: "基础未通过", className: "is-rejected", priority: 6 }),
  });

  const EVIDENCE_METRIC_LABELS = Object.freeze({
    actual: "实际日期",
    amount: "成交额",
    atr14: "ATR14",
    atr_pct: "ATR 占比",
    available_rows: "现有日线",
    avg_amount_20d: "20 日均额",
    bb_mid: "布林中轨",
    bb_upper: "布林上轨",
    close: "收盘价",
    distance_from_ma20: "距 MA20",
    drawdown_from_20d_high: "距 20 日高点",
    expected: "目标日期",
    gap_pct: "跳空幅度",
    high: "最高价",
    high_20: "20 日最高",
    high_60: "60 日最高",
    low: "最低价",
    low_20: "20 日最低",
    low_60: "60 日最低",
    ma5: "MA5",
    ma10: "MA10",
    ma20: "MA20",
    ma30: "MA30",
    ma60: "MA60",
    ma20_slope: "MA20 方向",
    macd: "MACD",
    macd_hist: "MACD 柱",
    macd_hist_diff: "MACD 柱变化",
    obv: "OBV",
    obv_slope_10: "OBV 十日变化",
    open: "开盘价",
    price: "价格",
    required_rows: "规则至少需要",
    ret_1d: "当日涨跌",
    roc20: "20 日动量",
    rsi: "RSI",
    rsi14: "RSI14",
    vol_ma5: "5 日均量",
    vol_ma20: "20 日均量",
    volume: "成交量",
    volume_ratio: "量比",
  });

  const UNASSIGNED_TAG = Object.freeze({ id: "", label: "未分类" });

  function clean(value) {
    return String(value ?? "").trim();
  }

  function apiFor(surface) {
    if (!Object.hasOwn(API, surface)) throw new Error(`未知面板类型：${surface}`);
    return API[surface];
  }

  function ruleSearchText(item = {}) {
    const inputs = Array.isArray(item.inputs) ? item.inputs.join(" ") : "";
    return [
      item.id,
      item.name,
      item.description,
      item.plain_template,
      item.group_label,
      item.family,
      inputs,
      item.implementation?.normalized_expression,
      item.implementation?.expression,
    ].map(clean).join(" ").toLocaleLowerCase("zh-CN");
  }

  // 规则库只按标签分组：标签清单由服务端给出（创建顺序），没有标签或标签已被
  // 删除的规则一律落到最后的「未分类」。这里不再有任何「基础 / 本人」的概念。
  function ruleTagGroups(items = [], tags = [], query = "", selectedRefs = []) {
    const selected = selectedRefs instanceof Set ? selectedRefs : new Set(selectedRefs || []);
    const needle = clean(query).toLocaleLowerCase("zh-CN");
    const seen = new Set();
    const catalogItems = (Array.isArray(items) ? items : []).filter((item) => {
      if (!item || item.kind && item.kind !== "scored") return false;
      if (clean(item.id).startsWith("system.")) return false;
      const ref = `${clean(item.id)}@${item.version}`;
      if (!ref || seen.has(ref)) return false;
      seen.add(ref);
      return !needle || ruleSearchText(item).includes(needle);
    });
    const declared = (Array.isArray(tags) ? tags : [])
      .map((tag) => ({ id: clean(tag?.id), label: clean(tag?.label) || clean(tag?.id) }))
      .filter((tag) => tag.id);
    const known = new Set(declared.map((tag) => tag.id));
    const tagOf = (item) => {
      const id = clean(item.tag_id);
      return known.has(id) ? id : UNASSIGNED_TAG.id;
    };
    const isSelected = (item) => selected.has(`${clean(item.id)}@${item.version}`);
    return [...declared, UNASSIGNED_TAG].map((tag) => {
      const all = catalogItems.filter((item) => tagOf(item) === tag.id);
      const available = all.filter((item) => !isSelected(item));
      return {
        id: tag.id,
        label: tag.label,
        unassigned: tag.id === UNASSIGNED_TAG.id,
        items: available,
        count: available.length,
        selectedCount: all.length - available.length,
      };
    }).filter((group) => !needle || group.count > 0);
  }

  function ruleSetEditorMode(ruleSet = {}) {
    const declared = clean(ruleSet.editor_mode);
    if (["simple_all", "legacy_advanced"].includes(declared)) return declared;
    const requiredCount = ruleSet.required?.rules?.length || 0;
    const vetoCount = ruleSet.veto?.rules?.length || 0;
    const secondaryCount = ruleSet.secondary?.rules?.length || 0;
    const scoredCount = ruleSet.scored?.rules?.length || Number(ruleSet.primary_rule_count || 0);
    const passRatio = Number(ruleSet.scored?.pass_ratio);
    if (
      requiredCount
      || vetoCount
      || secondaryCount
      || (scoredCount && (!Number.isFinite(passRatio) || Math.abs(passRatio - 1) > 1e-9))
    ) return "legacy_advanced";
    return "simple_all";
  }

  function ruleSetFlowSummary(ruleSet = {}) {
    const mode = ruleSetEditorMode(ruleSet);
    const refsFor = (key) => (
      Array.isArray(ruleSet?.[key]?.rules) ? [...ruleSet[key].rules] : []
    );
    const requiredRefs = refsFor("required");
    const vetoRefs = refsFor("veto");
    const scoredRefs = refsFor("scored");
    const secondaryRefs = refsFor("secondary");
    const primaryCount = scoredRefs.length || Math.max(0, Number(ruleSet.primary_rule_count) || 0);
    const secondaryCount = secondaryRefs.length || Math.max(0, Number(ruleSet.secondary_rule_count) || 0);
    const rawPassRatio = Number(ruleSet.scored?.pass_ratio);
    const passRatio = Number.isFinite(rawPassRatio) ? rawPassRatio : 1;
    const primaryMinimum = primaryCount
      ? (mode === "simple_all"
        ? primaryCount
        : Math.max(1, Math.min(primaryCount, Math.ceil(passRatio * primaryCount))))
      : 0;
    const rawSecondaryMinimum = Number(ruleSet.secondary?.minimum_match);
    const secondaryMinimumMatch = secondaryCount
      ? Math.max(
        1,
        Math.min(
          secondaryCount,
          Number.isFinite(rawSecondaryMinimum) && rawSecondaryMinimum > 0
            ? Math.ceil(rawSecondaryMinimum)
            : secondaryCount,
        ),
      )
      : 0;
    const rawTopN = Number(ruleSet.ranking?.top_n);
    const topN = Math.max(1, Math.min(20, Number.isFinite(rawTopN) ? Math.round(rawTopN) : 5));

    const primary = {
      label: "第 1 轮 · 初筛",
      status: primaryCount ? "ACTIVE" : "INACTIVE",
      mode: mode === "simple_all" ? "ALL" : "MINIMUM_MATCH",
      count: primaryCount,
      minimum: primaryMinimum,
      summary: mode === "simple_all"
        ? `${primaryCount} 条规则，全部满足才进入候选。`
        : `${primaryCount} 条候选规则，至少满足 ${primaryMinimum} 条。`,
    };
    const secondaryStatus = secondaryCount ? "ACTIVE" : "INACTIVE";
    const secondary = {
      label: "第 2 轮 · 复筛",
      status: secondaryStatus,
      mode: secondaryStatus === "ACTIVE" ? (mode === "simple_all" ? "ALL" : "MINIMUM_MATCH") : "NONE",
      count: secondaryCount,
      minimum: secondaryStatus === "ACTIVE" ? secondaryMinimumMatch : 0,
      summary: secondaryCount
        ? (mode === "simple_all"
          ? `${secondaryCount} 条规则，全部满足；只继续收窄一级候选。`
          : `${secondaryCount} 条二级规则，至少满足 ${secondaryMinimumMatch} 条；只继续收窄一级候选。`)
        : "未添加二级规则；一级通过后直接进入排序。",
    };
    const ranking = {
      label: "最后 · 排序展示",
      status: "ACTIVE",
      topN,
      summary: `只按一级规则顺序比较，二级仅过滤；默认展示前 ${topN} 只。`,
    };
    const groups = {
      required: {
        id: "required",
        label: "必须满足",
        refs: requiredRefs,
        count: requiredRefs.length,
        minimum: requiredRefs.length,
      },
      veto: {
        id: "veto",
        label: "命中即排除",
        refs: vetoRefs,
        count: vetoRefs.length,
        minimum: vetoRefs.length ? 1 : 0,
      },
      scored: {
        id: "scored",
        label: "一级候选规则",
        refs: scoredRefs,
        count: primaryCount,
        minimum: primaryMinimum,
      },
      secondary: {
        id: "secondary",
        label: "二级筛选规则",
        refs: secondaryRefs,
        count: secondaryCount,
        minimum: secondaryStatus === "ACTIVE" ? secondaryMinimumMatch : 0,
      },
    };
    const automaticRefs = refsFor("base_gates");
    const automatic = {
      id: "automatic",
      label: "系统检查",
      status: "AUTOMATIC",
      count: automaticRefs.length,
      minimum: automaticRefs.length,
      summary: "自动检查市场范围、停牌状态、历史长度和数据日期。",
    };
    const stages = [
      automatic,
      { id: "primary", ...primary },
      { id: "secondary", ...secondary },
      { id: "ranking", ...ranking },
    ];

    return {
      mode,
      primary,
      secondary,
      ranking,
      stages,
      groups,
    };
  }

  function ruleSetExactTargetOutcome(source = {}, targetId, targetVersion) {
    const normalizedTargetId = clean(targetId);
    const normalizedTargetVersion = Number(targetVersion);
    if (!normalizedTargetId || !Number.isFinite(normalizedTargetVersion) || normalizedTargetVersion <= 0) {
      return Object.freeze({
        status: "UNKNOWN",
        isExactCurrent: false,
        activeId: null,
        activeVersion: null,
      });
    }

    let active = null;
    let activeStateKnown = false;
    if (source && Object.hasOwn(source, "rule_sets")) {
      const ruleSets = source.rule_sets;
      activeStateKnown = Boolean(ruleSets && Object.hasOwn(ruleSets, "active"));
      active = ruleSets?.active ?? null;
    } else if (source && Object.hasOwn(source, "active")) {
      activeStateKnown = true;
      active = source.active ?? null;
    } else if (source && Object.hasOwn(source, "id")) {
      activeStateKnown = true;
      active = source;
    }
    if (!activeStateKnown) {
      return Object.freeze({
        status: "UNKNOWN",
        isExactCurrent: false,
        activeId: null,
        activeVersion: null,
      });
    }

    const activeId = clean(active?.id) || null;
    const activeVersionValue = Number(active?.version);
    const activeVersion = Number.isFinite(activeVersionValue) && activeVersionValue > 0
      ? activeVersionValue
      : null;
    if (!active) {
      return Object.freeze({
        status: "DIFFERENT",
        isExactCurrent: false,
        activeId,
        activeVersion,
      });
    }
    if (!activeId || !activeVersion) {
      return Object.freeze({
        status: "UNKNOWN",
        isExactCurrent: false,
        activeId,
        activeVersion,
      });
    }
    const isExactCurrent = activeId === normalizedTargetId
      && activeVersion === normalizedTargetVersion;
    return Object.freeze({
      status: isExactCurrent ? "CURRENT" : "DIFFERENT",
      isExactCurrent,
      activeId,
      activeVersion,
    });
  }

  function ruleSetActivationState(item = {}, active = null, activatingId = "") {
    const id = clean(item.id);
    const targetVersion = Number(
      item.latest_version || item.latest?.version || item.usable_version || item.version,
    );
    const outcome = ruleSetExactTargetOutcome({ active }, id, targetVersion);
    const currentLineage = Boolean(
      clean(active?.id)
      && clean(active?.id) === id
      && Number(active?.version) > 0
    );
    const activating = Boolean(id && clean(activatingId) === id);
    return Object.freeze({
      id,
      targetVersion: Number.isFinite(targetVersion) && targetVersion > 0 ? targetVersion : null,
      exactCurrent: outcome.isExactCurrent,
      currentLineage,
      activating,
      statusLabel: outcome.isExactCurrent ? "当前使用" : (currentLineage ? "旧版在用" : ""),
      actionLabel: activating ? "切换中…" : "使用",
    });
  }

  function ruleSetSummaries(ruleSets = {}) {
    const items = Array.isArray(ruleSets) ? ruleSets : ruleSets?.items || [];
    const active = Array.isArray(ruleSets) ? null : ruleSets?.active || null;
    const activeId = clean(active?.id);
    const activeVersion = Number(active?.version);
    return items
      .map((item) => {
        const id = clean(item.id);
        const itemActiveVersion = Number(item.active_version || item.usable_version);
        const isActive = activeId
          ? activeId === id && (
            !Number.isFinite(activeVersion)
            || !Number.isFinite(itemActiveVersion)
            || activeVersion === itemActiveVersion
          )
          : item.is_active === true || item.is_current === true;
        const capabilities = {
          ...(item.capabilities || {}),
          edit: item.can_edit ?? item.capabilities?.edit,
          clone: item.can_clone ?? item.capabilities?.clone,
          activate: item.can_activate ?? item.capabilities?.activate,
          delete: item.can_delete ?? item.capabilities?.delete,
        };
        return {
          ...item,
          id,
          name: clean(item.name || item.display_name || id || "未命名组合"),
          editor_mode: ruleSetEditorMode(item),
          latest_version: Number(item.latest_version || item.latest?.version) || null,
          active_version: activeId === id && Number.isFinite(activeVersion)
            ? activeVersion
            : (Number(item.active_version) || null),
          is_active: isActive,
          archived: item.archived === true,
          capabilities,
        };
      })
      .filter((item) => !item.archived)
      .sort((left, right) => (
        clean(left.name).localeCompare(clean(right.name), "zh-CN")
        || clean(left.id).localeCompare(clean(right.id))
      ));
  }

  function assertCompatibleContract(payload = {}) {
    const received = clean(payload.domain_contract_version);
    if (received !== DOMAIN_CONTRACT_VERSION) {
      const error = new Error("面板业务版本不一致，请重新启动服务并刷新页面。");
      error.code = "DOMAIN_CONTRACT_MISMATCH";
      error.expected = DOMAIN_CONTRACT_VERSION;
      error.received = received;
      throw error;
    }
    return true;
  }

  function selectedMinimum(active) {
    const selectedCount = active?.scored?.rules?.length || 1;
    return Math.max(
      1,
      Math.ceil(Number(active?.scored?.pass_ratio || 1) * selectedCount),
    );
  }

  function secondaryMinimum(active) {
    const selectedCount = active?.secondary?.rules?.length || 0;
    if (!selectedCount) return 0;
    return Math.max(
      1,
      Math.min(selectedCount, Number(active?.secondary?.minimum_match) || selectedCount),
    );
  }

  function resultStatus(row = {}) {
    const declared = clean(row.result_status).toUpperCase();
    if (Object.hasOwn(RESULT_STATUS, declared)) return declared;
    const selection = clean(row.selection_status).toUpperCase();
    const technical = clean(row.technical_status).toUpperCase();
    const history = clean(row.history_status).toUpperCase();
    if (row.final_candidate === true) return "CANDIDATE";
    if (selection === "PASS" && (row.primary_candidate === true || technical === "BUY_CANDIDATE")) {
      const secondary = clean(row.secondary_status || "INACTIVE").toUpperCase();
      if (secondary === "FAIL") return "SECONDARY_FILTERED";
      if (["DATA_GAP", "ERROR", "SKIPPED"].includes(secondary)) {
        return "SECONDARY_DATA_GAP";
      }
      return "CANDIDATE";
    }
    if (selection === "FAIL") return "REJECTED";
    if (selection === "DATA_GAP" || technical === "DATA_GAP" || history === "MISSING") {
      return "DATA_GAP";
    }
    if (selection === "PASS" && technical === "WATCH") return "NEAR_MISS";
    return "NOT_SELECTED";
  }

  function isCandidate(row) {
    return resultStatus(row) === "CANDIDATE";
  }

  function screeningRecordStatus(row) {
    const status = resultStatus(row);
    return { status, ...RESULT_STATUS[status] };
  }

  function screeningFreshness({
    result,
    dataset,
    activeRule,
    inputsUncertain = false,
    busy = false,
    offlineSnapshot = false,
  } = {}) {
    if (!result) {
      return { status: "empty", label: "暂无结果", message: "还没有筛选批次。", reasons: [] };
    }
    if (offlineSnapshot) {
      return {
        status: "stale",
        label: "离线快照",
        message: "当前只显示上次连接时保存的只读结果。",
        reasons: ["离线快照"],
      };
    }
    const meta = result.meta || {};
    const reasons = [];
    if (inputsUncertain) reasons.push("页面状态待刷新");
    if (meta.universe_scope !== "local_library") reasons.push("筛选范围不同");
    const currentDatasetHash = clean(dataset?.dataset_hash);
    const resultDatasetHash = clean(meta.dataset_hash);
    if (!currentDatasetHash || !resultDatasetHash) reasons.push("数据版本无法核对");
    else if (currentDatasetHash !== resultDatasetHash) reasons.push("数据已变化");
    const currentRuleHash = clean(activeRule?.rule_set_hash);
    const resultRuleHash = clean(meta.rule_hash || meta.rule_set_hash);
    if (!currentRuleHash || !resultRuleHash) reasons.push("规则版本无法核对");
    else if (currentRuleHash !== resultRuleHash) reasons.push("规则已变化");
    const uniqueReasons = [...new Set(reasons)];
    if (uniqueReasons.length) {
      return {
        status: "stale",
        label: `旧结果 · ${uniqueReasons[0]}`,
        message: `下方保留的是历史批次（${uniqueReasons.join("、")}），请按当前数据和规则重新筛选。`,
        reasons: uniqueReasons,
      };
    }
    if (busy) {
      return {
        status: "updating",
        label: "正在更新",
        message: "数据、规则或筛选任务正在进行，旧结果暂时只读。",
        reasons: [],
      };
    }
    return {
      status: "current",
      label: "当前结果",
      message: "结果与当前本地数据和规则一致。",
      reasons: [],
    };
  }

  function dataGapCount(dataset = {}) {
    const source = dataset || {};
    if (source.data_gap_count != null) return Number(source.data_gap_count);
    return Math.max(
      0,
      Number(source.discovered_count || 0) - Number(source.computable_count || 0),
    );
  }

  function datasetAlignmentSummary(dataset = {}) {
    const total = Math.max(0, Number(dataset.discovered_count || 0));
    const aligned = Math.max(0, Number(dataset.computable_count || 0));
    const currentDate = clean(dataset.current_date || dataset.today);
    const targetDate = clean(dataset.as_of_trade_date || dataset.target_trade_date);
    const actualDate = clean(
      dataset.actual_common_trade_date
      || dataset.latest_trade_date
      || (total > 0 && aligned === total ? targetDate : ""),
    );
    const declared = clean(dataset.alignment_status).toUpperCase();
    const supported = new Set([
      "CURRENT_DATE_ALIGNED",
      "ALIGNED_NOT_CURRENT",
      "PARTIAL",
      "EMPTY",
      "UNKNOWN",
    ]);
    let status = supported.has(declared) ? declared : "UNKNOWN";
    if (!supported.has(declared)) {
      if (!total) status = "EMPTY";
      else if (aligned !== total) status = "PARTIAL";
      else if (currentDate && targetDate && actualDate) {
        status = currentDate === targetDate && currentDate === actualDate
          ? "CURRENT_DATE_ALIGNED"
          : "ALIGNED_NOT_CURRENT";
      }
    }
    const explicitMatch = dataset.is_aligned_to_current_date;
    const isAlignedToCurrentDate = typeof explicitMatch === "boolean"
      ? explicitMatch
      : (status === "CURRENT_DATE_ALIGNED"
        ? true
        : (["ALIGNED_NOT_CURRENT", "PARTIAL"].includes(status) ? false : null));
    const presentation = {
      CURRENT_DATE_ALIGNED: {
        label: "已对齐当前日期",
        className: "is-ready",
        message: "全部本地股票已对齐，实际共同日期与今天一致。",
      },
      ALIGNED_NOT_CURRENT: {
        label: "库内已对齐 · 与今天不同",
        className: "is-warning",
        message: "全部股票彼此日期一致，但实际共同日期不是今天。周末或休市日出现这种情况可能正常。",
      },
      PARTIAL: {
        label: "部分未对齐",
        className: "is-warning",
        message: "本地股票没有全部对齐到本次目标日期，请查看逐股状态。",
      },
      EMPTY: {
        label: "暂无本地数据",
        className: "is-warning",
        message: "本地股票库暂无可用于比较日期的数据。",
      },
      UNKNOWN: {
        label: "对齐状态待确认",
        className: "is-warning",
        message: "当前响应缺少完整日期信息，暂时不能判断是否与今天一致。",
      },
    }[status];
    return {
      status,
      ...presentation,
      currentDate: currentDate || "—",
      targetDate: targetDate || "—",
      actualDate: actualDate || "—",
      total,
      aligned,
      unaligned: Math.max(0, total - aligned),
      isAlignedToCurrentDate,
      relationLabel: isAlignedToCurrentDate === true
        ? "是"
        : (isAlignedToCurrentDate === false ? "否" : "待确认"),
    };
  }

  function datasetStatusSummary(dataset = {}) {
    const alignment = datasetAlignmentSummary(dataset);
    const counts = Object.freeze({
      total: alignment.total,
      aligned: alignment.aligned,
      unaligned: alignment.unaligned,
      screenable: alignment.aligned,
    });
    const countLine = counts.total === 0
      ? "共 0 只 · 暂无可对齐数据"
      : (counts.unaligned > 0
        ? `共 ${counts.total} 只 · 已对齐 ${counts.aligned} 只 · 未对齐 ${counts.unaligned} 只`
        : `共 ${counts.total} 只 · 全部已对齐`);
    return Object.freeze({
      ...alignment,
      counts,
      dateLine: `目标 ${alignment.targetDate} · 实际 ${alignment.actualDate}`,
      countLine,
      relationLine: `与今天：${alignment.relationLabel}`,
      inlineLine: `${countLine} · 与今天：${alignment.relationLabel}`,
    });
  }

  function dataAlignmentStatus(entry = {}) {
    const status = clean(entry.status || entry.local_data_status).toUpperCase();
    if (status === "READY") return { status, label: "已对齐", className: "is-ready" };
    if (status === "INVALID") return { status, label: "数据异常", className: "is-error" };
    if (status === "MISSING") return { status, label: "缺少数据", className: "is-error" };
    if (status === "STALE_FOR_RUN") return { status, label: "日期落后", className: "is-warning" };
    return { status: status || "UNALIGNED", label: "未对齐", className: "is-warning" };
  }

  function catalogAction(item = {}) {
    if (!item.in_local_library) {
      return { type: "add", label: "加入并导入", disabled: false };
    }
    const localStatus = dataAlignmentStatus(item);
    if (localStatus.status === "READY") {
      return { type: "none", label: "已在本地", disabled: true, localStatus };
    }
    return {
      type: "update",
      label: localStatus.status === "STALE_FOR_RUN" ? "更新数据" : "补齐数据",
      disabled: false,
      localStatus,
    };
  }

  function evidenceMetricLabel(key) {
    return EVIDENCE_METRIC_LABELS[clean(key)] || clean(key);
  }

  function formatEvidenceValue(value, key = "") {
    if (value == null || value === "") return "—";
    if (typeof value === "boolean") return value ? "是" : "否";
    if (typeof value === "number") {
      if (!Number.isFinite(value)) return "—";
      if ([
        "atr_pct",
        "distance_from_ma20",
        "drawdown_from_20d_high",
        "gap_pct",
        "ret_1d",
        "roc20",
      ].includes(key)) {
        return `${new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 2 }).format(value * 100)}%`;
      }
      if (["available_rows", "required_rows"].includes(key)) return `${Math.round(value)} 日`;
      if (["amount", "avg_amount_20d"].includes(key)) {
        if (Math.abs(value) >= 100_000_000) return `${(value / 100_000_000).toFixed(2)} 亿元`;
        if (Math.abs(value) >= 10_000) return `${(value / 10_000).toFixed(2)} 万元`;
        return `${new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 0 }).format(value)} 元`;
      }
      if (["volume", "vol_ma5", "vol_ma20"].includes(key)) {
        if (Math.abs(value) >= 100_000_000) return `${(value / 100_000_000).toFixed(2)} 亿股`;
        if (Math.abs(value) >= 10_000) return `${(value / 10_000).toFixed(2)} 万股`;
        return `${new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 0 }).format(value)} 股`;
      }
      return new Intl.NumberFormat("zh-CN", {
        maximumFractionDigits: Number.isInteger(value) ? 0 : 4,
      }).format(value);
    }
    if (typeof value === "object") {
      try {
        return JSON.stringify(value);
      } catch (_error) {
        return "—";
      }
    }
    return clean(value);
  }

  function evidenceStatus(status) {
    const mapping = {
      PASS: ["通过", "is-pass"],
      FAIL: ["未通过", "is-fail"],
      DATA_GAP: ["数据待补", "is-data-gap"],
      ERROR: ["计算异常", "is-error"],
      SKIPPED: ["未计算", "is-skipped"],
    };
    const [label, className] = mapping[clean(status).toUpperCase()] || ["未判断", "is-skipped"];
    return { label, className };
  }

  function evidenceForRow(row = {}) {
    const all = Array.isArray(row.rule_evidence) ? row.rule_evidence : [];
    const rules = all.filter((item) => item.kind !== "base_gate");
    const baseIssues = all.filter((item) => item.kind === "base_gate" && item.status !== "PASS");
    return rules.length || baseIssues.length ? [...baseIssues, ...rules] : all;
  }

  function compactEvidenceCopy(item, resolveRuleName = null) {
    if (!item) return "没有可显示的规则实值";
    const fallbackName = clean(item.rule_id).split("@")[0] || "规则";
    const name = item.name || (resolveRuleName ? resolveRuleName(item.rule_id) : fallbackName) || fallbackName;
    const comparison = Array.isArray(item.comparisons) ? item.comparisons[0] : null;
    if (comparison) {
      return `${name}：${formatEvidenceValue(comparison.left)} ${clean(comparison.operator || "?")} ${formatEvidenceValue(comparison.right)}`;
    }
    const observed = Object.entries(item.observed || {})
      .filter(([, value]) => value !== undefined)
      .slice(0, 2)
      .map(([key, value]) => `${evidenceMetricLabel(key)} ${formatEvidenceValue(value, key)}`);
    return observed.length ? `${name}：${observed.join(" · ")}` : `${name}：暂无实值`;
  }

  function screeningRowsForDisplay(result = {}) {
    const all = Array.isArray(result.row_results)
      ? result.row_results
      : Array.isArray(result.rows) ? result.rows : [];
    const fullMarket = result.meta?.universe_source === "a_share_snapshot" || result.meta?.is_full_market;
    const visible = fullMarket
      ? (() => {
        const useful = all.filter((row) => (
          row.history_status && row.history_status !== "MISSING"
        ) || isCandidate(row) || resultStatus(row) === "NEAR_MISS");
        return (useful.length ? useful : all).slice(0, 100);
      })()
      : all;
    return visible.map((row, index) => ({ row, index }))
      .sort((left, right) => {
        const priorityDelta = RESULT_STATUS[resultStatus(left.row)].priority
          - RESULT_STATUS[resultStatus(right.row)].priority;
        if (priorityDelta) return priorityDelta;
        const leftRank = Number(left.row.rank_position || left.row.primary_rank_position);
        const rightRank = Number(right.row.rank_position || right.row.primary_rank_position);
        const leftHasRank = Number.isFinite(leftRank) && leftRank > 0;
        const rightHasRank = Number.isFinite(rightRank) && rightRank > 0;
        if (leftHasRank !== rightHasRank) return leftHasRank ? -1 : 1;
        if (leftHasRank && leftRank !== rightRank) return leftRank - rightRank;
        const leftScore = Number(left.row.ranking_score ?? left.row.primary_priority_score);
        const rightScore = Number(right.row.ranking_score ?? right.row.primary_priority_score);
        const safeLeftScore = Number.isFinite(leftScore) ? leftScore : -1;
        const safeRightScore = Number.isFinite(rightScore) ? rightScore : -1;
        if (safeLeftScore !== safeRightScore) return safeRightScore - safeLeftScore;
        const leftTotal = Number(left.row.active_rule_count || 0);
        const rightTotal = Number(right.row.active_rule_count || 0);
        const leftRatio = leftTotal ? Number(left.row.matched_rule_count || 0) / leftTotal : 0;
        const rightRatio = rightTotal ? Number(right.row.matched_rule_count || 0) / rightTotal : 0;
        if (leftRatio !== rightRatio) return rightRatio - leftRatio;
        return clean(left.row.symbol).localeCompare(clean(right.row.symbol)) || left.index - right.index;
      })
      .map((item) => item.row);
  }

  function screeningDisplayGroups(result = {}) {
    const rows = screeningRowsForDisplay(result);
    const topN = 5;
    const candidates = rows.filter((row) => resultStatus(row) === "CANDIDATE");
    const diagnostics = rows.filter((row) => resultStatus(row) !== "CANDIDATE");
    return {
      rows,
      topN,
      topCandidates: candidates.slice(0, topN),
      otherCandidates: candidates.slice(topN),
      diagnostics,
    };
  }

  function screeningRuleCounts(row = {}) {
    const primaryMatched = Number(
      row.primary_matched_rule_count ?? row.matched_rule_count ?? 0,
    );
    const primaryTotal = Number(
      row.primary_rule_count ?? row.active_rule_count ?? 0,
    );
    const secondaryMatched = Number(row.secondary_matched_rule_count ?? 0);
    const secondaryTotal = Number(row.secondary_rule_count ?? 0);
    const secondaryStatus = clean(row.secondary_status || "INACTIVE").toUpperCase();
    return {
      primaryMatched,
      primaryTotal,
      secondaryMatched,
      secondaryTotal,
      secondaryStatus,
      secondaryEnabled: secondaryTotal > 0 || !["", "INACTIVE", "SKIPPED"].includes(secondaryStatus),
    };
  }

  function screeningRank(row = {}) {
    const position = Number(row.rank_position || row.primary_rank_position);
    const score = Number(row.ranking_score ?? row.primary_priority_score);
    return {
      position: Number.isFinite(position) && position > 0 ? position : null,
      score: Number.isFinite(score) ? score : null,
      formula: clean(row.ranking_formula),
    };
  }

  function screeningCounts(summary = {}, rows = []) {
    const fallback = {
      candidates: 0,
      secondaryFiltered: 0,
      secondaryGaps: 0,
      near: 0,
      gaps: 0,
      notSelected: 0,
    };
    rows.forEach((row) => {
      const status = resultStatus(row);
      if (status === "CANDIDATE") fallback.candidates += 1;
      else if (status === "SECONDARY_FILTERED") fallback.secondaryFiltered += 1;
      else if (status === "SECONDARY_DATA_GAP") fallback.secondaryGaps += 1;
      else if (status === "NEAR_MISS") fallback.near += 1;
      else if (status === "DATA_GAP") fallback.gaps += 1;
      else fallback.notSelected += 1;
    });
    return {
      candidates: Number(summary.final_candidate_count ?? summary.candidate_count ?? fallback.candidates),
      secondaryFiltered: Number(summary.secondary_filtered_count ?? fallback.secondaryFiltered),
      secondaryGaps: Number(summary.secondary_data_gap_count ?? fallback.secondaryGaps),
      near: Number(summary.near_miss_count ?? fallback.near),
      gaps: Number(summary.result_data_gap_count ?? summary.data_gap_count ?? fallback.gaps),
      notSelected: Number(summary.not_selected_count ?? fallback.notSelected),
    };
  }

  function tradeDisciplineStatus({
    reasonTags = [],
    disciplineChecks = [],
    emotionFlags = [],
    emotionClear = false,
    requiredDisciplineCount = 3,
  } = {}) {
    if (emotionFlags.length) {
      return {
        status: "RISK_RECORDED",
        className: "is-risk",
        message: `已记录 ${emotionFlags.length} 项情绪风险；保存后历史会明确标出。`,
      };
    }
    if (reasonTags.length && disciplineChecks.length === requiredDisciplineCount && emotionClear) {
      return {
        status: "COMPLETE",
        className: "is-ready",
        message: `纪律核对完整：${reasonTags.length} 项操作依据、${requiredDisciplineCount} / ${requiredDisciplineCount} 项纪律、情绪已确认。`,
      };
    }
    return {
      status: "INCOMPLETE",
      className: "is-warning",
      message: `依据 ${reasonTags.length} 项 · 纪律 ${disciplineChecks.length} / ${requiredDisciplineCount} · ${emotionClear ? "情绪已确认" : "情绪未确认"}；仍可如实保存。`,
    };
  }

  function recordStatusClass(value) {
    if (value === "纪律核对完整") return "is-ready";
    if (value === "情绪风险已记录") return "is-risk";
    if (value === "纪律核对不完整") return "is-warning";
    return "";
  }

  global.StockPanelCore = Object.freeze({
    DOMAIN_CONTRACT_VERSION,
    CORE_TASKS,
    RESULT_STATUS,
    UNASSIGNED_TAG,
    apiFor,
    assertCompatibleContract,
    catalogAction,
    clean,
    compactEvidenceCopy,
    dataAlignmentStatus,
    datasetAlignmentSummary,
    datasetStatusSummary,
    dataGapCount,
    evidenceForRow,
    evidenceMetricLabel,
    evidenceStatus,
    formatEvidenceValue,
    isCandidate,
    recordStatusClass,
    resultStatus,
    ruleTagGroups,
    ruleSetActivationState,
    ruleSetEditorMode,
    ruleSetExactTargetOutcome,
    ruleSetFlowSummary,
    ruleSetSummaries,
    screeningCounts,
    screeningDisplayGroups,
    screeningFreshness,
    screeningRank,
    screeningRecordStatus,
    screeningRuleCounts,
    screeningRowsForDisplay,
    secondaryMinimum,
    selectedMinimum,
    tradeDisciplineStatus,
  });
})(globalThis);
