from __future__ import annotations

import asyncio
import json
import logging
import sys
from typing import Literal
from unittest.mock import Mock

import pytest
import requests
from pydantic import BaseModel, ConfigDict

from app.core.config import Settings
from app.llm.base_llm import (
    BaseLLM,
    LLMRequestError,
    LLMResponseError,
    QwenAnalysisLLM,
)


class FunctionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: Literal["ok"]


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


def test_call_function_forces_strict_tool_and_disables_thinking() -> None:
    llm = QwenAnalysisLLM(
        api_key="test",
        api_base_url="https://example.com/v1",
        extra_body={"thinking": {"type": "enabled"}},
    )
    captured: dict[str, object] = {}

    def fake_post(payload: dict[str, object], max_retries: int) -> dict[str, object]:
        captured.update(payload)
        assert max_retries == 2
        return {
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {
                                "type": "function",
                                "function": {
                                    "name": "submit_result",
                                    "arguments": '{"label":"ok"}',
                                },
                            }
                        ]
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        }

    llm._post_chat_completion = fake_post  # type: ignore[method-assign]
    result = llm.call_function(
        user_prompt="提交结果",
        function_name="submit_result",
        response_schema=FunctionResult,
    )

    assert result == FunctionResult(label="ok")
    assert captured["enable_thinking"] is False
    assert "thinking" not in captured
    assert captured["tool_choice"] == {
        "type": "function",
        "function": {"name": "submit_result"},
    }
    function = captured["tools"][0]["function"]  # type: ignore[index]
    assert function["strict"] is True
    assert function["parameters"]["additionalProperties"] is False
    assert "response_format" not in captured


def test_call_function_rejects_wrong_tool_and_invalid_arguments() -> None:
    llm = BaseLLM(
        api_key="test",
        api_base_url="https://example.com/v1",
    )
    responses = iter(
        [
            {
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {
                                    "function": {
                                        "name": "another_tool",
                                        "arguments": '{"label":"ok"}',
                                    }
                                }
                            ]
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            },
            {
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {
                                    "function": {
                                        "name": "submit_result",
                                        "arguments": "{bad-json",
                                    }
                                }
                            ]
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            },
        ]
    )
    llm._post_chat_completion = lambda *args, **kwargs: next(responses)  # type: ignore[method-assign]

    with pytest.raises(LLMResponseError, match="未唯一调用"):
        llm.call_function(
            user_prompt="提交结果",
            function_name="submit_result",
            response_schema=FunctionResult,
        )
    with pytest.raises(LLMResponseError, match="不是合法 JSON"):
        llm.call_function(
            user_prompt="提交结果",
            function_name="submit_result",
            response_schema=FunctionResult,
        )


def test_async_call_function_is_compatible_with_python_38_executor() -> None:
    llm = BaseLLM(
        api_key="test",
        api_base_url="https://example.com/v1",
    )
    llm._post_chat_completion = lambda *args, **kwargs: {  # type: ignore[method-assign]
        "choices": [
            {
                "message": {
                    "tool_calls": [
                        {
                            "function": {
                                "name": "submit_result",
                                "arguments": {"label": "ok"},
                            }
                        }
                    ]
                },
                "finish_reason": "tool_calls",
            }
        ]
    }

    result = asyncio.run(
        llm.async_call_function(
            user_prompt="提交结果",
            function_name="submit_result",
            response_schema=FunctionResult,
        )
    )

    assert result.label == "ok"


def test_task_specific_model_environment_fields_are_not_supported() -> None:
    assert "llm_model" not in Settings.model_fields
    assert "llm_default_temperature" not in Settings.model_fields
    assert "llm_extra_body" not in Settings.model_fields
    assert "morning_analysis_llm_model" not in Settings.model_fields
    assert "creator_opinion_llm_model" not in Settings.model_fields


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


def test_non_2xx_provider_error_is_preserved_safely_and_retried(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """验证非成功响应会保留欠费原因、执行重试，并过滤敏感及无关字段。"""
    api_key = "test-secret-api-key"
    request_prompt = "绝不能出现在错误中的请求正文"
    response = requests.Response()
    response.status_code = 400
    response.url = "https://example.com/v1/chat/completions"
    response.encoding = "utf-8"
    response._content = json.dumps(
        {
            "error": {
                "code": "Arrearage",
                "type": "insufficient_balance",
                "message": (
                    f"账户已欠费，请充值后重试。api_key={api_key}。"
                    + "补充说明" * 200
                    + "末尾不应保留"
                ),
            },
            "request_payload": {"messages": [request_prompt]},
            "response_headers": {"敏感响应头": "不应保留"},
        },
        ensure_ascii=False,
    ).encode("utf-8")
    llm = BaseLLM(
        api_key=api_key,
        api_base_url="https://example.com/v1",
    )
    post = Mock(return_value=response)
    sleep = Mock()
    monkeypatch.setattr(llm.session, "post", post)
    monkeypatch.setattr("app.llm.base_llm.time.sleep", sleep)

    with caplog.at_level(logging.WARNING, logger="app.llm.base_llm"):
        with pytest.raises(LLMRequestError) as exc_info:
            llm.chat(user_prompt=request_prompt, max_retries=2)

    exception_text = str(exc_info.value)
    log_text = caplog.text
    assert post.call_count == 2
    sleep.assert_called_once_with(1)
    assert "Arrearage" in exception_text
    assert "insufficient_balance" in exception_text
    assert "账户已欠费，请充值后重试" in exception_text
    assert "Arrearage" in log_text
    assert "账户已欠费，请充值后重试" in log_text
    for forbidden_text in (
        api_key,
        request_prompt,
        "敏感响应头",
        "末尾不应保留",
    ):
        assert forbidden_text not in exception_text
        assert forbidden_text not in log_text


def test_import_workers_package_does_not_preload_worker_modules() -> None:
    sys.modules.pop("app.workers", None)
    sys.modules.pop("app.workers.news_sector_judge_worker", None)
    sys.modules.pop("app.workers.news_sector_detail_worker", None)
    sys.modules.pop("app.workers.creator_content_extraction_worker", None)
    sys.modules.pop("app.workers.creator_opinion_analysis_worker", None)

    import app.workers  # noqa: F401

    assert "app.workers.news_sector_judge_worker" not in sys.modules
    assert "app.workers.news_sector_detail_worker" not in sys.modules
    assert "app.workers.creator_content_extraction_worker" not in sys.modules
    assert "app.workers.creator_opinion_analysis_worker" not in sys.modules
