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
  httpx \
  motor \
  pandas \
  playwright \
  pydantic \
  pydantic-settings \
  pymongo \
  python-dotenv \
  requests \
  pytest
```

这些包大致对应：

- 调度：`apscheduler`
- 东方财富行情页抓取：`playwright`（Chromium）
- 行情表格整理和日期转换：`pandas`（不在本地计算指标或筹码）
- HTML 解析：`beautifulsoup4`
- MongoDB：`motor`、`pymongo`
- 配置：`pydantic`、`pydantic-settings`、`python-dotenv`
- 异步网络请求：`httpx`
- 其他现有爬虫的同步网络请求：`requests`
- 测试：`pytest`

安装 Python 依赖后，还需要在同一个 `MyAgent` 环境安装 Chromium：

```bash
python -m playwright install chromium
```

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
  apscheduler beautifulsoup4 httpx motor pandas playwright pydantic pydantic-settings pymongo python-dotenv requests pytest \
  akshare \
  html5lib lxml
```

然后执行：

```bash
python -m playwright install chromium
```

## 7. 服务器进入项目

```bash
conda activate MyAgent
cd /home/txy/Agent_first/Stock_Project
python -V
which python
```

## 8. 验证单只股票行情页

先用单股脚本验证页面、代理池、网页指标和网页筹码，不写 MongoDB：

```bash
python app/manually_execute_script/fetch_eastmoney_quote_page_daily_detail.py \
  --code 002185 \
  --start-date 20240101 \
  --end-date 20260710
```

`002185` 的行情页地址是
`https://quote.eastmoney.com/concept/sz002185.html#chart-k-cyq`。日线页面只使用
51 代理，不回退本机直连；代理失败会被废弃并按 `max_retry` 获取新代理。严格模式
下不会使用本地指标/筹码公式。抓取失败时，脚本会把页面运行时诊断保存到
`.local/logs/eastmoney_runtime_diagnostics_002185.json`，排查完成后可以删除。

每日调度先从东方财富当日行情列表筛出确实有成交的股票，再抓取逐票网页数据。
默认使用 80 个协程和 80 个独立代理 IP 并发抓取，单 IP 同时只负责一个页面；
失败重试使用异步退避，不占用其他股票的执行机会。主任务每天 15:30 执行，批次
结束后立即补偿剩余网络失败股票，次日 15:20 再审计上一交易日。每次调度调用的
补偿轮数有限，未完成项会保留给后续调度继续重试。

## 9. 启动 scheduler

```bash
./.local/bin/start_scheduler.sh
tail -f .local/logs/scheduler.log
```

停止命令：

```bash
./.local/bin/stop_scheduler.sh
```
