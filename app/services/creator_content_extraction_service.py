from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import logging
import os
from pathlib import Path
import tempfile
from typing import Any, Protocol
from urllib.parse import urlparse

import httpx

from app.crawlers.creator_platforms import create_platform_crawler, get_account
from app.models.creator_monitoring import CreatorMediaTranscript, CreatorWork
from app.repositories.creator_monitoring_repository import CreatorWorkRepository
from app.services.creator_media_transcription_service import (
    FasterWhisperTranscriber,
    LazyRapidOCREngine,
)


logger = logging.getLogger(__name__)
PROCESSING_LEASE_SECONDS = 30 * 60
RETRY_DELAY_SECONDS = 5 * 60
MAX_MEDIA_BYTES = 300 * 1024 * 1024


class ExtractionRepository(Protocol):
    """定义内容提取处理阶段所需的仓储操作。"""

    async def claim_next_for_extraction(
        self, *, lease_timeout_seconds: int
    ) -> CreatorWork | None:
        """领取一个待内容提取作品，并递增其隔离尝试次数。"""

        ...

    async def mark_extraction_success(
        self,
        work_key: str,
        extracted_text: str,
        *,
        expected_attempt: int,
        asr_text: str = "",
        ocr_text: str = "",
    ) -> Any:
        """在 ``expected_attempt`` 仍持有租约时持久化提取文本。"""

        ...

    async def mark_extraction_failed(
        self,
        work_key: str,
        reason: str,
        *,
        expected_attempt: int,
        retry_delay_seconds: int,
    ) -> Any:
        """为匹配的处理尝试记录可重试的内容提取失败。"""

        ...


class CreatorMediaProvider(Protocol):
    """定义将博主作品公开媒体下载到本地所需的提供方接口。"""

    async def download_media(self, work: CreatorWork) -> Path | list[Path]:
        """下载内容提取所需的一个或多个临时媒体文件。"""

        ...


class MediaTranscriber(Protocol):
    """定义通过 ``asyncio.to_thread`` 调用的同步语音或字幕识别器。"""

    def transcribe(self, media_path: str | Path) -> CreatorMediaTranscript:
        """将一个本地视频或音频文件转换为结构化转写文本。"""

        ...


class ImageTextExtractor(Protocol):
    """定义适用于图片作品内容的同步 OCR 适配器。"""

    def extract(self, image_path: str | Path) -> str:
        """从一个本地图片返回可读文本；未识别到文本时抛出异常。"""

        ...


class RapidOCRImageTextExtractor:
    """从仅包含图片的社交作品中提取可读文本。"""

    def __init__(self, *, ocr_engine: Any | None = None) -> None:
        """保存可共享的延迟 OCR 引擎，未注入时创建当前提取器专用实例。"""

        # 可跨视频与图片复用，并在队列空闲后主动释放的 RapidOCR 引擎。
        self._ocr: Any = ocr_engine or LazyRapidOCREngine()

    def extract(self, image_path: str | Path) -> str:
        """从一个非空图片文件提取去重后的高置信度文本行。

        RapidOCR 会延迟导入，以保持非图片 worker 的导入开销较低。置信度低于
        0.55 的文本行和空结果会在返回前被拒绝。
        """

        path = Path(image_path)
        if not path.is_file() or path.stat().st_size == 0:
            raise RuntimeError("待识别的图片不存在或为空")
        output = self._ocr(str(path))
        lines = [
            str(text).strip()
            for text, score in zip(output.txts or [], output.scores or [])
            if str(text).strip() and float(score) >= 0.55
        ]
        text = "\n".join(dict.fromkeys(lines))
        if not text:
            raise RuntimeError("图片 OCR 未识别出有效文本")
        return text

    def release_resources(self) -> None:
        """请求共享 OCR 引擎释放已加载模型；未提供释放接口时保持原对象不变。"""

        release = getattr(self._ocr, "release", None)
        if callable(release):
            release()


class PlatformCreatorMediaProvider:
    """解析最新公开媒体地址，并下载受大小限制的临时文件。"""

    def __init__(
        self,
        *,
        timeout_seconds: float = 60,
        max_media_bytes: int = MAX_MEDIA_BYTES,
    ) -> None:
        """配置每次媒体下载使用的 HTTP 时间和字节大小限制。"""

        if timeout_seconds <= 0 or max_media_bytes <= 0:
            raise ValueError("媒体超时和大小限制必须大于 0")
        # 传递给异步 HTTP 客户端的单次请求超时。
        self.timeout_seconds = timeout_seconds
        # 每个临时文件允许的声明大小或实际流式大小上限。
        self.max_media_bytes = max_media_bytes

    async def download_media(self, work: CreatorWork) -> Path | list[Path]:
        """解析最新平台媒体地址，并下载作品所需的文件。

        抖音视频使用专用的最新媒体接口。图片作品允许部分下载，并可回退到来源
        文本；多段媒体则要求每一段都成功。返回前始终关闭平台抓取器。
        """

        account = get_account(work.account_id)
        crawler = create_platform_crawler(account.platform)
        try:
            fetch_media = getattr(crawler, "fetch_media", None)
            if (
                account.platform == "douyin"
                and work.content_type != "image_post"
                and callable(fetch_media)
            ):
                try:
                    return await fetch_media(account, work.platform_work_id)
                except Exception:
                    if not work.media_url:
                        raise
                    logger.warning(
                        "creator douyin media refresh failed; using persisted url "
                        "work=%s",
                        work.work_key,
                        exc_info=True,
                    )
                    return await self._download_urls([work.media_url], work=work)

            fetched = await crawler.fetch_work(account, work.platform_work_id)
            if work.content_type == "image_post":
                image_urls = self._http_urls(fetched.media_urls)
                if not image_urls and work.media_url:
                    image_urls = [work.media_url]
                if not image_urls:
                    if work.source_text:
                        return []
                    raise RuntimeError("图文作品没有可下载的公开图片地址")
                return await self._download_urls(
                    image_urls,
                    work=work,
                    allow_partial=True,
                )

            media_parts = fetched.metadata.get("media_parts") or []
            part_urls: list[str] = []
            for index, part in enumerate(media_parts, start=1):
                if not isinstance(part, dict):
                    raise RuntimeError(f"作品第 {index} 个媒体分段信息无效")
                audio_urls = self._http_urls(part.get("audio_urls") or [])
                video_urls = self._http_urls(part.get("video_urls") or [])
                candidates = audio_urls or video_urls
                if not candidates:
                    raise RuntimeError(f"作品第 {index} 个媒体分段没有公开媒体地址")
                part_urls.append(candidates[0])
            if part_urls:
                return await self._download_urls(part_urls, work=work)

            audio_urls = self._http_urls(fetched.metadata.get("audio_urls") or [])
            candidates = audio_urls or self._http_urls(fetched.media_urls)
            if not candidates and work.media_url:
                candidates = [work.media_url]
            if not candidates:
                raise RuntimeError("作品没有可下载的公开媒体地址")
            return await self._download_urls(candidates[:1], work=work)
        finally:
            close = getattr(crawler, "aclose", None)
            if callable(close):
                await close()

    async def _download_urls(
        self,
        urls: list[str],
        *,
        work: CreatorWork,
        allow_partial: bool = False,
    ) -> list[Path]:
        """按顺序下载地址，并应用全部成功或允许部分成功的批次失败语义。

        ``allow_partial=False`` 时，任一失败都会删除之前已下载的文件。图片作品
        可以保留成功下载的图片；当所有图片都失败时，如果存在来源文本则允许
        返回空文件列表，否则会串联最后一个错误以便诊断。
        """

        paths: list[Path] = []
        last_error: Exception | None = None
        for index, url in enumerate(urls, start=1):
            try:
                paths.append(await self._download_url(url, work=work))
            except Exception as exc:
                last_error = exc
                if not allow_partial:
                    for path in paths:
                        path.unlink(missing_ok=True)
                    raise
                logger.warning(
                    "creator image download failed work=%s image_index=%s",
                    work.work_key,
                    index,
                    exc_info=True,
                )
        if paths:
            return paths
        if allow_partial and work.source_text:
            return []
        if last_error is not None:
            raise RuntimeError("作品所有媒体均下载失败") from last_error
        raise RuntimeError("作品没有可下载的公开媒体地址")

    @staticmethod
    def _http_urls(values: Any) -> list[str]:
        """将单个地址或可迭代对象规范化为按来源顺序去重的 HTTP(S) 地址列表。"""

        if not values:
            return []
        if isinstance(values, str):
            values = [values]
        return list(
            dict.fromkeys(
                value
                for raw in values
                if (value := str(raw).strip()).startswith(("http://", "https://"))
            )
        )

    async def _download_url(self, url: str, *, work: CreatorWork) -> Path:
        """将一个有大小上限的地址流式下载到唯一命名的临时文件。

        ``Content-Length`` 和实际流式字节数都会执行配置的大小上限。空文件或
        下载失败时会在异常向外抛出前删除临时文件。
        """

        suffix = Path(urlparse(url).path).suffix[:10] or ".bin"
        fd, raw_path = tempfile.mkstemp(
            prefix=f"creator_{work.platform}_{work.platform_work_id}_",
            suffix=suffix,
        )
        os.close(fd)
        target = Path(raw_path)
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 Chrome/126.0 Safari/537.36"
            ),
            "Referer": work.canonical_url,
        }
        try:
            async with httpx.AsyncClient(
                follow_redirects=True,
                timeout=self.timeout_seconds,
                headers=headers,
            ) as client:
                async with client.stream("GET", url) as response:
                    response.raise_for_status()
                    declared = int(response.headers.get("content-length") or 0)
                    if declared > self.max_media_bytes:
                        raise RuntimeError("作品媒体超过允许的最大文件大小")
                    written = 0
                    with target.open("wb") as output:
                        async for chunk in response.aiter_bytes():
                            written += len(chunk)
                            if written > self.max_media_bytes:
                                raise RuntimeError("作品媒体超过允许的最大文件大小")
                            output.write(chunk)
            if target.stat().st_size == 0:
                raise RuntimeError("作品媒体下载结果为空")
            return target
        except BaseException:
            target.unlink(missing_ok=True)
            raise


@dataclass(frozen=True)
class CreatorExtractionProcessResult:
    """记录一次领取并提取单个博主作品的处理结果。"""

    # 是否领取到作品；为假表示队列为空。
    processed: bool = False
    # 已领取作品是否成功进入待观点分析状态。
    success: bool = False
    # 终止步骤标签，例如 ``finished``、``extraction`` 或 ``lease_lost``。
    stage: str = "empty"
    # 已领取作品的稳定键；队列为空时不存在。
    work_key: str | None = None
    # 未成功处理时的限长错误或租约丢失说明。
    reason: str | None = None


@dataclass(frozen=True)
class CreatorExtractionBatchResult:
    """记录一次有界轮询批次按顺序产生的内容提取结果。"""

    # 已领取作品的结果；最后一次用于确认队列为空的探测不包含在内。
    results: list[CreatorExtractionProcessResult] = field(default_factory=list)

    @property
    def total_claimed_count(self) -> int:
        """返回本批次领取的作品数量。"""

        return len(self.results)

    @property
    def success_count(self) -> int:
        """返回本批次成功完成内容提取的已领取作品数量。"""

        return sum(item.success for item in self.results)

    @property
    def failed_count(self) -> int:
        """返回本批次未完成内容提取的已领取作品数量。"""

        return sum(item.processed and not item.success for item in self.results)


class CreatorContentExtractionService:
    """领取媒体作品，运行现有 OCR/ASR 流程，并隔离结果写入。"""

    def __init__(
        self,
        *,
        repository: ExtractionRepository | None = None,
        media_provider: CreatorMediaProvider | None = None,
        transcriber: MediaTranscriber | None = None,
        image_text_extractor: ImageTextExtractor | None = None,
        lease_timeout_seconds: int = PROCESSING_LEASE_SECONDS,
        retry_delay_seconds: int = RETRY_DELAY_SECONDS,
    ) -> None:
        """组装内容提取依赖，并校验租约与重试时间配置。"""

        if lease_timeout_seconds <= 0 or retry_delay_seconds <= 0:
            raise ValueError("租约和重试延迟必须大于 0")
        # 用于领取作品和推进状态的尝试次数隔离作品仓储。
        self.repository = repository or CreatorWorkRepository()
        # 按平台解析媒体并创建本地临时文件的下载器。
        self.media_provider = media_provider or PlatformCreatorMediaProvider()
        # 默认视频和图片提取器共用同一个延迟 OCR 引擎，避免加载两套原生模型。
        shared_ocr_engine = LazyRapidOCREngine()
        # 视频或音频 ASR 以及字幕 OCR 实现。
        self.transcriber = transcriber or FasterWhisperTranscriber(
            ocr_engine=shared_ocr_engine
        )
        # 图片作品使用的静态图片 OCR 实现。
        self.image_text_extractor = image_text_extractor or RapidOCRImageTextExtractor(
            ocr_engine=shared_ocr_engine
        )
        # 放弃的内容提取领取在多少秒后可被恢复。
        self.lease_timeout_seconds = lease_timeout_seconds
        # 内容提取失败后再次允许领取前的等待秒数。
        self.retry_delay_seconds = retry_delay_seconds

    async def ensure_indexes(self) -> None:
        """当注入的仓储提供该钩子时创建仓储索引。"""

        create_indexes = getattr(self.repository, "create_indexes", None)
        if callable(create_indexes):
            await create_indexes()

    async def process_once(self) -> CreatorExtractionProcessResult:
        """领取并提取一个博主作品，同时按尝试次数隔离状态迁移。

        图片作品会将平台正文与所有成功图片的 OCR 合并；其他媒体逐段转写，并
        保留来源特有的 ASR/OCR 文本。媒体完全没有可识别内容但平台正文非空时，
        会明确回退到平台正文且保持 ASR/OCR 为空。失败任务会安排重试，过期状态
        写入会转换为租约丢失结果，每个下载的临时文件都会在 ``finally`` 中删除。
        """

        work = await self.repository.claim_next_for_extraction(
            lease_timeout_seconds=self.lease_timeout_seconds
        )
        if work is None:
            return CreatorExtractionProcessResult()
        media_paths: list[Path] = []
        try:
            if work.content_type == "image_post":
                try:
                    downloaded = await self.media_provider.download_media(work)
                    media_paths = self._normalize_media_paths(downloaded)
                except Exception:
                    if not work.source_text:
                        raise
                    logger.warning(
                        "creator image media unavailable; using source text work=%s",
                        work.work_key,
                        exc_info=True,
                    )
                image_texts: list[str] = []
                ocr_errors: list[Exception] = []
                for index, media_path in enumerate(media_paths, start=1):
                    try:
                        image_texts.append(
                            await asyncio.to_thread(
                                self.image_text_extractor.extract,
                                media_path,
                            )
                        )
                    except Exception as exc:
                        ocr_errors.append(exc)
                        logger.warning(
                            "creator image OCR failed work=%s image_index=%s",
                            work.work_key,
                            index,
                            exc_info=True,
                        )
                ocr_text = self._merge_texts(*image_texts)
                extracted_text = self._merge_texts(work.source_text, ocr_text)
                if not extracted_text:
                    if ocr_errors:
                        raise RuntimeError("图文作品所有图片 OCR 均失败") from ocr_errors[-1]
                    raise RuntimeError("图文作品没有可供分析的正文或 OCR 文本")
                asr_text = ""
            else:
                try:
                    downloaded = await self.media_provider.download_media(work)
                    media_paths = self._normalize_media_paths(downloaded)
                except Exception as exc:
                    if not work.source_text:
                        raise
                    logger.warning(
                        "creator media unavailable; using source text work=%s error=%s",
                        work.work_key,
                        (str(exc) or exc.__class__.__name__)[:300],
                    )
                transcripts: list[CreatorMediaTranscript] = []
                transcription_errors: list[Exception] = []
                for index, media_path in enumerate(media_paths, start=1):
                    try:
                        transcripts.append(
                            await asyncio.to_thread(
                                self.transcriber.transcribe,
                                media_path,
                            )
                        )
                    except Exception as exc:
                        transcription_errors.append(exc)
                        logger.warning(
                            "creator media transcription unavailable; "
                            "checking remaining media or source text work=%s "
                            "media_index=%s",
                            work.work_key,
                            index,
                            exc_info=True,
                        )
                if transcripts:
                    extracted_text = self._merge_texts(
                        *(transcript.text for transcript in transcripts)
                    )
                    asr_text = self._merge_texts(
                        *(transcript.asr_text for transcript in transcripts)
                    )
                    ocr_text = self._merge_texts(
                        *(transcript.ocr_text for transcript in transcripts)
                    )
                elif work.source_text:
                    extracted_text = work.source_text.strip()
                    asr_text = ""
                    ocr_text = ""
                elif transcription_errors:
                    raise RuntimeError(
                        "视频作品所有媒体均未识别出内容且没有平台正文"
                    ) from transcription_errors[-1]
                else:
                    raise RuntimeError("视频作品没有可供分析的媒体内容或平台正文")
            transition = await self.repository.mark_extraction_success(
                work.work_key,
                extracted_text,
                expected_attempt=work.processing_attempts,
                asr_text=asr_text,
                ocr_text=ocr_text,
            )
            if not self._was_modified(transition):
                return self._lease_lost(work.work_key)
            return CreatorExtractionProcessResult(
                processed=True,
                success=True,
                stage="finished",
                work_key=work.work_key,
            )
        except Exception as exc:
            reason = (str(exc) or exc.__class__.__name__)[:1000]
            logger.exception("creator content extraction failed work=%s", work.work_key)
            transition = await self.repository.mark_extraction_failed(
                work.work_key,
                reason,
                expected_attempt=work.processing_attempts,
                retry_delay_seconds=self.retry_delay_seconds,
            )
            if not self._was_modified(transition):
                return self._lease_lost(work.work_key)
            return CreatorExtractionProcessResult(
                processed=True,
                success=False,
                stage="extraction",
                work_key=work.work_key,
                reason=reason,
            )
        finally:
            for media_path in media_paths:
                try:
                    media_path.unlink(missing_ok=True)
                except OSError:
                    logger.warning(
                        "failed to remove creator temporary media work=%s path=%s",
                        work.work_key,
                        media_path,
                        exc_info=True,
                    )

    @staticmethod
    def _normalize_media_paths(downloaded: Path | list[Path]) -> list[Path]:
        """将提供方返回的单个路径或多个路径规范化为路径列表。"""

        if isinstance(downloaded, Path):
            return [downloaded]
        return [Path(path) for path in downloaded]

    @staticmethod
    def _merge_texts(*values: str) -> str:
        """合并多个文本来源中的非空去重行，并保持首次出现顺序。"""

        lines: list[str] = []
        seen: set[str] = set()
        for value in values:
            for raw_line in str(value or "").splitlines():
                line = raw_line.strip()
                if line and line not in seen:
                    seen.add(line)
                    lines.append(line)
        return "\n".join(lines)

    async def process_batch(
        self, *, batch_size: int
    ) -> CreatorExtractionBatchResult:
        """最多处理 ``batch_size`` 个作品，队列为空时停止。"""

        if batch_size <= 0:
            raise ValueError("batch_size 必须大于 0")
        results: list[CreatorExtractionProcessResult] = []
        for _ in range(batch_size):
            result = await self.process_once()
            if not result.processed:
                # 独立提取 worker 空闲时立即卸载 Whisper/OCR，避免长时间占用内存。
                self.release_resources()
                break
            results.append(result)
        return CreatorExtractionBatchResult(results=results)

    def release_resources(self) -> None:
        """在提取队列空闲时释放默认或自定义识别器提供的重量级模型资源。

        两个依赖可能共享同一 OCR 引擎，因此释放操作必须保持幂等；不实现
        ``release_resources`` 的测试替身或自定义轻量实现会被安全跳过。
        """

        for dependency in (self.transcriber, self.image_text_extractor):
            release = getattr(dependency, "release_resources", None)
            if callable(release):
                release()

    @staticmethod
    def _was_modified(result: Any) -> bool:
        """解释仓储更新元数据，并兼容轻量级测试替身对象。"""

        modified_count = getattr(result, "modified_count", None)
        return True if modified_count is None else int(modified_count) == 1

    @staticmethod
    def _lease_lost(work_key: str) -> CreatorExtractionProcessResult:
        """为丢失租约的隔离状态迁移构造并记录标准结果。"""

        logger.warning("creator extraction lease lost work=%s", work_key)
        return CreatorExtractionProcessResult(
            processed=True,
            success=False,
            stage="lease_lost",
            work_key=work_key,
            reason="processing lease lost",
        )


__all__ = [
    "CreatorContentExtractionService",
    "CreatorExtractionBatchResult",
    "CreatorExtractionProcessResult",
    "PlatformCreatorMediaProvider",
    "RapidOCRImageTextExtractor",
]
