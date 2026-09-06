# MACD 后置辅助指标研究

本目录保留各轮研究和原MACD对照。2026-09-06按用户决定，正式策略已升级为2.0.0：
原MACD＋ADX14买入＋E2延迟卖出。以下实验协议及历史结论保留当时含义，不能反向覆盖正式规则。
正式入口见`app/quant/README.md`；研究底层仍显式传入门控与退出策略。

## 第一轮冻结网格

第一轮严格固定为 67 组：1 组原始 MACD 基线、54 组核心辅助指标、12 组 RSI
冗余对照。

| 指标 | 参数 | 过滤规则 | 数量 |
| --- | --- | --- | ---: |
| RSRank | `L in {20,60,120}`，`S in {0,5}` | `rank >= {0.60,0.70,0.80}` | 18 |
| RTOV | `n in {10,20,40}` | `>=0.8`、`>=1.0`、`>=1.2`、`[1.0,3.0]` | 12 |
| ADX | `n in {7,14,21}` | `<20`、`<25`、`>=20且3日下降`、`>=20且3日上升` | 12 |
| NATR Rank | `n in {10,14,20}` | `[10%,90%]`、`[20%,90%]`、`[20%,80%]`、`[30%,80%]` | 12 |
| RSI 对照 | `n in {9,14,21}` | `[35,55]`、`[40,60]`、`[45,65]`、`[50,70]` | 12 |

第一轮完成前不新增指标、不增加因子阈值、不组合指标，也不做行业中性化。
代码在导入时校验 54/12/66 的数量和场景键唯一性，防止实验网格被静默扩展。

## 无未来函数边界

正式策略在盘中产生信号并在下一根三分钟 K 线开盘撮合，所以信号日尚未收盘时
不能使用当日最终日线：

- `RS_MOM(L,S) = Close[t-S] / Close[t-L] - 1`；`S=5` 使用 `t-5`，
  `S=0` 在盘中适配为最近完整交易日 `t-1`；
- RSRank 在每个信号日对独立量化日线库中的全 A 股票做横截面百分位排名，
  并列值取平均名次；
- `RTOV_n = Turnover[t-1] / Median(Turnover[t-n-1:t-1])`，分母是 `t-1`
  之前的完整 n 日，不含分子日；
- RTOV 使用 `stock_daily_detail.turnover_pct` 的历史真实换手率；价格、MACD、
  RSRank、ADX、NATR 和 RSI 继续使用独立量化日线；
- ADX、NATR 和 RSI 的最新输入均为 `t-1`；ADX 三日前比较是
  `ADX[t-1]` 对 `ADX[t-4]`；
- NATR Rank 也按每日全 A 横截面计算。

## 冻结评估口径

输出至少覆盖五组结果：

1. 信号压缩：日均、中位数、P10/P90、保留率、零信号日、最大单日信号数；
2. 单笔质量：净期望、均值/中位收益、胜率、盈亏比、Profit Factor、持仓日、
   MAE/MFE、P10/P90/P95；
3. 组合与时序留出：总收益、最大回撤、收益回撤比、四个连续时间折及第四折留出；
4. 尾部风险：ES90/ES95、最差 1%/5%、最大连亏、最大单笔亏损、MAE P90/P95；
5. 右尾赢家：基线前 5% 盈利交易保留率、筛后前 10% 盈家利润贡献和同数量随机对照。

随机对照使用固定种子，从基线闭合交易中无放回抽取与候选相同数量的交易，重复
1000 次，分别计算单笔净期望和 `平均收益 / abs(ES95)` 的经验分位。它是交易级
同数量对照，不替代完整路径的组合回放。

自动晋级必须同时满足：第四时序留出折净期望和收益回撤比均优于基线、随机对照
双 95 分位、四折全部具备最低交易样本且至少三折净期望增量为正、存在相邻参数
稳定平台、双倍交易成本仍有优势、回撤/ES95/最大单笔亏损不明显恶化，以及基线
前 5% 大赢家保留率不低于同信号保留率的随机预期。RSI 永远只作为对照，不自动晋级。

当前可用三分钟历史窗口较短时，第四折只应称为“临时时序留出集”，不能替代跨年度、
跨牛熊状态的真正 OOS 复验。短窗口即使通过自动门槛，也不能直接修改实盘规则。

## 运行与输出

```bash
PYTHONPATH=. python -m app.quant.cli.replay_factor_experiments \
  --start-date 2026-07-01 \
  --end-date 2026-08-31 \
  --sample-size 300 \
  --seed 20260903
```

结果目录后缀固定为 `_grid67`，主要文件包括：

- `report.md`：便于阅读的五组汇总与晋级结论；
- `scenario_metrics.csv`：67 组完整指标；
- `promotion_assessment.csv`：每条冻结晋级规则及失败原因；
- `time_fold_metrics.csv`：正常成本和双倍成本的四折结果；
- `signal_decisions.csv`、`factor_snapshots.csv`、`closed_trades.csv`：审计明细；
- `industry_attribution.csv`、`concept_attribution.csv`：板块暴露诊断。

信号压缩统一基于同一批 MACD 基线信号，完整收益则来自每个场景的独立路径回放。
行业和概念映射来自仓库内当前静态同花顺数据，不是历史时点快照，也不参与筛选或排名。


## 第二轮：固定12组买点提纯

```bash
PYTHONPATH=. python -m app.quant.cli.replay_factor_experiments \
  --grid purification12 \
  --start-date 2026-07-01 \
  --end-date 2026-08-31 \
  --sample-size 5501 \
  --seed 20260903
```

此轮复用原67组中的11条单因子规则加1组基线，不扩展参数网格：
ADX周期7/14/21，均为≥20且较3日前上升；RTOV周期10/20/40，各取≥1.0、≥1.2；
RS120跳过0/5日，各取排名≥80%。公式、全A横截面排名和t−1指标时点不变。

每股分配相同初始资金，仅供本股全仓买卖并继续累计盈亏。辅助指标只决定原MACD
有效买点是否执行，不改变观察、确认、信号有效期、卖出和撮合规则。
汇总收益率为全部独立账户收益率的算术平均，无交易账户也纳入分母。
期末持仓按原回放价格估值，单笔统计仅含闭合交易，无闭合交易的均值留空。

输出目录后缀为 `_purification12`，另输出：

- `baseline_trade_assignments.csv`：同一批基线闭合交易按原买入信号时点分组，
  原成交、退出和费用结果不变，列出指标缺失剔除原因。
- `purification_diagnostics.csv`：保留/剔除组全期及四个买点时间折的收益率均值、
  中位数、胜率、收益率盈亏比、MAE、ES、亏损分布、交易量和股票覆盖。
- `stock_paired_results.csv`：每股基线和辅助账户收益、收益差、买入及闭合交易次数、
  单股日终回撤变化，包含无交易账户与期末未平仓估值。
- `paired_summary.csv`：收益提高/不变/降低账户占比，增量均值、中位数、P10/P90；
  CSV收益和回撤均为小数比例，报告增量列换算为百分点。
- `parameter_comparisons.csv`：相邻参数原第四折金额增量平台及本轮收益率区分差。

正式结果由每股每配置完整重跑产生，不能用删除基线交易代替。
提纯诊断按原买入信号日期归折，沿用截至本窗口末已实现的退出结果，存在未平仓
交易截尾影响；旧时序回放表仍按退出日期归折，两者不能混用。
正常与双倍成本均独立回放，旧随机对照和晋级标准仅供解释原门槛，旧晋级规则在本轮12组范围内重算，
未入选的邻点不参与，不能用该表替代原67组晋级表。

本轮优先判断净收益率形式的交易质量和同批信号区分能力，然后判断配对账户收益
是否改善，不按金额净期望自动选优，也不按每股历史最佳参数选规则。
第一轮使用过的44个交易日及第四折都是已参与候选选择的研究样本，不是新的独立OOS。
旧自动晋级结论不等于本轮最终选择；长期有效性仍需后续未使用的时间窗口复验。

## 第三轮：ADX14/21固定五组

评价协议在新增窗口结果产生前冻结于 [ADX_PROTOCOL.md](ADX_PROTOCOL.md)。新增入口：

```bash
PYTHONPATH=. python -m app.quant.cli.replay_adx_comparison \
  --entry-start 2026-06-22 \
  --entry-end 2026-06-30 \
  --observation-end 2026-08-31
```

不传`--sample-size`时使用回放起点的全部股票；可传小样本做端到端检查。
五组固定为基线、ADX21/14/7≥20且较3日前上升、RTOV10≥1.0。
与12组入口共用原`replay()`，仅新增交易报告的入组/观察边界，不停止入组窗口后的
新交易，不重置独立账户资金，不为补齐样本强制卖出，不生成ADX组合策略。

新入口保存协议内容及SHA256。已有完成结果不覆盖；重跑请显式指定新的`--output-root`。
默认产物位于`factor_experiments/adx_comparison5/`：

- `baseline_cross_assignments.csv`、`baseline_cross_metrics.csv`：ADX14/21四类交叉
  分组和指标缺失组，包含未平仓市值；可核查指标时点和原买入信号日期。
- `cohort_trades.csv`、`cohort_quality.csv`：五组策略实际成交的入组交易，自然退出与
  观察截止仍持仓者分列。闭合胜率/亏损分布不含未平仓交易；市值收益不预扣未来卖出费用。
- `baseline_filter_quality.csv`：各单指标在同批基线入组交易上的保留/剔除诊断。
- `stock_paired_results.csv`、`paired_summary.csv`：入组终点与观察终点的全部逐股账户，
  候选相对MACD基线及ADX21相对ADX14的配对；含正常成本、双倍成本。
- `account_contributions.csv`：是否买入、是否期末持仓的四状态贡献，分母固定全部股票；
  另列已实现与未实现增量，避免把持仓组贡献等同于浮盈。
- `data_coverage.csv`：每个样本股票日线交易日是否有完整80根三分钟柱。
- `signal_decisions.csv`、`closed_trades.csv`：连续完整路径审计，包括入组后正常交易。

原7—8月产物可通过该模块的`audit_prior_report(source, output)`复核账户贡献、
亏损超过10%的比例、F1—F3统一时间权重，以及同批基线交易的交叉分组。
旧结果审计不重新计算行情或策略，也不作为新增独立样本。

截至2026-09-05，本地全市场同口径日线/三分钟数据只完整覆盖到2026-08-31，
2026-09-01仅一只股票。6月22日前三分钟股票覆盖不完整，故当前新增入组窗口为
6月22日至30日，观察至8月31日。持有区间与已使用研究窗口重叠，核心MACD定参历史
也无法完整确认，因此不能称为整套策略独立OOS；原8月31日期末账户的后续演化仍待数据。

本次结果产生后，另补充了入组日期敏感性诊断：`cross_by_entry_date.csv`列出每个
入组日的交叉样本，`same_entry_day_weight_sensitivity.csv`对“仅ADX21减仅ADX14”
使用每日相同的基线样本权重。闭合口径使用每日基线闭合交易数，市值口径使用每日
全部入组交易数；某日任一分歧组无样本时，加权值留空。该项标为事后描述性诊断，
不修改已冻结协议，不是新增晋级门槛，也不改变任何成交或账户结果。

## 第三轮后：左尾复核与最终三组冻结

后续主线仅保留原MACD、ADX21≥20且较3日前上升、ADX14同条件。
规则与评价优先级见 [ADX_FORWARD_PROTOCOL.md](ADX_FORWARD_PROTOCOL.md)。第三轮五组
协议和历史产物继续留档，不覆写旧规则、75%平台判定或原回放结果。

本次补充审计只读第三轮CSV，复算ADX分歧原因及统一入组日期权重的收益、胜率、
最终亏损超过10%的比例和MAE。不访问行情数据库、不运行新策略、不产生新OOS：

```bash
PYTHONPATH=. python -m app.quant.cli.audit_adx_left_tail \
  --source .local/quant/provisional_daily_macd_3m_v1/factor_experiments/adx_comparison5/2026-06-22_2026-06-30_observe_2026-08-31/nall_seed20260903 \
  --output .local/quant/provisional_daily_macd_3m_v1/factor_experiments/adx_left_tail_audit/third_round_20260905
```

输出必须为独立的新目录，已有目录不覆盖：

- `disagreement_assignments.csv`、`disagreement_reason_metrics.csv`：在保存的买点ADX值上
  重算阈值/上升条件分歧，属于机制描述，不能作为新筛选条件。
- `daily_left_tail.csv`、`weighted_left_tail.csv`：每天两独有组使用相同的全部基线闭合
  交易权重，包含缺失因子组对日期权重的贡献；空组留空，不删除日期后重归一。
- `validation.json`：输入SHA256、交易身份唯一性、指标交叉组一致性和原始输入未变检查。
- `forward_plan.json`：仅三条原规则及协议原文/哈希。状态明确为规则冻结、窗口待登记、
  新OOS未启动；入组和观察日期为空，不把规则清单误认为已运行的前瞻实验。

未来正式选择首先比较全部独立账户收益，其次比较左尾风险。当前日期加权结果只是
描述性标准化，不产生显著性结论。下一验证须同时满足买点和结果路径未参与候选选择，
并在结果出现前登记日期、检查频率与停止规则，不能把第三轮与此前重叠账户区间重复计票。

## 第四轮：ADX退出九组开发研究

[ADX_EXIT_RESEARCH_V1.md](ADX_EXIT_RESEARCH_V1.md)冻结E0原退出、E1提前保护、
E2延迟退出、E3双向调整。ADX14/21买卖同周期，各四组，加原MACD对照共九组。
退出控制器仅由新的研究CLI显式启用；正式策略及默认回放未启用此控制器。

```bash
PYTHONPATH=. python -m app.quant.cli.replay_adx_exits \
  --output .local/quant/provisional_daily_macd_3m_v1/exit_experiments/adx_exit_v1_full \
  --workers 8
```

使用第三轮6月22日期初5,496只股票及6月22日至8月31日完整窗口，正常/双倍成本
分别重跑决策。`--reference-source`指向接入前的回放源码快照，用于逐股E0精确回归；
当前快照位于`.local/adx_exit_v1_source/replay_stock_before.py`。`--codes`仅用于小规模
开发验证。`--resume`只接受源码哈希、股票数量及原股票池一致的分片。

一级结果是`account_metrics.csv`与`account_paired_summary.csv`，二级同买入退出
诊断是`exit_pairs.csv`与`exit_paired_summary.csv`。影子交易允许与后续参考买入
重叠，但绝不计入真实独立账户。每股压缩JSON保存回放和诊断审计，未完成交易继续
估值。已使用的开发窗口不能称作独立OOS；所有研究结果均不会自动更新实盘规则。
