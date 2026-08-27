import {
  API,
  PanelCore,
  byId,
  clean,
  dataMutationBusy,
  escapeHtml,
  formatDateTime,
  requestJson,
  screeningConfigurationBusy,
  setBusy,
  setFeedback,
  showToast,
  state,
} from "../platform.js";
import { renderDataSummary } from "./data.js";
import {
  activeRuleSet,
  renderRuleSetWorkspace,
} from "./rules.js";

  function activeScoredRules() {
    return (state.rules?.registry?.items || [])
      .filter((item) => item.status === "ACTIVE" && item.kind === "scored")
      .filter((item, index, all) => (
        all.findIndex((candidate) => candidate.id === item.id && candidate.version === item.version) === index
      ));
  }

  function selectedMinimum(active = activeRuleSet()) {
    return PanelCore.selectedMinimum(active);
  }

  function screeningInputsAreUncertain() {
    return Boolean(state.screeningInputsUncertain || state.ruleInputsUncertain);
  }

  function screeningFreshness(result = state.screening) {
    return PanelCore.screeningFreshness({
      result,
      dataset: state.dataset,
      activeRule: activeRuleSet(),
      inputsUncertain: screeningInputsAreUncertain(),
      busy: screeningConfigurationBusy() || state.screeningBusy,
    });
  }

  function renderScreeningAction() {
    const freshness = screeningFreshness();
    const freshnessElement = byId("screenResultFreshness");
    freshnessElement.textContent = freshness.label;
    freshnessElement.className = `result-freshness is-${freshness.status}`;
    freshnessElement.title = freshness.message;

    const currentResult = freshness.status === "current";
    document.querySelectorAll("[data-record-symbol]").forEach((button) => {
      button.hidden = !currentResult;
      button.disabled = !currentResult;
    });

    const button = byId("runScreeningButton");
    if (!state.screeningBusy) {
      button.disabled = !canScreen();
      button.textContent = freshness.status === "stale"
        ? "按当前规则重新筛选"
        : freshness.status === "current"
          ? "再次核对当前结果"
          : "按当前规则筛选";
    }
    return freshness;
  }


  function renderRuleSummary() {
    const active = activeRuleSet();
    const count = active?.scored?.rules?.length || 0;
    const flow = PanelCore.ruleSetFlowSummary(active || {});
    const legacyExtraCount = flow.groups.required.count + flow.groups.veto.count;
    const secondary = flow.secondary.status === "ACTIVE"
      ? (flow.mode === "simple_all"
        ? `二级 ${flow.secondary.count} 条全部满足`
        : `二级 ${flow.secondary.count} 条中至少满足 ${flow.secondary.minimum} 条`)
      : "无二级";
    byId("activeRuleName").textContent = active?.name || "尚未设置";
    byId("activeRuleSummary").textContent = count
      ? (flow.mode === "legacy_advanced"
        ? `旧版：一级 ${count} 条中至少满足 ${flow.primary.minimum} 条${legacyExtraCount ? ` · 附加条件 ${legacyExtraCount} 条` : ""} · ${secondary}`
        : `一级 ${count} 条全部满足 · ${secondary}`)
      : "请先选择筛选规则";
  }

  function canScreen() {
    return Number(state.dataset?.computable_count || 0) > 0
      && activeRuleSet()?.status === "ACTIVE"
      && !screeningInputsAreUncertain()
      && !screeningConfigurationBusy();
  }

  function renderScreenStatus() {
    renderDataSummary();
    renderRuleSummary();
    const freshness = renderScreeningAction();
    if (state.screeningBusy) return;
    if (state.rulesSavingBusy) {
      setFeedback("screenFeedback", "正在应用规则，完成后即可筛选。");
      return;
    }
    if (dataMutationBusy()) {
      setFeedback("screenFeedback", "正在更新本地股票数据，完成并对齐后即可筛选。");
      return;
    }
    if (screeningInputsAreUncertain()) {
      setFeedback(
        "screenFeedback",
        "数据或规则已保存，但页面状态未刷新；请刷新面板后再筛选。",
        false,
        false,
        true,
      );
      return;
    }
    if (canScreen()) {
      if (freshness.status === "stale") {
        setFeedback(
          "screenFeedback",
          "数据或规则已变化；下方保留旧批次，请重新筛选。",
          false,
          false,
          true,
        );
      } else {
        setFeedback("screenFeedback", "");
      }
    } else if (!Number(state.dataset?.computable_count || 0)) {
      setFeedback("screenFeedback", "还没有可筛选的本地日线，请先管理股票数据。", true);
    } else {
      setFeedback("screenFeedback", "还没有可用规则，请先编辑并应用规则。", true);
    }
  }

  function isCandidate(row) {
    return PanelCore.isCandidate(row);
  }

  function screeningRecordStatus(row) {
    return PanelCore.screeningRecordStatus(row);
  }

  function screeningScopeLabel(meta, summary) {
    const count = Number(summary.universe_count || 0);
    if (!count) return "";
    return meta.universe_source === "a_share_snapshot" || meta.is_full_market
      ? `全市场 ${count} 只`
      : `本地范围 ${count} 只`;
  }

  function ruleName(ruleId) {
    const id = clean(ruleId).split("@")[0];
    return activeScoredRules().find((item) => item.id === id)?.name || id;
  }

  function rowRuleName(row, ruleId) {
    const id = clean(ruleId).split("@")[0];
    const frozen = (row.rule_evidence || []).find(
      (item) => clean(item.rule_id).split("@")[0] === id,
    );
    return frozen?.name || ruleName(id);
  }

  function evidenceMetricLabel(key) {
    const metadata = (state.rules?.editor_contract?.fields || []).find(
      (item) => item.name === key,
    );
    return metadata?.evidence_label || PanelCore.evidenceMetricLabel(key);
  }

  function formatEvidenceValue(value, key = "") {
    return PanelCore.formatEvidenceValue(value, key);
  }

  function evidenceStatus(status) {
    return PanelCore.evidenceStatus(status);
  }

  function evidenceForRow(row) {
    return PanelCore.evidenceForRow(row);
  }

  function compactEvidenceCopy(item) {
    return PanelCore.compactEvidenceCopy(item, ruleName);
  }

  function renderEvidenceItem(item, ranking = null, order = null) {
    const status = evidenceStatus(item.status);
    const observed = Object.entries(item.observed || {}).filter(
      ([, value]) => value !== undefined && value !== null,
    );
    const comparisons = Array.isArray(item.comparisons) ? item.comparisons : [];
    const comparisonLines = comparisons.map((comparison) => {
      const left = formatEvidenceValue(comparison.left);
      const right = formatEvidenceValue(comparison.right);
      const operator = clean(comparison.operator || "?");
      return `${left} ${operator} ${right}`;
    }).filter(Boolean);
    const explanation = clean(item.plain_explanation);
    // 有比较式时，它就是「为什么通过」的完整答案：参数卡片与白话解释都只是同一事实的复述。
    // 没有比较式时（基础准入、日期核对、数据待补），实值只存在于 observed、
    // 唯一可读的原因只存在于 plain_explanation，两者都必须保留，不能删成只剩名称和状态。
    const detail = comparisonLines.length
      ? `<p class="evidence-comparison">${comparisonLines
        .map((line) => `<code>${escapeHtml(line)}</code>`).join("")}</p>`
      : [
        observed.length
          ? `<p class="evidence-observed">${observed.map(([key, value]) => (
            `<span><i>${escapeHtml(evidenceMetricLabel(key))}</i>${escapeHtml(formatEvidenceValue(value, key))}</span>`
          )).join("")}</p>`
          : "",
        explanation || !observed.length
          ? `<p class="evidence-explanation">${escapeHtml(explanation || "本条规则没有可用实值。")}</p>`
          : "",
      ].filter(Boolean).join("");
    // 排序优先级与强度直接标在这条规则上，不再另起一块「排序依据」重复规则名。
    const strength = ranking
      ? (ranking.comparable_strength
        ? `排序强度 ${escapeHtml(ranking.strength_percentile)}%`
        : "无可比强度 · 同分")
      : "";
    return `<article class="rule-evidence-card ${status.className}" role="listitem">
      ${order == null ? "" : `<span class="evidence-order" aria-label="${ranking ? "一级排序优先级" : "序号"} ${escapeHtml(order)}">${escapeHtml(order)}</span>`}
      <strong class="evidence-name">${escapeHtml(item.name || ruleName(item.rule_id))}</strong>
      ${detail}
      ${strength ? `<span class="evidence-strength">${strength}</span>` : ""}
      <span class="evidence-status ${status.className}">${escapeHtml(status.label)}</span>
    </article>`;
  }

  function screeningRowsForDisplay(result) {
    return PanelCore.screeningRowsForDisplay(result);
  }

  function screeningDisplayGroups(result) {
    return PanelCore.screeningDisplayGroups(result);
  }

  function screeningDisplayCounts(summary, rows) {
    return PanelCore.screeningCounts(summary, rows);
  }

  function screeningRuleCounts(row) {
    return PanelCore.screeningRuleCounts(row);
  }

  function screeningRank(row) {
    return PanelCore.screeningRank(row);
  }

  // 一级与二级的规则证据分开呈现，不混在同一串里。
  // 二级的过滤说明直接跟在「二级筛选」后面，不单独占一行。
  function renderEvidenceGroups(evidence, rankingMap) {
    const stageOf = (item) => (
      clean(item.screening_stage) === "secondary" ? "secondary" : "primary"
    );
    const gates = evidence.filter((item) => item.kind === "base_gate");
    const scored = evidence.filter((item) => item.kind !== "base_gate");
    const primary = scored.filter((item) => stageOf(item) === "primary");
    const secondary = scored.filter((item) => stageOf(item) === "secondary");
    // 两级都编号：一级用排序优先级（有业务含义），二级用展示顺序（不参与排名）。
    const render = (items, numbered = true) => items
      .map((item, index) => {
        const ranking = rankingMap.get(clean(item.rule_id)) || null;
        if (!numbered) return renderEvidenceItem(item, ranking, null);
        return renderEvidenceItem(item, ranking, ranking ? ranking.priority : index + 1);
      })
      .join("");
    // 没有二级证据时不加分组标题，避免单独一个「一级筛选」标题变成噪音。
    if (!secondary.length) return render(gates, false) + render(scored);
    return [
      render(gates, false),
      `<p class="evidence-group-label">一级筛选</p>`,
      render(primary),
      `<p class="evidence-group-label">二级筛选<small>只负责过滤，不计入排序分</small></p>`,
      render(secondary),
    ].join("");
  }

  function renderRankingEvidence(row) {
    const ranking = row.ranking_evidence || {};
    const secondaryItems = Array.isArray(ranking.secondary) ? ranking.secondary : [];
    const ruleCounts = screeningRuleCounts(row);
    if (!ruleCounts.secondaryTotal) return "";
    return `<section class="ranking-evidence" aria-label="二级筛选依据">
      <div class="ranking-stage secondary-filter-stage">
        <strong>二级</strong>
        <div>
          <span>${secondaryItems.length ? secondaryItems.map((item) => escapeHtml(item.name || item.rule_ref || "规则")).join("、") : `${ruleCounts.secondaryMatched} / ${ruleCounts.secondaryTotal} 条命中`}</span>
          <small>只负责过滤，不计入排序分</small>
        </div>
      </div>
    </section>`;
  }

  // 一级排序信息按 rule_ref 关联到对应的规则证据
  function rankingByRuleId(row) {
    const primary = Array.isArray(row.ranking_evidence?.primary)
      ? row.ranking_evidence.primary
      : [];
    const map = new Map();
    primary.forEach((item) => {
      const key = clean(item.rule_ref).split("@")[0];
      if (key) map.set(key, item);
    });
    return map;
  }

  function renderScreeningCard(row, freshness) {
    const passed = (row.passed_candidate_rules || [])
      .slice(0, 3)
      .map((ruleId) => rowRuleName(row, ruleId));
    const recordStatus = screeningRecordStatus(row);
    const ruleCounts = screeningRuleCounts(row);
    const rank = screeningRank(row);
    const evidence = evidenceForRow(row);
    const rankingMap = rankingByRuleId(row);
    const primaryEvidence = evidence.find((item) => item.kind !== "base_gate") || evidence[0];
    const evidenceCount = evidence.length;
    const resultMeta = state.screening?.meta || {};
    const batchDate = clean(resultMeta.as_of_trade_date || resultMeta.snapshot_date);
    const rowDate = clean(row.history_date || row.data_date);
    const exceptionalDate = rowDate && (!batchDate || rowDate !== batchDate)
      ? ` · 数据 ${rowDate}`
      : "";
    const secondaryCopy = {
      PASS: `二级 ${ruleCounts.secondaryMatched}/${ruleCounts.secondaryTotal}`,
      FAIL: `二级 ${ruleCounts.secondaryMatched}/${ruleCounts.secondaryTotal} 未通过`,
      DATA_GAP: "二级数据待补",
      ERROR: "二级计算异常",
      INACTIVE: "二级未启用",
      SKIPPED: "二级未执行",
    }[ruleCounts.secondaryStatus] || "二级未执行";
    const scoreMarkup = rank.score == null
      ? `<span>收盘价</span><strong>${row.latest_price == null ? "—" : Number(row.latest_price).toFixed(2)}</strong>`
      : `<span>排序分</span><strong>${rank.score.toFixed(1)}</strong>`;
    return `<article class="candidate-card screening-record ${recordStatus.className}" role="listitem">
      <div class="screening-record-main">
        <div class="candidate-stock">
          <div>
            <strong>${escapeHtml(row.stock_name || row.symbol)}</strong>
            <small>${escapeHtml(row.symbol)}${row.latest_price == null ? "" : ` · 收盘 ${Number(row.latest_price).toFixed(2)}`}${escapeHtml(exceptionalDate)}</small>
          </div>
          ${rank.position ? `<span class="rank-badge" aria-label="排名第 ${rank.position}">#${rank.position}</span>` : ""}
        </div>
        <div class="candidate-match">
          <strong>一级 ${ruleCounts.primaryMatched} / ${ruleCounts.primaryTotal} · ${escapeHtml(secondaryCopy)}</strong>
          <small>${escapeHtml(compactEvidenceCopy(primaryEvidence) || passed[0] || row.technical_reason || row.reason || "按当前规则完成判断")}</small>
        </div>
        <div class="candidate-price candidate-ranking">${scoreMarkup}</div>
        <span class="screening-record-status ${recordStatus.className}">${escapeHtml(recordStatus.label)}</span>
        ${isCandidate(row) ? `<button class="button button-secondary" type="button" data-record-symbol="${escapeHtml(row.symbol)}" ${freshness.status === "current" ? "" : "hidden disabled"}>记录操作</button>` : ""}
      </div>
      <details class="record-evidence-details">
        <summary>
          <span class="evidence-summary-copy">
            <strong>查看规则证据</strong>
          </span>
          <span class="evidence-summary-action">${evidenceCount} 条</span>
        </summary>
        <div class="rule-evidence-list" role="list" aria-label="${escapeHtml(row.stock_name || row.symbol)}的规则实值">
          ${evidence.length ? renderEvidenceGroups(evidence, rankingMap) : `<p class="evidence-no-value">本条记录没有可显示的规则证据。</p>`}
        </div>
      </details>
    </article>`;
  }

  function renderCollapsedScreeningGroup(label, rows, freshness, className) {
    if (!rows.length) return "";
    return `<details class="result-overflow ${className}">
      <summary><span>${escapeHtml(label)}</span><strong>${rows.length} 只</strong></summary>
      <div class="candidate-list nested-candidate-list" role="list">
        ${rows.map((row) => renderScreeningCard(row, freshness)).join("")}
      </div>
    </details>`;
  }

  function renderScreening() {
    const result = state.screening;
    const list = byId("candidateList");
    const freshness = renderScreeningAction();
    if (!result) {
      byId("candidateCount").textContent = "0";
      byId("screenRankingNote").hidden = true;
      byId("screenResultStats").textContent = "";
      byId("screenResultStats").hidden = true;
      byId("screenResultMeta").textContent = "尚未运行筛选";
      list.innerHTML = `<div class="empty-state">
        <span aria-hidden="true">筛</span>
        <strong>还没有筛选记录</strong>
        <p>确认数据和规则后，点击上方按钮即可。</p>
      </div>`;
      return;
    }

    const groups = screeningDisplayGroups(result);
    const rows = groups.rows;
    const meta = result.meta || {};
    const summary = result.summary || {};
    const counts = screeningDisplayCounts(summary, rows);
    const scopeLabel = screeningScopeLabel(meta, summary);
    byId("screenRankingNote").hidden = counts.candidates < 1;
    byId("candidateCount").textContent = counts.candidates > groups.topCandidates.length
      ? `${groups.topCandidates.length} / ${counts.candidates}`
      : String(counts.candidates);
    const primaryGapCount = Math.max(0, counts.gaps - counts.secondaryGaps);
    const diagnosticStats = [
      counts.secondaryFiltered ? `二级未通过 ${counts.secondaryFiltered}` : "",
      counts.secondaryGaps ? `二级待补 ${counts.secondaryGaps}` : "",
      counts.near ? `接近 ${counts.near}` : "",
      counts.notSelected ? `未入选 ${counts.notSelected}` : "",
      primaryGapCount ? `待补 ${primaryGapCount}` : "",
    ].filter(Boolean).join(" · ");
    byId("screenResultStats").textContent = diagnosticStats;
    byId("screenResultStats").hidden = !diagnosticStats;
    byId("screenResultMeta").textContent = [
      meta.as_of_trade_date || meta.snapshot_date || "—",
      scopeLabel,
      meta.rule_set_name || "当前规则",
      formatDateTime(meta.created_at),
    ].filter(Boolean).join(" · ");

    if (!rows.length) {
      list.innerHTML = `<div class="empty-state">
        <span aria-hidden="true">0</span>
        <strong>本次没有可显示的逐股记录</strong>
        <p>筛选批次已经完成；这是有效结果，面板不会自动降低规则。</p>
      </div>`;
      return;
    }

    const topMarkup = groups.topCandidates.length
      ? groups.topCandidates.map((row) => renderScreeningCard(row, freshness)).join("")
      : `<div class="empty-state compact-result-state">
          <span aria-hidden="true">0</span>
          <strong>本次没有最终候选</strong>
          <p>这是有效结果；可展开其他诊断查看一级候选和阻断原因。</p>
        </div>`;
    list.innerHTML = [
      topMarkup,
      renderCollapsedScreeningGroup("其他候选", groups.otherCandidates, freshness, "other-candidates"),
      renderCollapsedScreeningGroup("其他逐股诊断", groups.diagnostics, freshness, "other-diagnostics"),
    ].filter(Boolean).join("");
  }

  function revealScreeningResult() {
    const result = byId("screenResult");
    const heading = byId("candidateHeading");
    result.scrollIntoView({
      block: "start",
      behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches
        ? "auto"
        : "smooth",
    });
    heading.focus({ preventScroll: true });
  }

  async function loadExistingScreening(runId) {
    state.screening = await requestJson(
      `${API.screenings}/${encodeURIComponent(runId)}`,
    );
    renderScreening();
    revealScreeningResult();
  }

  async function runScreening() {
    if (state.screeningBusy || !canScreen()) return;
    const button = byId("runScreeningButton");
    state.screeningBusy = true;
    byId("candidateList").setAttribute("aria-busy", "true");
    setBusy(button, true, "正在检查数据与规则…");
    renderScreeningAction();
    renderDataSummary();
    renderRuleSetWorkspace({ announce: false, preserveInputs: true });
    setFeedback("screenFeedback", "正在检查数据日期和当前规则，请稍候。");
    let preflightWarnings = [];
    try {
      const preflight = await requestJson(API.screeningPreflight, {
        method: "POST",
        body: JSON.stringify({
          market_context: "unknown",
          universe_scope: "local_library",
        }),
      });
      preflightWarnings = Array.isArray(preflight.warnings)
        ? preflight.warnings.filter(Boolean)
        : [];
      if (!preflight.can_run || preflight.blocking_errors?.length) {
        throw new Error(preflight.blocking_errors?.[0] || "当前数据不能运行筛选。");
      }
      if (preflight.duplicate_run?.exists && preflight.duplicate_run.run_id) {
        await loadExistingScreening(preflight.duplicate_run.run_id);
        setFeedback(
          "screenFeedback",
          preflightWarnings.length
            ? `已复用相同数据与规则的批次。${preflightWarnings.join(" ")}`
            : "",
          false,
          false,
          Boolean(preflightWarnings.length),
        );
        showToast("已显示相同数据与规则的最近结果。");
        return;
      }
      setBusy(button, true, "正在筛选候选股…");
      state.screening = await requestJson(API.screeningRun, {
        method: "POST",
        body: JSON.stringify({ preflight_id: preflight.preflight_id }),
      });
      renderScreening();
      revealScreeningResult();
      const count = Number(state.screening.summary?.candidate_count || 0);
      setFeedback(
        "screenFeedback",
        preflightWarnings.length ? preflightWarnings.join(" ") : "",
        false,
        false,
        Boolean(preflightWarnings.length),
      );
      showToast(count ? `筛选完成：${count} 只候选股。` : "筛选完成：本次没有候选股。");
    } catch (error) {
      if (error.code === "DUPLICATE_RUN" && error.duplicateRunId) {
        try {
          await loadExistingScreening(error.duplicateRunId);
          setFeedback(
            "screenFeedback",
            "",
          );
        } catch (recoveryError) {
          setFeedback("screenFeedback", `历史结果读取失败：${recoveryError.message}`, true);
          showToast(recoveryError.message, true);
        }
      } else {
        setFeedback("screenFeedback", error.message, true);
        showToast(error.message, true);
      }
    } finally {
      state.screeningBusy = false;
      byId("candidateList").setAttribute("aria-busy", "false");
      setBusy(button, false);
      renderScreeningAction();
      renderDataSummary();
      renderRuleSetWorkspace({ announce: false, preserveInputs: true });
    }
  }


  function bindScreeningEvents() {
    byId("runScreeningButton").addEventListener("click", runScreening);
  }

export {
  bindScreeningEvents,
  renderScreening,
  renderScreeningAction,
  renderScreenStatus,
  screeningFreshness,
};
