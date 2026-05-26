from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from app.llm import NewsSectorJudgeLLMAnalyzer


FIXTURE_INDUSTRY_BOARDS_FILE = Path(__file__).parent / "fixtures" / "industry_boards.json"


def build_analyzer() -> NewsSectorJudgeLLMAnalyzer:
    return NewsSectorJudgeLLMAnalyzer(
        api_key="test-key",
        model="test-model",
        api_base_url="https://example.com/v1",
        industry_boards_file=str(FIXTURE_INDUSTRY_BOARDS_FILE),
    )


def test_news_sector_judge_analyzer_discards_detail_fields_and_invalid_names(
) -> None:
    analyzer = build_analyzer()

    def fake_chat(**kwargs: Any) -> str:
        assert "sector_name" in kwargs["system_prompt"]
        assert "发布时间" in kwargs["user_prompt"]
        assert kwargs["temperature"] == 0
        return """
        [
          {
            "sector_name": " 半导体 ",
            "sector_llm_analysis": {
              "score": 90,
              "reason": "不应该出现在第一阶段",
              "companies": ["芯片公司"]
            }
          },
          {
            "sector_name": "人工智能",
            "sector_llm_analysis": null
          }
        ]
        """

    analyzer.chat = fake_chat  # type: ignore[method-assign]

    result = asyncio.run(
        analyzer.analyze(
            title="芯片公司发布新一代工业机器人芯片",
            content="公司称新产品将用于工业机器人控制器和智能制造设备。",
            publish_time="2026-05-21 09:30:00",
        )
    )

    assert [item.sector_name for item in result] == ["半导体"]
    assert all(item.sector_llm_analysis is None for item in result)


def test_news_sector_judge_analyzer_falls_back_to_other_sector(
) -> None:
    analyzer = build_analyzer()

    def fake_chat(**kwargs: Any) -> str:
        return """
        [
          {
            "sector_name": "   ",
            "sector_llm_analysis": null
          },
          {
            "sector_name": "候选集外板块",
            "sector_llm_analysis": null
          }
        ]
        """

    analyzer.chat = fake_chat  # type: ignore[method-assign]

    result = asyncio.run(
        analyzer.analyze(
            title="市场全天震荡整理",
            content="主要指数涨跌互现，未提及明确产业方向。",
            publish_time="2026-05-21 09:30:00",
        )
    )

    assert len(result) == 1
    assert result[0].sector_name == "不涉及版块"
    assert result[0].sector_llm_analysis is None
