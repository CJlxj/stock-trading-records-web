# 架构与多端边界

本仓库当前只有网页版一个视图。这份文档说明视图层与共享层的边界，以及以后加 iOS 版时应该落在哪里——目的是让第二个端加进来时不需要重构现有代码。

## 分层

```text
视图层（每个平台各自独立，互不引用）
└── webapp/          网页版，本机回环 127.0.0.1:8765

共享层（唯一的业务与事实来源，所有端共用）
├── src/             后端业务模块
│   └── panel_contract.py    服务端合同：接口能力、结果状态、纪律枚举
├── shared_ui/
│   └── panel-core.js        前端共用的纯业务语义选择器
├── rules/           规则库与规则组合
├── config/          策略配置
├── data/            本地行情（运行时生成）
├── records/         持仓与成交记录（运行时生成）
└── history/         SQLite 历史库（运行时生成）
```

## 隔离规则

1. **每个端拥有自己独立的** HTML/CSS、视图控制器、静态资源根目录、版本号和默认端口。
2. **视图之间不得互相引用或复制运行时状态。** 需要共用的纯业务判断统一放进 `shared_ui/panel-core.js`。
3. **`src/`、`rules/`、`config/`、`data/`、`records/`、`history/` 是唯一共享的业务与事实层**，避免生成两份互相漂移的数据。
4. **`src/panel_contract.py` 是服务端合同。** 候选状态、旧结果语义、证据含义和纪律枚举由它统一定义，任何端都不得自行重写。
5. **日常只启动一个数据所有者进程**，保证行情更新、规则写入、筛选和记录共用同一把锁。
6. **网页端只开放本机回环访问。** 任何面向局域网的端只能开放该端核心流程所需的最小白名单路由。

## 以后加 iOS 版时

iOS 版作为**顶层平级目录**加入，不动现有任何路径：

```text
stock-trading-records-web/
├── webapp/          现有网页版
├── ios/             ← 新增，与 webapp/ 平级
├── src/             不动，两端共享
├── shared_ui/       不动，两端共享
└── ...
```

具体形态还未确定，两条路都能落在这个结构里：

| 形态 | 落法 | 需要补的配置 |
|---|---|---|
| 原生 App（Swift + Xcode） | `ios/` 放 Xcode 工程，通过 HTTP 调用本机 Python 服务 | `.gitignore` 补 Xcode 段；CI 增加一个 macOS runner 的 job |
| PWA / 网页封装 | `ios/` 放独立的 HTML/CSS/JS + `manifest.webmanifest` + service worker，由同一 Python 进程另开端口托管 | 无额外语言链，沿用现有 CI |

无论哪种，都需要在 `src/panel_contract.py` 的 `PANEL_API_CAPABILITIES` 里为每个能力补上该端的路由声明（现有条目已经是 `{"method": ..., "web": ...}` 这种按端分列的形状，加一个键即可），并保持 `domain_contract_version` 与网页端一致。

**端口约定**：网页版 8765，第二个端从 8766 起顺延。

## 版本方案

仓库里有**三条互相独立**的版本轴，不要混为一谈：

| 版本轴 | 位置 | 当前值 | 什么时候升 |
|---|---|---|---|
| 各端产品版本 | 各端自己的 `VERSION.json` | web `1.0.1` | 该端的功能或界面发生变化 |
| 领域合同版本 | `src/panel_contract.py` 的 `DOMAIN_CONTRACT_VERSION` | `panel-domain-v1.8` | 候选状态、证据含义或纪律枚举的**语义**变化 |
| 后端 API 版本 | `VERSION.json` 的 `backend_api_version` | `0.9` | HTTP 接口的形状变化 |

网页版是 `1.0.1` 而合同是 `v1.8`，这不是笔误：产品版本在开源首发时重新计数，合同版本延续既有谱系，两者不在同一条轴上。

### 各端独立走版本

每个端有自己的 `VERSION.json`，互不牵连——只改 iOS 不需要给网页版升版本。iOS 版加入时从 `1.0.0` 起。

网页版的版本号出现在四个地方，改动时必须同步（`tests/test_webapp_structure.py` 和 `tests/test_webapp_server_security.py` 会断言它们一致）：

- `webapp/VERSION.json`
- `webapp/server.py` 的 `WEB_RELEASE`
- `webapp/static/index.html`：3 处 `?v=` 缓存参数、`data-app-version` 和页脚发布徽标
- 上述两个测试文件里的断言

### Git tag 命名

按端加前缀，避免两个端的 tag 互相打架：

```text
web-v1.0.1
ios-v1.0.0     （将来）
```

### 语义化版本

- **主版本**：破坏性变化，比如移除面板功能、改变已有记录文件的格式。
- **次版本**：新增功能，向后兼容。
- **修订版本**：修复缺陷，不改变行为契约。
