请直接修改现有前端量化页面，适配后端v1.2已交易账户口径接口，完成代码、联调、类型检查、构建和必要测试，不要只输出方案。沿用现有技术栈和视觉风格。

本次要求覆盖此前“隐藏指标、参数、判断原因、确认进度”的要求：现在只隐藏真实策略名称和含名称的内部存储编号，其他业务与计算数据尽可能完整展示。策略名称必须用接口的“策略1”，编号用 strategy_1；不要根据参数拼出真实策略名称。指标名称、指标值、参数、判断依据、细分状态、确认进度、执行尝试记录均允许展示。

先读接口文档和类型，若当前工作区无法访问文件，则读取已配置后端的 /openapi.json 并核对实际响应：

- /home/txy/Agent_first/Stock_Project/docs/量化前端接口.md
- /home/txy/Agent_first/Stock_Project/docs/量化前端OpenAPI.json
- /home/txy/Agent_first/Stock_Project/docs/量化前端响应示例.json

后端服务端口为8100，复用项目已有 API base URL、环境变量和代理，不要把浏览器请求地址硬编码成 localhost:8100。

接口前缀：/api/v1/quant/strategies/{strategy_id}
目录：GET /api/v1/quant/strategies
目录只使用实际存在的策略，当前只有策略1，不要伪造其他策略和比较曲线。

需要接入：

1. /overview：本金、现金、市值、总资产、累计收益、当日收益、成交金额、费用、数量统计；还包含 strategy 参数、execution_rule 成交约定、runtime 数据质量、timeline 事件时间线。
2. /performance：按交易日升序的资产和收益曲线，支持日期范围及分页。
3. /accounts：只返回曾买入的逐股独立账户，包括已清仓者，排除从未买入者；展示本金、现金、市值、总资产、累计已实现/未实现/总盈亏、收益率、持仓数量；支持 code 和 has_position 筛选。
4. /observations：观察方向、通用状态、细分状态、数据状态、原因、指标及日期、条件是否满足、确认次数/要求次数；用详情抽屉完整展示。
5. /signals：方向、时间、信号价格、通用状态、细分状态、原因、指标、执行尝试、执行结果。未成交不能标成已成交。
6. /executions：模拟成交时间、价格、数量、金额、佣金、印花税、费用合计、现金变动，以及参考价、滑点、涨跌停价格、执行原因。
7. /holdings：数量、建仓时间、信号价、成交价、含费成本、估值及时间、毛/净浮盈亏、账户当日盈亏、可卖状态。
8. /closed-trades：所选交易日平仓明细、进出时间和价格、费用、毛/净收益。
9. /preselections：买入预选及原因、参考价格、状态。
10. /sell-candidates：卖出候选及原因、参考价格。
11. /exit-decisions：持有、延期和退出判断记录；展示时点、前后状态、原因、指标、是否允许延期、估算净收益、数据异常等。判断记录不等同于成交。
12. /daily-results/{trade_date}：同一份完整公开快照，包含上述明细池和全部曾买入账户。数据较大，日常使用分页接口，不要每次轮询全量下载。

保持页面层次清晰：主要资产、收益和列表放在主视图；参数、指标、确认进度和尝试过程放在可展开区域或详情抽屉。数据完整提供不代表把所有字段堆在首页。用户可以查看到明细，也可以按业务状态筛选。

兼容规则：

- 原 status/state 仍是通用状态，不要改用细分值覆盖它们。
- 新 status_detail/state_detail 是细分状态，可以分别作为查询参数；两类筛选同时传入时取交集。
- 例如：confirming=连续确认中，deferred_exit=延期退出，rejected_adx=趋势条件未通过，rejected_limit_up=涨停未执行买入，rejected_insufficient_cash=资金不足，deferred_t1=当日不可卖，deferred_limit_down=跌停顺延。
- 未知细分状态提供兜底文案，不要让页面报错或把它直接视为成功。
- reason 保留详细原因，仅真实策略名称已被后端替换。
- attempts 是已保存的有限长度执行尝试记录，attempt_count 为总次数，二者可能不同。
- observation_summary 同时有通用 state_counts 与细分 detail_state_counts。
- data_status_detail 保留缺少前收盘价等细分数据问题。
- 不要自行生成不存在的评分、胜率、风险值或历史记录。

请求与数据口径：

- 总览/完整快照使用 response.data；分页接口直接使用 items、total、page、page_size，无额外 data 层。
- page 从1起，page_size 默认50、最大200；筛选、日期、策略切换时重置分页。曲线历史超过一页要继续加载。
- 先加载 overview，再携带其 trade_date 和 snapshot_id 请求列表；409时刷新总览、清理旧分页并重试，避免无限循环。
- 根据 snapshot_id 判断快照变化，不只看 runtime.version；切换页面时取消过期请求，避免旧响应覆盖新筛选结果。
- latest 是最新已有交易日，不一定是今天；始终显示实际 trade_date。
- 金额为人民币元，价格元/股，股数为股，收益率为小数比例；0.0123显示1.23%，不重复乘100。
- 股票代码保留前导零，时间按 Asia/Shanghai 展示。
- null显示“—”，0正常显示0；accounts.available=false表示缺少账本，不代表真实账户为0。
- 总收益率按截至该日曾买入账户的初始本金计算（return_basis=traded_accounts_initial_capital）；已清仓继续计入，从未买入排除。账户收益率按单股初始本金，持仓收益率按该笔成本，勿混用。
- total_pnl是累计收益；account_day_pnl、成交笔数、成交金额和成交费用是当日口径。
- gross与net分开展示，费用已计入净盈亏，不能再扣一次。
- 当前 execution_kind=shadow_simulation，明确标注“模拟账户/模拟成交”，不是券商实盘成交。
- recording.mode=historical_replay是实际历史行情补录，live是实时运行；computed_at不是成交时点。
- 连续账户记录已扩展为2026-08-20至最新已有交易日；当前最新为2026-09-04，共12个交易日。日期范围读取 recording.start_date 和实际响应，不要硬编码9月3日。
- 本次从8月20日空仓起点连续重算，9月3日、9月4日数值已更新；清除旧快照和旧起点收益缓存，不可拼接旧曲线。不混入其他研究图表数据，不硬编码股票数量。
- 补录记录保持 historical_replay；recording.reference_price_method、historical_bar_policy 和 runtime.preparation_quality 提供来源与参考价核验信息。缺口继续展示 closed_partial，不补造收益。
- partial、closed_partial、error展示对应提示；加载失败不伪装成0收益或空记录。
- 请求层错误与数据中的判断原因要用安全文本渲染，不把接口文本直接插入HTML。

请统一API类型、状态映射、金额与收益率格式化，并检查页面标题、图例、详情、导出文件：策略身份始终为公开名称；其他接口提供的数据可以完整查看。

验证真实接口、分页、筛选、日期切换、0/null、正负收益、409/404/503、请求竞态以及当前项目构建。若后端不可达，明确说明联调未完成，不能用mock数据声称已联调。

完成后说明修改文件、已接入模块、验证结果及实际阻塞。

本次优先修改模拟账户金额与曲线，严格按以下v1.2口径验收：
- 本金、现金、总资产、账户数量、累计收益率直接使用summary对应字段。禁止使用覆盖股票数×10万元作为本金。
- 首次买入成交才纳入本金；挂单/观察/拒绝不算；清仓不移除本金，后续再次买入不重复纳入。
- 账户列表只展示/accounts返回的曾买入账户。has_position=false表示曾买入但已清仓；显示first_buy_at，has_traded=true。
- universe_account_count和inactive_account_count仅作为覆盖统计，可放详情，不加进资产卡片。
- capital_inflow是当日新纳入本金，不是收益，也不是成交现金净变动net_cash_flow。
- account_day_pnl=total_assets-opening_total_assets-capital_inflow；收益率以account_day_return为准，其分母account_day_return_base=opening_total_assets+capital_inflow。
- 收益图用total_pnl或total_return。total_return是动态本金口径，不能与日收益连乘净值混用；资产图显示initial_capital本金线或新增本金标记，防止把入金画成收益。
- 历史日期已经逐日重算，直接读取对应日期，不要用最新136个账户或最新本金回算历史；切换版本后刷新总览和曲线缓存，仍遵循snapshot_id的409刷新流程。
- 2026-09-04验收：账户136，持仓130，已清仓6；本金13,600,000元，现金783,241.20元，市值12,862,839元，总资产13,646,080.20元，累计盈亏+46,080.20元，收益率+0.338825%。
- 当日新增本金2,800,000元，当日盈亏-84,929.71元，当日收益率约-0.618525%；这些字段与累计收益率不同。
- 未发生任何买入时展示“尚未买入”，金额为0，避免NaN/Infinity；缺失null仍展示未知，不能一律替换为0。
- 保留策略名称匿名规则，只展示策略1；无需更改买卖信号逻辑。


新增需求：个股日K的模拟成交标记接入跨日成交接口（后端已提供，无需逐日请求）。
1. 使用GET /api/v1/quant/strategies/strategy_1/executions?code={六位股票代码}&start_date={开始日}&end_date={结束日}&page=1&page_size=100；可加action=buy或sell。
2. start_date/end_date首尾均包含，必须一起传，不与trade_date混用。保留原单日调用兼容；code始终按字符串处理。
3. total代表成交笔数，自动分页到取完；后续页带上第一页history_version。收到409时清空该查询的所有页重新加载；切换股票/区间/方向清空版本并取消旧请求，防止异步响应串股。
4. 区间外层trade_date=null、snapshot_id=history_version。每条items.trade_date才是成交对应日；items.snapshot_id是单日源版本，与外层区间版本不同。
5. 只将/executions中status=filled的数据画作模拟成交，默认“模拟买入/模拟卖出”；execution_kind=shadow_simulation，marker_type=simulated_execution。未成交信号单独查询/signals、单独样式，不混成买卖成交。
6. 当前日K为前复权。price_basis=recorded_execution_price表示execution_price是当时记录的成交原值（含撮合滑点），没有换算到当前前复权价格。按trade_date匹配日K，买入标记放K线下方、卖出放上方，不直接把execution_price当作前复权K线纵坐标。
7. 标记详情展示execution_at、execution_price、shares、notional、commission、stamp_duty、total_fees和模拟成交标签。重算/补录时间只放数据说明，不用于标记定位；对应K线缺失时不挪到相邻日期。
8. history_version作为查询缓存修订键；history.history_rebased_at为区间最近历史重建时间，history.accounting_rebased_at为账户口径更新时间，history.computed_at为最近计算时间。history.strategy_versions是策略业务版本，不能替代历史版本号。
9. history.covered_start_date/end_date/trade_day_count表示区间已有日快照覆盖；history.incomplete_trade_dates保留数据质量提示。请求六月至今时只返回已记录的成交，不意味着后端已把六月研究档案接入当前账户。
10. 200空列表表示区间没有成交；422表示参数错误；404表示策略不存在（单日模式也可能无日快照）；409按上述规则重载。不要将这几类状态混成“加载失败”。
11. 按日K所需将成交排序为正序，稳定键使用event_id去重；不要对同日多笔成交只保留最后一笔。交易金额/手续费仍以接口值为准，不用K线价重算。
12. 完成两个日期以上、同日多笔、只买/只卖、空区间、跨页无重复、历史重算409、股票切换竞态和前复权标记定位的验证。
