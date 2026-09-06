# Stock Project Query API

这是一个只读 FastAPI 查询层。它复用现有 MongoDB 数据和实时行情抓取器，不创建任务队列、操作记录或新的业务集合，也不改变 Scheduler 和 Worker。

## 启动

在 `MyAgent` 环境中执行：

```bash
uvicorn app.api.app:create_app --factory --host 0.0.0.0 --port 8100
```

API 使用 `.local/env/.env` 中的 `MONGO_URI` 和 `MONGO_DB_NAME`。服务生命周期内只创建一个 Motor Client，并在退出时关闭。

## 响应约定

列表接口统一返回：

```json
{"items": [], "total": 0, "page": 1, "page_size": 50}
```

`page_size` 范围是 1 到 200。详情接口返回 `{"data": {...}}`。MongoDB `_id` 不会返回，`ObjectId`、日期和时间会递归转换为 JSON 字符串。

## 接口

| 路径 | MongoDB 集合/来源 | 说明 |
| --- | --- | --- |
| `GET /api/v1/health` | MongoDB | 连接健康检查 |
| `GET /api/v1/news` | `news_data` | 新闻分页；支持 `source`、`status`、`start_ts`、`end_ts`、`sector_name`、`company`、`keyword` |
| `GET /api/v1/news/{event_id}` | `news_data` | 新闻完整详情 |
| `GET /api/v1/news-rankings` | `news_ranking_snapshots` | 快照列表；支持 `biz_date` |
| `GET /api/v1/news-rankings/latest` | `news_ranking_snapshots` | 最新完成快照 |
| `GET /api/v1/news-rankings/{snapshot_id}` | `news_ranking_snapshots` | 快照完整详情 |
| `GET /api/v1/morning-analyses` | `daily_market_analysis` | 报告列表；支持 `start_date`、`end_date`、`data_quality` |
| `GET /api/v1/morning-analyses/latest` | `daily_market_analysis` | 最新完整报告 |
| `GET /api/v1/morning-analyses/{analysis_date}` | `daily_market_analysis` | 指定日期完整报告 |
| `GET /api/v1/market/latest-trade-date` | `stock_daily_detail`、`daily_market_analysis` | 分别返回 `latest_trade_date` 和 `latest_analysis_date`；盘前页面必须使用后者 |
| `GET /api/v1/market/indices/realtime` | 腾讯/Sina 公共行情 + 进程内缓存 | 顶部五个大盘指数；交易时段每次请求实时获取，闭市展示最后缓存 |
| `GET /api/v1/stocks/realtime?codes=600519,000001` | 现有腾讯/Sina 实时抓取器 | 批量获取个股当前最新价格，不设置分钟缓存 |
| `GET /api/v1/stocks/{code}/realtime` | 现有腾讯/Sina 实时抓取器 | 获取单只个股当前最新价格 |
| `GET /api/v1/stocks/{code}/intraday` | `stock_realtime_minute_bars` | 返回指定交易日全部分时 K 线；默认今天和 `interval=1m` |
| `GET /api/v1/stocks` | `stock_daily_detail` | 聚合每只股票最新一条日线；支持 `keyword`、`adjust` |
| `GET /api/v1/stocks/{code}/daily` | `stock_daily_detail` | 单只股票日线分页；支持日期范围和 `adjust` |
| `GET /api/v1/stocks/{code}/daily/{trade_date}` | `stock_daily_detail` | 单只股票单日完整详情 |
| `GET /api/v1/stock-daily/{trade_date}` | `stock_daily_detail` | 全市场单日分页；排序字段有白名单 |
| `GET /api/v1/creator-accounts` | 代码注册表 | 博主账号公开配置，不读取新集合 |
| `GET /api/v1/creator-accounts/{account_key}` | 代码注册表 | 账号详情，例如 `douyin:203775400` |
| `GET /api/v1/creator-works` | `creator_works` | 作品分页；支持账号、平台、状态、A 股相关性、时间和关键词 |
| `GET /api/v1/creator-works/{work_key}` | `creator_works` | 作品完整详情，例如 `douyin:7666142391678622287` |
| `GET /api/v1/creator-opinion-analyses` | `creator_opinion_analyses` | 博主观点汇总分页；支持姓名和最低准确率 |
| `GET /api/v1/creator-opinion-analyses/{creator_id}` | `creator_opinion_analyses` | 观点汇总详情；`creator_id` 来自 MongoDB `_id` |
| `GET /api/v1/quant/strategies` | 公开目录 | 策略展示编号、名称和执行类型 |
| `GET /api/v1/quant/strategies/{strategy_id}/accounts` | `quant_daily_results` | 曾买入的逐股独立账户，含已清仓账户 |
| `GET /api/v1/quant/strategies/{strategy_id}/preselections` | `quant_daily_results` | 买入预选及原因 |
| `GET /api/v1/quant/strategies/{strategy_id}/sell-candidates` | `quant_daily_results` | 卖出候选及原因 |
| `GET /api/v1/quant/strategies/{strategy_id}/exit-decisions` | `quant_daily_results` | 持有/延期/退出判断明细 |
| `GET /api/v1/quant/strategies/{strategy_id}/overview` | `quant_daily_results` | 资产、收益、当日成交金额与费用；支持 `trade_date` |
| `GET /api/v1/quant/strategies/{strategy_id}/performance` | `quant_daily_results` | 日度收益曲线；支持起止日期与分页 |
| `GET /api/v1/quant/strategies/{strategy_id}/closed-trades` | `quant_daily_results` | 指定交易日平仓明细 |
| `GET /api/v1/quant/daily-results/latest` | `quant_daily_results` | 最新交易日的公开观察、信号、成交、持仓、平仓和汇总 |
| `GET /api/v1/quant/daily-results/{trade_date}` | `quant_daily_results` | 指定交易日的公开业务快照 |
| `GET /api/v1/quant/intraday/latest` | `quant_daily_results` | 最新影子盘运行状态、汇总、观察状态计数和最近20条信号 |
| `GET /api/v1/quant/signals` | `quant_daily_results` | 信号分页；支持 `trade_date`、`action=buy|sell`、`status` |
| `GET /api/v1/quant/observations` | `quant_daily_results` | 观察池分页；支持 `trade_date`、`action=buy|sell|hold`、`state` |
| `GET /api/v1/quant/strategies/{strategy_id}/intraday/latest` | `quant_daily_results` | 指定策略最新运行状态和盈亏汇总 |
| `GET /api/v1/quant/strategies/{strategy_id}/observations` | `quant_daily_results` | 指定策略独立观察池 |
| `GET /api/v1/quant/strategies/{strategy_id}/signals` | `quant_daily_results` | 指定策略独立信号池 |
| `GET /api/v1/quant/strategies/{strategy_id}/executions` | `quant_daily_results` | 已成交模拟买卖；支持 `code`＋起止日期、分页和历史版本校验 |
| `GET /api/v1/quant/strategies/{strategy_id}/holdings` | `quant_daily_results` | 指定策略独立持仓及实时盈亏 |
| `GET /api/v1/quant/strategies/{strategy_id}/daily-results/latest` | `quant_daily_results` | 指定策略最新完整快照 |
| `GET /api/v1/quant/strategies/{strategy_id}/daily-results/{trade_date}` | `quant_daily_results` | 指定策略和交易日完整快照 |
| `GET /api/v1/stats` | 多个集合 | 必要的数量、状态和最新日期统计 |

盘前分析列表会排除早报/复盘原文、来源备忘录等大字段；指定日期和 latest 接口返回完整 MongoDB 文档。博主作品列表会排除 ASR/OCR、提取文本和分析正文，详情接口返回这些字段。

## 示例

```bash
curl 'http://127.0.0.1:8100/api/v1/health'
curl 'http://127.0.0.1:8100/api/v1/market/latest-trade-date'
curl 'http://127.0.0.1:8100/api/v1/market/indices/realtime'
curl 'http://127.0.0.1:8100/api/v1/stocks/realtime?codes=600519,000001'
curl 'http://127.0.0.1:8100/api/v1/stocks/600519/realtime'
curl 'http://127.0.0.1:8100/api/v1/stocks/600519/intraday?trade_date=2026-08-11&interval=1m'
curl 'http://127.0.0.1:8100/api/v1/news?page=1&page_size=20&source=cls'
curl 'http://127.0.0.1:8100/api/v1/morning-analyses/2026-08-05'
curl 'http://127.0.0.1:8100/api/v1/quant/daily-results/latest'
curl 'http://127.0.0.1:8100/api/v1/quant/intraday/latest'
curl 'http://127.0.0.1:8100/api/v1/quant/signals?trade_date=2026-09-03&action=buy&page=1&page_size=50'
curl 'http://127.0.0.1:8100/api/v1/quant/observations?trade_date=2026-09-03&state=watching&page=1&page_size=50'
curl 'http://127.0.0.1:8100/api/v1/quant/strategies/strategy_1/holdings?page=1&page_size=50'
curl 'http://127.0.0.1:8100/api/v1/stocks/002185/daily?start_date=2026-01-01'
curl 'http://127.0.0.1:8100/api/v1/creator-works/douyin%3A7666142391678622287'
```

OpenAPI 文档启动后位于 `/docs` 和 `/openapi.json`。

## 量化前端契约

前端使用公开编号 `strategy_1` 和名称“策略1”。所有量化路径（包含每日快照和旧的
无策略路径）只对真实策略名称和存储标识匿名化；v1.2完整提供参数、指标、判断原因、细分状态、
执行尝试和运行数据。逐股账本提取为独立接口，不直接返回恢复状态的冗余结构。具体字段、示例、状态及分页规则见[量化前端接口](量化前端接口.md)。

完整类型由 `/openapi.json` 提供，可生成前端类型。响应携带 `trade_date`、`snapshot_id`、
`execution_kind`。先请求总览，再将其日期和快照编号传给分页接口；快照变化返回409，
前端刷新总览后重试。`executions` 区间模式使用自身返回的 `history_version` 校验后续分页，
不使用总览的单日快照编号；版本变化返回409后重新获取整个区间。`performance` 返回已有交易日，不填充周末或缺失数据。

此版本调整了量化响应结构：原内部策略路径编号不再接受；前端统一改为 `strategy_1`，
无策略路径默认同一策略。通用观察与信号筛选保留原枚举，新增 `state_detail` / `status_detail` 可筛选细分状态。
旧路径保留。v1.2保留完整业务数据；总金额和收益率仅计入截至所选日期曾经买入的账户，已清仓账户继续计入。

金额单位为人民币元，股数为股，所有 `*_return` 是小数比例，`0.0123` 表示1.23%。
当前记录起点为2026-08-20；真实行情补录与实时采集通过 `recording.mode` 区分。
当前成交属于 `shadow_simulation`（模拟成交），不能显示成券商实盘成交。
