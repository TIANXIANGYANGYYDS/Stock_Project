# Stock_Project 包安装文档

这份文档只保留项目当前用到的第三方包安装说明，方便你在其他环境快速装依赖。

## 1. Python 版本

- 服务器使用 `MyAgent` Conda 环境，已验证 Python 3.13.12。
- 执行命令前先确认 `python -V` 和 `which python` 指向该环境。

## 2. 先切到清华源

```bash
export PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
export PIP_TRUSTED_HOST=pypi.tuna.tsinghua.edu.cn
```

如果你要长期使用清华源，也可以执行：

```bash
python3 -m pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple
python3 -m pip config set global.trusted-host pypi.tuna.tsinghua.edu.cn
```

## 3. 创建虚拟环境

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip setuptools wheel -i https://pypi.tuna.tsinghua.edu.cn/simple
```

## 4. 核心依赖

下面这组是当前主流程和测试会用到的包：

```bash
python -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple \
  apscheduler \
  beautifulsoup4 \
  curl_cffi==0.9.0 \
  exchange_calendars \
  httpx \
  motor \
  pandas \
  pydantic \
  pydantic-settings \
  pymongo \
  python-dotenv \
  requests \
  pytest
```

这些包大致对应：

- 调度：`apscheduler`
- A 股交易日判断：`exchange_calendars`
- 创作者平台协议抓取：`curl_cffi`（复用 Chrome TLS 指纹）
- 东方财富逆向行情请求：`curl_cffi`
- 行情表格整理和日期转换：`pandas`（不在本地计算指标或筹码）
- HTML 解析：`beautifulsoup4`
- MongoDB：`motor`、`pymongo`
- 配置：`pydantic`、`pydantic-settings`、`python-dotenv`
- 异步网络请求：`httpx`
- 其他现有爬虫的同步网络请求：`requests`
- 测试：`pytest`

## 5. 可选依赖

如果你要运行手动脚本，比如 [app/manually_execute_script/fetch_a_stock_sectors.py](app/manually_execute_script/fetch_a_stock_sectors.py)，再安装这组：

```bash
python -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple \
  akshare \
  html5lib \
  lxml
```

## 6. 一次装完

如果你就是想一次装完当前文档里的所有第三方包，可以直接执行：

```bash
python -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple \
  apscheduler beautifulsoup4 curl_cffi==0.9.0 exchange_calendars httpx motor pandas pydantic pydantic-settings pymongo python-dotenv requests pytest \
  faster-whisper==1.2.1 rapidocr==3.9.2 \
  akshare \
  html5lib lxml
```

## 7. 服务器进入项目

```bash
conda activate MyAgent
cd /home/txy/Agent_first/Stock_Project
python -V
which python
```

## 8. 验证单只股票逆向行情

先用单股脚本验证纯协议逆向、代理池、技术指标和筹码计算，不写 MongoDB：

```bash
python app/manually_execute_script/fetch_eastmoney_daily_detail.py \
  --code 002185 \
  --start-date 20240101 \
  --end-date 20260710
```

默认路径使用 `curl_cffi` 的 Chrome 124 TLS 指纹和 51 代理请求东方财富日 K 接口，
不启动浏览器，也不回退本机直连。候选代理第一次失败就立即废弃；已验证代理连接失败
时只重建一次连接，仍失败则废弃。东方财富日线功能不再包含页面模拟回退。

每日调度先从东方财富当日行情列表筛出确实有成交的股票，再抓取逐票数据。日常任务
默认使用 20 个协程和最多 20 个独立代理 IP，单 IP 同时只处理一个请求、请求起始间隔
至少 1.2 秒，并在 40 次目标请求后主动轮换。服务启动补数和失败补偿仍限制为 8 个
协程。主任务每天 15:30 执行，批次结束后立即补偿剩余网络失败股票，次日 15:20 再
审计上一交易日。每次调度调用的补偿轮数有限，未完成项会保留给后续调度继续重试。

交易日判断优先使用项目已有的 `exchange_calendars` XSHG 日历，覆盖范围内无需
联网；超出范围时才回退到东方财富，并记录 `local_trade_calendar_out_of_bounds`。
出现该告警应更新日历依赖，避免恢复检查反复依赖远端行情接口。

## 9. 启动 scheduler

```bash
./.local/bin/start_scheduler.sh
tail -f .local/logs/scheduler.log
```

停止命令：

```bash
./.local/bin/stop_scheduler.sh
```

## 10. 启动解耦的博主处理 worker

五个平台的作品发现任务属于 scheduler；视频下载、OCR、ASR 和单作品内容分析 LLM
分别属于内容提取 worker 和 LLM 1 worker。收盘验证由 scheduler 独立执行：

```bash
./.local/bin/workers.sh start creator_extraction
./.local/bin/workers.sh start creator_analysis
tail -f .local/logs/creator_content_extraction_worker.log
tail -f .local/logs/creator_opinion_analysis_worker.log
```

博主分析使用两个彼此解耦的 LLM，调用顺序固定如下：

1. `CreatorContentAnalysisLLMAnalyzer` 读取标题、正文、提取文本、ASR 和 OCR，输出
   `CreatorWorkAnalysis`（可证伪观点、有效期、指标和逐字 `source_quote`），直接写回
   `creator_works`，并将可验证观点同步到
   `creator_opinion_analyses.pending_opinions`。此阶段不读取行情。
2. 收盘任务在内存中构建同花顺复盘、新闻榜单、目标板块和条件资产行情，不单独写
   行情快照集合。
3. `CreatorOpinionVerificationService` 筛选已经到 `verification_date`、在评价日
   15:00 仍有效的观点。观点与派生快照一起交给
   `CreatorOpinionVerificationLLMAnalyzer`。LLM 2 默认联网，输出逐观点结论和可复核
   网页证据；程序计算累计准确性评分，并在 `creator_opinion_analyses` 中原子地把
   到期观点从 `pending_opinions` 移入 `verified_opinions`。

作品采集每小时整点以账号并发 1 串行执行；调度任务单实例并合并错过触发。内容提取、
LLM 1 和收盘验证可以独立重试和补跑；
15:40 与 16:30 都只验证 08:20 前已完成的同一批观点，后续补录不会进入当天评分。

本机固定使用 CPU `int8` 的 faster-whisper small 模型并启用字幕 OCR，ASR 和 OCR
各限制为两个 CPU 线程，媒体提取 worker 以 `nice=10` 启动。模型应
提前放到：

```text
.local/models/faster-whisper-small/
```

视频容器会同时执行 ASR 和字幕 OCR；B站 DASH 音频等音频流只执行 ASR，不会交给
OpenCV。媒体下载失败或超过大小上限时，平台正文非空的作品会直接使用正文进入 LLM 1，
避免可分析内容因媒体问题耗尽重试。

目标博主账号、抓取间隔与范围、ASR 设备、字幕 OCR、30 分钟处理租约和
失败重试均为代码中的固定业务规则，不写入 `.local/env/.env`。环境文件只保留
API 密钥、服务地址、数据库、代理、日志等级和 `DOUYIN_SESSION_COOKIE` 等实际部署
凭据。当前抖音匿名主页会隐藏近期作品，作品列表任务必须配置有效授权会话；该值不得
写入代码或日志，失效时任务会明确记录为平台阻断而不是“没有新作品”。调度器启动时及
每天 09:05 检查 `sid_guard` 到期时间，剩余 7 天内记录
`douyin_session_cookie_expiring`，过期后记录 `douyin_session_cookie_expired`；两类日志
均不包含 Cookie 值，可直接接入现有日志告警规则。

公开页面可能触发平台风控，任务会记录失败并有限重试，不会尝试绕过验证码。

## 11. 博主集合

MongoDB 只保留 `creator_works` 与 `creator_opinion_analyses` 两张博主业务集合。前者
保存原始正文、OCR/ASR、处理状态、北京时间发布时间/入库时间和 LLM 1 得出的 A 股观点；
后者每位博主一条，保存已验证观点、累计准确性评分和待验证观点。迁移脚本会在备份后
删除 `creator_work_processing`、`creator_crawl_checkpoints` 和
`creator_daily_verifications`，运行中的 scheduler/worker 必须在迁移前停止。

## 12. 启动只读查询 API

API 与 scheduler、worker 独立运行，只查询现有 MongoDB 集合，不创建任务队列或写入新的
业务集合。使用 `MyAgent` 环境启动：

```bash
conda activate MyAgent
cd /home/txy/Agent_first/Stock_Project
uvicorn app.api.app:create_app --factory --host 0.0.0.0 --port 8100
```

启动后可检查：

```bash
curl http://127.0.0.1:8100/api/v1/health
curl http://127.0.0.1:8100/openapi.json
```

完整查询路径和参数见 [docs/API.md](docs/API.md)。

## 13. 持久服务、心跳与故障恢复

2026-11-02 起，本机 scheduler 与查询 API 使用用户级 systemd 服务，模板位于
`deploy/systemd/`。安装到 `~/.config/systemd/user/` 后执行 `systemctl --user daemon-reload`
及 `systemctl --user enable --now stock-scheduler stock-query-api stock-quote-chart.timer`。模板使用本机
`~/Agent_first/Stock_Project` 和 `~/miniconda3/envs/MyAgent/bin/python`；其他机器先调整路径。
需要开机且未登录时运行，应确认该用户已开启 linger。

日常管理：

```bash
systemctl --user status stock-scheduler stock-query-api
systemctl --user restart stock-scheduler
systemctl --user restart stock-query-api
curl -fsS http://127.0.0.1:8100/api/v1/health/ready
```

个股图表索引由 `stock-quote-chart.timer` 调用独立的低优先级任务生成。每个五分钟区间结束后按股票生成图表块，初次部署先处理最新交易日，再补齐原始数据保留期。用 `journalctl --user -u stock-quote-chart.service` 查看进度。查询会逐批核对原始索引，尚未生成的区间和迟到补写的批次可直接读取原报价。

本机 `.local/bin/{start,stop,restart}_scheduler.sh` 已转交 systemd，避免手工启动绕过
监督机制。非交互环境若找不到用户总线，设置 `XDG_RUNTIME_DIR=/run/user/$(id -u)` 和
`DBUS_SESSION_BUS_ADDRESS=unix:path=$XDG_RUNTIME_DIR/bus`。服务子进程通过
`STOCK_SYSTEMD_LAUNCH=1` 调用底层启动脚本，防止递归调用 systemd。

服务器校时后，重启 `stock-scheduler` 与 `stock-query-api`，清除进程内旧时间偏移并
按当前时间重新注册任务。调度器明确使用 `Asia/Shanghai`，不依赖启动环境的本地时区。
应用的双来源 HTTP 时间校验继续保留：共用的五分钟偏移缓存同时比较系统时间与单调
时钟，检测到超过一秒的跳变立即失效并重新校验；校验请求期间若发生时间跳变则拒绝
该样本。分钟采集在每个交易时段开始时重新校验，盘中主动校时后仍须重启调度器以
清除该时段的偏移。历史记录中的 `clock_offset_seconds` 是当时的采集证据，不能清零。
启动时先暂停执行、移除已被五秒采集替代的旧 `stock_fund_flow_5m` 任务，再恢复调度，
避免 MongoDB job store 中的旧定义在重启后继续报错。

Scheduler 的事件循环每 5 秒原子更新 `.local/run/scheduler_heartbeat.json` 并通知
systemd。120 秒未收到心跳时，由 systemd 终止整个服务进程组并按失败策略重启。
分钟历史修复改为独立子进程，取消或超时会终止子进程，完成后释放全市场清单占用的内存。
短线情绪引擎在建立归一化历史后释放原始日线输入，并在恢复当前日检查点后释放前日快照。

本次故障中 `MemoryHigh=2.5G` 导致进程持续回收和限流，进程没有退出，`Restart=on-failure`
无法起效。现在不使用软限流，保留 scheduler 4 GiB、API 2 GiB 的硬限制和各自
256 MiB 的 swap 限制；OOM 或无心跳触发恢复。`MALLOC_ARENA_MAX=2` 限制线程分配器的
内存保留。调整上限前应测量峰值并检查主机可用内存，不能取消硬上限来掩盖增长。

`/api/v1/health` 保留原有 API/Mongo 连通性契约；新增 `/api/v1/health/ready` 检查
调度器心跳以及最近应完成交易日的日线、量化收盘状态和全 A 股情绪定稿。北京时间
19:00 前检查前一交易日，之后检查当日（休市日取最近交易日）。数据或心跳未就绪返回
HTTP 503 和具体检查结果，不能只用旧健康接口判断业务恢复。
日线须有最近全市场同步成功记录。量化记录保留 `closed_partial` 的真实状态；只有缺口
全部得到当日原始来源明确停牌、且无成交量额的证据支持时，健康检查才将这些代码列为
`verified_suspended_codes` 并允许就绪，未知缺口仍返回 503。

分钟缺口修复按交易日历枚举日期，因此整天没有日线和分钟记录的交易日也会进入检查。
历史补录只插入真实来源的缺失行情，保留实际取回时间；缺少北交所历史收盘报价的全 A 股
情绪日不能凭分钟收盘价伪造限价或证券状态，仍须保留缺口。事后重建的盘前报告须标记
`historical_reconstruction`、实际计算时间及输入不足，不作为当时实时生成的报告。

午后分钟采集允许在校准后的开盘时间前 10 分钟准备，优先读取数据库中的股票清单，
公开清单在后台更新，避免慢请求占用开盘首分钟。断点恢复查询按时间和周期分组；
查询超时保留已归档行情，恢复成功后按原始行情时间聚合，避免丢掉 13:00 的观测。
暂存队列有内存上限，持续数据库故障造成的溢出会记录错误，仍须依靠真实历史分钟补齐。

已关闭的量化日保持实时计算入口幂等。分钟补数成功后、调度器启动时，以及交易日
16:15–22:15 每小时，独立进程检查最近 5 个交易日中是否有可恢复的 `closed_partial`。
发现真实分钟和昨收参考价补齐后，从最早受影响日连续重算所有后续已保存交易日；
有盘中账本或日线同步未成功时暂缓。首日保留冻结的开盘输入，后续日期基于修正后的
资金、持仓、退出状态和待执行信号重新建账，不会只替换某一天造成账户断链。
重算结果标记 `historical_replay`、实际计算时间和 `minute_backfill` 恢复原因。
逐日重复重放、账户连续性和资金核对通过后，校验正式数据没有并发变化，再原子替换结果集合。
停牌或仍缺少真实来源的代码继续保留不完整标记，不合成价格。

手工恢复已结束交易日（省略 `--apply` 只生成暂存结果和校验报告）：

```bash
python -m app.quant.cli.recover_partial_days \
  --start-date 2026-11-03 --end-date 2026-11-05 --apply
```

默认审计目录为 `.local/quant_recovery/<运行时间及标识>/`，包含受影响日期原始结果备份、
恢复后结果、前序账本、源数据摘要和 `report.json`。恢复进程采用文件锁，避免重复执行。

## 14. 固定五秒资金流向与独立日档

资金采集由 `stock_fund_flow_5s` 每 5 秒调度，使用校准时间与 A 股交易日历，
覆盖 09:30–11:30 和 13:00–15:00（收尾容许 5 秒）。09:25、12:55 起预热股票清单，
盘中每小时后台刷新清单，避免辅助查询阻塞采集。使用现有代理配置建立 2 个 IP 的池，
每个 IP 最多 2 个在途请求、全局最多 4 并发和每秒 4 个请求，每批最多 500 只。
秒级轮次的网络预算为 4 秒，不在同一轮重试；保留完成批次并标记超时缺口。

某轮超时后，下一轮按已返回的批次数轮换起始股票。排查覆盖率时，先看
`missing_count`（未收到报价的股票数），再看 `unusable_count`（已收到但质量检查未通过的股票数）。
`response_complete` 表示已收到全部请求代码，`complete` 还要求每只股票的报价均可用。
定时采样返回 `spooled: true` 表示本地暂存文件已持久化，后台写库成功后才移除文件。

频率固定，不自动降到分钟级；同一任务不重叠、不积压补跑。主机可用内存低于
总量 15% 或 2 GiB（取较大值）、一分钟负载达到 CPU 数的 80% 时，当前轮明确记为
`missed`，下一个调度点仍为 5 秒。这是资源紧急保护，不能把漏采视为完整数据。
`market_observation_runs` 按日累计失败/部分成功/漏采次数并保存最近异常原因。

`stock_fund_flow_snapshots` 按股票代码尾数分块压缩，单股查询只读取相应分块。
写入前在 `.local/fund_flow_spool/` 持久暂存，数据库恢复后幂等回放；原始库与暂存
均沿用股票快照的 5 个交易日保留规则。`fund_flow_retention` 每小时分批清理原始库，
保留窗口截至最近实际采到的交易日，不会删除 `stock_fund_flow_daily`。

`stock_fund_flow_daily` 从 15:05:20 开始独立采集，每 15 分钟重试至 23:50:20，
已有完整日档时跳过。每股每天每来源唯一，缺失时仅补缺口；保存当天收盘后累计净额，
不累加盘中值。启动时也检查日档；跨日恢复只可使用该日实际盘后快照，不使用今日数据
冒充历史。来源没有独立资金统计时间，日档明确为盘后观察值。

手工采集和诊断见 [资金流向接入记录](docs/资金流向与DDE数据接入_20261106.md)。
