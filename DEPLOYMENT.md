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
