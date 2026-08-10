# Stock Project Query API

这是一个只读 FastAPI 查询层。它复用现有 MongoDB 数据，不创建任务队列、操作记录或新的业务集合，也不改变 Scheduler 和 Worker。

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
| `GET /api/v1/market/latest-trade-date` | `stock_daily_detail` | 返回 `qfq` 行情的最新交易日；空库时返回 `null` |
| `GET /api/v1/market/indices/realtime` | 腾讯/Sina 公共行情 + 进程内缓存 | 顶部五个大盘指数；交易时段每次请求实时获取，闭市展示最后缓存 |
| `GET /api/v1/stocks/realtime?codes=600519,000001` | `stock_realtime_minute_bars` | 批量读取现有个股实时行情；支持 `interval=1m/5m/15m/30m/60m/120m` |
| `GET /api/v1/stocks/{code}/realtime` | `stock_realtime_minute_bars` | 读取单只个股最新一条现有实时行情，不触发新的行情抓取 |
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
| `GET /api/v1/stats` | 多个集合 | 必要的数量、状态和最新日期统计 |

盘前分析列表会排除早报/复盘原文、来源备忘录等大字段；指定日期和 latest 接口返回完整 MongoDB 文档。博主作品列表会排除 ASR/OCR、提取文本和分析正文，详情接口返回这些字段。

## 示例

```bash
curl 'http://127.0.0.1:8100/api/v1/health'
curl 'http://127.0.0.1:8100/api/v1/market/latest-trade-date'
curl 'http://127.0.0.1:8100/api/v1/market/indices/realtime'
curl 'http://127.0.0.1:8100/api/v1/stocks/realtime?codes=600519,000001'
curl 'http://127.0.0.1:8100/api/v1/stocks/600519/realtime'
curl 'http://127.0.0.1:8100/api/v1/news?page=1&page_size=20&source=cls'
curl 'http://127.0.0.1:8100/api/v1/morning-analyses/2026-08-05'
curl 'http://127.0.0.1:8100/api/v1/stocks/002185/daily?start_date=2026-01-01'
curl 'http://127.0.0.1:8100/api/v1/creator-works/douyin%3A7666142391678622287'
```

OpenAPI 文档启动后位于 `/docs` 和 `/openapi.json`。
