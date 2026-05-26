from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, TypeVar, cast

import requests
from pydantic import TypeAdapter
import asyncio
from app.core.config import get_settings


logger = logging.getLogger(__name__)

SchemaT = TypeVar("SchemaT")


class LLMError(RuntimeError):
	pass


class LLMConfigError(LLMError):
	pass


class LLMRequestError(LLMError):
	pass


class LLMResponseError(LLMError):
	pass


class BaseLLM:
	"""
	LLM 分析的通用底座。

	设计原则：
	1. 不区分具体 provider 的 chat 逻辑。
	2. 只通过 LLM_API_KEY / LLM_MODEL / LLM_API_BASE_URL 切换模型。
	3. 深度思考等差异参数统一通过 LLM_EXTRA_BODY 透传。
	4. 这里只放模型配置、统一请求、JSON 提取与结果校验，不放业务 prompt。
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
		settings = get_settings()

		self.api_key = (api_key or settings.llm_api_key or "").strip()
		self.model = (model or settings.llm_model or "").strip()
		self.api_base_url = (api_base_url or settings.llm_api_base_url or "").strip()
		self.timeout = timeout if timeout is not None else settings.llm_timeout
		self.default_temperature = (
			default_temperature
			if default_temperature is not None
			else settings.llm_default_temperature
		)
		self.extra_body = self._merge_extra_body(
			settings_extra_body=settings.llm_extra_body,
			init_extra_body=extra_body,
		)

		self._validate_config()
		self.chat_completions_url = self._build_chat_completions_url(self.api_base_url)

		self.session = requests.Session()
		self.session.headers.update(
			{
				"Authorization": f"Bearer {self.api_key}",
				"Content-Type": "application/json",
			}
		)

	def chat(
		self,
		*,
		user_prompt: str,
		system_prompt: str | None = None,
		temperature: float | None = None,
		max_tokens: int | None = None,
		response_format: dict[str, Any] | None = None,
		max_retries: int = 2,
		extra_body: dict[str, Any] | None = None,
	) -> str:
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

		if extra_body:
			payload.update(extra_body)

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
		extra_body: dict[str, Any] | None = None,
	) -> Any:
		content = self.chat(
			user_prompt=user_prompt,
			system_prompt=system_prompt,
			temperature=temperature,
			max_tokens=max_tokens,
			response_format={"type": "json_object"},
			max_retries=max_retries,
			extra_body=extra_body,
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
		extra_body: dict[str, Any] | None = None,
	) -> SchemaT:
		data = self.analyze_json(
			user_prompt=user_prompt,
			system_prompt=system_prompt,
			temperature=temperature,
			max_tokens=max_tokens,
			max_retries=max_retries,
			extra_body=extra_body,
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
		extra_body: dict[str, Any] | None = None,
	) -> str:
		"""
		异步包装版 chat。

		BaseLLM.chat 当前基于 requests，是同步阻塞调用。
		业务分析器如果是 async analyze，可以通过 async_chat 调用，
		避免在业务层重复写 inspect.isawaitable / _chat 这类包装代码。
		"""

		return await asyncio.to_thread(
			self.chat,
			user_prompt=user_prompt,
			system_prompt=system_prompt,
			temperature=temperature,
			max_tokens=max_tokens,
			response_format=response_format,
			max_retries=max_retries,
			extra_body=extra_body,
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
		"""

		try:
			return cast(SchemaT, TypeAdapter(response_schema).validate_python(data))
		except Exception as exc:
			raise LLMResponseError(f"LLM 结果结构校验失败: {data}") from exc


	@staticmethod
	def build_json_output_instruction(response_schema: Any) -> str:
		schema = TypeAdapter(response_schema).json_schema()
		return (
			"请只返回合法 JSON，不要输出 Markdown 代码块、解释或额外文本。"
			f"输出必须满足以下 JSON Schema: {json.dumps(schema, ensure_ascii=False)}"
		)

	@staticmethod
	def extract_json_text(text: str) -> str:
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

		for opening, closing in (("{", "}"), ("[", "]")):
			start = cleaned.find(opening)
			end = cleaned.rfind(closing)
			if start != -1 and end != -1 and end > start:
				candidate = cleaned[start : end + 1]
				try:
					json.loads(candidate)
					return candidate
				except json.JSONDecodeError:
					continue

		raise LLMResponseError(f"未能从 LLM 返回中提取 JSON: {cleaned[:300]}")

	def _post_chat_completion(self, payload: dict[str, Any], max_retries: int) -> dict[str, Any]:
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
		choices = data.get("choices") or []
		if not choices:
			raise LLMResponseError(f"LLM 返回缺少 choices: {data}")

		first_choice = choices[0]
		if not isinstance(first_choice, dict):
			raise LLMResponseError(f"LLM 返回 choices[0] 不是 object: {data}")

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

		raise LLMResponseError(f"LLM 返回缺少 message.content: {data}")

	@staticmethod
	def _build_chat_completions_url(api_base_url: str) -> str:
		api_base_url = api_base_url.rstrip("/")
		if api_base_url.endswith("/chat/completions"):
			return api_base_url

		return api_base_url + "/chat/completions"

	@staticmethod
	def _merge_extra_body(
		*,
		settings_extra_body: str | dict[str, Any] | None,
		init_extra_body: dict[str, Any] | None,
	) -> dict[str, Any]:
		result: dict[str, Any] = {}

		if isinstance(settings_extra_body, dict):
			result.update(settings_extra_body)
		elif isinstance(settings_extra_body, str) and settings_extra_body.strip():
			try:
				loaded = json.loads(settings_extra_body)
			except json.JSONDecodeError as exc:
				raise LLMConfigError(
					f"LLM_EXTRA_BODY 不是合法 JSON: {settings_extra_body}"
				) from exc

			if not isinstance(loaded, dict):
				raise LLMConfigError(
					f"LLM_EXTRA_BODY 必须是 JSON object: {settings_extra_body}"
				)

			result.update(loaded)

		if init_extra_body:
			result.update(init_extra_body)

		return result

	def _validate_config(self) -> None:
		if not self.api_key:
			raise LLMConfigError("未配置 LLM_API_KEY")

		if not self.model:
			raise LLMConfigError("未配置 LLM_MODEL")

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
				f"LLM_DEFAULT_TEMPERATURE 非法: {self.default_temperature}"
			) from exc

		if temperature < 0:
			raise LLMConfigError(
				f"LLM_DEFAULT_TEMPERATURE 不能小于 0: {self.default_temperature}"
			)

		self.timeout = timeout
		self.default_temperature = temperature
