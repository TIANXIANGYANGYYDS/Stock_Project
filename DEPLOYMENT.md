# Stock_Project 包安装文档

这份文档只保留项目当前用到的第三方包安装说明，方便你在其他环境快速装依赖。

## 1. Python 版本

- 建议 Python 3.10 或 3.11。
- `akshare` 建议只在 Python 3.10 或 3.11 下安装。

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
  motor \
  pydantic \
  pydantic-settings \
  pymongo \
  python-dotenv \
  requests \
  pytest
```

这些包大致对应：

- 调度：`apscheduler`
- HTML 解析：`beautifulsoup4`
- MongoDB：`motor`、`pymongo`
- 配置：`pydantic`、`pydantic-settings`、`python-dotenv`
- 网络请求：`requests`
- 测试：`pytest`

## 5. 可选依赖

如果你要运行手动脚本，比如 [app/manually_execute_script/fetch_a_stock_sectors.py](app/manually_execute_script/fetch_a_stock_sectors.py)，再安装这组：

```bash
python -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple \
  pandas \
  html5lib \
  lxml \
  akshare
```

## 6. 一次装完

如果你就是想一次装完当前文档里的所有第三方包，可以直接执行：

```bash
python -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple \
  apscheduler beautifulsoup4 motor pydantic pydantic-settings pymongo python-dotenv requests pytest \
  pandas html5lib lxml akshare
```