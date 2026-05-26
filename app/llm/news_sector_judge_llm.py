from __future__ import annotations

import asyncio
import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter

from app.llm.base_llm import BaseLLM, LLMResponseError
from app.models import NewsSectorLLMAnalysis


PROJECT_ROOT = Path(__file__).resolve().parents[2]

THS_INDUSTRY_BOARDS_FILE = "app/manually_execute_script/data/a_stock_ths_industry_boards.json"

OTHER_SECTOR_NAME = "不涉及版块"

MAX_SECTOR_COUNT = 3


SECTOR_DISAMBIGUATION_RULES = """
一、总原则

1. 只判断新闻直接涉及的行业，不要因为概念联想无限扩散。
2. 如果新闻只提到宏观政策、指数涨跌、市场情绪，但没有明确产业对象，返回“不涉及版块”。
3. 如果新闻同时涉及上游材料、中游制造、下游应用，优先选择新闻核心事件直接作用的环节。
4. 如果新闻明确提到上市公司，并且该公司属于候选行业之一，优先参考该公司主营业务对应行业。
5. 本地股票行业提示只是辅助线索。如果提示中的股票名在新闻里明显只是普通概念，不要采用该提示。
6. “AI、6G、低空经济、机器人、算力、大模型”等是概念词，不是行业名，必须映射到候选集里的行业。
7. “其他电子”“其他电源设备”“其他社会服务”等其他类行业是兜底行业，只有无法归入更明确细分行业时才使用。
8. 行业选择必须宁缺毋滥。不能确定时少选，不要为了凑数量多选。

二、通信服务 vs 通信设备

通信服务：
- 核心是运营、建设、维护、租赁、网络服务、IDC、云通信、短信、流量、通信工程、增值服务。
- 关键词：运营商、移动、联通、电信、通信工程、网络运维、IDC、数据中心运营、云通信、短信服务、专线、流量经营、算力租赁服务。
- 新闻说运营商投资、通信服务合同、IDC扩容、通信工程订单、数据中心服务、算力租赁服务，优先“通信服务”或“IT服务”，不要选“通信设备”。

通信设备：
- 核心是通信硬件设备和通信硬件产业链。
- 关键词：基站设备、光模块、光通信、光纤光缆、交换机、路由器、通信模组、射频器件、天线、通信终端、卫星通信设备、网络设备。
- 新闻说 5G/6G 基站、光模块、光通信设备、通信模组、光纤光缆、网络设备、卫星通信终端，优先“通信设备”。

裁决：
- 建设、运营、服务、租赁、工程、运维，偏“通信服务”。
- 设备、器件、模组、光模块、线缆、终端、基站硬件，偏“通信设备”。
- 6G 频率、6G 标准、网络建设规划：如果偏网络建设和设备升级，优先“通信设备”；如果偏运营商网络服务，优先“通信服务”；无法判断具体产业链时只选最直接的一个。

三、电子化学品 vs 其他电子

电子化学品：
- 核心是用于半导体、面板、PCB、光伏电子制造过程的专用化学材料。
- 关键词：光刻胶、电子特气、湿电子化学品、CMP抛光液、CMP抛光垫、显影液、刻蚀液、清洗液、前驱体、封装胶、PI材料、PCB化学品、面板化学材料。
- 新闻说某类化学材料国产替代、涨价、扩产、进入晶圆厂供应链，优先“电子化学品”。

其他电子：
- 核心是无法归入半导体、元件、光学光电子、消费电子、电子化学品的电子类业务。
- 这是电子行业兜底，不是看见“电子”就选。
- 只有新闻明确属于电子行业，但无法落入更细分行业时，才选“其他电子”。

裁决：
- 出现光刻胶、电子特气、湿电子、CMP、显影、刻蚀、清洗、前驱体，优先“电子化学品”。
- 出现芯片、晶圆、封测、半导体设备、功率器件、GPU、AI芯片，优先“半导体”。
- 出现 PCB、电容、电阻、电感、连接器、被动元件，优先“元件”。
- 出现面板、LED、显示、摄像头模组、光学镜头，优先“光学光电子”。
- 以上都不命中，才考虑“其他电子”。

四、IT服务 vs 软件开发 vs 计算机设备

软件开发：
- 核心是软件产品本身。
- 关键词：操作系统、数据库、中间件、办公软件、ERP、工业软件、安全软件、AI软件、SaaS产品、应用软件、算法平台、模型平台。
- 新闻说软件发布、软件订单、国产软件替代、AI应用平台，优先“软件开发”。

IT服务：
- 核心是为企业或政府提供信息化服务、系统集成、云服务、算力服务、数据中心服务、外包服务。
- 关键词：系统集成、数字化转型、云服务、IT外包、数据中心、IDC、算力租赁、算力服务、信息化项目、政企IT服务。
- 新闻说算力租赁、云计算服务、系统集成大单、政企信息化项目，优先“IT服务”。

计算机设备：
- 核心是计算机硬件设备。
- 关键词：服务器、整机、存储设备、AI服务器、边缘计算设备、终端设备、工控机、金融机具、安防硬件、打印机、扫描仪、POS机。
- 新闻说服务器、AI服务器、算力硬件、存储、终端设备，优先“计算机设备”。

裁决：
- 卖软件、软件产品、软件平台，选“软件开发”。
- 做项目、系统集成、云服务、算力租赁、IDC服务，选“IT服务”。
- 卖服务器、终端、硬件设备，选“计算机设备”。

五、半导体 vs 元件 vs 消费电子

半导体：
- 芯片设计、晶圆制造、封测、半导体设备、半导体材料、功率器件、AI芯片、GPU、存储芯片、模拟芯片、MCU、传感芯片。

元件：
- PCB、电容、电阻、电感、连接器、被动元件、电路板、电子元器件。

消费电子：
- 手机、耳机、平板、电脑、智能手表、AR/VR、AI眼镜、智能终端、整机组装、消费电子零部件。

裁决：
- 芯片本体和晶圆产业链，优先“半导体”。
- PCB、连接器、被动元件，优先“元件”。
- 手机、耳机、AI眼镜、智能穿戴、消费终端，优先“消费电子”。

六、其他电源设备 vs 电池 vs 电网设备

其他电源设备：
- 核心是电源、电力电子、UPS、逆变器、充电模块、电源模块、电源管理设备、服务器电源、AIDC供电、电源架构。
- 新闻说 AIDC供电、服务器电源、数据中心供电架构、阶跃脉冲负载、电源模块、UPS、逆变器，优先“其他电源设备”。

电池：
- 核心是锂电池、电芯、正负极、隔膜、电解液、电池材料、电池制造、电池回收。
- 新闻说动力电池、储能电池、电芯、锂电材料，优先“电池”。

电网设备：
- 核心是输配电设备、电力变压器、开关柜、智能电表、特高压、配网设备、电缆、继电保护。
- 新闻说电网投资、输配电、变压器、开关、智能电表，优先“电网设备”。

裁决：
- 数据中心、AI服务器、AIDC 的供电产品，优先“其他电源设备”。
- 电芯和电池材料，优先“电池”。
- 电网侧输配电设备，优先“电网设备”。

七、返回数量规则

1. 默认返回 1 个最核心行业。
2. 只有新闻明确同时影响多个产业链环节时，才返回多个。
3. 多个行业最多返回 3 个。
4. 不要因为一个新闻里出现多个概念词就返回多个行业。
5. 如果只能通过远距离联想才能关联某行业，不要返回该行业。

八、典型样例

新闻：6G试验频率正式获批，6G有望商用。
优先：通信设备。
可选：通信服务。
不要返回：软件开发、其他电子。

新闻：光刻胶国产替代加速，多家公司扩产。
优先：电子化学品。
不要返回：其他电子。

新闻：DeepSeek API降价，算力租赁上市公司回应海外卡部署传闻。
优先：IT服务。
可选：计算机设备、半导体，只有新闻明确提到服务器、GPU、芯片供给时才选。
不要返回：软件开发，除非新闻核心是软件产品或大模型应用开发。

新闻：AIDC供电架构变革，毫秒级阶跃脉冲负载产品成为结构性必需品。
优先：其他电源设备。
可选：计算机设备，只有新闻明确提到服务器整机或计算机硬件设备时才选。
不要返回：电池、电网设备、半导体。

新闻：光模块需求受AI数据中心拉动。
优先：通信设备。
可选：通信服务，只有新闻核心是IDC运营或云服务时才选。

新闻：AI服务器订单增长。
优先：计算机设备。
可选：半导体，只有新闻明确提到GPU、AI芯片供给时才选。
""".strip()


NEWS_SECTOR_JUDGE_SYSTEM_PROMPT_TEMPLATE = """
你是一个A股新闻行业板块判断助手。

你的任务：
根据输入的一条新闻，从给定的【同花顺行业板块候选集】中判断该新闻直接涉及的A股行业板块。

同花顺行业板块候选集：
{industry_board_names_text}

行业边界裁决规则：
{sector_disambiguation_rules}

严格要求：
1. 只能从【同花顺行业板块候选集】中选择 sector_name。
2. 如果新闻不涉及明确行业板块，sector_name 必须返回“不涉及版块”。
3. 不允许编造候选集外的板块名称。
4. 只判断新闻直接涉及的行业板块，不判断概念板块。
5. 不要分析利好利空。
6. 不要输出分数。
7. 不要输出分析理由。
8. 不要输出 Markdown。
9. 只输出严格 JSON。
10. 输出字段必须严格使用 sector_name 和 sector_llm_analysis。
11. sector_llm_analysis 必须为 null。
12. 默认返回 1 个最核心行业，只有新闻明确涉及多个直接行业时才返回多个。
13. 最多返回 3 个行业板块。
14. 如果多个行业都相关，按新闻相关性从高到低排序。
15. “其他电子”“其他电源设备”“其他社会服务”等其他类行业只能作为兜底，不能优先选择。
16. 在输出前自行检查：sector_name 是否完全等于候选集中的某个名称；不完全一致则改为“不涉及版块”。

返回格式必须是 JSON 数组：

[
  {
    "sector_name": "板块名称",
    "sector_llm_analysis": null
  }
]

如果新闻不涉及明确A股行业板块，返回：

[
  {
    "sector_name": "不涉及版块",
    "sector_llm_analysis": null
  }
]
""".strip()


NEWS_SECTOR_JUDGE_SYSTEM_PROMPT = NEWS_SECTOR_JUDGE_SYSTEM_PROMPT_TEMPLATE


def _resolve_path(file_path: str | Path) -> Path:
    path = Path(file_path)

    if path.is_absolute():
        return path

    return PROJECT_ROOT / path


@lru_cache(maxsize=16)
def _load_ths_industry_data(file_path: str = THS_INDUSTRY_BOARDS_FILE) -> tuple[dict[str, Any], ...]:
    path = _resolve_path(file_path)

    if not path.exists():
        raise FileNotFoundError(f"同花顺行业板块文件不存在: {path}")

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"同花顺行业板块文件不是合法 JSON: {path}") from exc

    industries = data.get("industries")

    if not isinstance(industries, list):
        raise RuntimeError(f"同花顺行业板块文件缺少 industries 数组: {path}")

    return tuple(item for item in industries if isinstance(item, dict))


@lru_cache(maxsize=16)
def _load_ths_industry_board_names(file_path: str = THS_INDUSTRY_BOARDS_FILE) -> tuple[str, ...]:
    names: list[str] = []
    seen: set[str] = set()

    for industry in _load_ths_industry_data(file_path):
        name = str(industry.get("name") or "").strip()

        if not name or name in seen:
            continue

        seen.add(name)
        names.append(name)

    if not names:
        raise RuntimeError(f"同花顺行业板块文件没有解析出任何行业名称: {_resolve_path(file_path)}")

    return tuple(names)


@lru_cache(maxsize=16)
def _load_stock_industry_hint_map(
    file_path: str = THS_INDUSTRY_BOARDS_FILE,
) -> dict[str, tuple[str, ...]]:
    mapping: dict[str, set[str]] = {}

    for industry in _load_ths_industry_data(file_path):
        industry_name = str(industry.get("name") or "").strip()
        stocks = industry.get("stocks")

        if not industry_name or not isinstance(stocks, list):
            continue

        for stock in stocks:
            if not isinstance(stock, dict):
                continue

            stock_name = str(stock.get("name") or "").strip()

            if not stock_name:
                continue

            mapping.setdefault(stock_name, set()).add(industry_name)

    return {
        stock_name: tuple(sorted(industry_names))
        for stock_name, industry_names in mapping.items()
    }


def _build_industry_board_names_text(names: tuple[str, ...]) -> str:
    return "\n".join(f"- {name}" for name in names)


def _build_news_sector_judge_system_prompt(names: tuple[str, ...]) -> str:
    return (
        NEWS_SECTOR_JUDGE_SYSTEM_PROMPT_TEMPLATE
        .replace("{industry_board_names_text}", _build_industry_board_names_text(names))
        .replace("{sector_disambiguation_rules}", SECTOR_DISAMBIGUATION_RULES)
    )


def _build_stock_industry_hints(
    *,
    title: str,
    content: str,
    industry_boards_file: str,
    max_hints: int = 20,
) -> str:
    text = f"{title or ''}\n{content or ''}"
    hint_map = _load_stock_industry_hint_map(industry_boards_file)

    hints: list[str] = []
    seen: set[str] = set()

    for stock_name, industry_names in hint_map.items():
        if stock_name not in text:
            continue

        for industry_name in industry_names:
            key = f"{stock_name}:{industry_name}"

            if key in seen:
                continue

            seen.add(key)
            hints.append(f"- {stock_name} -> {industry_name}")

            if len(hints) >= max_hints:
                return "\n".join(hints)

    return "\n".join(hints) if hints else "无"


def _build_news_sector_judge_user_prompt(
    *,
    title: str,
    content: str,
    publish_time: str,
    industry_boards_file: str,
) -> str:
    return (
        "新闻标题：\n"
        f"{title or ''}\n\n"
        "新闻正文：\n"
        f"{content or ''}\n\n"
        "发布时间：\n"
        f"{publish_time or ''}\n\n"
        "本地股票行业提示：\n"
        f"{_build_stock_industry_hints(title=title, content=content, industry_boards_file=industry_boards_file)}\n\n"
        "请从系统提示词给定的同花顺行业板块候选集中，判断该新闻直接涉及的A股行业板块。"
    )


class NewsSectorJudgeLLMAnalyzer(BaseLLM):
    """
    新闻行业板块判断分析器。

    输入：
    title
    content
    publish_time

    输出：
    list[NewsSectorLLMAnalysis]
    """

    def __init__(
        self,
        *,
        industry_boards_file: str = THS_INDUSTRY_BOARDS_FILE,
        **llm_kwargs: Any,
    ) -> None:
        super().__init__(**llm_kwargs)

        self.industry_boards_file = industry_boards_file
        self.industry_board_names = _load_ths_industry_board_names(industry_boards_file)
        self.valid_sector_names = set(self.industry_board_names) | {OTHER_SECTOR_NAME}
        self.system_prompt = _build_news_sector_judge_system_prompt(self.industry_board_names)
        self._result_adapter = TypeAdapter(list[NewsSectorLLMAnalysis])

    async def analyze(
        self,
        *,
        title: str,
        content: str,
        publish_time: str,
        temperature: float | None = 0,
        max_tokens: int | None = 3000,
        max_retries: int = 2,
    ) -> list[NewsSectorLLMAnalysis]:
        title = (title or "").strip()
        content = (content or "").strip()
        publish_time = (publish_time or "").strip()

        if not title and not content:
            raise ValueError("title 和 content 不能同时为空")

        raw_result = await asyncio.to_thread(
            self.chat,
            system_prompt=self.system_prompt,
            user_prompt=_build_news_sector_judge_user_prompt(
                title=title,
                content=content,
                publish_time=publish_time,
                industry_boards_file=self.industry_boards_file,
            ),
            temperature=temperature,
            max_tokens=max_tokens,
            max_retries=max_retries,
        )

        data = self._loads_json_array(raw_result)
        result = self._result_adapter.validate_python(data)
        return self._normalize_result(result)

    @staticmethod
    def _loads_json_array(raw_result: str) -> Any:
        json_text = BaseLLM.extract_json_text(raw_result)

        try:
            data = json.loads(json_text)
        except json.JSONDecodeError as exc:
            raise LLMResponseError(
                f"新闻行业板块判断返回的内容不是合法 JSON: {json_text[:300]}"
            ) from exc

        if not isinstance(data, list):
            raise LLMResponseError(
                f"新闻行业板块判断返回的 JSON 顶层必须是数组: {json_text[:300]}"
            )

        return data

    def _normalize_result(
        self,
        result: list[NewsSectorLLMAnalysis],
    ) -> list[NewsSectorLLMAnalysis]:
        normalized: list[NewsSectorLLMAnalysis] = []
        seen: set[str] = set()

        for item in result:
            sector_name = item.sector_name.strip()

            if not sector_name:
                continue

            if sector_name not in self.valid_sector_names:
                continue

            if sector_name in seen:
                continue

            seen.add(sector_name)
            normalized.append(
                NewsSectorLLMAnalysis(
                    sector_name=sector_name,
                    sector_llm_analysis=None,
                )
            )

            if len(normalized) >= MAX_SECTOR_COUNT:
                break

        valid_sectors = [
            item
            for item in normalized
            if item.sector_name != OTHER_SECTOR_NAME
        ]

        if valid_sectors:
            return valid_sectors

        return [
            NewsSectorLLMAnalysis(
                sector_name=OTHER_SECTOR_NAME,
                sector_llm_analysis=None,
            )
        ]