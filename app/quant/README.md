# 量化模块

量化模块只实现一个正式策略：

```text
策略ID   provisional_daily_macd_3m_v1
版本     2.0.0
名称     原MACD＋ADX14买入＋E2延迟卖出
```

完整规则见 `strategies/provisional_daily_macd_3m/README.md`。

## 固定规则

- 每只股票独立10万元，日线MACD固定为 `(20, 100, 30)`，预热130根日线；
- 昨日绿柱继续变长且空仓，今日进入买入观察池；
- 昨日红柱继续变长、持仓且满足T+1，今日进入卖出观察池；
- 每根完整3分钟K线收盘后，从昨日冻结的日线EMA状态试算今日临时日线MACD；
- 临时柱保持原符号并相对昨日缩短至少1%，连续3根成立后产生信号；
- 原MACD买点再通过ADX14门控：ADX14[t−1]≥20且ADX14[t−1]>ADX14[t−4]，缺失或不满足则拒绝；
- 原卖点出现后，仅ADX14仍强、估算扣费清算净收益>0、临时DIF>0且H>0时E2延期；任一失效立即提交退出，不等下一原卖点；
- 已提交退出不能撤销；延期及待卖状态跨日恢复。不启用E1提前退出；
- 信号在下一根3分钟K线开盘撮合；
- 双边佣金万一、卖出印花税万五、双边不利滑点万五；
- 涨停不买，跌停不卖并顺延，买入当日不能卖出；
- 不使用额外止盈、止损、金叉、死叉或涨停次日兜底规则。

## 目录

```text
app/quant/
├── core/                         # 行情、指标、金额精度
├── data/market_data.py           # 独立量化日线和3分钟集合名
├── runtime/
│   ├── daily_macd.py             # 临时日线MACD计算
│   ├── daily_flow.py             # 观察池、成交、持仓及前端结果
│   └── live.py                   # 盘中3分钟聚合和确定性重放
├── strategies/
│   └── provisional_daily_macd_3m/# 唯一正式策略及固定参数
└── cli/
    ├── replay_stock.py           # 单股票完整审计回放
    ├── replay_sample.py          # 随机样本独立账户回放
    └── replay_factor_experiments.py # MACD不变的单因子后置筛选研究
```

## 历史回放数据

单股和抽样历史回放入口读取独立量化历史：

```text
日线    stock_history_daily_bars_ths_forward_stage
3分钟   stock_history_3m_bars_ths_forward_stage
复权    qfq
```

3分钟历史由 `app/manually_execute_script/materialize_quant_3m_history.py`
从独立量化1分钟历史生成，每个完整交易日80根。

## 正式影子盘

正式运行只给出观察、信号和模拟成交，不连接券商、不提交委托：

```text
09:20       冻结前一交易日日线状态和当天观察池
09:30-15:00 从 stock_realtime_minute_bars 流式读取1分钟柱
每3分钟     聚合完整3分钟柱，从开盘不可变状态确定性重放
15:05       完成最后一根数据稳定检查并冻结当日结果
```

盘前日线读取 `stock_daily_detail` 的 `qfq` 数据；盘中行情是未复权价格，使用行情源
的前收盘价与昨日 qfq 收盘价之比缩放冻结的 EMA、DEA 和柱体状态。若前收盘价缺失，
该股票只展示数据异常，不生成信号和模拟成交。

每日快照按 `(strategy_id, trade_date)` 幂等覆盖 `quant_daily_results`。每个策略的
观察、信号、模拟成交、持仓和盈亏独立，策略之间只共享行情数据和公开字段协议。
连续记录从2026-08-20开始，每股独立10万元；期初池按2026-08-19已知股票冻结，不在后续加入新股。
现金和累计已实现盈亏随本股账户跨日保存。汇总本金包含无交易账户，持仓不强制卖出。
内部保存不可变开盘状态、ADX端点、E2状态及逐股账户，Scheduler 重启后会
从开盘重新重放，因此相同分钟输入必然得到相同信号、成交和持仓。内部恢复字段不会
通过 API 返回。启动时顺序补录缺失的已结束交易日，不能跳日重置账户。
`recording.mode=historical_replay`标识事后用真实行情补录，`live`标识实时运行；
`recording.computed_at`记录实际计算时间，成交始终为影子模拟。
9月3日、4日旧MACD记录保存在`quant_daily_results_strategy_archive`，新版仍沿用稳定策略ID，版本为2.0.0。

### 内存边界

- 日线和分钟线均使用 MongoDB 流式游标，批大小固定为1000；日线只保留当前一只
  股票的序列，一分钟原始数据也只保留当前一只股票，聚合后最多80根；
- 量化跟踪代码硬上限2000，只保留最多16万根三分钟柱；单股日线最多10000根、
  单日一分钟原始行最多300条，越界立即失败并保留上一版快照；
- 全市场实时采集每250只股票分块写库，已完成的多周期聚合柱立即释放；全市场代码、
  未刷分钟柱和聚合柱都有硬上限；
- 单个顺延信号只保存最近10次撮合尝试，同时保留累计尝试次数，防止跌停顺延导致
  MongoDB 文档和进程内存持续增长。
- 一分钟持仓估值直接通过 `code + interval + timestamp` 索引取每只股票最新一行，
  每批最多并发50只，不扫描或缓存整日持仓分钟线。

任务失败时，上一版量化结果不会被半成品覆盖；`runtime.data_status` 会变为 `error`，
并记录截断后的 `last_error` 和 `last_error_at`，下一个三分钟周期自动重试。

## 回放

正式`replay_stock`和`replay_sample`命令固定启用ADX14/E2。底层`replay()`仍是研究对照引擎，
只有研究入口显式传入候选规则；旧实验与已交付数据保持历史含义。


单股票：

```bash
PYTHONPATH=. python -m app.quant.cli.replay_stock \
  --code 002491 \
  --start-date 2026-07-20 \
  --end-date 2026-08-07
```

随机样本：

```bash
PYTHONPATH=. python -m app.quant.cli.replay_sample \
  --start-date 2026-07-01 \
  --end-date 2026-08-31 \
  --sample-size 300 \
  --seed 20260903
```

第一轮单因子后置筛选研究：

```bash
PYTHONPATH=. python -m app.quant.cli.replay_factor_experiments \
  --start-date 2026-07-01 \
  --end-date 2026-08-31 \
  --sample-size 300 \
  --seed 20260903
```

研究口径、无未来函数边界和固定场景见 `research/README.md`。该入口不会改变正式策略，
也不会写入影子盘每日结果。

策略参数不通过命令行开放，防止正式结果被不同周期或阈值污染。默认结果统一写到
`.local/quant/provisional_daily_macd_3m_v1/`。

## 每日结果

`DailyFlow`保存盘前观察池、盘中成交、持有池、闭合交易、费用和盈亏，默认携带正式
策略ID、版本和3分钟周期。`app/quant/public.py` 定义公开字段
及 OpenAPI 响应类型，前端统一使用 `strategy_1` / “策略1”。v1.2只隐藏真实名称和
存储标识，恢复指标、参数、原因、执行尝试和细分状态，另外提供截至所选日期曾经买入的逐股独立账户（包括已清仓账户）。
对接文档见 `docs/量化前端接口.md`。前端读取接口：

```text
GET /api/v1/quant/strategies
GET /api/v1/quant/strategies/strategy_1/overview
GET /api/v1/quant/strategies/strategy_1/performance
GET /api/v1/quant/daily-results/latest
GET /api/v1/quant/daily-results/{trade_date}
GET /api/v1/quant/intraday/latest
GET /api/v1/quant/signals?trade_date=2026-09-03&action=buy&status=filled
GET /api/v1/quant/observations?trade_date=2026-09-03&state=watching
GET /api/v1/quant/strategies/{strategy_id}/intraday/latest
GET /api/v1/quant/strategies/{strategy_id}/observations
GET /api/v1/quant/strategies/{strategy_id}/signals
GET /api/v1/quant/strategies/{strategy_id}/executions
GET /api/v1/quant/strategies/{strategy_id}/holdings
GET /api/v1/quant/strategies/{strategy_id}/daily-results/{trade_date}
```

前端每分钟轮询 `overview` 即可，快照是否变化以 `snapshot_id` 为准；
明细请求携带同一 `trade_date` 和 `snapshot_id`，409表示需要刷新总览后重试。
`data_status` 为 `partial`、`closed_partial` 或 `error` 时应显示数据质量提示，不能把
缺失数据解释为“没有信号”。

三分钟K线只控制策略信号确认频率，持仓估值单独按最新完整一分钟行情更新。单票
`total_pnl` 是从买入至今扣除买入手续费后的浮盈亏，`market_day_pnl` 是相对前收盘价
的标的当日盈亏，`account_day_pnl` 是策略账户当天实际持有期间的盈亏。对应的
`total_return`、`market_day_return`、`account_day_return` 返回小数比例；策略汇总按
各自资金基数加权，不对单票收益率做算术平均。
