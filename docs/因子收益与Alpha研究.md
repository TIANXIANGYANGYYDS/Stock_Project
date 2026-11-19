# 因子收益与 Alpha 研究

这套离线研究把已有指标转换为统一规则的组合，再与同日期的上证指数比较。它不修改正式策略 1，也不把全部股票的分组收益当作 MACD 信号条件下的收益。入口为 `python -m app.quant.cli.replay_factor_alpha`。

## 三种指标的区别

所有日收益先复利连接，不能直接求和：`累计收益 = ∏(1 + 日收益) - 1`。

| 指标 | 计算 | 解读 |
|---|---|---|
| 累计超额收益 | 组合累计净收益 − 同期指数累计收益 | 以百分点表示，回答是否跑赢指数 |
| 相对财富收益 | 组合累计净值 ÷ 指数累计净值 − 1 | 以百分比表示，回答相对财富增长了多少 |
| Jensen / CAPM Alpha | `Rp,t − Rf,t = α + β(Rb,t − Rf,t) + εt` 的截距 | 调整市场敏感度 Beta 后的每日收益差；不是简单累计相减 |

例如组合涨 10%、指数涨 5%，累计超额为 5 个百分点，相对财富收益为 `1.10 / 1.05 - 1 = 4.7619%`；仅凭这两个期末收益无法求出回归 Alpha。

`alpha.py` 强制组合与指数的日期完全一致，拒绝缺值、重复日期和无效收益。默认至少 30 个交易日才计算回归。输出 Beta、R²、每日 Alpha、线性年化 Alpha、5 阶 Newey–West HAC 置信区间、双侧近似 p 值、跟踪误差及信息比率。所有可回归组合一起做 Benjamini–Hochberg 校正；不能只检验最后选中的赢家。

主无风险利率是**明确假设年化 0%**，并另外计算年化 1%、2% 的回归敏感性；没有把这些数当成真实国债收益率。日无风险率由 `(1 + 年化率)^(1/252) - 1` 换算。该敏感性只调整回归，不给组合现金补算利息。统计函数也支持输入真正的逐日无风险收益序列。

主基准是 `sh.000001` / `000001.SH` **上证综合价格指数**，不是深市 `000001` 股票；另列沪深 300 和同股票池等权组合。上证的全收益版本为 `000888`，不能与价格指数混称。股票使用已保存日涨跌幅构造收益链，未独立登记分红，供应商复权版本与指数分红口径仍是比较限制。

方法来源：[CFA Institute 关于 Jensen Alpha、Beta 及其限制](https://rpc.cfainstitute.org/-/media/documents/code/gips/measures-risk-adjusted-return.pdf)、[上海证券交易所指数列表](https://www.sse.com.cn/market/sseindex/indexlist/index.shtml?classid=010202)。

## 指标与可得时间

原先清单的 121 个字段登记在 `app/quant/research/factor_catalog.json`，包含名称、类别、公式和用途。不是每个字段都适合作为买入信号。

| 类型 | 实现和处理 |
|---|---|
| 股票横截面 | 收益/动量、均线、波动、ATR/NATR、换手、量价、K线、MACD、RSI、KDJ、CCI、WR、布林、筹码、资金流、行业/板块相对强弱、ADX、历史热榜和龙虎榜等 |
| 市场环境 | 涨跌家数、涨跌停、封板率、市场金额、连板高度等；以及可得的盘前分析、新闻排名汇总和收盘情绪快照；采用择时对照而不是股票分组 |
| 信号事件 | `dif_pct`、`shrink_ratio` 仅在真正保存的信号时点计算，另存事件表 |
| 执行诊断 | 因子滞后交易日、信号到成交的延迟、成交分钟；不能用成交后才知道的数预测同次买入 |
| 数据质量 | 完成率、缺失、覆盖率等作为解释与质量控制，不宣称是有效收益信号 |

日线指标的行标签是**已完成交易日**，组合读取前一交易日的值。滚动窗口保留市场日历上的空行；Wilder 类状态指标按有效价格观测递推，缺失当天不输出值。ADX/NATR/RSI 的原引擎版本另有一致性测试。历史行业分类按保存的日期向后衔接，不用当前行业回填过去。

价格、资金流、市场宽度使用可追溯的历史回溯版本，不保证是当时首次收到的版本。AI 盘前报告、热榜、龙虎榜和收盘快照要求实际更新时间/生成时间在决策开盘之前。缺失值不当作零；龙虎榜要求前五个交易日都有完整披露记录，热榜只在确实取得完整前 100 名快照时把榜外股票标记为榜外。

动态扩展只读取数值、布尔值及明确的状态编码，不把新闻正文任意转成股票评分。题材适配器要求当时保存的个股归属；本轮没有满足条件的有效题材映射，不能声称已完成题材 Alpha 验证。财务/估值、市值、机构、融资融券、DDE 和创作者历史评分缺少本轮已核验的可得时间面板，列入待补数据，不制造收益。

### 主力强度与暗盘强度

设净流入 `N` 和成交额 `A` 都为元，`r` 为涨跌幅百分数（如跌 0.73% 填 `-0.73`）：

```text
main_strength = (N / A) * 100
main_net_yi = N / 100000000
dark_strength = main_strength * r * main_net_yi * 100
```

最后一个 `×100` 来自用户第二张截图的实际算例；第一张截图的简写公式省略了它。保留明确单位，避免阈值相差 100 倍。

原暗盘公式含 `N²`，因而会消除净流入方向，正负主要跟随涨跌幅。代码同时提供有符号对数、绝对值对数、数据源涨跌幅版本，以及 `dark_preserve_flow = main_strength * r * abs(main_net_yi) * 100` 对照。对照公式只是研究变体，不能沿用截图的分档解释。资金流先核对“大单+超大单=主力”及净流入/成交额与来源占比的一致性，不合格记录保持缺失。

## 统一组合协议

1. 股票池由样本开始前真实保存过历史记录的股票冻结。后来上市的股票不加入，后来退市的代码不按当前存续状态删除；每天候选要求前一日存在有效报价和成交量。
2. 股票因子分为五组，Q1 低、Q5 高；相同值留在同组。常量不构成选股信号，二元指标只有低、高组。每五个交易日调仓，每组至少 20 只，因子覆盖至少为当日候选的 80%。数据不够时保持持仓，不假装卖出。
3. 以前一完整交易日的因子，下一交易日开盘买卖。初始净值为 1，无新增资金，无借款，卖出已有持仓后按现金比例买入，不卖当天新买的持仓。
4. 双边佣金各万一、滑点各万五，卖出印花税万五；双倍成本独立重算。期末不强制卖出，所以不额外收取不存在的清仓费用。
5. 市场变量与此前 20 个决策日、至少 10 个有效值的中位数比较，分别运行“高于时持有等权池”和“低于时持有等权池”；否则现金。可用且选择现金的日期也计入收益，不能从第一次买入才起算。
6. 开盘涨跌停按板块及已存名称近似限制；无有效开盘报价不能成交。缺少当日收盘价时沿用最后已知价并报告旧价格估值比例。这不是精确的历史 ST 状态、撮合、封单或流动性审计。
7. 采用可分割的单位化研究持仓，未模拟 100 股整手、最低收费、容量、市场冲击。不能据此宣称实际账户可原样成交。
8. 组合从第一次**有效决策**起与同日期指数比较。主统计、月度收益和平均持仓使用同一区间；完整窗口要求研究第一日已有有效决策，且至少 80% 计划调仓点有效。逐日底表保留整段日历，`start_date` 之前的现金只是计算占位，报告曲线不画成有效历史。

本段历史已用于多轮探索，不是新的独立留出集。日 Alpha 乘 252 是线性表达，不是未来年收益预测；87 个交易日和大量相关组合也不足以证明稳定 Alpha。

## 复跑

环境先安装项目依赖，以及可选研究依赖：

```bash
python -m pip install -r requirements.txt -r requirements-research.txt
```

直接重用本轮冻结输入，输出到一个新目录：

```bash
python -m app.quant.cli.replay_factor_alpha \
  --start-date 2026-07-01 \
  --end-date 2026-11-06 \
  --prepared-dir .local/analysis/factor_alpha_20260701_20261106 \
  --benchmark .local/analysis/factor_alpha_20260701_20261106/benchmark.parquet \
  --signals .local/analysis/strategy1_comprehensive_20261108/signals_raw.parquet \
  --output-root .local/analysis/factor_alpha_replay
```

`--signals` 可省略，省略后不输出真实信号事件。`--annual-risk-free` 可改变明确的年化假设，`--rebalance-sessions` 可改变统一调仓间隔；改参数属于新研究，应输出新目录并保留旧结果。

需要重建全部因子时，以可重复的 `--daily` 参数代替 `--prepared-dir`，另传 `--funds`、`--industry`、`--observations-dir`。至少提供 130 个研究开始前的交易日用于预热。输入规范：

| 输入 | 格式及字段 |
|---|---|
| 日线 | Parquet；`code` 六位字符串、`trade_date` ISO 日期、`open/high/low/close`、`pct_chg` 百分数、`volume`、`amount` 元、`turnover_pct` 百分数；可带供应商 MA、MACD、RSI、KDJ、CCI、WR、布林、筹码列。重复股票日拒绝执行，未提供的可选指标保持缺失 |
| 资金 | Parquet；`code/trade_date`、`main_net_yuan/large_net_yuan/super_large_net_yuan/small_net_yuan`、`main_ratio_pct`、`flow_change_pct` |
| 行业 | Parquet；`date/code/industry/industry_update_date`，采用当时保存的行业版本 |
| 基准 | Parquet；`date/code/daily_return`，收益为小数；必须包含 `sh.000001` 与 `sh.000300` 的完整评估交易日，无重复日期 |
| 辅助观察 | JSON.GZ，文件名为 `market_sentiment_daily`、`daily_market_analysis`、`stock_ths_hot_rank_snapshots`、`stock_billboard_daily`、`short_sentiment_snapshots` 加 `.json.gz`；保留来源文档及带时区的观测/更新时间，快照需解压为其 payload 结构 |
| 预构建面板 | `factor_panel.parquet`、`context_stock.parquet`、`context_market.parquet`、`context_specs.json`、`universe.json`；前者按完成日，两个 context 按决策日，禁止重复移位 |

CLI 不写数据库、不调用交易，也不修改正式策略参数。原始指数从现有 BaoStock 数据源读取，快照保存在研究目录 `raw/`；CLI 明确读取本地基准，避免在重跑时静默更换历史版本。输入和实现文件分别记录 SHA-256 于 `input_hashes.json`、`implementation_hashes.json`。

仅刷新报告：

```bash
python -m app.quant.cli.replay_factor_alpha \
  --start-date 2026-07-01 --end-date 2026-11-06 \
  --output-root .local/analysis/factor_alpha_20260701_20261106 --report-only
```

## 本次产物与验证

本次窗口为 **2026-07-01 至 2026-11-06，共 87 个交易日**，股票池 **5,530 只**。登记 283 个原始/派生字段，216 个得到可评估组合，连同基线共产出 703 个组合；548 个满足完整窗口条件。283 不是 283 个独立有效因子。短历史和缺失来源的状态详见覆盖表。

默认目录 `.local/analysis/factor_alpha_20260701_20261106/`：

- `因子收益与Alpha报告.html`：无需联网，选择因子和净值/相对净值图、搜索组合、切换完整窗口。
- `因子收益与Alpha.xlsx`：中文统计、原始小数统计、因子状态、月度收益、每日净值、限制、真实信号事件；目录中若有原策略账本对照文件，则另附该表。
- `组合收益与Alpha.csv`、`全部因子实现与覆盖.csv`、`逐月表现.csv`、`逐日净值.csv`：便于独立计算。
- `信号事件指标.csv`：本轮 3,340 条真实已成交买入事件，仅作事件研究和执行诊断。
- `rebalance_audit.parquet`：逐组合调仓覆盖和有效决策记录。
- `protocol.json`、哈希文件、`context_audit.json`、`benchmark_checks.json`：协议、来源版本与时间可得性核对。

原策略 1 的正式账本只有 8 月 20 日至 11 月 6 日的 51 个交易日。该账本的单独 Alpha 对照不等于 87 日完整原策略重放，更不能拼接 7 月不同版本的策略。固定全池资金收益、活跃账户时间加权收益、累计损益/活跃初始本金三种分母不可混用。

测试覆盖：Alpha/Beta 已知真值、同日期约束、无风险换算、HAC/BH、费用与现金守恒、不可成交和旧价估值、分组同值处理、未来数据不改变历史、交易日缺口、原引擎指标一致性、辅助信息时点、暗盘单位/方向、信号诊断，以及短历史现金和月度区间一致性。

```bash
python -m pytest -q tests/quant/research/test_alpha.py \
  tests/quant/research/test_factor_portfolio.py \
  tests/quant/research/test_factor_features.py \
  tests/quant/research/test_factor_alpha_study.py
```
