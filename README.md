# 股票规则 Web 面板

[![web-tests](https://github.com/CJlxj/stock-trading-records-web/actions/workflows/tests.yml/badge.svg)](https://github.com/CJlxj/stock-trading-records-web/actions/workflows/tests.yml)

> **免责声明**：本项目只做本地行情管理与规则化候选筛选，技术信号不构成任何投资建议或买卖依据。据此操作，风险自负。

这是从原项目整理出的独立 Web-only 副本。它保留当前网页面板的四项功能：

- 本地股票数据管理；
- 规则库与规则组合管理；
- 收盘后候选筛选；
- 已发生操作记录。

本仓库不包含 Mobile 客户端、提示词、工作流、回测、研究报告、发布快照，也不包含真实行情、个人自选、持仓、成交和历史数据库。

## 目录结构

```text
stock-trading-records-web/
├── webapp/                  Web 服务与静态前端
│   ├── server.py            HTTP 服务入口，只绑定本机回环地址
│   ├── bootstrap.py         首屏数据装配
│   ├── legacy_review_routes.py
│   ├── VERSION.json
│   └── static/              页面、样式与前端模块
│       └── modules/         data / rules / screening / records 四个功能模块
│
├── shared_ui/
│   └── panel-core.js        前端共用的纯业务语义合同
│
├── src/                     后端业务层
│   ├── market_data.py       行情抓取、导入与落盘
│   ├── io_loader.py         本地行情读取与 OHLCV 规范化
│   ├── market_screening.py  全市场筛选
│   ├── screening/           数据清单、筛选协调与排序
│   ├── stock_catalog.py     全市场股票目录
│   ├── stock_library.py     本地股票池
│   ├── personal_data.py     持仓与成交记录
│   ├── panel_contract.py    前后端契约与状态语义
│   ├── rules/               规则 DSL、规则库、标签与版本管理
│   └── ...                  指标、复盘、仓位等其余模块
│
├── rules/
│   ├── catalog/builtin/     出厂规则，首次启动导入本地规则库
│   ├── catalog/user/        本地可编辑规则（运行时生成，默认空）
│   ├── custom/              自定义规则插件示例
│   └── rule_sets/           用户规则组合（运行时生成，默认空）
│
├── config/
│   └── strategy_rules.yaml  最小默认策略配置
│
├── docs/
│   └── ARCHITECTURE.md      多端边界与版本方案
│
├── tests/                   Web 与业务层测试
│   └── market_fixture.py    确定性合成行情
│
├── data/                    本地行情（运行时生成，默认空）
├── records/                 本地操作记录（运行时生成，默认空）
├── history/                 SQLite 历史库与导入备份（运行时生成，默认空）
│
└── .github/workflows/
    └── tests.yml            GitHub Actions：单元测试与 JS 语法检查
```

`webapp/` 是视图层，`src/` + `shared_ui/` + 数据目录是共享的业务与事实层。以后的 iOS 版会作为与 `webapp/` 平级的顶层目录 `ios/` 加入，共享层不动——边界规则与两种实现形态见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。

## 数据存放位置

所有路径都相对项目根目录（由 `webapp/server.py` 的 `PROJECT_ROOT` 解析）。这三个运行时目录已被 `.gitignore` 排除，不会进入版本库。

### 行情 K 线

```text
data/<股票代码>/raw/<周期>/<代码>_<周期>_<最新日期>.csv
data/<股票代码>/raw/<周期>/<代码>_<周期>_<最新日期>.meta.json
```

例：`data/600406.SH/raw/day/600406.SH_day_20260813.csv`

同名 `.meta.json` 记录数据源、复权方式与抓取时间。写入见 `src/market_data.py`（先写临时文件再原子替换），读取见 `src/io_loader.py` 的 `find_latest_csv()`——在同一目录下按「CSV 内最新交易日 + 文件修改时间」排序取最新一份。面板上有哪些股票，取决于 `data/` 下有哪些子目录。

### 全市场股票名录

```text
data/universe/a_share_snapshot.csv
data/universe/a_share_snapshot.meta.json
```

### 其他本地数据

| 内容 | 路径 |
| --- | --- |
| 持仓快照 / 成交记录 | `records/positions_snapshot.csv`、`records/my_trades.csv` |
| 筛选历史库 | `history/screenings.sqlite3` |
| 复盘历史库 | `history/reviews.sqlite3` |
| 导入与成交备份 | `history/imports/`、`history/trade_records/` |
| 本地股票池 | `config/local_stock_library.csv` |

## 环境

- Python 3.12+
- Node.js 20+（仅用于 JavaScript 语法和共享合同测试）

安装核心依赖：

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

需要 AKShare 数据源时再安装可选依赖：

```bash
python -m pip install -r requirements-market-data.txt
```

## 启动

```bash
python webapp/server.py --port 8765
```

浏览器打开 `http://127.0.0.1:8765/`。服务只允许绑定本机回环地址。

首次启动会把 `rules/catalog/builtin/` 中的出厂规则导入本地可编辑规则库。运行时生成的行情、记录、数据库和个人规则状态已由 `.gitignore` 排除。

## 测试

```bash
python -m unittest discover -s tests -p 'test_*.py'
```

JavaScript 语法检查：

```bash
find webapp/static shared_ui -name '*.js' -print0 | xargs -0 -n1 node --check
```

## 测试数据

仓库故意不附带真实行情和个人数据。可以从页面「管理数据」更新或导入行情。

单元测试使用 `tests/market_fixture.py` 中的确定性合成数据，绝大多数在临时目录运行。例外是 `tests/test_webapp_server_security.py` 的路由连通性用例——它以真实项目根跑通所有已声明路由，会在 `history/` 下留下一个 `screenings.sqlite3`。该文件已被 `.gitignore` 排除，可以直接删除。

## 版本与发布

当前网页版 **v1.0.1**（见 [CHANGELOG.md](CHANGELOG.md)）。

仓库里有三条互相独立的版本轴：各端产品版本（`webapp/VERSION.json`）、领域合同版本（`src/panel_contract.py` 的 `panel-domain-v1.8`）、后端 API 版本（`0.9`）。各端独立走版本，git tag 按端加前缀（`web-v1.0.1`，将来 `ios-v1.0.0`）。完整规则见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#版本方案)。

## 许可

[MIT](LICENSE)
