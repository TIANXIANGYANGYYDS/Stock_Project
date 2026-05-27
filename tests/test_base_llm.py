from __future__ import annotations

import json
import sys

import pytest

from app.llm.base_llm import BaseLLM, LLMResponseError


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


def test_import_workers_package_does_not_preload_worker_modules() -> None:
    sys.modules.pop("app.workers", None)
    sys.modules.pop("app.workers.news_sector_judge_worker", None)
    sys.modules.pop("app.workers.news_sector_detail_worker", None)

    import app.workers  # noqa: F401

    assert "app.workers.news_sector_judge_worker" not in sys.modules
    assert "app.workers.news_sector_detail_worker" not in sys.modules
