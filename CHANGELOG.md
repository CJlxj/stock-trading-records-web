# 更新日志

本文件记录网页版（channel `web`）的变化。版本方案见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#版本方案)。

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循语义化版本。

## [1.0.1] - 2026-08-27

网页版迁入独立仓库 `stock-trading-records-web`，功能与行为没有变化。

### 变更

- 仓库地址迁至 `CJlxj/stock-trading-records-web`，同步 README 的 CI badge、目录结构树根名与本文件的发布链接。
- 网页版版本 1.0.0 → 1.0.1，同步 `webapp/VERSION.json`、`webapp/server.py` 的 `WEB_RELEASE`、`webapp/static/index.html` 六处与两个测试文件的断言。
- 领域合同版本 `panel-domain-v1.8` 与后端 API 版本 `0.9` 不随之变动——两者不在同一条版本轴上。

## 1.0.0 - 2026-08-27

首个公开版本。从原有私有项目中抽取出的独立网页版，只保留面板的四项核心功能。

### 包含

- **本地股票数据管理**：行情抓取、手工导入、落盘与本地数据清单。
- **规则库与规则组合管理**：规则 DSL、出厂规则、自定义规则插件、标签与版本管理。
- **收盘后候选筛选**：按当前规则组合生成候选，附判定证据。
- **已发生操作记录**：持仓快照与成交记录的导入和查看。

### 不包含

Mobile 客户端、提示词、工作流、回测、研究报告、发布快照，以及任何真实行情、个人自选、持仓、成交和历史数据库。

### 说明

- 服务只绑定本机回环地址 `127.0.0.1`。
- 领域合同版本为 `panel-domain-v1.8`，后端 API 版本为 `0.9`——这两条版本轴延续既有谱系，未随产品版本重置。
- 248 个单元测试与 JavaScript 语法检查在 CI 中执行。
- 该版本未在本仓库打 tag，独立仓库的发布从 1.0.1 起。

[1.0.1]: https://github.com/CJlxj/stock-trading-records-web/releases/tag/web-v1.0.1
