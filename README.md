下面这套可以作为你这个 **Stock_Project 的大方向和整体框架**。核心思路是：

> **采集、入库、任务流转、分析、结果沉淀、前端展示 / 通知** 全部分层，不要每个流程单独硬写。
> 所有流程都统一走：**定时器 / 事件触发 → 任务队列 → Worker → Repository → 分析服务 → 结果入库**。

---

# 一、项目整体定位

你的项目本质上不是单纯的爬虫项目，而是一个：

> **股票新闻 + 行情数据 + 筹码数据 + 板块情绪 + LLM 分析的自动化股票辅助分析系统**

它应该分成六大数据流：

| 编号 | 流程             | 类型        | 触发方式       | 主要目标           |
| -- | -------------- | --------- | ---------- | -------------- |
| 1  | 网站新闻爬虫入库       | 高频采集流     | 每 3 分钟轮询   | 获取新闻、清洗、去重、入库  |
| 2  | LLM 分析流        | 队列工作流     | 新闻入库后状态驱动  | 情绪、事件、题材、影响分析  |
| 3  | 每日 K 线分析       | 日终批处理     | 每天 15:30 后 | 分析趋势、涨跌结构、技术指标 |
| 4  | 每日 / 实时筹码分析    | 可日终 / 可实时 | 取决于数据源能力   | 判断筹码集中度、主力变化   |
| 5  | 日内实时行情分析       | 高频交易时段流   | 交易时段每 30 秒 | 实时监控异动、量价、板块联动 |
| 6  | 同花顺早报 / 晚间复盘分析 | 每日资讯批处理   | 每天 8:00    | 判断当日板块主线、市场情绪  |

---

# 二、推荐的总架构

建议整体架构如下：

```text
                ┌────────────────────┐
                │   APScheduler       │
                │ 定时任务调度中心     │
                └─────────┬──────────┘
                          │
                          ▼
                ┌────────────────────┐
                │   Task Queue        │
                │ Redis / Mongo Task  │
                └─────────┬──────────┘
                          │
                          ▼
                ┌────────────────────┐
                │     Workers         │
                │ 新闻 / LLM / 行情等  │
                └─────────┬──────────┘
                          │
          ┌───────────────┼────────────────┐
          ▼               ▼                ▼
 ┌────────────────┐ ┌────────────────┐ ┌────────────────┐
 │ Crawlers       │ │ Analysis       │ │ LLM Services   │
 │ 新闻/行情/筹码 │ │ 技术/筹码/板块 │ │ 情绪/事件/总结 │
 └────────┬───────┘ └────────┬───────┘ └────────┬───────┘
          │                  │                  │
          ▼                  ▼                  ▼
                ┌────────────────────┐
                │      MongoDB        │
                │ 原始数据 + 分析结果 │
                └─────────┬──────────┘
                          │
                          ▼
                ┌────────────────────┐
                │ FastAPI / Admin API │
                │ 查询、看板、通知接口 │
                └────────────────────┘
```

---

# 三、项目目录建议

可以按这个结构来拆：

```text
Stock_Project/
├── app/
│   ├── main.py
│   ├── config/
│   │   ├── settings.py
│   │   ├── logging.py
│   │   └── schedule_config.py
│   │
│   ├── models/
│   │   ├── news.py
│   │   ├── stock.py
│   │   ├── kline.py
│   │   ├── chip.py
│   │   ├── realtime.py
│   │   ├── report.py
│   │   └── task.py
│   │
│   ├── repositories/
│   │   ├── base.py
│   │   ├── news_repository.py
│   │   ├── kline_repository.py
│   │   ├── chip_repository.py
│   │   ├── realtime_repository.py
│   │   ├── report_repository.py
│   │   └── task_repository.py
│   │
│   ├── crawlers/
│   │   ├── base_news_crawler.py
│   │   ├── cls_crawler.py
│   │   ├── ths_report_crawler.py
│   │   ├── stock_kline_provider.py
│   │   ├── stock_chip_provider.py
│   │   └── stock_realtime_provider.py
│   │
│   ├── workflows/
│   │   ├── news_workflow.py
│   │   ├── llm_analysis_workflow.py
│   │   ├── kline_workflow.py
│   │   ├── chip_workflow.py
│   │   ├── realtime_workflow.py
│   │   └── ths_report_workflow.py
│   │
│   ├── services/
│   │   ├── dedup_service.py
│   │   ├── text_clean_service.py
│   │   ├── llm_service.py
│   │   ├── kline_analysis_service.py
│   │   ├── chip_analysis_service.py
│   │   ├── realtime_analysis_service.py
│   │   ├── sector_analysis_service.py
│   │   └── signal_service.py
│   │
│   ├── scheduler/
│   │   ├── scheduler_app.py
│   │   ├── jobs.py
│   │   └── locks.py
│   │
│   ├── workers/
│   │   ├── base_worker.py
│   │   ├── news_worker.py
│   │   ├── llm_worker.py
│   │   ├── kline_worker.py
│   │   ├── chip_worker.py
│   │   ├── realtime_worker.py
│   │   └── report_worker.py
│   │
│   ├── queues/
│   │   ├── task_queue.py
│   │   └── task_dispatcher.py
│   │
│   └── api/
│       ├── news_api.py
│       ├── stock_api.py
│       ├── signal_api.py
│       └── task_api.py
│
├── scripts/
│   ├── run_scheduler.py
│   ├── run_worker.py
│   └── init_indexes.py
│
└── tests/
```

---

# 四、六条流程的规范化设计

## 1. 新闻爬虫入库流程

### 触发方式

```text
每 3 分钟执行一次
```

### 流程

```text
定时任务
  ↓
创建 news_crawl_task
  ↓
调用多个新闻源 crawler
  ↓
统一清洗正文
  ↓
生成 event_id / content_hash
  ↓
去重 upsert 入库
  ↓
新数据进入 LLM_PENDING 状态
```

### 新闻状态建议

```python
class NewsStatus:
    FETCHED = "fetched"              # 已抓取
    DUPLICATED = "duplicated"        # 重复数据
    LLM_PENDING = "llm_pending"      # 待 LLM 分析
    LLM_PROCESSING = "llm_processing"
    LLM_DONE = "llm_done"
    LLM_FAILED = "llm_failed"
    ARCHIVED = "archived"
```

### 入库字段建议

```python
{
    "event_id": "严格去重ID",
    "source": "cls",
    "title": "新闻标题",
    "content": "清洗后的正文",
    "raw_content": "原始正文",
    "detail_url": "原文链接",
    "publish_time": 1710000000000,
    "fetch_time": 1710000000000,

    "content_hash": "正文hash",
    "dedup_key": "source + normalized_content_hash",

    "status": "llm_pending",

    "llm_analysis_status": {
        "sentiment": "pending",
        "event_extract": "pending"
    },

    "raw_payload": {}
}
```

这里你之前的思路是对的：
**正文入库应该存清洗后的正文，event_id 也应该基于严格清洗后的正文生成。**

建议：

```text
event_id = md5(source + normalized_content)
```

或者更稳一点：

```text
event_id = md5(source + publish_date + normalized_content)
```

如果你希望跨站点新闻也能去重，可以额外加一个：

```text
global_content_hash = md5(normalized_content)
```

这样：

| 字段                  | 作用              |
| ------------------- | --------------- |
| event_id            | 本站点内唯一事件 ID     |
| content_hash        | 当前 source 下正文去重 |
| global_content_hash | 跨 source 去重     |
| raw_payload         | 保留原始数据，方便回溯     |

---

## 2. LLM 分析工作流

你这个流程不建议做成定时任务，而应该做成：

> **状态驱动 + 队列消费**

也就是新闻入库后，如果状态是 `llm_pending`，就投递 LLM 分析任务。

### 流程

```text
新闻入库
  ↓
状态变成 llm_pending
  ↓
投递 LLM 分析任务
  ↓
LLM Worker 消费任务
  ↓
执行两个 LLM 分析
  ↓
结果写入 news_analysis
  ↓
新闻状态更新为 llm_done
```

### 两个 LLM 分析建议拆成独立 analysis_type

不要把两个 LLM 分析硬写死在一个字段里，建议抽象成：

```python
class LLMAnalysisType:
    SENTIMENT = "sentiment"              # 情绪 / 利好利空分析
    EVENT_EXTRACT = "event_extract"      # 事件抽取 / 股票关联 / 板块关联
    SUMMARY = "summary"                  # 可选：摘要
    STOCK_RELEVANCE = "stock_relevance"  # 可选：股票相关性
    SECTOR_RELEVANCE = "sector_relevance"
```

### LLM 分析任务表

```python
{
    "task_id": "uuid",
    "news_id": "xxx",
    "event_id": "xxx",
    "analysis_type": "sentiment",

    "status": "pending",
    "retry_count": 0,
    "max_retry": 3,

    "input_snapshot": {
        "title": "...",
        "content": "..."
    },

    "result": {},
    "error_msg": None,

    "created_at": 1710000000000,
    "updated_at": 1710000000000
}
```

### LLM 分析结果表

```python
{
    "event_id": "xxx",
    "news_id": "xxx",

    "sentiment": {
        "label": "positive / negative / neutral",
        "score": 0.82,
        "reason": "..."
    },

    "event_extract": {
        "related_stocks": [
            {
                "code": "000001",
                "name": "平安银行",
                "relevance_score": 0.7
            }
        ],
        "related_sectors": ["机器人", "算力", "半导体"],
        "event_type": "政策 / 业绩 / 并购 / 产业 / 风险",
        "impact_level": "high / medium / low"
    },

    "summary": "...",
    "created_at": 1710000000000
}
```

重点是：
**LLM 分析必须异步化、任务化、可重试、可回溯。**

---

## 3. 每日 K 线获取与趋势分析

### 触发方式

你说的是每天 15:30，我建议稍微保守一点：

```text
每天 15:35 或 15:40 执行
```

因为有些数据源收盘后不会立刻稳定。

### 流程

```text
15:35 定时触发
  ↓
获取今日所有股票日 K
  ↓
写入 stock_daily_kline
  ↓
计算技术指标
  ↓
生成趋势分析结果
  ↓
写入 stock_daily_analysis
```

### K 线表

```python
{
    "code": "000001",
    "name": "平安银行",
    "trade_date": "2026-05-15",

    "open": 10.1,
    "high": 10.5,
    "low": 10.0,
    "close": 10.3,
    "pre_close": 10.0,

    "volume": 123456789,
    "amount": 1234567890.0,
    "turnover_rate": 2.3,
    "pct_chg": 3.0,

    "created_at": 1710000000000
}
```

### 日线分析表

```python
{
    "code": "000001",
    "trade_date": "2026-05-15",

    "trend": {
        "ma5": 10.2,
        "ma10": 10.0,
        "ma20": 9.8,
        "ma60": 9.5,
        "trend_label": "uptrend / downtrend / shock",
        "breakout": True
    },

    "volume_price": {
        "volume_ratio": 1.8,
        "is_volume_breakout": True,
        "price_volume_match": "放量上涨"
    },

    "risk": {
        "is_high_position": False,
        "is_large_fall": False,
        "risk_level": "low"
    },

    "signal": {
        "score": 82,
        "tags": ["放量突破", "均线多头", "趋势转强"]
    }
}
```

### K 线分析建议做这些指标

第一阶段先做这些就够：

```text
MA5 / MA10 / MA20 / MA60
涨跌幅
成交量放大倍数
换手率
是否突破前高
是否跌破均线
是否连续上涨
是否连续下跌
是否放量长阳
是否缩量回调
```

不要一开始就堆太多复杂指标。
你这个项目更重要的是 **新闻 + 板块 + 行情 + 筹码的综合联动**。

---

## 4. 筹码数据分析流程

这个流程要分两套模式，因为取决于你能拿到的数据源能力。

---

## 4.1 如果只能拿到日终筹码

那它就和 K 线一样，是日终批处理。

### 触发方式

```text
每天 15:40 / 16:00 执行
```

### 流程

```text
获取每日筹码分布
  ↓
入库 chip_daily
  ↓
和昨日筹码对比
  ↓
分析筹码集中度变化
  ↓
写入 chip_daily_analysis
```

### 筹码原始表

```python
{
    "code": "000001",
    "trade_date": "2026-05-15",

    "avg_cost": 10.2,
    "profit_ratio": 0.68,
    "concentration_90": 12.5,
    "concentration_70": 8.2,

    "distribution": [
        {
            "price": 9.8,
            "ratio": 0.05
        },
        {
            "price": 10.0,
            "ratio": 0.08
        }
    ],

    "created_at": 1710000000000
}
```

### 筹码分析表

```python
{
    "code": "000001",
    "trade_date": "2026-05-15",

    "chip_change": {
        "avg_cost_change": 0.15,
        "profit_ratio_change": 0.08,
        "concentration_change": -1.2
    },

    "main_force_signal": {
        "label": "疑似吸筹 / 疑似派发 / 筹码稳定 / 筹码松动",
        "score": 75,
        "reason": "获利盘提升，同时筹码集中度提高，价格放量上涨"
    }
}
```

---

## 4.2 如果可以拿到实时筹码

那它就和实时行情一起进入交易时段轮询。

### 触发方式

```text
9:30 - 11:30
13:00 - 15:00
每 30 秒一次
```

### 流程

```text
交易时段定时任务
  ↓
获取实时行情
  ↓
获取实时筹码
  ↓
实时行情入库
  ↓
实时筹码入库
  ↓
计算日内筹码变化
  ↓
生成日内信号
```

### 重点建议

实时筹码数据通常会很重，建议你不要一开始就对全市场每 30 秒取完整筹码。

更合理的是分层：

```text
第一层：全市场实时行情扫描
第二层：筛选异动股票池
第三层：只对异动股票池获取筹码
```

例如：

```text
全市场股票数量：5000+
每 30 秒全部取筹码：压力太大
先筛选：
- 涨幅 > 3%
- 跌幅 > 3%
- 成交额排名前 300
- 量比 > 2
- 板块热度靠前
- 新闻相关股票
```

然后只对这些股票取筹码。

这样架构更稳。

---

## 5. 日内实时行情分析流程

这是你项目里最容易做大、也最容易出性能问题的一条流。

### 触发方式

```text
9:30 - 11:30
13:00 - 15:00
每 30 秒一次
```

### 流程

```text
定时触发
  ↓
获取全市场实时行情
  ↓
写入 realtime_snapshot
  ↓
和上一轮数据对比
  ↓
计算异动指标
  ↓
生成 stock_intraday_signal
  ↓
聚合板块热度
  ↓
写入 sector_intraday_signal
```

### 实时行情表

建议不要所有实时数据都长期保留到一个大表里，否则数据量会非常大。

可以分两层：

| 表                      | 作用            |
| ---------------------- | ------------- |
| realtime_snapshot      | 原始快照，短期保存     |
| stock_intraday_signal  | 计算后的日内信号，长期保存 |
| sector_intraday_signal | 板块聚合信号，长期保存   |

### 实时快照表

```python
{
    "code": "000001",
    "name": "平安银行",
    "trade_date": "2026-05-15",
    "snapshot_time": "2026-05-15 10:30:00",

    "price": 10.3,
    "pct_chg": 3.0,
    "volume": 123456789,
    "amount": 1234567890.0,
    "turnover_rate": 2.3,
    "volume_ratio": 1.8,

    "bid": [],
    "ask": [],

    "created_at": 1710000000000
}
```

### 日内信号表

```python
{
    "code": "000001",
    "trade_date": "2026-05-15",
    "signal_time": "2026-05-15 10:30:00",

    "signal_type": "volume_breakout",
    "signal_level": "high",

    "price_change_5m": 1.2,
    "amount_change_5m": 35000000,
    "volume_ratio": 2.5,

    "reason": "5分钟内快速放量上涨，成交额显著放大",

    "score": 86
}
```

### 日内分析第一阶段可以做这些

```text
5分钟涨速
10分钟涨速
成交额突增
量比突增
换手率异常
接近涨停
炸板
回封
快速跳水
板块内多股共振
新闻相关股票异动
```

---

## 6. 同花顺早报 / 晚间复盘分析流程

你这个流程非常重要，因为它可以作为当天板块判断的“先验信息”。

### 触发方式

```text
每天 8:00
```

### 流程

```text
8:00 定时任务
  ↓
获取同花顺早报
  ↓
获取前一日同花顺晚间复盘
  ↓
清洗正文
  ↓
LLM 分析板块、题材、利好方向
  ↓
生成 today_market_context
  ↓
供日内行情分析使用
```

### 早报 / 复盘表

```python
{
    "report_id": "xxx",
    "source": "ths",
    "report_type": "morning_report / evening_review",
    "trade_date": "2026-05-15",

    "title": "...",
    "content": "...",
    "raw_content": "...",

    "related_sectors": [],
    "related_stocks": [],

    "created_at": 1710000000000
}
```

### 当日市场上下文表

```python
{
    "trade_date": "2026-05-15",

    "market_sentiment": {
        "label": "strong / neutral / weak",
        "score": 72,
        "reason": "..."
    },

    "main_sectors": [
        {
            "sector_name": "机器人",
            "reason": "政策催化 + 多家公司公告",
            "priority": 1
        },
        {
            "sector_name": "半导体",
            "reason": "消息面催化",
            "priority": 2
        }
    ],

    "risk_factors": [
        "外围市场下跌",
        "高位题材分歧"
    ],

    "watch_stocks": [
        {
            "code": "xxx",
            "name": "xxx",
            "reason": "早报重点提及"
        }
    ]
}
```

然后你的实时行情分析可以引用它：

```text
如果某只股票属于 today_market_context.main_sectors，
并且日内出现放量上涨，
则信号分数提高。
```

这就是你项目里比较有价值的地方：
**不是单独看行情，而是把新闻、早报、板块、实时异动、筹码变化合起来看。**

---

# 五、统一任务系统设计

你这六条流程不要直接在 scheduler 里写死逻辑，建议统一变成 task。

## task 表

```python
{
    "task_id": "uuid",
    "task_type": "news_crawl / llm_analysis / kline_daily / chip_daily / realtime_scan / ths_report",

    "biz_key": "news_crawl:cls:2026-05-15-10:30",
    "status": "pending / running / success / failed / skipped",

    "payload": {},

    "retry_count": 0,
    "max_retry": 3,

    "locked_by": None,
    "locked_until": None,

    "error_msg": None,

    "created_at": 1710000000000,
    "updated_at": 1710000000000
}
```

## 为什么需要 biz_key？

防止重复任务。

例如新闻每 3 分钟执行一次：

```text
news_crawl:cls:2026-05-15-10:30
```

K 线每日任务：

```text
kline_daily:2026-05-15
```

实时行情任务：

```text
realtime_scan:2026-05-15-10:30:00
```

这样即使 scheduler 重启、重复触发，也不会重复执行。

---

# 六、调度计划建议

建议所有时间都按：

```text
Asia/Shanghai
```

因为你做的是 A 股市场，不要用服务器本地时区。

## 调度表

| 任务         | 时间                         | 频率           |
| ---------- | -------------------------- | ------------ |
| 新闻爬虫       | 全天或 7:00 - 23:00           | 每 3 分钟       |
| LLM 分析     | 常驻 Worker                  | 队列驱动         |
| 日 K 获取     | 15:35 / 15:40              | 每个交易日一次      |
| 日 K 分析     | K 线入库后                     | 事件驱动         |
| 筹码日终       | 15:40 / 16:00              | 每个交易日一次      |
| 实时行情       | 9:30 - 11:30，13:00 - 15:00 | 每 30 秒       |
| 实时筹码       | 如果支持实时                     | 每 30 秒或只对异动池 |
| 同花顺早报 / 复盘 | 8:00                       | 每个交易日一次      |
| 清理任务       | 23:30                      | 每日一次         |
| 补偿任务       | 16:30 / 20:00              | 每日一次         |

---

# 七、核心数据流之间的关系

你最终要做的是综合分析，所以几条数据流不要孤立。

可以这么串起来：

```text
同花顺早报 / 晚间复盘
        ↓
生成今日重点板块、重点题材、风险因素
        ↓
实时行情扫描
        ↓
发现板块 / 个股异动
        ↓
结合新闻 LLM 分析
        ↓
结合 K 线趋势
        ↓
结合筹码变化
        ↓
生成综合信号
```

最终可以生成一个统一的 `stock_signal` 表。

## 综合信号表

```python
{
    "code": "000001",
    "name": "平安银行",
    "trade_date": "2026-05-15",
    "signal_time": "2026-05-15 10:30:00",

    "signal_type": "综合异动",
    "signal_level": "high",

    "score": 88,

    "components": {
        "news_score": 70,
        "kline_score": 82,
        "chip_score": 76,
        "realtime_score": 90,
        "sector_score": 85
    },

    "reasons": [
        "属于早报重点板块",
        "日内放量上涨",
        "成交额快速放大",
        "K线处于多头趋势",
        "筹码集中度提升"
    ],

    "risk_flags": [
        "短线涨幅较高"
    ]
}
```

---

# 八、推荐的第一阶段实现顺序

不要一开始六条线全铺开，否则很容易乱。

我建议按这个顺序做：

## 第一阶段：基础框架

先完成：

```text
Mongo Repository 基类
任务表 task
APScheduler 调度器
Worker 消费模型
日志系统
配置系统
去重服务
文本清洗服务
```

这一步是地基。

---

## 第二阶段：新闻爬虫 + 新闻入库

先把第 1 条流程跑通：

```text
新闻源 crawler
正文清洗
标题兜底
event_id 去重
Mongo 入库
状态流转
```

这一阶段完成后，你的项目就已经有稳定数据源了。

---

## 第三阶段：LLM 分析流

然后接第 2 条流程：

```text
llm_pending 查询
创建 LLM 分析任务
LLM Worker 消费
两个分析任务独立执行
结果写入 news_analysis
状态更新
失败重试
```

注意：LLM 这块一定要有：

```text
JSON 解析兜底
重试
失败原因记录
输入快照
输出原文保存
```

---

## 第四阶段：K 线日终分析

实现第 3 条流程：

```text
获取全市场日 K
入库
计算 MA / 涨跌幅 / 成交量 / 趋势标签
生成 stock_daily_analysis
```

这条流程相对稳定，适合第三步之后做。

---

## 第五阶段：同花顺早报 / 晚间复盘

实现第 6 条流程：

```text
早报爬虫
晚间复盘爬虫
LLM 提取板块主线
生成 today_market_context
```

这个流程会给实时行情分析提供上下文。

---

## 第六阶段：实时行情扫描

实现第 5 条流程：

```text
交易时段每 30 秒获取行情
计算涨速、量比、成交额变化
生成日内异动信号
聚合板块热度
```

这一步开始会涉及性能和数据量，需要谨慎。

---

## 第七阶段：筹码分析

最后接第 4 条流程。

原因是筹码数据源不确定性比较高，先抽象接口：

```python
class ChipProvider:
    async def get_daily_chip(self, code: str, trade_date: str):
        ...

    async def get_realtime_chip(self, code: str):
        ...

    def support_realtime(self) -> bool:
        ...
```

如果支持实时，就接入实时流程；
如果只支持日终，就接入日终流程。

---

# 九、技术选型建议

## 后端

```text
FastAPI
```

负责：

```text
数据查询接口
任务状态接口
信号查询接口
管理后台接口
```

## 调度

```text
APScheduler
```

你之前已经有独立 APScheduler 调度器的想法，这里非常适合。

建议不要和 FastAPI 主服务强绑定，最好是：

```text
FastAPI 进程
Scheduler 进程
Worker 进程
```

分开跑。

## 数据库

```text
MongoDB
```

适合你这种：

```text
新闻文本
LLM 结果
行情快照
结构不完全固定的分析结果
```

## 缓存 / 锁 / 队列

可以先用：

```text
Redis
```

负责：

```text
分布式锁
短期缓存
任务队列
实时行情中间状态
```

如果你暂时不想引 Redis，也可以先用 Mongo 做 task queue，但后面实时行情高频起来，Redis 会更合适。

---

# 十、关键工程原则

## 1. 每个流程必须幂等

任何任务重复执行，都不能造成重复数据。

例如：

```text
news event_id 唯一
kline code + trade_date 唯一
chip code + trade_date 唯一
realtime code + snapshot_time 唯一
report source + report_type + trade_date 唯一
```

Mongo 建唯一索引。

---

## 2. 原始数据和分析数据分开

不要把所有东西塞到一个表里。

建议：

```text
原始新闻表
新闻分析表

原始 K 线表
K 线分析表

原始筹码表
筹码分析表

实时快照表
实时信号表
```

这样后面重跑分析不会污染原始数据。

---

## 3. 状态流转必须明确

所有异步流程都要有：

```text
pending
running
success
failed
skipped
```

不要只靠有没有字段来判断。

---

## 4. LLM 输入输出必须留痕

LLM 分析结果要保存：

```text
input_snapshot
raw_output
parsed_output
error_msg
model_name
prompt_version
```

否则后面很难排查。

---

## 5. 实时行情不要一开始就全量重分析

实时数据很大，建议：

```text
全量采集
轻量计算
重点股票深度分析
```

也就是：

```text
5000 只股票都拿基础行情
只对异动股票做更重的分析
```

---

# 十一、最终推荐的大框架一句话总结

你的项目可以按这个方向定型：

> 以 APScheduler 作为统一调度入口，以 MongoDB 作为核心数据仓库，以 Task Queue + Worker 作为任务执行层，以新闻、行情、筹码、早报复盘作为多源输入，以 LLM 和规则指标作为分析引擎，最终沉淀个股信号、板块信号和每日市场上下文。

最小可落地版本可以先做成：

```text
新闻爬虫
  ↓
新闻去重入库
  ↓
LLM 分析
  ↓
每日 K 线分析
  ↓
早报 / 复盘板块分析
  ↓
实时行情异动分析
  ↓
综合信号表
```

筹码流先预留接口，等数据源确定后再决定是：

```text
日终筹码分析
```

还是：

```text
交易时段实时筹码分析
```

这样架构不会推翻重做。
