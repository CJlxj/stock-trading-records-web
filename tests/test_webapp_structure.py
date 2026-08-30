from __future__ import annotations

import re
import unittest
from html.parser import HTMLParser
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
STATIC_ROOT = PROJECT_ROOT / "webapp" / "static"
WEB_JAVASCRIPT_PATHS = (
    STATIC_ROOT / "platform.js",
    STATIC_ROOT / "modules" / "data.js",
    STATIC_ROOT / "modules" / "screening.js",
    STATIC_ROOT / "modules" / "rules.js",
    STATIC_ROOT / "modules" / "records.js",
    STATIC_ROOT / "app.js",
)


class MarkupCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: set[str] = set()
        self.id_counts: dict[str, int] = {}
        self.tags: list[tuple[str, dict[str, str | None]]] = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        self.tags.append((tag, attributes))
        identifier = attributes.get("id")
        if identifier:
            self.ids.add(identifier)
            self.id_counts[identifier] = self.id_counts.get(identifier, 0) + 1


class WebappStructureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (STATIC_ROOT / "index.html").read_text(encoding="utf-8")
        cls.web_sources = {
            path.relative_to(STATIC_ROOT).as_posix(): path.read_text(encoding="utf-8")
            for path in WEB_JAVASCRIPT_PATHS
        }
        cls.javascript = "\n".join(cls.web_sources.values())
        cls.styles = (STATIC_ROOT / "styles.css").read_text(encoding="utf-8")
        cls.shared = (PROJECT_ROOT / "shared_ui" / "panel-core.js").read_text(
            encoding="utf-8"
        )
        cls.server = (PROJECT_ROOT / "webapp" / "server.py").read_text(encoding="utf-8")
        cls.markup = MarkupCollector()
        cls.markup.feed(cls.html)

    def test_static_assets_ids_and_javascript_references_are_valid(self):
        for path in (
            STATIC_ROOT / "index.html",
            STATIC_ROOT / "styles.css",
            *WEB_JAVASCRIPT_PATHS,
        ):
            self.assertTrue(path.is_file(), path.name)
        referenced_ids = set(
            re.findall(r'byId\("([A-Za-z][A-Za-z0-9_-]*)"\)', self.javascript)
        )
        self.assertFalse(
            referenced_ids - self.markup.ids,
            sorted(referenced_ids - self.markup.ids),
        )
        duplicates = sorted(
            identifier
            for identifier, count in self.markup.id_counts.items()
            if count > 1
        )
        self.assertFalse(duplicates, f"duplicate HTML ids: {duplicates}")

    def test_web_code_is_split_into_platform_and_four_feature_modules(self):
        entry = self.web_sources["app.js"]
        platform = self.web_sources["platform.js"]
        feature_binders = {
            "data.js": "bindDataEvents",
            "rules.js": "bindRuleEvents",
            "screening.js": "bindScreeningEvents",
            "records.js": "bindRecordEvents",
        }

        self.assertIn('<script type="module" src="/app.js?v=1.0.1"></script>', self.html)
        self.assertIn('from "./platform.js"', entry)
        self.assertNotIn('from "./modules/', platform)
        for module_name, binder in feature_binders.items():
            source = self.web_sources[f"modules/{module_name}"]
            self.assertIn(f'from "./modules/{module_name}"', entry)
            self.assertIn(f"function {binder}()", source)
            self.assertIn(f"{binder}();", entry)

        for domain_function in (
            "renderDataSummary",
            "renderRuleSetWorkspace",
            "renderScreening",
            "renderRecordsSurface",
        ):
            self.assertNotIn(f"function {domain_function}", entry)

    def test_ui_has_three_pages_and_exactly_four_core_tasks(self):
        nav_buttons = [
            attrs
            for tag, attrs in self.markup.tags
            if tag == "button" and "nav-button" in (attrs.get("class") or "")
        ]
        view_panels = [
            attrs
            for _tag, attrs in self.markup.tags
            if attrs.get("data-view-panel") is not None
        ]
        self.assertEqual(3, len(nav_buttons))
        self.assertEqual(["screen", "rules", "records"], [
            attrs["data-view"] for attrs in nav_buttons
        ])
        self.assertEqual(3, len(view_panels))
        for label in ["股票数据", "规则组合", "按当前规则筛选", "操作记录"]:
            self.assertIn(label, self.html)
        self.assertIn('data-app-version="web-v1.0.1"', self.html)
        self.assertIn('href="/styles.css?v=1.0.1"', self.html)
        self.assertIn('src="/shared/panel-core.js?v=1.0.1"', self.html)
        self.assertIn('type="module" src="/app.js?v=1.0.1"', self.html)
        self.assertIn("WEB V1.0.1 · 2026-08-27", self.html)

    def test_day_and_night_themes_share_one_semantic_token_layer(self):
        # 主题必须由 token 层驱动：跟随系统 + <html data-theme> 手动覆盖。
        self.assertIn("color-scheme: light dark;", self.styles)
        self.assertIn(':root[data-theme="light"]', self.styles)
        self.assertIn(':root[data-theme="dark"]', self.styles)
        self.assertIn('<meta name="color-scheme" content="light dark">', self.html)

        # 每个语义 token 用 light-dark() 一行给出日/夜两套取值，不重复声明整套色板。
        token_block, _, rest = self.styles.partition(
            ':root[data-theme="dark"] {\n  color-scheme: dark;\n}\n'
        )
        self.assertGreaterEqual(token_block.count("light-dark("), 30)

        # token 块之后不允许再出现裸色值，否则日间模式会漏色。
        leftover = re.findall(r"#[0-9a-fA-F]{3,8}|rgba?\([^)]*\)", rest)
        self.assertEqual([], leftover, f"styles.css 仍有硬编码颜色: {sorted(set(leftover))}")

        # CSP 是 script-src 'self'，引导脚本必须是独立文件并被服务端登记。
        self.assertIn('src="/theme.js?v=1.0.1"', self.html)
        self.assertIn('"/theme.js": ("theme.js"', self.server)
        self.assertIn('id="themeToggleButton"', self.html)
        self.assertIn("function cycleTheme()", self.javascript)
        self.assertIn("function initializeTheme()", self.javascript)
        self.assertIn('THEME_STORAGE_KEY = "stockRules.web.v1.theme"', self.javascript)

    def test_removed_complex_workflows_do_not_return_to_daily_ui(self):
        combined = self.html + self.javascript
        for removed in [
            "纪律复盘",
            "股票档案",
            "单股审查",
            "审查草稿",
            "规则工作台",
            "复制当前方案",
            "审计 JSON",
            "基本面抓取",
            "/api/review-cases",
            "/api/review-drafts",
            'requestJson("/api/review"',
        ]:
            self.assertNotIn(removed, combined)
        self.assertNotIn("<canvas", self.html)
        self.assertNotRegex(self.javascript, r'fetch\(\s*["\']https?://')

    def test_interface_complexity_has_a_hard_upper_bound(self):
        button_count = sum(1 for tag, _attrs in self.markup.tags if tag == "button")
        dialog_count = sum(1 for tag, _attrs in self.markup.tags if tag == "dialog")
        input_count = sum(1 for tag, _attrs in self.markup.tags if tag == "input")
        # 当前模块化界面的结构预算：
        # 规则页四态：两个子入口及容器(3) + 三个主视图容器(3)
        #   + 编辑器返回与面包屑(2) + 选择器标题/计数/取消/确认/面包屑(5)
        #   - 已移除的规则库内一级/二级切换及其标签(3)
        # 记录页主从：首页与编辑子页容器(2) + 新增记录(1) + 编辑器返回(1)
        #   + 草稿提示/摘要/继续/放弃(4) + 编辑器标题与草稿状态(2)
        # 事实台账：可选成交时间(1) + 按股票汇总的标题/计数/列表(3) + 可选本笔费用合计(1)
        self.assertLessEqual(len(self.markup.ids), 165)
        # 26 + 两个子入口页签 + 编辑器返回 + 选择器取消 + 选择器确认 - 移除的两个级别切换
        # +2：共用确认弹窗的取消与确定，替掉了浏览器原生 confirm/prompt
        self.assertLessEqual(button_count, 36)
        # 数据、规则定义，加一个所有确认与改名共用的居中确认弹窗
        self.assertLessEqual(dialog_count, 3)
        self.assertLessEqual(input_count, 30)

    def test_each_page_has_one_clear_primary_action(self):
        self.assertEqual(1, self.html.count('id="runScreeningButton"'))
        self.assertEqual(1, self.html.count('id="saveRulesButton"'))
        self.assertEqual(1, self.html.count('id="saveTradeButton"'))
        self.assertIn("按当前规则筛选", self.html)
        self.assertIn(">保存组合</button>", self.html)
        self.assertIn("PanelCore.ruleSetActivationState", self.javascript)
        self.assertIn("activation.actionLabel", self.javascript)
        self.assertIn(">保存操作记录</button>", self.html)

    def test_ranked_results_and_simple_rule_workspace_are_progressively_disclosed(self):
        for identifier in (
            "ruleSetList",
            "newRuleSetButton",
            "ruleEditorForm",
            "ruleSetName",
            "ruleList",
            "ruleSearchInput",
            "ruleLibraryGroups",
            "ruleLibraryPanel",
            "addRuleTagButton",
            "importDefaultRulesButton",
            "ruleTag",
            "customRuleFeedback",
            "secondaryRuleList",
            "saveRulesButton",
            "resetRulesButton",
            "rulesSurfaceTabs",
            "setsSurfaceTab",
            "librarySurfaceTab",
            "ruleSetsView",
            "ruleEditorView",
            "ruleLibraryView",
            "ruleEditorBackButton",
            "rulePickerTitle",
            "rulePickerCancelButton",
            "rulePickerConfirmButton",
        ):
            self.assertIn(f'id="{identifier}"', self.html)
        # 规则库内的一级/二级切换被入口决定目标取代
        for removed in ("ruleStageTabs", "ruleLibraryTarget", "ruleLibraryTargetLabel"):
            self.assertNotIn(f'id="{removed}"', self.html)
        for identifier in (
            "secondaryMinimumRuleCount",
            "minimumRuleCount",
            "ruleWorkspaceStatus",
            "activateRuleSetButton",
            "goScreeningButton",
            "ruleSetLifecycleActions",
        ):
            self.assertNotIn(f'id="{identifier}"', self.html)
        for token in (
            "function renderCollapsedScreeningGroup(",
            "groups.topCandidates",
            'renderCollapsedScreeningGroup("其他候选"',
            'renderCollapsedScreeningGroup("其他逐股诊断"',
            # 一级排序信息已折进各自的规则证据行，不再单列一块重复规则名
            "二级筛选依据",
            "排序强度",
            "只负责过滤，不计入排序分",
            "不代表上涨概率",
            "function renderRuleSetWorkspace(",
            "function moveWorkspaceRule(",
            "PanelCore.ruleTagGroups",
            "PanelCore.ruleSetFlowSummary",
            'editor_mode: "simple_all"',
            "secondary_rules: secondarySelected.map((ref) => ({ ref }))",
            "未添加二级规则，一级通过后直接进入排序",
        ):
            self.assertIn(token, self.javascript + self.html)
        self.assertNotIn("ruleWorkflowSteps", self.javascript + self.html)
        self.assertNotIn("ruleExecutionStages", self.javascript + self.html)
        self.assertNotIn("data-select-rule-set", self.javascript)
        self.assertIn("data-activate-rule-set", self.javascript)
        self.assertIn("data-edit-rule-set", self.javascript)
        self.assertIn("data-delete-rule-set", self.javascript)
        self.assertIn(".result-overflow", self.styles)
        self.assertIn(".rank-badge", self.styles)

    def test_stock_data_entry_supports_full_catalog_search_and_local_library_import(self):
        for identifier in [
            "dataDialog",
            "updateAllDataButton",
            "syncStockCatalogButton",
            "dialogDataCounts",
            "dialogCurrentDate",
            "dialogTargetDate",
            "dialogActualDate",
            "dialogAlignmentStatus",
            "dataStockSearch",
            "dataCatalogMeta",
            "dataStockList",
            "dataStockListTitle",
            "dataStockListCount",
            "dataStockListMeta",
            "marketCsvImport",
            "marketCsvImportForm",
            "marketCsvSymbol",
            "marketCsvFile",
            "marketCsvVolumeUnit",
            "marketCsvAdjust",
            "marketCsvFeedback",
            "importMarketCsvButton",
        ]:
            self.assertIn(f'id="{identifier}"', self.html)
        self.assertNotIn('class="data-overview"', self.html)
        self.assertNotIn(".data-overview", self.styles)
        summary_flow = self.javascript.split(
            "function renderDataSummary()", 1
        )[1].split("function dataEntries()", 1)[0]
        self.assertIn('...(unaligned > 0 ? [`未对齐 ${unaligned} 只`] : [])', summary_flow)
        self.assertNotIn('id="scanDataButton"', self.html)
        self.assertNotIn('id="dataSymbol"', self.html)
        self.assertNotIn('id="updateDataButton"', self.html)
        update_flow = self.javascript.split(
            "async function updateAllMarketData", 1
        )[1].split("async function saveTrade", 1)[0]
        self.assertIn("for (let index = 0; index < entries.length; index += 1)", update_flow)
        self.assertLess(
            update_flow.find("requestJson(API.dataUpdate"),
            update_flow.find("requestJson(API.dataScan"),
        )
        self.assertIn('timeframe: "day"', update_flow)
        self.assertIn("failures.push({ symbol, message: error.message })", update_flow)

        sync_flow = self.javascript.split(
            "async function syncFullStockCatalog", 1
        )[1].split("async function addCatalogStock", 1)[0]
        self.assertIn("requestJson(API.catalogSync", sync_flow)
        self.assertIn("state.catalog = result", sync_flow)
        self.assertIn("这不会自动加入或下载全部股票", sync_flow)

        add_flow = self.javascript.split(
            "async function addCatalogStock", 1
        )[1].split("async function updateAllMarketData", 1)[0]
        self.assertIn("requestJson(API.stocks", add_flow)
        self.assertIn("JSON.stringify({ symbol })", add_flow)
        self.assertIn('result.import_status === "FAILED"', add_flow)
        self.assertIn("已加入本地库", add_flow)

        update_stock_flow = self.javascript.split(
            "async function updateCatalogStock", 1
        )[1].split("async function updateAllMarketData", 1)[0]
        self.assertIn("requestJson(API.dataUpdate", update_stock_flow)
        self.assertIn("数据已写入，但页面清单待同步", update_stock_flow)

        self.assertIn("function renderDataStockList()", self.javascript)
        self.assertIn("function renderLocalDataStockList()", self.javascript)
        self.assertIn("function renderCatalogSearchResults()", self.javascript)
        self.assertIn("function scheduleCatalogSearch()", self.javascript)
        self.assertIn("async function importManualMarketData(", self.javascript)
        self.assertIn("requestJson(API.dataImport", self.javascript)
        self.assertIn("arrayBufferToBase64(await file.arrayBuffer())", self.javascript)
        self.assertIn("result.normalization?.exact_duplicates_removed", self.javascript)
        self.assertIn(
            '`${API.catalogSearch}?q=${encodeURIComponent(query)}&limit=20`',
            self.javascript,
        )
        self.assertIn("serial !== state.catalogSearchSerial", self.javascript)
        self.assertIn("catalogSearchTimer = window.setTimeout(", self.javascript)
        self.assertIn("() => searchCatalog(query, serial)", self.javascript)
        self.assertIn(
            'byId("dataStockSearch").addEventListener("input", scheduleCatalogSearch)',
            self.javascript,
        )
        self.assertIn('event.target.closest("[data-add-stock]")', self.javascript)
        self.assertIn('event.target.closest("[data-update-stock]")', self.javascript)
        for label in [
            "管理本地股票库",
            "本地股票库",
            "已对齐",
            "未对齐",
            "一键更新全部股票",
            "更新完整股票目录",
            "搜索或新增股票",
            "加入并导入",
            "手动导入 CSV",
            "校验并导入",
        ]:
            self.assertIn(label, self.html + self.javascript + self.shared)

    def test_trade_symbol_picker_can_switch_without_clearing_current_code(self):
        for identifier in [
            "tradeSymbolPicker",
            "tradeSymbol",
            "tradeSymbolToggle",
            "tradeSymbolOptions",
        ]:
            self.assertIn(f'id="{identifier}"', self.html)
        trade_symbol = next(
            (tag, attrs)
            for tag, attrs in self.markup.tags
            if attrs.get("id") == "tradeSymbol"
        )
        self.assertEqual("input", trade_symbol[0])
        self.assertEqual("combobox", trade_symbol[1].get("role"))
        self.assertEqual("list", trade_symbol[1].get("aria-autocomplete"))
        self.assertEqual("tradeSymbolOptions", trade_symbol[1].get("aria-controls"))
        self.assertIsNone(trade_symbol[1].get("list"))
        self.assertNotIn("<datalist", self.html)
        self.assertIn('const query = showAll ? ""', self.javascript)
        self.assertIn("openTradeSymbolPicker({ showAll: true })", self.javascript)
        self.assertIn("openTradeSymbolPicker({ showAll: false })", self.javascript)
        self.assertIn("function shouldShowAllTradeSymbols()", self.javascript)
        self.assertIn("value === fullSymbol || value === fullSymbol.slice(0, 6)", self.javascript)
        self.assertIn("clean(item.symbol).toLowerCase().includes(query)", self.javascript)
        self.assertIn("clean(item.stock_name).toLowerCase().includes(query)", self.javascript)
        self.assertIn('event.target.closest("[data-trade-symbol]")', self.javascript)
        self.assertIn('event.key === "ArrowDown"', self.javascript)
        self.assertIn('event.key === "Escape"', self.javascript)
        choose_flow = self.javascript.split(
            "function chooseTradeSymbol(symbol)", 1
        )[1].split("function moveTradeSymbolSelection", 1)[0]
        self.assertLess(
            choose_flow.find('byId("tradeSymbol").value = symbol'),
            choose_flow.find("closeTradeSymbolPicker()"),
        )
        self.assertIn('byId("tradeSymbol").focus()', choose_flow)
        bind_flow = self.javascript.split(
            "function bindRecordEvents()", 1
        )[1].split("\n  }\n\nexport {", 1)[0]
        focus_flow = bind_flow.split(
            'byId("tradeSymbol").addEventListener("focus"', 1
        )[1].split('byId("tradeSymbol").addEventListener("click"', 1)[0]
        click_flow = bind_flow.split(
            'byId("tradeSymbol").addEventListener("click"', 1
        )[1].split('byId("tradeSymbol").addEventListener("input"', 1)[0]
        input_flow = bind_flow.split(
            'byId("tradeSymbol").addEventListener("input"', 1
        )[1].split('byId("tradeSymbol").addEventListener("keydown"', 1)[0]
        option_click_flow = bind_flow.split(
            'byId("tradeSymbolOptions").addEventListener("click"', 1
        )[1].split('document.addEventListener("pointerdown"', 1)[0]
        self.assertIn('byId("tradeSymbolOptions").hidden', focus_flow)
        self.assertIn("openTradeSymbolPicker({ showAll: shouldShowAllTradeSymbols() })", focus_flow)
        self.assertIn('byId("tradeSymbolOptions").hidden', click_flow)
        self.assertIn("openTradeSymbolPicker({ showAll: shouldShowAllTradeSymbols() })", click_flow)
        self.assertIn("openTradeSymbolPicker({ showAll: false })", input_flow)
        self.assertIn("chooseTradeSymbol(option.dataset.tradeSymbol)", option_click_flow)
        enter_flow = self.javascript.split(
            'if (event.key === "Enter"', 1
        )[1].split('if (event.key === "Escape")', 1)[0]
        self.assertIn("event.preventDefault()", enter_flow)
        self.assertIn("options.length === 1", enter_flow)
        self.assertIn("setActiveTradeSymbolOption(0", enter_flow)

    def test_dialog_backdrops_do_not_discard_the_current_task(self):
        data_bind_flow = self.javascript.split(
            "function bindDataEvents()", 1
        )[1].split("\n  }\n\nexport {", 1)[0]
        rule_bind_flow = self.javascript.split(
            "function bindRuleEvents()", 1
        )[1].split("\n  }\n\nexport {", 1)[0]
        self.assertNotIn(
            'byId("dataDialog").addEventListener("click"',
            data_bind_flow,
        )
        self.assertNotIn(
            'byId("ruleDialog").addEventListener("click"',
            rule_bind_flow,
        )
        self.assertNotIn('event.target === byId("dataDialog")', data_bind_flow)
        self.assertNotIn('event.target === byId("ruleDialog")', rule_bind_flow)
        self.assertIn(
            'byId("closeDataDialogButton").addEventListener("click", closeDataDialog)',
            data_bind_flow,
        )
        self.assertIn(
            'byId("closeRuleDialogButton").addEventListener("click", () => void closeRuleDialog())',
            rule_bind_flow,
        )

    def test_rule_editor_can_manage_combinations_and_custom_rules(self):
        for identifier in [
            "ruleSetList",
            "newRuleSetButton",
            "ruleSetName",
            "ruleList",
            "cancelRuleEditButton",
            "ruleLibraryGroups",
            "resetRulesButton",
            "secondaryRuleList",
            "saveRulesButton",
            "addRuleButton",
            "ruleDialog",
            "ruleName",
            "ruleExpression",
            "ruleDescription",
            "confirmRuleDefinitionButton",
        ]:
            self.assertIn(f'id="{identifier}"', self.html)
        self.assertIn("API.ruleSets", self.javascript)
        self.assertIn("expected_rule_set_hash", self.javascript)
        self.assertIn("expected_active_rule_set_hash", self.javascript)
        self.assertIn("function openRuleDialog(", self.javascript)
        self.assertIn("function saveWorkspaceRuleDefinition(", self.javascript)
        self.assertIn("function saveWorkspaceRuleSet(", self.javascript)
        self.assertIn("data-edit-library-rule=", self.javascript)
        self.assertIn("data-delete-library-rule=", self.javascript)
        # 组合只提交引用；建规则一律走规则库。
        self.assertIn("rules: selected.map((ref) => ({ ref }))", self.javascript)
        self.assertNotIn("eval(", self.javascript)
        self.assertNotIn("new Function", self.javascript)
        self.assertIn('`${API.ruleSets}/${encodeURIComponent', self.javascript)
        self.assertNotIn("历史组合 · 只读", self.javascript)
        self.assertNotIn("复制为可编辑组合", self.javascript)
        self.assertNotIn('id="copyAdvancedRuleSetButton"', self.html + self.javascript)
        self.assertIn('<details class="legacy-extra-conditions">', self.javascript)
        self.assertIn(".legacy-extra-conditions", self.styles)
        self.assertNotIn('id="ruleWorkflowSteps"', self.html)
        self.assertNotIn('id="ruleExecutionStages"', self.html)
        self.assertIn('id="primaryRuleCount"', self.html)
        self.assertIn('id="secondaryRuleCount"', self.html)
        self.assertLess(self.html.index('id="ruleList"'), self.html.index('id="ruleLibraryHeading"'))
        self.assertLess(
            self.html.index('id="ruleLibraryHeading"'),
            self.html.index('id="ruleSearchInput"'),
        )
        # 目标级别改由「从规则库选择」入口决定，选择器内部不再切换级别
        self.assertNotIn('class="rule-library-target"', self.html)
        self.assertIn('id="rulePickerCancelButton"', self.html)
        self.assertIn('id="rulePickerConfirmButton"', self.html)
        self.assertNotIn("function workspaceIsArchived()", self.javascript)
        self.assertIn("function workspaceIsReadOnly()", self.javascript)

        self.assertNotIn("该组合已归档", self.javascript)
        self.assertLess(self.html.index('id="ruleFeedback"'), self.html.index('id="ruleLibraryHeading"'))
        self.assertLess(self.html.index('</form>', self.html.index('id="ruleEditorForm"')), self.html.index('id="ruleLibraryPanel"'))
        for removed_summary in ("ruleExecutionNotice", "editingRuleSetName", "editingRuleSetSummary"):
            self.assertNotIn(f'id="{removed_summary}"', self.html)
            self.assertNotIn(f'byId("{removed_summary}")', self.javascript)
        self.assertIn('data-add-library-rule="${escapeHtml(ref)}"', self.javascript)
        # 选择器用原生复选框；已在其他级别的同源规则禁选并说明原因
        self.assertIn('aria-label="${usedStage ? `已在${usedLabel}筛选，不能重复选择`', self.javascript)
        self.assertIn('`选择加入${targetLabel}筛选`', self.javascript)
        self.assertIn('type="checkbox" data-add-library-rule=', self.javascript)
        self.assertIn("function workspaceRuleSourceStage(token)", self.javascript)
        # 「从规则库选择」属于编辑器，可见性不能取决于是否处于选择器
        self.assertIn('button.hidden = !canPickRules;', self.javascript)
        self.assertIn("const canPickRules = state.ruleEditorOpen", self.javascript)
        self.assertIn('openRulePicker(button.dataset.stageAdd === "secondary"', self.javascript)
        self.assertIn("function workspaceLibraryCanSelect()", self.javascript)
        # 四态：可选性由是否处于 RULE_PICKER 决定，不再有常驻加入目标区
        self.assertIn('return rulesSurface() === "picker"', self.javascript)
        self.assertNotIn('.closest(".rule-library").hidden = readOnly', self.javascript)
        self.assertNotIn('byId("addRuleButton").hidden = readOnly', self.javascript)
        self.assertIn("async function saveLibraryRule()", self.javascript)
        self.assertIn("requestJson(API.customRuleCreate", self.javascript)
        # 编辑就是「同一条规则的新版本」：base_ref 指向当前版本，旧版本不动。
        self.assertIn("request_id: requestId,", self.javascript)
        self.assertIn("base_ref: state.ruleEditingRef", self.javascript)
        self.assertIn('state.rules = await requestJson(API.rules)', self.javascript)
        self.assertIn("openRuleDialog()", self.javascript)
        # 分类标签独立于规则定义：增删改标签都只写标签表。
        self.assertIn("async function createRuleTag()", self.javascript)
        self.assertIn("async function renameRuleTag(", self.javascript)
        self.assertIn("async function deleteRuleTag(", self.javascript)
        self.assertIn("async function importDefaultRules()", self.javascript)
        self.assertIn("PanelCore.ruleTagGroups", self.javascript)
        self.assertIn("customRuleCreatePendingReceipt", self.javascript)
        self.assertIn('button.textContent = "重新核对保存"', self.javascript)
        self.assertIn("focusLibraryRule", self.javascript)
        self.assertIn('CUSTOM_RULE_DRAFT_STORAGE_KEY = "stockRules.web.v1.customRuleDraft"', self.javascript)
        self.assertIn("function saveStandaloneRuleDraftCache()", self.javascript)
        self.assertIn("function restoreStandaloneRuleDraftCache()", self.javascript)
        self.assertIn("function clearStandaloneRuleDraftCache()", self.javascript)
        restore_flow = self.javascript.split(
            "function restoreStandaloneRuleDraftCache()", 1
        )[1].split("function formatMoney", 1)[0]
        for cached_field in (
            "payload.requestId",
            "payload.pendingReceipt",
            "payload.name",
            "payload.expression",
            "payload.description",
        ):
            self.assertIn(cached_field, restore_flow)
        self.assertIn('state.customRuleCreateRequestId = clean(payload.requestId)', restore_flow)
        self.assertIn('byId("confirmRuleDefinitionButton").textContent = "重新核对保存"', restore_flow)
        self.assertIn("restoreStandaloneRuleDraftCache();", self.javascript.split("async function loadCore()", 1)[1])
        self.assertNotIn('name="library_rule"', self.javascript)
        self.assertNotIn("rule-set-lifecycle", self.html + self.javascript + self.styles)
        initially_hidden = {
            attrs["id"]
            for _tag, attrs in self.markup.tags
            if attrs.get("id") and "hidden" in attrs
        }
        for identifier in (
            "resetRulesButton",
            "ruleEditorForm",
        ):
            self.assertIn(identifier, initially_hidden)
        self.assertNotIn('id="activateRuleSetButton"', self.html)
        self.assertNotIn('id="goScreeningButton"', self.html)
        self.assertIn('id="saveRulesButton" type="submit" disabled', self.html)
        list_flow = self.javascript.split(
            "function renderWorkspaceRuleSetList()", 1
        )[1].split("function updateWorkspaceState", 1)[0]
        self.assertIn('data-activate-rule-set="${escapeHtml(item.id)}"', list_flow)
        self.assertIn('data-edit-rule-set="${escapeHtml(item.id)}"', list_flow)
        self.assertIn('data-delete-rule-set="${escapeHtml(item.id)}"', list_flow)
        self.assertIn('data-toggle-rule-set="${escapeHtml(item.id)}"', list_flow)
        self.assertIn('aria-expanded="${selected}"', list_flow)
        self.assertIn('selected ? `<div class="rule-set-detail-mount"', list_flow)
        self.assertIn("byId(\"ruleSetList\").after(editor)", list_flow)
        self.assertGreaterEqual(
            list_flow.count('data-rule-set-version="${targetVersion}"'),
            2,
        )
        self.assertIn('>编辑</button>', list_flow)
        self.assertNotIn('>查看</button>', list_flow)
        self.assertIn("rule-set-current-badge", list_flow)
        self.assertIn("activation.statusLabel", list_flow)
        self.assertIn("activation.actionLabel", list_flow)
        self.assertIn("activation.activating ? 'aria-busy=\"true\"' : \"\"", list_flow)
        self.assertRegex(
            list_flow,
            r'(?s)\$\{exactCurrent\s*\?\s*""\s*:\s*`<button[^`]+data-activate-rule-set=',
        )
        self.assertNotIn("使用最新版", list_flow)
        self.assertIn('state.ruleDetailMode = "view"', self.javascript)
        self.assertIn('state.ruleDetailMode = "edit"', self.javascript)
        self.assertIn('state.ruleDetailMode = "view";', self.javascript.split(
            "async function saveWorkspaceRuleSet", 1
        )[1].split("async function initializeRuleSetWorkspace", 1)[0])
        self.assertIn('policy: "legacy_to_two_stage_all_v1"', self.javascript)
        self.assertIn("dropped_veto_refs: [...veto]", self.javascript)
        self.assertIn("是否确认以上变化并保存", self.javascript)
        # 规则库五个分类合成一个列表，页签取消；主视图页签仍是 tablist
        self.assertNotIn('aria-label="规则库类型"', self.html)
        self.assertIn('role="tablist" aria-label="规则页主视图"', self.html)
        self.assertIn('class="editor-card rule-library" id="ruleLibraryPanel"', self.html)
        self.assertNotIn(".archived-toggle", self.styles)
        self.assertIn(".row-action.is-danger", self.styles)
        add_button_css = self.styles.split(
            ".rule-library-add-button", 1
        )[1].split("}", 1)[0]
        self.assertIn("min-height: 44px", add_button_css)
        for hidden_technical_term in ["L1 可视化", "L2 安全 DSL", "DRAFT", "VALIDATED"]:
            self.assertNotIn(hidden_technical_term, self.html)
        self.assertIn('path == "/api/rule-sets"', self.server)

    def test_rule_editor_cancel_and_collapse_restore_clean_local_state(self):
        dirty_flow = self.javascript.split(
            "function workspaceDirty()", 1
        )[1].split("function workspaceTargetExists", 1)[0]
        self.assertIn(
            'if (["new", "copy"].includes(state.ruleSetDraftKind)) return state.ruleEditorOpen',
            dirty_flow,
        )

        cancel_flow = self.javascript.split(
            "async function cancelWorkspaceRuleEdit(", 1
        )[1].split("async function refreshWorkspaceRules", 1)[0]
        new_cancel_flow = cancel_flow.split(
            'if (state.ruleSetDraftKind === "new")', 1
        )[1].split("} else {", 1)[0]
        for reset in (
            "state.ruleEditorOpen = false",
            'state.selectedRuleSetId = ""',
            "state.selectedRuleSetVersion = null",
            "state.selectedRuleSetExact = null",
            "state.ruleDraftRefs = new Set()",
            "state.secondaryRuleDraftRefs = new Set()",
            'state.rulePristineSignature = ""',
            "safeStorageRemove(RULE_DRAFT_STORAGE_KEY)",
        ):
            self.assertIn(reset, new_cancel_flow)

        list_click_flow = self.javascript.split(
            'byId("ruleSetList").addEventListener("click"', 1
        )[1].split('byId("newRuleSetButton")', 1)[0]
        collapse_flow = list_click_flow.split(
            'if (state.ruleEditorOpen && state.ruleSetDraftKind === "existing"', 1
        )[1].split("await selectWorkspaceRuleSet(id", 1)[0]
        self.assertIn("setWorkspaceFromExact(item, state.selectedRuleSetExact)", collapse_flow)
        self.assertLess(
            collapse_flow.index("setWorkspaceFromExact(item, state.selectedRuleSetExact)"),
            collapse_flow.index("state.ruleEditorOpen = false"),
        )
        self.assertIn("safeStorageRemove(RULE_DRAFT_STORAGE_KEY)", collapse_flow)

        list_flow = self.javascript.split(
            "function renderWorkspaceRuleSetList()", 1
        )[1].split("function focusWorkspaceRuleSetRow", 1)[0]
        self.assertIn("const deleteLocked = selectionLocked || workspaceDirty()", list_flow)
        self.assertIn("selectionLocked || workspaceDirty() || !canActivate", list_flow)

    def test_screen_data_summary_is_inline_and_refreshes_on_foreground(self):
        summary_flow = self.javascript.split(
            "function renderDataSummary()", 1
        )[1].split("function dataEntries()", 1)[0]
        self.assertIn("PanelCore.datasetStatusSummary(dataset)", summary_flow)
        self.assertIn('byId("dataDate").textContent = alignment.dateLine', summary_flow)
        self.assertIn('byId("dataSummary").textContent = alignment.inlineLine', summary_flow)
        self.assertIn("async function refreshDatasetStatus", self.javascript)
        refresh_flow = self.javascript.split(
            "async function refreshDatasetStatus", 1
        )[1].split("async function syncFullStockCatalog", 1)[0]
        self.assertIn("state.dataStatusRefreshSerial", refresh_flow)
        self.assertIn("serial !== state.dataStatusRefreshSerial", refresh_flow)
        self.assertIn("state.screeningInputsUncertain = true", refresh_flow)
        self.assertIn('document.addEventListener("visibilitychange"', self.javascript)
        self.assertIn('window.addEventListener("focus"', self.javascript)

    def test_screening_reuses_duplicate_and_shows_per_stock_rule_evidence(self):
        screening_flow = self.javascript.split(
            "async function runScreening", 1
        )[1].split("function ruleSignature", 1)[0]
        existing_flow = self.javascript.split(
            "async function loadExistingScreening", 1
        )[1].split("async function runScreening", 1)[0]
        self.assertLess(
            screening_flow.find("requestJson(API.screeningPreflight"),
            screening_flow.find("requestJson(API.screeningRun"),
        )
        self.assertIn("preflight.duplicate_run?.exists", screening_flow)
        self.assertIn("${API.screenings}/${encodeURIComponent", existing_flow)
        self.assertIn("revealScreeningResult()", existing_flow)
        self.assertIn("revealScreeningResult()", screening_flow)
        self.assertIn("catch (recoveryError)", screening_flow)
        display_flow = self.javascript.split(
            "function screeningRowsForDisplay(", 1
        )[1].split("function screeningDisplayCounts(", 1)[0]
        self.assertNotIn("filter(isCandidate)", display_flow)
        self.assertIn("function screeningRowsForDisplay(", self.javascript)
        self.assertIn("function screeningDisplayCounts(", self.javascript)
        self.assertIn("PanelCore.screeningCounts", self.javascript)
        self.assertIn("function evidenceForRow(", self.javascript)
        self.assertIn("function renderEvidenceItem(", self.javascript)
        self.assertIn("function compactEvidenceCopy(", self.javascript)
        self.assertIn("item.observed || {}", self.javascript)
        self.assertIn("item.comparisons", self.javascript)
        self.assertIn("item.plain_explanation", self.javascript)
        self.assertIn("record-evidence-details", self.javascript)
        self.assertIn('universe_scope: "local_library"', screening_flow)
        self.assertNotIn(
            "state.dataset = state.screening.dataset_manifest",
            screening_flow,
        )
        self.assertIn("function screeningScopeLabel(", self.javascript)
        self.assertIn("面板不会自动降低规则", self.javascript)
        self.assertIn('data-record-symbol=', self.javascript)

    def test_screening_result_freshness_is_explicit_and_recoverable(self):
        self.assertIn('id="screenResultFreshness"', self.html)
        self.assertIn("function screeningFreshness(", self.javascript)
        self.assertIn("function renderScreeningAction(", self.javascript)
        self.assertIn("dataset?.dataset_hash", self.shared)
        self.assertIn("activeRule?.rule_set_hash", self.shared)
        self.assertIn('meta.universe_scope !== "local_library"', self.shared)
        self.assertIn("数据或规则已变化", self.javascript)
        self.assertIn("按当前规则重新筛选", self.javascript)
        self.assertIn("结果与当前本地数据和规则一致", self.shared)
        self.assertIn('freshness.status === "current" ? "" : "hidden disabled"', self.javascript)
        self.assertIn("screeningInputsUncertain", self.javascript)
        self.assertIn("旧结果暂时只读", self.shared)
        self.assertIn(".result-freshness.is-current", self.styles)
        self.assertIn(".result-freshness.is-stale", self.styles)
        self.assertIn(".result-freshness.is-updating", self.styles)
        self.assertIn(".record-evidence-details > summary", self.styles)
        self.assertIn(".screening-record-status.is-not-selected", self.styles)
        self.assertIn("[hidden] {\n  display: none !important;", self.styles)
        self.assertIn(
            ".candidate-card.screening-record {\n  display: block;",
            self.styles,
        )

    def test_interaction_closure_keeps_async_state_and_focus_consistent(self):
        self.assertIn('id="candidateHeading" tabindex="-1"', self.html)
        self.assertIn('id="recordList" role="list"', self.html)
        # 刚保存的记录在返回首页后高亮，便于核对
        self.assertIn('class="record-card${clean(record.request_id)', self.javascript)
        self.assertIn('is-highlighted', self.javascript)
        self.assertIn("function setFormLocked(", self.javascript)
        self.assertIn("function dataMutationBusy()", self.javascript)
        self.assertIn("function resolveTradeStockInput()", self.javascript)
        self.assertIn('input.setCustomValidity("请从股票列表中选择一个明确的代码或名称。")', self.javascript)
        self.assertIn("state.rulesSavingBusy = true", self.javascript)
        self.assertIn("state.tradeSavingBusy = true", self.javascript)
        self.assertIn('byId("candidateList").setAttribute("aria-busy", "true")', self.javascript)
        self.assertIn('byId("dataDialog").addEventListener("cancel"', self.javascript)
        self.assertIn('byId("ruleDialog").addEventListener("cancel"', self.javascript)
        self.assertIn('window.addEventListener("beforeunload"', self.javascript)
        self.assertIn("Promise.allSettled", self.javascript)
        self.assertIn("function tradeDraftDirty()", self.javascript)
        self.assertIn("function unsavedDialogDraft()", self.javascript)
        self.assertIn('toggle.setAttribute("aria-label", "收起股票列表")', self.javascript)
        toggle_flow = self.javascript.split(
            'byId("tradeSymbolToggle").addEventListener("click"', 1
        )[1].split('byId("tradeSymbolOptions").addEventListener("pointerdown"', 1)[0]
        self.assertIn('byId("tradeSymbolOptions").hidden', toggle_flow)
        self.assertIn("openTradeSymbolPicker({ showAll: true })", toggle_flow)
        self.assertIn("closeTradeSymbolPicker()", toggle_flow)

        save_rules_flow = self.javascript.split(
            "async function saveWorkspaceRuleSet", 1
        )[1].split("async function initializeRuleSetWorkspace", 1)[0]
        self.assertIn("state.rulesSavingBusy = true", save_rules_flow)
        self.assertIn("await reconcileWorkspaceTarget", save_rules_flow)
        self.assertIn("另一端刚更新了组合", save_rules_flow)
        self.assertNotIn("/activate", save_rules_flow)
        self.assertNotIn("activate", save_rules_flow)
        self.assertIn("组合已保存", save_rules_flow)
        self.assertIn("当前状态待核对", save_rules_flow)
        self.assertIn('setRuleOperationPhase(operationToken, "conflict")', save_rules_flow)
        self.assertIn("requestId: state.ruleApplyRequestId", self.javascript)

        save_trade_flow = self.javascript.split(
            "async function saveTrade", 1
        )[1].split("function prepareTradeForCandidate", 1)[0]
        self.assertLess(
            save_trade_flow.find("requestJson(API.trades"),
            save_trade_flow.find("resetTradeForm()"),
        )
        self.assertIn("操作记录已经保存，但最近记录刷新失败", save_trade_flow)

    def test_operation_record_is_disciplined_append_only_and_idempotent(self):
        for name in [
            "request_id",
            "trade_date",
            "trade_time",
            "symbol",
            "side",
            "price",
            "shares",
            "fee",
            "notes",
            "reason_tag",
            "discipline_check",
            "emotion_flag",
            "emotion_clear",
        ]:
            self.assertIn(f'name="{name}"', self.html)
        # 成交时间可以补录，但绝不能必填：不知道就按「未记录」保存，
        # 服务端也不再用提交时间顶上（见 src/personal_data.py 的 _normalize_trade_time）。
        trade_time_input = next(
            attrs
            for tag, attrs in self.markup.tags
            if tag == "input" and attrs.get("id") == "tradeTime"
        )
        self.assertEqual("time", trade_time_input.get("type"))
        self.assertNotIn("required", trade_time_input)
        self.assertIn("不填按「未记录」保存", self.html)
        self.assertIn('trade_time: byId("tradeTime").value', self.javascript)
        # 本笔费用合计必须可选，且必须能表达「未知」：留空不得被当成 0。
        fee_input = next(
            attrs
            for tag, attrs in self.markup.tags
            if tag == "input" and attrs.get("id") == "tradeFee"
        )
        self.assertEqual("number", fee_input.get("type"))
        self.assertNotIn("required", fee_input)
        self.assertEqual("0", fee_input.get("min"))
        self.assertIn("留空表示费用未知", self.html)
        self.assertIn("确实没有费用请填 0", self.html)
        self.assertIn('fee: byId("tradeFee").value', self.javascript)
        self.assertIn("requestJson(API.trades", self.javascript)
        self.assertIn("requestJson(`${API.trades}?limit=50`)", self.javascript)
        self.assertIn("function updateTradeAmount()", self.javascript)
        self.assertIn("function updateTradeDisciplineStatus()", self.javascript)
        self.assertIn("reason_tags: checklist.reasonTags", self.javascript)
        self.assertIn("discipline_checks: checklist.disciplineChecks", self.javascript)
        self.assertIn("emotion_flags: checklist.emotionFlags", self.javascript)
        self.assertIn("emotion_clear: checklist.emotionClear", self.javascript)
        self.assertIn("record.rule_status", self.javascript)
        self.assertIn("record.emotion", self.javascript)
        self.assertIn("record.operation", self.javascript)
        self.assertIn("function setDataActionsBusy(", self.javascript)
        # 幂等编号在提交前捕获一次，提交与回执核对必须使用同一个值，
        # 否则回执不确定时会用另一个编号去核对，去重失效。
        self.assertIn(
            'const requestId = byId("tradeRequestId").value',
            self.javascript,
        )
        self.assertIn("request_id: requestId", self.javascript)
        self.assertIn("symbol: stock.symbol", self.javascript)
        self.assertIn('path == "/api/trades"', self.server)
        self.assertNotIn("data-delete-trade", self.html + self.javascript)

    def test_busy_error_and_accessibility_feedback_are_explicit(self):
        self.assertIn('aria-live="polite"', self.html)
        self.assertIn('aria-current="page"', self.html)
        self.assertNotIn('id="goScreeningButton"', self.html)
        self.assertNotIn("function canGoToScreening()", self.javascript)
        self.assertIn("function focusWorkspaceRuleSetRow", self.javascript)
        self.assertIn('stage: "activation_receipt"', self.javascript)
        self.assertIn('byId("resetRulesButton").hidden = !recoveryRequired', self.javascript)
        self.assertIn("focusWorkspaceLibrarySummary", self.javascript)
        self.assertIn("focusWorkspaceMoveControl", self.javascript)
        self.assertIn("focus({ preventScroll: true })", self.javascript)
        self.assertIn("button.setAttribute(\"aria-busy\", \"true\")", self.javascript)
        self.assertIn("无法连接本地面板服务", self.javascript)
        self.assertIn("showToast(error.message, true)", self.javascript)
        self.assertIn('if (document.querySelector("dialog[open]")) return;', self.javascript)
        self.assertIn(".dialog-feedback", self.styles)
        self.assertIn(".form-feedback.is-success", self.styles)
        self.assertIn(".form-feedback.is-warning", self.styles)
        self.assertIn(":focus-visible", self.styles)
        self.assertIn("prefers-reduced-motion: reduce", self.styles)
        self.assertIn("forced-colors: active", self.styles)

    def test_rule_list_activation_and_recovery_have_closed_feedback_flow(self):
        self.assertEqual(1, self.html.count('id="resetRulesButton"'))
        self.assertLess(
            self.html.index('id="resetRulesButton"'),
            self.html.index('id="ruleEditorForm"'),
        )

        select_flow = self.javascript.split(
            "async function selectWorkspaceRuleSet", 1
        )[1].split("function startWorkspaceRuleSet", 1)[0]
        self.assertIn("state.ruleEditorOpen = Boolean(openEditor)", select_flow)
        self.assertIn(
            'state.ruleDetailMode = openEditor && detailMode === "edit" ? "edit" : "view"',
            select_flow,
        )

        focus_flow = self.javascript.split(
            "function focusWorkspaceRuleSetRow", 1
        )[1].split("function focusWorkspaceRuleEditor", 1)[0]
        self.assertIn("[data-toggle-rule-set]", focus_flow)
        self.assertNotIn("[data-edit-rule-set]", focus_flow)

        activation_flow = self.javascript.split(
            "async function activateWorkspaceRuleSet", 1
        )[1].split("async function deleteWorkspaceRuleSet", 1)[0]
        self.assertIn("writeReceiptMayBeUncertain(activationError)", activation_flow)
        self.assertIn("if (activationRejected)", activation_flow)
        self.assertIn('requestJson(API.rules)', activation_flow)
        self.assertIn('"RULE_SET_CHANGED", "ACTIVE_RULE_SET_CHANGED"', activation_flow)
        self.assertIn("页面已更新，请重新确认后再使用", activation_flow)

        list_flow = self.javascript.split(
            "function renderWorkspaceRuleSetList()", 1
        )[1].split("function focusWorkspaceRuleSetRow", 1)[0]
        disclosure_start = list_flow.index('<button class="rule-set-disclosure"')
        summary_start = list_flow.index('<span class="record-row-main">')
        disclosure_end = list_flow.index("</button>", disclosure_start)
        self.assertLess(disclosure_start, summary_start)
        self.assertLess(summary_start, disclosure_end)

        state_flow = self.javascript.split(
            "function updateWorkspaceState", 1
        )[1].split("function renderRuleSetWorkspace", 1)[0]
        # 组合说明字段已整体移除，只读态不再需要单独的说明展示分支。
        self.assertNotIn("savedDescription", state_flow)
        self.assertIn(".rule-set-list:has(.rule-set-card.is-selected)", self.styles)
        self.assertIn(
            ".rules-surface",
            self.styles,
        )
        self.assertIn("const disclosureDisabled = selectionLocked && !selected", list_flow)
        self.assertIn('disclosureDisabled ? "disabled" : ""', list_flow)
        self.assertIn('byId("saveRulesButton").hidden = readOnly || recoveryRequired', state_flow)
        self.assertIn('byId("cancelRuleEditButton").hidden = readOnly || recoveryRequired', state_flow)

        delete_flow = self.javascript.split(
            "async function deleteWorkspaceRuleSet", 1
        )[1].split("async function saveWorkspaceRuleSet", 1)[0]
        self.assertIn("writeReceiptMayBeUncertain(error)", delete_flow)
        self.assertNotIn("item.is_active", delete_flow)
        save_flow = self.javascript.split(
            "async function saveWorkspaceRuleSet", 1
        )[1].split("function addWorkspaceLibraryRule", 1)[0]
        self.assertIn("writeReceiptMayBeUncertain(error)", save_flow)
        custom_save_flow = self.javascript.split(
            "async function saveLibraryRule", 1
        )[1].split("function saveWorkspaceRuleDefinition", 1)[0]
        self.assertIn("writeReceiptMayBeUncertain(error)", custom_save_flow)

        restore_flow = self.javascript.split(
            "function restoreWorkspaceDraftCache", 1
        )[1].split("function workspaceExactFromPayload", 1)[0]
        self.assertIn('const listOnlyRecovery = ["activation_receipt", "delete_receipt"]', restore_flow)
        self.assertIn("state.ruleEditorOpen = !listOnlyRecovery", restore_flow)
        self.assertIn('state.ruleDetailMode = listOnlyRecovery ? "view" : "edit"', restore_flow)

        recovery_flow = self.javascript.split(
            'byId("resetRulesButton").addEventListener("click"', 1
        )[1].split('byId("addRuleButton")', 1)[0]
        activation_recovery = recovery_flow.split(
            'pendingTarget?.stage === "activation_receipt"', 1
        )[1].split('pendingTarget?.stage === "delete_receipt"', 1)[0]
        self.assertIn('setFeedback("ruleSetListFeedback", "")', activation_recovery)
        self.assertIn('setFeedback("ruleSetListFeedback", "已核对：设置未生效，可重新操作。"', activation_recovery)
        delete_recovery = recovery_flow.split(
            'pendingTarget?.stage === "delete_receipt"', 1
        )[1].split('pendingTarget?.stage === "save_receipt"', 1)[0]
        self.assertIn('setFeedback("ruleSetListFeedback", message', delete_recovery)
        self.assertIn('setFeedback("ruleSetListFeedback", "")', delete_recovery)

    def test_records_page_is_a_history_home_with_a_separate_entry_subpage(self):
        # 首页只展示历史；录入表单移到独立子页面，不再与历史并列。
        for identifier in (
            "recordsHomeView",
            "recordEditorView",
            "newRecordButton",
            "recordEditorBackButton",
            "recordDraftNotice",
            "recordDraftSummary",
            "resumeDraftButton",
            "discardDraftButton",
            "recordDraftStatus",
        ):
            self.assertIn(f'id="{identifier}"', self.html)
        home = self.html.split('id="recordsHomeView"', 1)[1].split(
            'id="recordEditorView"', 1
        )[0]
        self.assertIn('id="recordList"', home)
        self.assertIn('id="newRecordButton"', home)
        self.assertNotIn('id="tradeForm"', home)
        self.assertNotIn("records-layout", self.html)
        # 历史仍是只读追加：没有编辑或删除入口
        self.assertNotIn("data-delete-trade", self.html + self.javascript)
        self.assertNotIn("data-edit-trade", self.html + self.javascript)

        # 主从互斥由用户动作切换，绝不由视口宽度决定
        self.assertIn("function renderRecordsSurface()", self.javascript)
        self.assertIn('byId("recordsHomeView").hidden = editing', self.javascript)
        self.assertIn('byId("recordEditorView").hidden = !editing', self.javascript)
        self.assertNotRegex(
            self.javascript,
            r"(innerWidth|matchMedia)[^\n]*recordsSurface",
        )

        # 版本化本机草稿：单一键、防抖、恢复后沿用原请求编号
        self.assertIn(
            'TRADE_DRAFT_STORAGE_KEY = "stockRules.web.v1.tradeDraft"',
            self.javascript,
        )
        self.assertIn("TRADE_DRAFT_SCHEMA", self.javascript)
        for fn in (
            "function saveTradeDraftCache()",
            "function clearTradeDraftCache()",
            "function restoreTradeDraftCache()",
            "function applyTradeDraft(",
            "function openRecordEditor(",
            "function leaveRecordEditor()",
            "function discardTradeDraft()",
        ):
            self.assertIn(fn, self.javascript)
        restore = self.javascript.split("function restoreTradeDraftCache()", 1)[1].split(
            "function applyTradeDraft(", 1
        )[0]
        self.assertIn("Number(payload.schemaVersion) !== TRADE_DRAFT_SCHEMA", restore)
        apply_flow = self.javascript.split("function applyTradeDraft(", 1)[1].split(
            "function openRecordEditor(", 1
        )[0]
        self.assertIn('byId("tradeRequestId").value = clean(payload.requestId)', apply_flow)
        # 放弃草稿是不可恢复动作，必须二次确认
        discard = self.javascript.split("async function discardTradeDraft()", 1)[1].split(
            "function tradeDraftDirty()", 1
        )[0]
        # 二次确认改用页面内居中确认弹窗，不再用浏览器原生 confirm
        self.assertIn("askConfirm(", discard)
        # 正式保存确认成功后才清草稿
        save_flow = self.javascript.split("async function saveTrade", 1)[1].split(
            "\n  function prepareTrade", 1
        )[0]
        self.assertIn("clearTradeDraftCache()", save_flow)
        self.assertIn('state.recordsSurface = "home"', save_flow)
        # 候选带入不得静默覆盖已有草稿
        candidate = self.javascript.split("async function prepareTradeForCandidate(", 1)[1].split(
            "\n  function ", 1
        )[0]
        self.assertIn("state.tradeDraftMeta", candidate)
        self.assertIn("askConfirm(", candidate)

    def test_records_page_shows_the_ledger_facts_and_a_per_symbol_summary(self):
        # 每笔成交必须显示成交后剩余、成交后平均成本和该笔已实现盈亏，
        # 并且按股票给出「现在还剩多少、成本是多少、最后动的是哪天」。
        for identifier in (
            "recordSummaryHeading",
            "recordSummaryCount",
            "recordSummaryList",
        ):
            self.assertIn(f'id="{identifier}"', self.html)
        home = self.html.split('id="recordsHomeView"', 1)[1].split(
            'id="recordEditorView"', 1
        )[0]
        self.assertIn('id="recordSummaryList"', home)
        # 汇总在明细之前：先回答「现在还持有多少」，再看每笔怎么来的。
        self.assertLess(
            home.index('id="recordSummaryList"'),
            home.index('id="recordList"'),
        )
        self.assertIn("function renderRecordSummaries()", self.javascript)
        self.assertIn("renderRecordSummaries()", self.javascript)
        self.assertIn("state.trades?.symbol_summaries", self.javascript)

        record_flow = self.javascript.split("function renderTrades()", 1)[1].split(
            "async function loadTrades(", 1
        )[0]
        for field in (
            "record.remaining_shares",
            "record.avg_cost_after_trade",
            "record.realized_pnl",
            "record.operation_label",
            "record.trade_time_label",
            "record.incomplete_label",
            "record.missing_fields",
        ):
            self.assertIn(field, record_flow)

        summary_flow = self.javascript.split(
            "function renderRecordSummaries()", 1
        )[1].split("function renderTrades()", 1)[0]
        for field in (
            "item.remaining_shares",
            "item.avg_cost",
            "item.last_trade_date",
            "item.last_operation_label",
            "item.incomplete_label",
        ):
            self.assertIn(field, summary_flow)
        # 汇总只复述事实：不出现仓位比例、市值占比或任何操作结论。
        self.assertNotIn("position_pct", summary_flow)
        self.assertIn("不算仓位比例", self.html)
        # 汇总必须叫「成交账本」：未与持仓快照核对前不得自称当前实际持仓。
        self.assertIn("成交账本剩余股数", summary_flow)
        self.assertIn("成交账本平均成本", summary_flow)
        self.assertNotIn("当前剩余股数", summary_flow)
        self.assertNotIn("当前平均成本", summary_flow)
        self.assertIn("不等于当前实际持仓", summary_flow)
        # 费用未知必须传到汇总，并且数值旁边带限定语。
        self.assertIn("item.fee_complete", summary_flow)
        self.assertIn("item.fee_label", summary_flow)
        self.assertIn("record.cost_fee_note", record_flow)
        self.assertIn("record.realized_fee_note", record_flow)
        self.assertIn("function feeNote(", self.javascript)
        self.assertIn("fact-caveat", self.javascript)
        self.assertIn(".fact-caveat", self.styles)

        # 缺字段只标记不倒推：数字位显示「—」，绝不退回 0。
        fact_flow = self.javascript.split("function factValue(", 1)[1].split(
            "function renderRecordSummaries()", 1
        )[0]
        self.assertIn('value == null ? "—" : render(value)', fact_flow)
        self.assertIn('未记录', self.javascript)
        # 成本价按台账的 4 位小数显示，屏幕上的数字要能和 CSV 里那一笔对上。
        self.assertIn("maximumFractionDigits: 4", self.web_sources["platform.js"])
        self.assertIn("factValue(record.avg_cost_after_trade, formatCost)", record_flow)
        self.assertIn("factValue(item.avg_cost, formatCost)", summary_flow)
        for selector in (
            ".record-facts dd",
            ".record-incomplete",
            ".summary-card",
            ".summary-facts dt",
        ):
            self.assertIn(selector, self.styles)

    def test_records_page_shows_an_honest_empty_state_instead_of_zeroes(self):
        # 空数据时界面只能说「还没有」，不能给出 0 股 / ¥0 这种看起来已核对过的数字。
        summary_flow = self.javascript.split(
            "function renderRecordSummaries()", 1
        )[1].split("function renderTrades()", 1)[0]
        self.assertIn("还没有可汇总的成交", summary_flow)
        empty_branch = summary_flow.split("还没有可汇总的成交", 1)[1]
        self.assertNotIn("0 股", empty_branch)
        self.assertNotIn("¥0", empty_branch)

        record_flow = self.javascript.split("function renderTrades()", 1)[1].split(
            "async function loadTrades(", 1
        )[0]
        self.assertIn("还没有操作记录", record_flow)

        # 读不到记录时必须把汇总也清掉：绝不保留上一次的数字充当当前事实。
        load_flow = self.javascript.split("async function loadTrades(", 1)[1].split(
            "async function reconcileTradeReceipt(", 1
        )[0]
        self.assertIn('byId("recordSummaryCount").textContent = "0"', load_flow)
        self.assertIn("没有读到成交记录，这里不显示任何数字", load_flow)
        self.assertIn("汇总暂时无法核对", load_flow)

        # HTML 里的初始空态同样不得预置数字。
        home = self.html.split('id="recordsHomeView"', 1)[1].split(
            'id="recordEditorView"', 1
        )[0]
        self.assertIn("还没有可汇总的成交", home)
        self.assertIn("还没有操作记录", home)

    def test_css_is_balanced_readable_and_mobile_safe(self):
        without_comments = re.sub(r"/\*.*?\*/", "", self.styles, flags=re.DOTALL)
        self.assertEqual(without_comments.count("{"), without_comments.count("}"))
        self.assertNotRegex(self.styles, r"font-size:\s*(?:8|9|10|11)px")
        self.assertNotIn("linear-gradient", self.styles)
        mobile = self.styles.split("@media (max-width: 720px)", 1)[1]
        self.assertIn("position: fixed", mobile)
        self.assertIn("env(safe-area-inset-bottom)", mobile)
        self.assertIn("grid-template-columns: repeat(3, minmax(0, 1fr))", mobile)
        self.assertIn("width: calc(100% - 28px)", mobile)
        self.assertIn("@media (pointer: coarse)", self.styles)


if __name__ == "__main__":
    unittest.main()
