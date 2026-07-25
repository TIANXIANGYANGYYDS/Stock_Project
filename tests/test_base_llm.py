from __future__ import annotations

import json
import sys

import pytest

from app.core.config import Settings
from app.llm.base_llm import BaseLLM, LLMResponseError, QwenAnalysisLLM


def test_base_llm_uses_code_defaults_for_model_and_thinking() -> None:
    llm = BaseLLM(
        api_key="test",
        api_base_url="https://example.com/v1",
    )

    assert llm.model == "qwen-plus"
    assert llm.thinking_enabled is True


def test_qwen_analysis_llm_sends_fixed_model_and_thinking_parameter() -> None:
    llm = QwenAnalysisLLM(
        api_key="test",
        api_base_url="https://example.com/v1",
    )
    captured: dict[str, object] = {}

    def fake_post(payload: dict[str, object], max_retries: int) -> dict[str, object]:
        captured.update(payload)
        assert max_retries == 2
        return {
            "choices": [
                {
                    "message": {"content": "{}"},
                    "finish_reason": "stop",
                }
            ]
        }

    llm._post_chat_completion = fake_post  # type: ignore[method-assign]
    assert llm.chat(user_prompt="测试") == "{}"
    assert captured["model"] == "qwen3.7-max"
    assert captured["enable_thinking"] is True


def test_qwen_analysis_llm_replaces_empty_model_with_fixed_model() -> None:
    llm = QwenAnalysisLLM(
        api_key="test",
        model="",
        api_base_url="https://example.com/v1",
    )

    assert llm.model == "qwen3.7-max"


def test_task_specific_model_environment_fields_are_not_supported() -> None:
    assert "llm_model" not in Settings.model_fields
    assert "llm_default_temperature" not in Settings.model_fields
    assert "llm_extra_body" not in Settings.model_fields
    assert "morning_analysis_llm_model" not in Settings.model_fields
    assert "douyin_analysis_llm_model" not in Settings.model_fields


def test_thinking_enabled_recognizes_qwen_and_deepseek_parameters() -> None:
    common = {
        "api_key": "test",
        "model": "test-model",
        "api_base_url": "https://example.com/v1",
    }

    assert BaseLLM(**common, extra_body={"enable_thinking": True}).thinking_enabled
    assert BaseLLM(
        **common,
        extra_body={"thinking": {"type": "enabled"}},
    ).thinking_enabled
    assert not BaseLLM(
        **common,
        extra_body={"enable_thinking": False},
    ).thinking_enabled


def test_extract_message_content_falls_back_to_reasoning_content_json_array() -> None:
    response_data = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "reasoning_content": (
                        "先做内部判断。\n"
                        "[{\"sector_name\":\"不涉及版块\",\"sector_llm_analysis\":null}]"
                    ),
                },
                "finish_reason": "stop",
            }
        ]
    }

    content = BaseLLM._extract_message_content(response_data)

    assert json.loads(content) == [
        {
            "sector_name": "不涉及版块",
            "sector_llm_analysis": None,
        }
    ]


def test_extract_message_content_rejects_length_truncated_empty_content() -> None:
    response_data = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "reasoning_content": "[{\"sector_name\":\"半导体\"",
                },
                "finish_reason": "length",
            }
        ]
    }

    with pytest.raises(LLMResponseError, match="finish_reason=length"):
        BaseLLM._extract_message_content(response_data)


def test_extract_json_text_keeps_outer_object_when_it_contains_array() -> None:
    raw = '分析结果：{"market_style":"轮动","mainlines":[{"rank":1}]}'

    parsed = json.loads(BaseLLM.extract_json_text(raw))

    assert parsed == {
        "market_style": "轮动",
        "mainlines": [{"rank": 1}],
    }


def test_extract_json_text_prefers_final_standalone_container() -> None:
    raw = '格式示例：{}\n最终结果：[{"sector_name":"半导体"}]'

    parsed = json.loads(BaseLLM.extract_json_text(raw))

    assert parsed == [{"sector_name": "半导体"}]


def test_import_workers_package_does_not_preload_worker_modules() -> None:
    sys.modules.pop("app.workers", None)
    sys.modules.pop("app.workers.news_sector_judge_worker", None)
    sys.modules.pop("app.workers.news_sector_detail_worker", None)
    sys.modules.pop("app.workers.douyin_creator_analysis_worker", None)

    import app.workers  # noqa: F401

    assert "app.workers.news_sector_judge_worker" not in sys.modules
    assert "app.workers.news_sector_detail_worker" not in sys.modules
    assert "app.workers.douyin_creator_analysis_worker" not in sys.modules
