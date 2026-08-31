"""操作纪律路线共用的合成场景工厂。

隔离合同：

1. 每个构建函数都显式接收 ``target_root``，只往调用方给的目录写。
2. 只从仓库读取两类**随代码走的版本化种子**——默认策略配置和内置规则定义；
   绝不读取、也绝不回退到真实 PROJECT_ROOT 下的运行数据
   （``data/``、``records/``、``history/``、``config/local_stock_library.csv``、
   ``watchlist.csv``、``rules/catalog/user/``、``rules/rule_sets/``）。
3. 派生结果一律调用正式业务代码生成，不手写。规则库就用正式的首次启动
   初始化逻辑导入，这样「内置模板」不会被误当成用户运行数据。
4. 写入前先确认目标目录在仓库之外，避免误改开发目录里的真实运行数据。
5. 使用固定业务日期与固定测试代码，不读取当前日期、不访问网络。

第一版提供四个基础场景：``EMPTY_FIRST_RUN``（隔离根与空实例）、
``READY_CURRENT_FLOW``（当前能力的最小完整链路）、``ZERO_CANDIDATE``
（筛选成功但零候选）、``LEDGER_INCOMPLETE``（一段历史成交缺必要字段）。
功能 1.2 在此基础上增量增加 ``TIME_ORDER_AMBIGUOUS``，只制造同日成交顺序
无法确认这一项局部状态，不复制行情、规则或筛选数据。
全部是临时合成场景，不含任何真实行情或个人数据。
后续场景由真正需要它们的功能增量扩展，共享场景只建立一次，
不为每个功能重复造一套平行数据。
"""

from __future__ import annotations

from datetime import date
import shutil
from pathlib import Path
from typing import Any

import pandas as pd

from src.personal_data import append_trade_record, import_personal_data
from src.rules.simple_editor import ensure_rule_catalog_seeded
from src.rules.version_store import RuleSetStore
from src.screening.engine import ScreeningCoordinator
from src.stock_library import add_local_stock_member
from tests.market_fixture import sample_ohlcv

# 只用于读取随代码走的种子文件。刻意不叫 PROJECT_ROOT，避免被误解成
# 「可以从这里拿运行数据」。
SEED_SOURCE_ROOT = Path(__file__).resolve().parents[1]

SEED_CONFIG_FILE = Path("config") / "strategy_rules.yaml"
SEED_RULE_CATALOG_DIR = Path("rules") / "catalog" / "builtin"

# 出现任何一项就说明场景里混进了用户运行数据。
USER_RUNTIME_PATHS: tuple[Path, ...] = (
    Path("data"),
    Path("watchlist.csv"),
    Path("config") / "local_stock_library.csv",
    Path("records") / "my_trades.csv",
    Path("records") / "positions_snapshot.csv",
    Path("rules") / "rule_sets",
    Path("history") / "screenings.sqlite3",
    Path("history") / "reviews.sqlite3",
)

EMPTY_FIRST_RUN = "EMPTY_FIRST_RUN"


def require_isolated_root(target_root: str | Path) -> Path:
    """确认目标目录在仓库之外。

    共享场景只允许写进调用方自己的 TemporaryDirectory；写进仓库会污染
    开发 worktree 里的真实运行数据。
    """
    root = Path(target_root).resolve()
    if root == SEED_SOURCE_ROOT or SEED_SOURCE_ROOT in root.parents:
        raise AssertionError(
            f"共享场景只能写入仓库之外的独立临时目录，收到的是仓库内路径：{root}"
        )
    return root


def install_panel_defaults(target_root: str | Path) -> Path:
    """放入正式启动必需的版本化默认配置与内置规则定义。

    只复制这两类种子，不复制任何运行数据。
    """
    root = require_isolated_root(target_root)
    (root / SEED_CONFIG_FILE.parent).mkdir(parents=True, exist_ok=True)
    shutil.copy(SEED_SOURCE_ROOT / SEED_CONFIG_FILE, root / SEED_CONFIG_FILE)
    shutil.copytree(
        SEED_SOURCE_ROOT / SEED_RULE_CATALOG_DIR,
        root / SEED_RULE_CATALOG_DIR,
        dirs_exist_ok=True,
    )
    return root


def assert_no_user_runtime_data(target_root: str | Path) -> Path:
    """场景自检：目标目录里不允许出现任何用户运行数据。"""
    root = Path(target_root)
    present = sorted(
        str(relative) for relative in USER_RUNTIME_PATHS if (root / relative).exists()
    )
    if present:
        raise AssertionError(f"共享空实例混入了用户运行数据：{present}")
    return root


def build_empty_first_run(
    target_root: str | Path,
    *,
    seed_rule_catalog: bool = True,
) -> dict[str, Any]:
    """EMPTY_FIRST_RUN：能正式启动，但没有任何用户运行数据。

    无本地股票、无行情、无用户创建的当前规则组合、无筛选批次、
    无成交、无持仓、无账户资产。

    ``seed_rule_catalog`` 默认走正式的首次启动初始化，把内置规则模板导入
    可编辑规则库——这是正式启动本来就会做的事，导入结果属于出厂模板，
    不是用户运行数据。
    """
    root = install_panel_defaults(target_root)
    seeded: dict[str, Any] = {"seeded": False, "imported_count": 0}
    if seed_rule_catalog:
        seeded = ensure_rule_catalog_seeded(root)
    assert_no_user_runtime_data(root)
    return {
        "root": root,
        "scenario": EMPTY_FIRST_RUN,
        "rule_catalog_seeded": seeded,
    }


READY_CURRENT_FLOW = "READY_CURRENT_FLOW"

# 固定业务日期：场景不读当前日期，结果不随运行时刻漂移。
SCENARIO_TRADE_DATE = "2026-06-12"

# 三只合成测试股票。代码固定、名称合成，不使用真实证券名称。
CANDIDATE_SYMBOL = "600001.SH"
PRIMARY_REJECT_SYMBOL = "000001.SZ"
SECONDARY_REJECT_SYMBOL = "600002.SH"
SCENARIO_STOCKS: tuple[tuple[str, str], ...] = (
    (CANDIDATE_SYMBOL, "测试甲"),
    (PRIMARY_REJECT_SYMBOL, "测试乙"),
    (SECONDARY_REJECT_SYMBOL, "测试丙"),
)

# 一级只看站上 MA20，二级只看站上 MA60。三只股票的日线被塑造成分别落在
# 「都过」「一级过二级不过」「一级就不过」三种结果上。
PRIMARY_RULE_REF = "close_above_ma20@1"
SECONDARY_RULE_REF = "close_above_ma60@1"
SCENARIO_RULE_SET_NAME = "共享场景一级 MA20 二级 MA60"

# 成交事实：同一只候选股票上依次首次买入、加仓、减仓，时间和费用都明确。
SCENARIO_TRADES: tuple[dict[str, Any], ...] = (
    {"request_id": "scenario-first-buy-0001", "trade_date": "2026-06-15",
     "trade_time": "09:35", "side": "BUY", "price": 49.19, "shares": 1000, "fee": 5.20},
    {"request_id": "scenario-add-0002", "trade_date": "2026-06-16",
     "trade_time": "10:20", "side": "BUY", "price": 50.10, "shares": 500, "fee": 3.10},
    {"request_id": "scenario-reduce-0003", "trade_date": "2026-06-17",
     "trade_time": "14:05", "side": "SELL", "price": 52.60, "shares": 600, "fee": 8.40},
)


def _reshape(close: pd.Series, base: pd.DataFrame) -> pd.DataFrame:
    """按给定收盘价重建一份完整日线，其余字段跟着收盘价走。"""
    frame = base.copy()
    frame["close"] = close.values
    frame["open"] = frame["close"] - 0.1
    frame["high"] = frame["close"] + 0.3
    frame["low"] = frame["close"] - 0.3
    frame["amount"] = frame["close"] * frame["volume"]
    return frame


def scenario_day_frames() -> dict[str, pd.DataFrame]:
    """三只股票的确定性日线，全部从 tests/market_fixture.sample_ohlcv 派生。

    不新建第二套 OHLCV 生成器：这里只是把同一份合成行情重塑成三种形态。
    """
    base = sample_ohlcv(end=SCENARIO_TRADE_DATE)

    # 一路上行：收盘价同时站上 MA20 与 MA60。
    candidate = base.copy()

    # 末段下挫：收盘价跌破 MA20，一级就不通过。
    primary_reject_close = base["close"].copy()
    primary_reject_close.iloc[-12:] = primary_reject_close.iloc[-12:] * 0.80

    # 长期下行后小幅反弹：站上 MA20 但仍在 MA60 之下，一级过、二级不过。
    secondary_reject_close = pd.Series(base["close"].values[::-1], index=base.index)
    secondary_reject_close.iloc[-5:] = secondary_reject_close.iloc[-5:] * 1.02

    return {
        CANDIDATE_SYMBOL: candidate,
        PRIMARY_REJECT_SYMBOL: _reshape(primary_reject_close, base),
        SECONDARY_REJECT_SYMBOL: _reshape(secondary_reject_close, base),
    }


def _install_scenario_market(target_root: Path) -> None:
    """把三只测试股票装进本地股票库，并各自落一份确定性日线。

    ``READY_CURRENT_FLOW`` 与 ``ZERO_CANDIDATE`` 共用同一份行情事实：
    两者的差别只在当前规则组合，不另建第二套测试数据。
    """
    frames = scenario_day_frames()
    for symbol, stock_name in SCENARIO_STOCKS:
        add_local_stock_member(
            target_root,
            symbol=symbol,
            stock_name=stock_name,
            catalog_snapshot_date=SCENARIO_TRADE_DATE,
        )
        day_dir = target_root / "data" / symbol / "raw" / "day"
        day_dir.mkdir(parents=True, exist_ok=True)
        frames[symbol].to_csv(
            day_dir / f"{symbol}_day_{SCENARIO_TRADE_DATE.replace('-', '')}.csv",
            index=False,
        )


def _activate_simple_rule_set(
    target_root: Path,
    *,
    name: str,
    rules: list[str],
    secondary_rules: list[str] | None = None,
) -> dict[str, Any]:
    """通过正式规则存储逻辑建立并启用一个 simple_all 组合。"""
    store = RuleSetStore(target_root)
    created = store.create_simple(
        name=name, rules=rules, secondary_rules=secondary_rules
    )
    store.validate(created["id"], 1)
    return store.activate(created["id"], 1)


def _run_official_screening(target_root: Path) -> dict[str, Any]:
    """通过正式筛选服务生成一个不可变批次。"""
    coordinator = ScreeningCoordinator(target_root)
    preflight = coordinator.preflight(
        {"universe_scope": "local_library", "as_of_trade_date": SCENARIO_TRADE_DATE},
        reference_date=date.fromisoformat(SCENARIO_TRADE_DATE),
    )
    if preflight.get("blocking_errors"):
        raise AssertionError(f"共享场景筛选预检被阻断：{preflight['blocking_errors']}")
    return coordinator.run(preflight["preflight_id"])


def build_ready_current_flow(target_root: str | Path) -> dict[str, Any]:
    """READY_CURRENT_FLOW：当前已有能力的最小完整链路。

    本地股票库与行情 → 当前规则组合 → 正式筛选批次 → 已发生成交与账本投影。
    每一层都由正式业务入口产生，不手写任何派生结果。
    """
    scenario = build_empty_first_run(target_root)
    root = scenario["root"]
    _install_scenario_market(root)

    active_rule_set = _activate_simple_rule_set(
        root,
        name=SCENARIO_RULE_SET_NAME,
        rules=[PRIMARY_RULE_REF],
        secondary_rules=[SECONDARY_RULE_REF],
    )
    screening = _run_official_screening(root)

    # 在候选股票上通过正式成交写入入口依次形成首次买入、加仓、减仓。
    trades = [
        append_trade_record(
            root,
            {
                **trade,
                "symbol": CANDIDATE_SYMBOL,
                "stock_name": dict(SCENARIO_STOCKS)[CANDIDATE_SYMBOL],
            },
        )["record"]
        for trade in SCENARIO_TRADES
    ]

    return {
        "root": root,
        "scenario": READY_CURRENT_FLOW,
        "rule_catalog_seeded": scenario["rule_catalog_seeded"],
        "rule_set": active_rule_set,
        "screening": screening,
        "trades": trades,
    }


def screening_rows_by_symbol(screening: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rows = screening.get("row_results") or screening.get("rows") or []
    return {str(row["symbol"]): row for row in rows}


ZERO_CANDIDATE = "ZERO_CANDIDATE"

# 零候选用的严格组合：三只股票各自倒在不同的一条规则上。
#   rsi_healthy       挡住 000001.SZ 与 600001.SH
#   close_above_ma60  挡住 000001.SZ 与 600002.SH
# simple_all 要求一级规则全部满足，于是三只都不成为候选。
# 这是一次「规则很严，今天没有股票达标」的正常结果，
# 不是把规则调松、也不是拿接近门槛的股票冒充候选。
ZERO_CANDIDATE_RULE_REFS = ["close_above_ma60@1", "rsi_healthy@1"]
ZERO_CANDIDATE_RULE_SET_NAME = "共享场景零候选严格组合"


def build_zero_candidate(target_root: str | Path) -> dict[str, Any]:
    """ZERO_CANDIDATE：行情、股票库、规则组合都有效，筛选成功但最终候选为零。

    与 ``READY_CURRENT_FLOW`` 共用同一份行情事实，唯一差别是当前规则组合更严格。
    它既不是空实例，也不是数据缺口，更不是运行失败——是一次有效的成功批次。
    """
    scenario = build_empty_first_run(target_root)
    root = scenario["root"]
    _install_scenario_market(root)

    active_rule_set = _activate_simple_rule_set(
        root,
        name=ZERO_CANDIDATE_RULE_SET_NAME,
        rules=list(ZERO_CANDIDATE_RULE_REFS),
    )
    screening = _run_official_screening(root)

    rows = screening_rows_by_symbol(screening)
    candidates = [symbol for symbol, row in rows.items() if row.get("final_candidate")]
    if candidates:
        raise AssertionError(f"ZERO_CANDIDATE 不应产生最终候选，实际有：{candidates}")

    return {
        "root": root,
        "scenario": ZERO_CANDIDATE,
        "rule_catalog_seeded": scenario["rule_catalog_seeded"],
        "rule_set": active_rule_set,
        "screening": screening,
    }


LEDGER_INCOMPLETE = "LEDGER_INCOMPLETE"

# 只让候选股票的**第一段**历史来自一份缺列的券商导出：
# 没有成交后余额、剩余成本价、实现盈亏，也没有任何费用列。
# 其余两笔仍走正式手工录入入口，日期各不相同，不制造同日顺序歧义。
LEDGER_INCOMPLETE_IMPORT_FILENAME = "券商导出_缺成交后字段.csv"
LEDGER_INCOMPLETE_IMPORT_CSV = (
    "成交日期,成交时间,证券代码,证券名称,买卖方向,成交价格,成交数量\n"
    "2026-06-15,09:35:00,600001,测试甲,买入,49.19,1000\n"
)


def build_ledger_incomplete(target_root: str | Path) -> dict[str, Any]:
    """LEDGER_INCOMPLETE：在完整链路之上，只让一只股票的一段历史成交缺必要字段。

    行情、规则组合和筛选批次与 ``READY_CURRENT_FLOW`` 完全相同；
    唯一变化是那只股票的第一笔成交来自缺列的券商导出。
    缺失字段保持为空——不补零、不倒推——受影响的账本数字和汇总随之标记不完整，
    但原始成交（日期、时间、方向、价格、数量）继续如实显示。
    """
    scenario = build_empty_first_run(target_root)
    root = scenario["root"]
    _install_scenario_market(root)

    active_rule_set = _activate_simple_rule_set(
        root,
        name=SCENARIO_RULE_SET_NAME,
        rules=[PRIMARY_RULE_REF],
        secondary_rules=[SECONDARY_RULE_REF],
    )
    screening = _run_official_screening(root)

    # 一、缺列的历史段：走正式导入入口，源文件没给的字段一律留空。
    imported = import_personal_data(
        root,
        {
            "kind": "trades",
            "mode": "commit",
            "filename": LEDGER_INCOMPLETE_IMPORT_FILENAME,
            "csv_text": LEDGER_INCOMPLETE_IMPORT_CSV,
        },
    )

    # 二、其后两笔仍走正式手工录入入口，事实照常保存。
    trades = [
        append_trade_record(
            root,
            {
                **trade,
                "symbol": CANDIDATE_SYMBOL,
                "stock_name": dict(SCENARIO_STOCKS)[CANDIDATE_SYMBOL],
            },
        )["record"]
        for trade in SCENARIO_TRADES[1:]
    ]

    return {
        "root": root,
        "scenario": LEDGER_INCOMPLETE,
        "rule_catalog_seeded": scenario["rule_catalog_seeded"],
        "rule_set": active_rule_set,
        "screening": screening,
        "imported": imported,
        "trades": trades,
    }


TIME_ORDER_AMBIGUOUS = "TIME_ORDER_AMBIGUOUS"
TIME_ORDER_AMBIGUOUS_DATE = "2026-06-18"
TIME_ORDER_AMBIGUOUS_TRADES: tuple[dict[str, Any], ...] = (
    {
        "request_id": "scenario-order-known-0004",
        "trade_date": TIME_ORDER_AMBIGUOUS_DATE,
        "trade_time": "09:40",
        "side": "BUY",
        "price": 51.20,
        "shares": 100,
        "fee": 1.20,
    },
    {
        "request_id": "scenario-order-unknown-0005",
        "trade_date": TIME_ORDER_AMBIGUOUS_DATE,
        "trade_time": "",
        "side": "SELL",
        "price": 52.10,
        "shares": 50,
        "fee": 1.10,
    },
)


def build_time_order_ambiguous(target_root: str | Path) -> dict[str, Any]:
    """TIME_ORDER_AMBIGUOUS：复用完整链路，只增加同日已知＋未知时间成交。

    两笔都通过正式追加入口保存；第二笔时间未知，因此原始成交仍可见，
    但该股票不能选择最后成交或账本汇总依据。
    """
    scenario = build_ready_current_flow(target_root)
    root = scenario["root"]
    appended = [
        append_trade_record(
            root,
            {
                **trade,
                "symbol": CANDIDATE_SYMBOL,
                "stock_name": dict(SCENARIO_STOCKS)[CANDIDATE_SYMBOL],
            },
        )["record"]
        for trade in TIME_ORDER_AMBIGUOUS_TRADES
    ]
    return {
        **scenario,
        "scenario": TIME_ORDER_AMBIGUOUS,
        "ambiguous_trades": appended,
    }
