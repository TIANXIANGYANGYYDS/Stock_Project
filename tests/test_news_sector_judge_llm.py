from __future__ import annotations

import asyncio

from app.llm import NewsSectorJudgeLLMAnalyzer
from app.models import News


class SyncFakeLLMClient:
    def __init__(self, response: str) -> None:
        self.response = response

    def chat(self, *, system_prompt: str, user_prompt: str) -> str:
        assert "sector_name" in system_prompt
        assert "A股市场板块" in user_prompt
        return self.response


class AsyncFakeLLMClient:
    def __init__(self, response: str) -> None:
        self.response = response

    async def chat(self, *, system_prompt: str, user_prompt: str) -> str:
        assert "sector_llm_analysis" in system_prompt
        assert "发布时间" in user_prompt
        return self.response


def build_news() -> News:
    return News(
        event_id="news-1",
        publish_time="2026-05-21 09:30:00",
        publish_ts=1747791000,
        title="某公司发布新一代工业机器人芯片",
        content="公司称新产品将用于工业机器人控制器和智能制造设备。",
        source="cls",
    )


def test_news_sector_judge_analyzer_discards_detail_fields() -> None:
    analyzer = NewsSectorJudgeLLMAnalyzer(
        SyncFakeLLMClient(
            """
            [
              {
                "sector_name": " 半导体 ",
                "sector_llm_analysis": {
                  "score": 90,
                  "reason": "不应该出现在第一阶段",
                  "companies": ["某公司"]
                }
              },
              {
                "sector_name": "人工智能",
                "sector_llm_analysis": null
              }
            ]
            """
        )
    )

    result = asyncio.run(analyzer.analyze(build_news()))

    assert [item.sector_name for item in result] == ["半导体", "人工智能"]
    assert all(item.sector_llm_analysis is None for item in result)


def test_news_sector_judge_analyzer_falls_back_to_other() -> None:
    analyzer = NewsSectorJudgeLLMAnalyzer(
        AsyncFakeLLMClient(
            """
            [
              {
                "sector_name": "   ",
                "sector_llm_analysis": null
              },
              {
                "sector_name": "   ",
                "sector_llm_analysis": null
              }
            ]
            """
        )
    )

    result = asyncio.run(analyzer.analyze(build_news()))

    assert len(result) == 1
    assert result[0].sector_name == "其他"
    assert result[0].sector_llm_analysis is None