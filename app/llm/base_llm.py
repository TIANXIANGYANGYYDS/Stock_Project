from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any, TypeVar, cast

import requests
from pydantic import TypeAdapter

from app.core.config import get_settings


logger = logging.getLogger(__name__)

SchemaT = TypeVar("SchemaT")

# 新闻分析链路的固定默认模型；高价值分析任务会使用 QwenAnalysisLLM 覆盖。
DEFAULT_LLM_MODEL = "qwen-plus"
# 所有请求在未显式指定时使用的低随机性采样温度。
DEFAULT_LLM_TEMPERATURE = 0.2
# 深度思考是生产默认能力，不再依赖环境变量中的 JSON 字符串。
DEFAULT_LLM_EXTRA_BODY: dict[str, Any] = {"enable_thinking": True}
# 盘前和抖音分析共享的强推理模型名称。
QWEN_ANALYSIS_MODEL = "qwen3.7-max"


class LLMError(RuntimeError):
	"""所有 LLM 调用异常的共同基类。"""


class LLMConfigError(LLMError):
	"""LLM 认证、接口地址或请求参数缺失、非法时抛出的异常。"""


class LLMRequestError(LLMError):
	"""网络请求失败或供应商返回明确错误时抛出的异常。"""


class LLMResponseError(LLMError):
	"""返回内容无法解析、校验失败或违反业务约束时抛出的异常。"""


class BaseLLM:
	"""
	LLM 分析的通用请求底座。

	环境只提供部署相关的 API Key、接口地址和超时；模型、采样温度及深度思考
	默认值由本模块集中管理。业务子类仅负责提示词、输入组装和领域校验。

	运行期字段：api_key 为认证令牌；model 为模型名称；api_base_url 为兼容 OpenAI
	的服务根地址；timeout 为请求超时秒数；default_temperature 为默认采样温度；
	extra_body 为供应商扩展参数；chat_completions_url 为最终请求地址；session 为复用
	连接的 requests 会话。
	"""

	def __init__(
		self,
		*,
		api_key: str | None = None,
		model: str | None = None,
		api_base_url: str | None = None,
		timeout: int | float | None = None,
		default_temperature: float | None = None,
		extra_body: dict[str, Any] | None = None,
	) -> None:
		"""加载集中默认配置、建立 HTTP 会话，并验证请求前置条件。"""
		settings = get_settings()

		self.api_key = (api_key or settings.llm_api_key or "").strip()
		self.model = (model or DEFAULT_LLM_MODEL).strip()
		self.api_base_url = (api_base_url or settings.llm_api_base_url or "").strip()
		self.timeout = timeout if timeout is not None else settings.llm_timeout
		self.default_temperature = (
			default_temperature
			if default_temperature is not None
			else DEFAULT_LLM_TEMPERATURE
		)
		self.extra_body = self._merge_extra_body(
			init_extra_body=extra_body,
		)

		self._validate_config()
		self.chat_completions_url = self._build_chat_completions_url(self.api_base_url)

		# 复用 TCP 连接并统一携带认证头，供所有同步和异步包装请求使用。
		self.session = requests.Session()
		self.session.headers.update(
			{
				"Authorization": f"Bearer {self.api_key}",
				"Content-Type": "application/json",
			}
		)

	@property
	def thinking_enabled(self) -> bool:
		"""返回实例默认请求是否携带 Qwen 或 DeepSeek 的深度思考开关。"""
		if self.extra_body.get("enable_thinking") is True:
			return True
		thinking = self.extra_body.get("thinking")
		return isinstance(thinking, dict) and thinking.get("type") == "enabled"

	def chat(
		self,
		*,
		user_prompt: str,
		system_prompt: str | None = None,
		temperature: float | None = None,
		max_tokens: int | None = None,
		response_format: dict[str, Any] | None = None,
		max_retries: int = 2,
	) -> str:
		"""发送一次同步 Chat Completions 请求，并返回 assistant 正文。"""
		user_prompt = user_prompt.strip()
		if not user_prompt:
			raise ValueError("user_prompt 不能为空")

		if max_retries <= 0:
			raise ValueError("max_retries 必须大于 0")

		messages: list[dict[str, str]] = []
		if system_prompt and system_prompt.strip():
			messages.append(
				{
					"role": "system",
					"content": system_prompt.strip(),
				}
			)

		messages.append(
			{
				"role": "user",
				"content": user_prompt,
			}
		)

		payload: dict[str, Any] = {
			"model": self.model,
			"messages": messages,
			"temperature": self.default_temperature if temperature is None else temperature,
		}

		if max_tokens is not None:
			payload["max_tokens"] = max_tokens

		if response_format is not None:
			payload["response_format"] = response_format

		if self.extra_body:
			payload.update(self.extra_body)

		response_data = self._post_chat_completion(payload, max_retries=max_retries)
		return self._extract_message_content(response_data)

	def analyze_json(
		self,
		*,
		user_prompt: str,
		system_prompt: str | None = None,
		temperature: float | None = None,
		max_tokens: int | None = None,
		max_retries: int = 2,
	) -> Any:
		"""调用 chat 后把文本解析为 Python JSON 值。"""
		content = self.chat(
			user_prompt=user_prompt,
			system_prompt=system_prompt,
			temperature=temperature,
			max_tokens=max_tokens,
			response_format={"type": "json_object"},
			max_retries=max_retries,
		)

		json_text = self.extract_json_text(content)
		try:
			return json.loads(json_text)
		except json.JSONDecodeError as exc:
			raise LLMResponseError(f"LLM 返回的内容不是合法 JSON: {json_text[:300]}") from exc

	def analyze_schema(
		self,
		*,
		user_prompt: str,
		response_schema: Any,
		system_prompt: str | None = None,
		temperature: float | None = None,
		max_tokens: int | None = None,
		max_retries: int = 2,
	) -> SchemaT:
		"""调用 chat、解析 JSON，并按给定 Pydantic schema 校验返回值。"""
		data = self.analyze_json(
			user_prompt=user_prompt,
			system_prompt=system_prompt,
			temperature=temperature,
			max_tokens=max_tokens,
			max_retries=max_retries,
		)

		try:
			return cast(SchemaT, TypeAdapter(response_schema).validate_python(data))
		except Exception as exc:
			raise LLMResponseError(f"LLM 结果结构校验失败: {data}") from exc

	async def async_chat(
		self,
		*,
		user_prompt: str,
		system_prompt: str | None = None,
		temperature: float | None = None,
		max_tokens: int | None = None,
		response_format: dict[str, Any] | None = None,
		max_retries: int = 2,
	) -> str:
		"""
		异步包装版 chat。

		BaseLLM.chat 当前基于 requests，是同步阻塞调用。
		业务分析器如果是 async analyze，可以通过 async_chat 调用，
		避免在业务层重复写线程池包装代码。同步 requests 调用会被移入线程池，
		因此不会阻塞当前 asyncio 事件循环。
		"""

		return await asyncio.to_thread(
			self.chat,
			user_prompt=user_prompt,
			system_prompt=system_prompt,
			temperature=temperature,
			max_tokens=max_tokens,
			response_format=response_format,
			max_retries=max_retries,
		)

	@classmethod
	def loads_llm_json(cls, raw_result: str) -> Any:
		"""
		从 LLM 原始返回中提取并解析 JSON。

		支持 JSON object 和 JSON array。
		适合业务分析器返回数组结构，例如：
		[
		  {"sector_name": "通信设备", "sector_llm_analysis": null}
		]

		解析失败会统一转为 LLMResponseError，避免业务层处理供应商原始文本。
		"""

		json_text = cls.extract_json_text(raw_result)

		try:
			return json.loads(json_text)
		except json.JSONDecodeError as exc:
			raise LLMResponseError(f"LLM 返回的内容不是合法 JSON: {json_text[:300]}") from exc

	@staticmethod
	def validate_llm_schema(data: Any, response_schema: Any) -> SchemaT:
		"""
		用 Pydantic TypeAdapter 校验 LLM JSON 结果。

		校验异常统一包装为 LLMResponseError，供上层决定是否重试。
		"""

		try:
			return cast(SchemaT, TypeAdapter(response_schema).validate_python(data))
		except Exception as exc:
			raise LLMResponseError(f"LLM 结果结构校验失败: {data}") from exc


	@staticmethod
	def build_json_output_instruction(response_schema: Any) -> str:
		"""依据 Pydantic schema 生成“仅返回合法 JSON”的提示片段。"""
		schema = TypeAdapter(response_schema).json_schema()
		return (
			"请只返回合法 JSON，不要输出 Markdown 代码块、解释或额外文本。"
			f"输出必须满足以下 JSON Schema: {json.dumps(schema, ensure_ascii=False)}"
		)

	@staticmethod
	def extract_json_text(text: str) -> str:
		"""从纯文本、Markdown 代码块或夹杂解释的回复中提取最外层 JSON。"""
		cleaned = (text or "").strip()
		if not cleaned:
			raise LLMResponseError("LLM 返回内容为空")

		if cleaned.startswith("```"):
			cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
			cleaned = re.sub(r"\s*```$", "", cleaned)
			cleaned = cleaned.strip()

		if (cleaned.startswith("{") and cleaned.endswith("}")) or (
			cleaned.startswith("[") and cleaned.endswith("]")
		):
			return cleaned

		decoder = json.JSONDecoder()
		last_container: str | None = None
		start = 0
		while start < len(cleaned):
			if cleaned[start] not in "[{":
				start += 1
				continue
			try:
				value, end = decoder.raw_decode(cleaned[start:])
			except json.JSONDecodeError:
				start += 1
				continue
			if isinstance(value, (dict, list)):
				last_container = cleaned[start : start + end]
				# Skip nested containers; only compare standalone top-level candidates.
				start += end
				continue
			start += 1

		if last_container is not None:
			return last_container

		raise LLMResponseError(f"未能从 LLM 返回中提取 JSON: {cleaned[:300]}")

	@classmethod
	def _extract_json_from_reasoning_content(cls, text: str) -> str | None:
		"""
		从 reasoning_content 中兜底提取合法 JSON。

		部分推理模型可能把最终 JSON 放进 reasoning_content，同时 message.content
		为空。业务层只需要 JSON 时，可以在这个异常形态下复用已经生成的合法 JSON。
		找不到完整 JSON 时返回 None，让调用方生成准确错误信息。
		"""

		try:
			json_text = cls.extract_json_text(text)
			parsed = json.loads(json_text)
		except (LLMResponseError, json.JSONDecodeError):
			return None

		return json.dumps(parsed, ensure_ascii=False)

	def _post_chat_completion(self, payload: dict[str, Any], max_retries: int) -> dict[str, Any]:
		"""按指数退避重试 HTTP 调用，并返回供应商的顶层 JSON object。"""
		last_error: Exception | None = None

		for attempt in range(1, max_retries + 1):
			try:
				response = self.session.post(
					self.chat_completions_url,
					json=payload,
					timeout=self.timeout,
				)
				response.raise_for_status()
				data = response.json()

				if not isinstance(data, dict):
					raise LLMResponseError(f"LLM 返回不是 JSON object: {data}")

				if data.get("error"):
					raise LLMRequestError(f"LLM 接口返回 error: {data['error']}")

				return data
			except (requests.RequestException, ValueError, LLMRequestError, LLMResponseError) as exc:
				last_error = exc
				logger.warning(
					"llm request failed model=%s url=%s attempt=%s/%s error=%s",
					self.model,
					self.chat_completions_url,
					attempt,
					max_retries,
					exc,
				)

				if attempt < max_retries:
					time.sleep(min(2 ** (attempt - 1), 8))

		raise LLMRequestError(f"LLM 请求失败 model={self.model}") from last_error

	@staticmethod
	def _extract_message_content(data: dict[str, Any]) -> str:
		"""从响应 choices 中提取正文，必要时从 reasoning_content 兜底取 JSON。"""
		choices = data.get("choices") or []
		if not choices:
			raise LLMResponseError(f"LLM 返回缺少 choices: {data}")

		first_choice = choices[0]
		if not isinstance(first_choice, dict):
			raise LLMResponseError(f"LLM 返回 choices[0] 不是 object: {data}")

		finish_reason = first_choice.get("finish_reason")
		message = first_choice.get("message") or {}
		if not isinstance(message, dict):
			raise LLMResponseError(f"LLM 返回 message 不是 object: {data}")

		content = message.get("content")

		if isinstance(content, str) and content.strip():
			return content.strip()

		if isinstance(content, list):
			text_parts: list[str] = []
			for item in content:
				if not isinstance(item, dict):
					continue

				item_type = item.get("type")
				text = item.get("text")
				if item_type in {"text", "output_text"} and isinstance(text, str) and text.strip():
					text_parts.append(text.strip())

			if text_parts:
				return "\n".join(text_parts)

		reasoning_content = message.get("reasoning_content")

		if finish_reason == "length":
			raise LLMResponseError(
				"LLM 输出被截断，finish_reason=length，message.content 为空。"
				"请增大 max_tokens / max_completion_tokens，或缩短 prompt。"
				f" response={data}"
			)

		if isinstance(reasoning_content, str) and reasoning_content.strip():
			fallback_json = BaseLLM._extract_json_from_reasoning_content(reasoning_content)
			if fallback_json:
				return fallback_json

			raise LLMResponseError(
				"LLM 只返回 reasoning_content，message.content 为空，"
				"且 reasoning_content 中无法提取合法 JSON。"
				f" response={data}"
			)

		raise LLMResponseError(f"LLM 返回缺少 message.content: {data}")

	@staticmethod
	def _build_chat_completions_url(api_base_url: str) -> str:
		"""将服务根地址规范化为 `/chat/completions` 请求地址。"""
		api_base_url = api_base_url.rstrip("/")
		if api_base_url.endswith("/chat/completions"):
			return api_base_url

		return api_base_url + "/chat/completions"

	@staticmethod
	def _merge_extra_body(
		*,
		init_extra_body: dict[str, Any] | None,
	) -> dict[str, Any]:
		"""合并代码默认扩展参数和显式覆盖项，显式参数优先级更高。"""
		result = dict(DEFAULT_LLM_EXTRA_BODY)

		if init_extra_body:
			result.update(init_extra_body)

		return result

	def _validate_config(self) -> None:
		"""验证认证、接口地址、超时和温度，避免发送明显无效的请求。"""
		if not self.api_key:
			raise LLMConfigError("未配置 LLM_API_KEY")

		if not self.model:
			raise LLMConfigError("LLM 模型名称不能为空")

		if not self.api_base_url:
			raise LLMConfigError("未配置 LLM_API_BASE_URL")

		if not self.api_base_url.startswith(("http://", "https://")):
			raise LLMConfigError(f"LLM_API_BASE_URL 非法: {self.api_base_url}")

		try:
			timeout = float(self.timeout)
		except (TypeError, ValueError) as exc:
			raise LLMConfigError(f"LLM_TIMEOUT 非法: {self.timeout}") from exc

		if timeout <= 0:
			raise LLMConfigError(f"LLM_TIMEOUT 必须大于 0: {self.timeout}")

		try:
			temperature = float(self.default_temperature)
		except (TypeError, ValueError) as exc:
			raise LLMConfigError(
				f"default_temperature 非法: {self.default_temperature}"
			) from exc

		if temperature < 0:
			raise LLMConfigError(
				f"default_temperature 不能小于 0: {self.default_temperature}"
			)

		self.timeout = timeout
		self.default_temperature = temperature


class QwenAnalysisLLM(BaseLLM):
	"""盘前与抖音分析共同使用的 qwen3.7-max 请求配置。"""

	# 固定模型不从环境读取，防止两个关键分析任务在部署时发生配置漂移。
	MODEL = QWEN_ANALYSIS_MODEL

	def __init__(self, **llm_kwargs: Any) -> None:
		"""默认注入 qwen3.7-max；显式参数仅用于测试或受控诊断。"""
		if not llm_kwargs.get("model"):
			llm_kwargs["model"] = self.MODEL
		super().__init__(**llm_kwargs)
