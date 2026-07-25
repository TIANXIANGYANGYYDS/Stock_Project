from __future__ import annotations

import json
import re
from collections.abc import Sequence
from typing import Any

from pydantic import TypeAdapter

from app.llm.base_llm import BaseLLM, LLMResponseError
from app.models import NewsLLMAnalysis, NewsSectorLLMAnalysis


OTHER_SECTOR_NAME = "不涉及版块"

MAX_COMPANY_COUNT = 8


NEWS_SECTOR_DETAIL_SYSTEM_PROMPT_TEMPLATE = """
你是一个面向 A 股事件驱动交易与舆情研判的新闻板块详情分析助手。

你的任务：
根据输入的一条新闻，以及已经由第一阶段确定好的【待分析行业板块】，判断该新闻在未来 1~3 个交易日内，对每个输入板块的短线交易方向和把握程度。

注意：
你不是新闻摘要助手。
你不是重新判断板块的助手。
你只负责给输入的每个 sector_name 补充 sector_llm_analysis。

====================
一、输入板块约束
====================

1. 输出中的 sector_name 必须完全等于输入的待分析板块名称。
2. 不允许新增板块。
3. 不允许删除板块。
4. 不允许改写板块名称。
5. 不允许输出输入列表之外的板块。
6. 如果某个输入板块与新闻实际影响无关，仍然要保留该 sector_name，但 sector_llm_analysis 返回 null。
7. 如果输入板块是“不涉及版块”，sector_llm_analysis 必须返回 null。

====================
二、分析目标
====================

你评估的不是“新闻重要性”，而是：

1. 该消息是否能直接传导到当前输入板块。
2. 该消息是否会被 A 股市场在未来 1~3 个交易日内交易。
3. 当前板块更可能上涨、下跌，还是方向不明确。
4. 这个方向判断的把握有多高。

如果新闻能清晰作用于输入板块，且方向明确，就不要机械给 0。
如果只是泛泛提到行业、会议、口号、长期规划，但缺少短期交易催化，应明显压分。

====================
三、score 定义
====================

score 是整数，范围必须在 -100 到 100。

score > 0：对当前板块偏利好，未来 1~3 个交易日上涨概率/把握更高。
score < 0：对当前板块偏利空，未来 1~3 个交易日下跌概率/把握更高。
score = 0：方向不明确、多空抵消、影响过弱，或无法形成短线交易判断。

score 不是新闻热度，不是长期价值判断，不是简单利好利空标签。
abs(score) 越大，代表方向越清晰、短线交易胜率越高、越容易被盘面承接。

正分区间：
+90 ~ +100：极强直接催化，近乎明牌，极可能引发强势上涨。
+80 ~ +89：高确定性强催化，大概率上涨。
+70 ~ +79：较高把握，板块短线承接概率较强。
+60 ~ +69：中高把握，有明确上涨概率。
+40 ~ +59：中等把握，有交易价值但确定性一般。
+20 ~ +39：偏弱正向，有一定上涨预期但催化不强。
+1 ~ +19：轻微正向，只能识别出弱传导。

负分完全对称：
-90 ~ -100：极强直接利空，极可能引发明显下跌。
-80 ~ -89：高确定性强利空。
-70 ~ -79：较高把握利空。
-60 ~ -69：中高把握利空。
-40 ~ -59：中等利空。
-20 ~ -39：偏弱利空。
-1 ~ -19：轻微负向。

====================
四、加分规则
====================

以下事件如果能直接作用于当前输入板块，通常应提高 abs(score)：

1. 国家级、部委级政策明确落地。
2. 明确补贴、收储、限产、配额、税收优惠、监管放松或监管收紧。
3. 明确涨价、跌价、供需缺口、库存拐点。
4. 行业订单放量、核心客户导入、产能紧缺、量产突破。
5. 技术突破可以直接改变产业链需求或利润预期。
6. 明确影响上市公司收入、利润、订单、成本、合规风险。
7. 新闻具备短线资金容易理解、容易交易、容易扩散的逻辑。

如果同时满足“政策/供需/价格/订单明确 + A股映射清楚 + 短线交易性强”，score 通常不应低于 60。
如果是高确定性强事件，score 可以进入 80 以上。

====================
五、压分规则
====================

以下情况必须压分：

1. 只是会议、表态、倡议、长期规划，缺少落地细节。
2. 是旧闻、重复消息、市场已充分预期。
3. 是未经证实的传闻。
4. 只是宏观情绪，不能落到当前板块经营、需求、价格、订单、成本或监管。
5. 多空因素明显对冲。
6. 新闻只提到概念词，但没有实质产业变化。
7. 输入板块只是远距离联想，并非新闻直接作用对象。
8. 海外事件无法清晰传导到 A 股，或传导链条过长。

传闻类消息：
- 未证实传闻，abs(score) 通常不超过 40。
- 公司澄清利空传闻，若能缓解板块风险，可给正分，但通常不超过 50。
- 公司确认利空传闻，按实际影响给负分。
- 无法判断真实性和影响方向时，score 返回 0 或 sector_llm_analysis 返回 null。

会议/表态类消息：
- 只有泛泛支持，没有新增政策工具，abs(score) 通常不超过 30。
- 如果会议明确提出可执行政策、资金、补贴、监管安排，可按政策强度上调。

====================
六、方向判断
====================

对每个输入板块，必须先判断净方向：

1. 明确偏多：
   sector_llm_analysis.score 为正数。

2. 明确偏空：
   sector_llm_analysis.score 为负数。

3. 中性 / 方向不清 / 无直接影响：
   如果仍有轻微信息价值，score 可以为 0。
   如果与该板块没有直接影响，sector_llm_analysis 返回 null。

注意：
如果 sector_llm_analysis 不为 null，必须给出 score 和 reason。
如果方向明确，原则上不要给 0。
如果方向明确但很弱，使用 +1 到 +19 或 -1 到 -19。
如果只是输入板块名被新闻提到，但没有可判断的交易方向，可以给 0。

====================
七、reason 要求
====================

reason 必须简洁，但要有信息量。

要求：
1. 2~4 句以内。
2. 必须体现“事件 -> 传导路径 -> 当前板块短线方向/确定性”。
3. 必须说明为什么分数不是更高或更低。
4. 不要只复述新闻。
5. 不要写空话。
6. 不要输出交易建议。
7. 不要预测具体涨跌幅。
8. 不要超过 80 个中文字。

优秀 reason 示例：
“电报提到 AIDC 供电架构变化，直接强化服务器电源和电源模块需求预期，对其他电源设备形成短线催化。但未披露明确订单或政策落地，分数不宜过高。”

差 reason 示例：
“该消息利好相关板块，未来有望上涨。”

====================
八、companies 要求
====================

1. companies 只填写新闻中直接出现的公司，或映射关系极其明确的公司。
2. 不允许根据板块联想公司。
3. 不允许从行业股票池中补全公司。
4. 没有明确公司时，companies 必须为 null。
5. companies 最多返回 8 个。
6. 公司名称使用新闻中的原始名称。

====================
九、输出格式
====================

最终只能输出严格 JSON 数组。
不要输出 Markdown。
不要输出代码块。
不要输出任何额外解释。

数组长度必须与输入的待分析行业板块数量一致。
数组顺序必须与输入的待分析行业板块顺序一致。

每个元素字段必须严格为：
sector_name
sector_llm_analysis

sector_llm_analysis 如果不为 null，字段必须严格为：
score
reason
companies

返回示例：

[
  {
    "sector_name": "其他电源设备",
    "sector_llm_analysis": {
      "score": 62,
      "reason": "AIDC供电架构变化直接强化服务器电源和电源模块需求，对板块形成短线催化。但缺少明确订单数据，确定性低于强政策事件。",
      "companies": null
    }
  }
]

如果某个板块无直接影响，返回：

[
  {
    "sector_name": "板块名称",
    "sector_llm_analysis": null
  }
]

如果输入板块是“不涉及版块”，返回：

[
  {
    "sector_name": "不涉及版块",
    "sector_llm_analysis": null
  }
]

====================
十、最终自检
====================

输出前必须自检：

1. 是否只输出 JSON 数组。
2. 是否没有 Markdown、没有代码块、没有额外文字。
3. 数组长度是否等于输入板块数量。
4. sector_name 是否完全等于输入板块名称。
5. 是否没有新增、删除、改写板块。
6. score 是否为整数，且在 -100 到 100。
7. companies 没有时是否为 null。
8. 无直接影响的板块是否返回 sector_llm_analysis 为 null。
9. reason 是否体现“事件 -> 传导路径 -> 当前板块短线方向/确定性”。
10. 是否避免把泛泛会议、旧闻、传闻打成过高分。
""".strip()


NEWS_SECTOR_DETAIL_SYSTEM_PROMPT = NEWS_SECTOR_DETAIL_SYSTEM_PROMPT_TEMPLATE


def _build_sector_names_text(sector_names: Sequence[str]) -> str:
    """把待分析行业名称按固定顺序转换为提示词中的项目列表。"""
    return "\n".join(f"- {sector_name}" for sector_name in sector_names)


def _build_news_sector_detail_user_prompt(
    *,
    title: str,
    content: str,
    publish_time: str,
    sector_names: Sequence[str],
) -> str:
    """组装新闻板块详情阶段的用户提示词。

    标题、正文和发布时间用于提供事件上下文；板块名称会原样列出，要求模型
    按相同顺序返回，便于后续校验、去重和补齐缺失项。
    """
    return (
        "新闻标题：\n"
        f"{title or ''}\n\n"
        "新闻正文：\n"
        f"{content or ''}\n\n"
        "发布时间：\n"
        f"{publish_time or ''}\n\n"
        "待分析行业板块，必须按此顺序原样返回：\n"
        f"{_build_sector_names_text(sector_names)}\n\n"
        "请只针对上述板块，判断新闻在未来 1~3 个交易日内对每个板块的短线交易方向、传导路径和确定性。"
    )


class NewsSectorDetailLLMAnalyzer(BaseLLM):
    """
    新闻行业板块详情分析器。

    输入：
    title
    content
    publish_time
    sectors

    输出：
    list[NewsSectorLLMAnalysis]

    说明：
    该分析器只做第二阶段详情分析。
    第一阶段已经判断出的 sector_name 必须原样传入。
    """

    def __init__(self, **llm_kwargs: Any) -> None:
        """初始化详情分析器并准备固定系统提示词和结果适配器。"""
        super().__init__(**llm_kwargs)

        # 详情阶段专用系统约束，规定板块名称、分数、理由和公司字段的输出边界。
        self.system_prompt = NEWS_SECTOR_DETAIL_SYSTEM_PROMPT
        # Pydantic 适配器，用于把清洗后的数组校验成业务模型列表。
        self._result_adapter = TypeAdapter(list[NewsSectorLLMAnalysis])

    async def analyze(
        self,
        *,
        title: str,
        content: str,
        publish_time: str,
        sectors: Sequence[str | NewsSectorLLMAnalysis] | None,
        temperature: float | None = 0,
        max_tokens: int | None = 3000,
        max_retries: int = 2,
    ) -> list[NewsSectorLLMAnalysis]:
        """分析一条新闻对输入板块未来 1~3 个交易日的短线影响。

        先规范输入板块并调用统一 LLM 入口，再解析、过滤和校验模型返回值；最终
        按输入顺序返回每个板块一条记录。缺失、重复或越界的模型输出不会直接泄漏
        到业务层，而是按安全的中性/空分析结果处理。
        """
        title = (title or "").strip()
        content = (content or "").strip()
        publish_time = (publish_time or "").strip()
        sector_names = self._normalize_input_sector_names(sectors)

        if not title and not content:
            raise ValueError("title 和 content 不能同时为空")

        raw_result = await self.async_chat(
            system_prompt=self.system_prompt,
            user_prompt=_build_news_sector_detail_user_prompt(
                title=title,
                content=content,
                publish_time=publish_time,
                sector_names=sector_names,
            ),
            temperature=temperature,
            max_tokens=max_tokens,
            max_retries=max_retries,
        )

        data = self._loads_json_array(raw_result)
        data = self._sanitize_raw_data(data, valid_sector_names=set(sector_names))

        try:
            result = self._result_adapter.validate_python(data)
        except Exception as exc:
            raise LLMResponseError(
                f"新闻行业板块详情分析返回内容无法通过模型校验: {str(exc)[:300]}"
            ) from exc

        return self._normalize_result(
            result=result,
            input_sector_names=sector_names,
        )

    @staticmethod
    def _normalize_input_sector_names(
        sectors: Sequence[str | NewsSectorLLMAnalysis] | None,
    ) -> tuple[str, ...]:
        """清洗输入板块名称，去重并确定无有效板块时的兜底板块。

        既接受原始字符串，也接受第一阶段已经生成的板块模型；空值、空字符串和
        重复名称会被忽略。当输入同时包含“不涉及版块”和其他板块时，移除该兜底项，
        避免它占用真实板块的输出位置。
        """
        if not sectors:
            return (OTHER_SECTOR_NAME,)

        names: list[str] = []
        seen: set[str] = set()

        for item in sectors:
            if isinstance(item, NewsSectorLLMAnalysis):
                sector_name = item.sector_name
            else:
                sector_name = str(item or "")

            sector_name = sector_name.strip()

            if not sector_name:
                continue

            if sector_name in seen:
                continue

            seen.add(sector_name)
            names.append(sector_name)

        if not names:
            return (OTHER_SECTOR_NAME,)

        if len(names) > 1 and OTHER_SECTOR_NAME in names:
            names = [
                sector_name for sector_name in names if sector_name != OTHER_SECTOR_NAME
            ]

        return tuple(names)

    @staticmethod
    def _loads_json_array(raw_result: str) -> Any:
        """解析模型原文并确认顶层结构是 JSON 数组。

        允许原文带 Markdown 代码块或少量解释，由公共提取器负责剥离；JSON 非法或
        顶层不是数组时统一抛出领域异常，调用方可据此触发重试。
        """
        json_text = BaseLLM.extract_json_text(raw_result)

        try:
            data = json.loads(json_text)
        except json.JSONDecodeError as exc:
            raise LLMResponseError(
                f"新闻行业板块详情分析返回的内容不是合法 JSON: {json_text[:300]}"
            ) from exc

        if not isinstance(data, list):
            raise LLMResponseError(
                f"新闻行业板块详情分析返回的 JSON 顶层必须是数组: {json_text[:300]}"
            )

        return data

    @classmethod
    def _sanitize_raw_data(
        cls,
        data: list[Any],
        *,
        valid_sector_names: set[str],
    ) -> list[dict[str, Any]]:
        """过滤并规整模型数组，保留合法板块和可用的详情字段。

        未知板块、非对象元素、非法分数、空理由以及错误结构都会被丢弃或降级为
        空分析；分数和公司列表先经过宽容转换，再交给 Pydantic 做最终结构校验。
        """
        sanitized: list[dict[str, Any]] = []

        for item in data:
            if not isinstance(item, dict):
                continue

            sector_name = str(item.get("sector_name") or "").strip()

            if not sector_name:
                continue

            if sector_name not in valid_sector_names:
                continue

            analysis = item.get("sector_llm_analysis")

            if sector_name == OTHER_SECTOR_NAME or analysis is None:
                sanitized.append(
                    {
                        "sector_name": sector_name,
                        "sector_llm_analysis": None,
                    }
                )
                continue

            if not isinstance(analysis, dict):
                sanitized.append(
                    {
                        "sector_name": sector_name,
                        "sector_llm_analysis": None,
                    }
                )
                continue

            score = cls._coerce_score(analysis.get("score"))
            reason = str(analysis.get("reason") or "").strip()
            companies = cls._coerce_companies(analysis.get("companies"))

            if score is None or not reason:
                sanitized.append(
                    {
                        "sector_name": sector_name,
                        "sector_llm_analysis": None,
                    }
                )
                continue

            sanitized.append(
                {
                    "sector_name": sector_name,
                    "sector_llm_analysis": {
                        "score": score,
                        "reason": reason,
                        "companies": companies,
                    },
                }
            )

        return sanitized

    @staticmethod
    def _coerce_score(value: Any) -> int | None:
        """把模型可能返回的数字、数字字符串或带“分”后缀的值转成整数。

        布尔值、空字符串和无法解析的类型返回 ``None``；结果会限制在业务允许的
        [-100, 100] 范围内，避免异常值破坏后续校验。
        """
        if isinstance(value, bool):
            return None

        if isinstance(value, int):
            score = value
        elif isinstance(value, float):
            score = round(value)
        elif isinstance(value, str):
            text = value.strip()

            if not text:
                return None

            text = text.removeprefix("+")
            text = text.replace("分", "").strip()

            try:
                score = round(float(text))
            except ValueError:
                return None
        else:
            return None

        if score > 100:
            return 100

        if score < -100:
            return -100

        return int(score)

    @staticmethod
    def _coerce_companies(value: Any) -> list[str] | None:
        """把公司字段规范为去重、最多八项的字符串列表或 ``None``。

        兼容模型返回的分隔字符串和列表结构，并过滤“无明确公司”等占位文本；公司
        名称本身不做行业推断，保留模型原始表述供上层审计。
        """
        if value is None:
            return None

        raw_items: list[Any]

        if isinstance(value, str):
            text = value.strip()

            if not text:
                return None

            if text.lower() in {"none", "null"} or text in {"无", "无明确公司", "没有"}:
                return None

            raw_items = re.split(r"[、,，;；\s]+", text)
        elif isinstance(value, list | tuple | set):
            raw_items = list(value)
        else:
            return None

        companies: list[str] = []
        seen: set[str] = set()

        for raw_item in raw_items:
            company = str(raw_item or "").strip()

            if not company:
                continue

            if company.lower() in {"none", "null"} or company in {
                "无",
                "无明确公司",
                "没有",
            }:
                continue

            if company in seen:
                continue

            seen.add(company)
            companies.append(company)

            if len(companies) >= MAX_COMPANY_COUNT:
                break

        return companies or None

    def _normalize_result(
        self,
        *,
        result: list[NewsSectorLLMAnalysis],
        input_sector_names: tuple[str, ...],
    ) -> list[NewsSectorLLMAnalysis]:
        """按输入板块顺序补齐最终结果，并统一空分析和字段格式。

        模型可能漏项、乱序或重复返回；方法只接受输入集合内的首个记录，并为所有
        缺失板块生成 ``sector_llm_analysis=None``，从而向服务层提供稳定的定长结果。
        “不涉及版块”始终保持空分析，不参与短线方向判断。
        """
        result_map: dict[str, NewsLLMAnalysis | None] = {}

        for item in result:
            sector_name = item.sector_name.strip()

            if sector_name not in input_sector_names:
                continue

            if sector_name in result_map:
                continue

            if sector_name == OTHER_SECTOR_NAME:
                result_map[sector_name] = None
                continue

            analysis = item.sector_llm_analysis

            if analysis is None:
                result_map[sector_name] = None
                continue

            reason = analysis.reason.strip()

            result_map[sector_name] = NewsLLMAnalysis(
                score=self._coerce_score(analysis.score) or 0,
                reason=reason,
                companies=self._coerce_companies(analysis.companies),
            )

        normalized: list[NewsSectorLLMAnalysis] = []

        for sector_name in input_sector_names:
            if sector_name == OTHER_SECTOR_NAME:
                normalized.append(
                    NewsSectorLLMAnalysis(
                        sector_name=sector_name,
                        sector_llm_analysis=None,
                    )
                )
                continue

            normalized.append(
                NewsSectorLLMAnalysis(
                    sector_name=sector_name,
                    sector_llm_analysis=result_map.get(sector_name),
                )
            )

        return normalized
