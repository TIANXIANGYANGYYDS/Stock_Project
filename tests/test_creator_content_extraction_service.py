from __future__ import annotations

import asyncio
from datetime import datetime
from types import SimpleNamespace

from app.models.creator_monitoring import CN_TZ, CreatorMediaTranscript, CreatorWork
from app.services.creator_content_extraction_service import (
    CreatorContentExtractionService,
    PlatformCreatorMediaProvider,
)


def pending_work(*, source_text: str = "视频简介") -> CreatorWork:
    """创建可按需带平台正文的待提取视频作品。"""

    now = datetime(2026, 7, 24, 12, tzinfo=CN_TZ)
    return CreatorWork(
        creator_id="creator-1",
        account_id="douyin:account-1",
        platform="douyin",
        platform_work_id="work-1",
        content_type="video",
        title="视频简介",
        canonical_url="https://example.com/work-1",
        published_at=now,
        first_seen_at=now,
        fetched_at=now,
        source_text=source_text,
        processing_attempts=1,
        status={"status": "extracting"},
    )


def pending_image_work(*, source_text: str = "") -> CreatorWork:
    work = pending_work().model_copy(
        update={
            "work_key": "weibo:image-1",
            "account_id": "weibo:account-1",
            "platform": "weibo",
            "platform_work_id": "image-1",
            "content_type": "image_post",
            "source_text": source_text,
        }
    )
    return CreatorWork.model_validate(work.model_dump())


class FakeRepository:
    def __init__(self, work, *, modified_count=1) -> None:
        self.work = work
        self.modified_count = modified_count
        self.success = []
        self.failures = []

    async def claim_next_for_extraction(self, *, lease_timeout_seconds):
        work, self.work = self.work, None
        return work

    async def mark_extraction_success(
        self,
        work_key,
        extracted_text,
        *,
        expected_attempt,
        asr_text="",
        ocr_text="",
    ):
        self.success.append(
            (work_key, extracted_text, expected_attempt, asr_text, ocr_text)
        )
        return SimpleNamespace(modified_count=self.modified_count)

    async def mark_extraction_failed(
        self,
        work_key,
        reason,
        *,
        expected_attempt,
        retry_delay_seconds,
    ):
        self.failures.append((work_key, reason, expected_attempt))
        return SimpleNamespace(modified_count=self.modified_count)


class FakeMediaProvider:
    def __init__(self, path) -> None:
        self.path = path

    async def download_media(self, work):
        paths = self.path if isinstance(self.path, list) else [self.path]
        for path in paths:
            path.write_bytes(b"media")
        return self.path


class FailingMediaProvider:
    async def download_media(self, work):
        raise RuntimeError("media download failed")


class FakeTranscriber:
    def __init__(self, *, fail=False) -> None:
        self.fail = fail

    def transcribe(self, media_path):
        if self.fail:
            raise RuntimeError("asr failed")
        return CreatorMediaTranscript(
            text="完整转写",
            asr_text="语音转写",
            ocr_text="字幕文本",
            provider="test",
            model="test",
            transcribed_at=datetime(2026, 7, 24, 13, tzinfo=CN_TZ),
        )


class FakeImageTextExtractor:
    def __init__(self, results=None) -> None:
        self.results = list(results or ["图片中的市场观点"])
        self.calls = []

    def extract(self, image_path):
        self.calls.append(image_path)
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class PartTranscriber:
    def __init__(self) -> None:
        self.calls = []

    def transcribe(self, media_path):
        self.calls.append(media_path)
        part = media_path.stem
        return CreatorMediaTranscript(
            text=f"共同观点\n{part}观点",
            asr_text=f"共同语音\n{part}语音",
            ocr_text=f"共同字幕\n{part}字幕",
            provider="test",
            model="test",
            transcribed_at=datetime(2026, 7, 24, 13, tzinfo=CN_TZ),
        )


def test_default_extractors_share_one_lazy_ocr_engine() -> None:
    """验证默认视频字幕和图片正文提取器共用一套延迟 OCR 模型。"""

    service = CreatorContentExtractionService(repository=FakeRepository(None))

    assert service.transcriber._ocr is service.image_text_extractor._ocr


def test_extraction_persists_transcript_and_removes_temporary_media(tmp_path) -> None:
    media_path = tmp_path / "work.mp4"
    repository = FakeRepository(pending_work())
    result = asyncio.run(
        CreatorContentExtractionService(
            repository=repository,
            media_provider=FakeMediaProvider(media_path),
            transcriber=FakeTranscriber(),
        ).process_once()
    )

    assert result.success is True
    assert repository.success == [
        ("douyin:work-1", "完整转写", 1, "语音转写", "字幕文本")
    ]
    assert not media_path.exists()


def test_extraction_failure_is_recorded_and_media_is_removed(tmp_path) -> None:
    media_path = tmp_path / "work.mp4"
    repository = FakeRepository(pending_work(source_text=""))
    result = asyncio.run(
        CreatorContentExtractionService(
            repository=repository,
            media_provider=FakeMediaProvider(media_path),
            transcriber=FakeTranscriber(fail=True),
        ).process_once()
    )

    assert result.stage == "extraction"
    assert repository.failures[0][0] == "douyin:work-1"
    assert "没有平台正文" in repository.failures[0][1]
    assert not media_path.exists()


def test_video_uses_source_text_when_media_has_no_recognizable_content(
    tmp_path,
) -> None:
    """验证纯音乐媒体不会覆盖已有正文，也不会伪造 ASR/OCR 结果。"""

    media_path = tmp_path / "silent.mp3"
    repository = FakeRepository(pending_work(source_text="也年轻过"))
    result = asyncio.run(
        CreatorContentExtractionService(
            repository=repository,
            media_provider=FakeMediaProvider(media_path),
            transcriber=FakeTranscriber(fail=True),
        ).process_once()
    )

    assert result.success is True
    assert repository.success == [
        ("douyin:work-1", "也年轻过", 1, "", "")
    ]
    assert repository.failures == []
    assert not media_path.exists()


def test_video_uses_source_text_when_media_download_fails() -> None:
    """验证媒体下载失败时仍保留平台提供的可分析正文。"""

    repository = FakeRepository(pending_work(source_text="平台正文仍可分析"))
    result = asyncio.run(
        CreatorContentExtractionService(
            repository=repository,
            media_provider=FailingMediaProvider(),
            transcriber=FakeTranscriber(fail=True),
        ).process_once()
    )

    assert result.success is True
    assert repository.success == [
        ("douyin:work-1", "平台正文仍可分析", 1, "", "")
    ]
    assert repository.failures == []


def test_image_post_uses_ocr_instead_of_asr(tmp_path) -> None:
    media_path = tmp_path / "work.jpg"
    repository = FakeRepository(pending_image_work())
    result = asyncio.run(
        CreatorContentExtractionService(
            repository=repository,
            media_provider=FakeMediaProvider(media_path),
            transcriber=FakeTranscriber(fail=True),
            image_text_extractor=FakeImageTextExtractor(),
        ).process_once()
    )

    assert result.success is True
    assert repository.success == [
        ("weibo:image-1", "图片中的市场观点", 1, "", "图片中的市场观点")
    ]
    assert not media_path.exists()


def test_image_post_merges_source_and_all_successful_ocr_in_order(tmp_path) -> None:
    media_paths = [tmp_path / f"image-{index}.jpg" for index in range(1, 4)]
    repository = FakeRepository(pending_image_work(source_text="正文观点"))
    extractor = FakeImageTextExtractor(
        [
            "正文观点\n图片观点一",
            RuntimeError("second image OCR failed"),
            "图片观点一\n图片观点二",
        ]
    )

    result = asyncio.run(
        CreatorContentExtractionService(
            repository=repository,
            media_provider=FakeMediaProvider(media_paths),
            transcriber=FakeTranscriber(fail=True),
            image_text_extractor=extractor,
        ).process_once()
    )

    assert result.success is True
    assert extractor.calls == media_paths
    assert repository.success == [
        (
            "weibo:image-1",
            "正文观点\n图片观点一\n图片观点二",
            1,
            "",
            "正文观点\n图片观点一\n图片观点二",
        )
    ]
    assert all(not path.exists() for path in media_paths)


def test_image_post_uses_source_text_when_its_only_image_ocr_fails(tmp_path) -> None:
    media_path = tmp_path / "work.jpg"
    repository = FakeRepository(pending_image_work(source_text="正文仍可分析"))
    extractor = FakeImageTextExtractor([RuntimeError("ocr failed")])

    result = asyncio.run(
        CreatorContentExtractionService(
            repository=repository,
            media_provider=FakeMediaProvider(media_path),
            image_text_extractor=extractor,
        ).process_once()
    )

    assert result.success is True
    assert repository.success == [
        ("weibo:image-1", "正文仍可分析", 1, "", "")
    ]
    assert repository.failures == []
    assert not media_path.exists()


def test_video_transcribes_all_downloaded_parts_and_merges_in_order(tmp_path) -> None:
    media_paths = [tmp_path / "第一P.m4s", tmp_path / "第二P.m4s"]
    repository = FakeRepository(pending_work())
    transcriber = PartTranscriber()

    result = asyncio.run(
        CreatorContentExtractionService(
            repository=repository,
            media_provider=FakeMediaProvider(media_paths),
            transcriber=transcriber,
        ).process_once()
    )

    assert result.success is True
    assert transcriber.calls == media_paths
    assert repository.success == [
        (
            "douyin:work-1",
            "共同观点\n第一P观点\n第二P观点",
            1,
            "共同语音\n第一P语音\n第二P语音",
            "共同字幕\n第一P字幕\n第二P字幕",
        )
    ]
    assert all(not path.exists() for path in media_paths)


def test_platform_media_provider_downloads_each_bilibili_part_once(
    tmp_path, monkeypatch
) -> None:
    fetched = SimpleNamespace(
        media_urls=["https://media.example/fallback-video.m4s"],
        metadata={
            "media_parts": [
                {
                    "cid": "11",
                    "audio_urls": ["https://media.example/part-1-audio.m4s"],
                    "video_urls": ["https://media.example/part-1-video.m4s"],
                },
                {
                    "cid": "22",
                    "audio_urls": [],
                    "video_urls": ["https://media.example/part-2-video.m4s"],
                },
            ]
        },
    )

    class FakeCrawler:
        async def fetch_work(self, account, platform_work_id):
            return fetched

        async def aclose(self):
            return None

    class RecordingMediaProvider(PlatformCreatorMediaProvider):
        def __init__(self):
            super().__init__()
            self.urls = []

        async def _download_url(self, url, *, work):
            self.urls.append(url)
            path = tmp_path / f"part-{len(self.urls)}.m4s"
            path.write_bytes(b"media")
            return path

    monkeypatch.setattr(
        "app.services.creator_content_extraction_service.get_account",
        lambda _account_id: SimpleNamespace(platform="bilibili"),
    )
    monkeypatch.setattr(
        "app.services.creator_content_extraction_service.create_platform_crawler",
        lambda _platform: FakeCrawler(),
    )
    provider = RecordingMediaProvider()
    work = pending_work().model_copy(
        update={
            "work_key": "bilibili:BV1",
            "account_id": "bilibili:37663924",
            "platform": "bilibili",
            "platform_work_id": "BV1",
        }
    )
    work = CreatorWork.model_validate(work.model_dump())

    paths = asyncio.run(provider.download_media(work))

    assert provider.urls == [
        "https://media.example/part-1-audio.m4s",
        "https://media.example/part-2-video.m4s",
    ]
    assert [path.name for path in paths] == ["part-1.m4s", "part-2.m4s"]


def test_douyin_media_provider_falls_back_to_persisted_url(
    tmp_path, monkeypatch
) -> None:
    """验证抖音实时媒体解析受阻时会回退到采集阶段保存的地址。"""

    class BlockedCrawler:
        """模拟详情页触发平台风控、但仍可正常关闭的抖音抓取器。"""

        async def fetch_media(self, account, platform_work_id):
            """模拟无法从最新详情响应解析媒体文件。"""

            raise RuntimeError("douyin media fetch failed")

        async def aclose(self):
            """模拟关闭抓取器持有的网络资源。"""

            return None

    class RecordingMediaProvider(PlatformCreatorMediaProvider):
        """记录回退下载地址，并创建可供断言的临时媒体文件。"""

        def __init__(self):
            """初始化公共下载配置和本测试的地址记录列表。"""

            super().__init__()
            self.urls = []

        async def _download_url(self, url, *, work):
            """记录下载地址并返回一个非空的模拟媒体文件。"""

            self.urls.append(url)
            path = tmp_path / "persisted.mp4"
            path.write_bytes(b"media")
            return path

    monkeypatch.setattr(
        "app.services.creator_content_extraction_service.get_account",
        lambda _account_id: SimpleNamespace(platform="douyin"),
    )
    monkeypatch.setattr(
        "app.services.creator_content_extraction_service.create_platform_crawler",
        lambda _platform: BlockedCrawler(),
    )
    provider = RecordingMediaProvider()
    work = pending_work().model_copy(
        update={"media_url": "https://media.example/persisted.mp4"}
    )
    work = CreatorWork.model_validate(work.model_dump())

    paths = asyncio.run(provider.download_media(work))

    assert provider.urls == ["https://media.example/persisted.mp4"]
    assert [path.name for path in paths] == ["persisted.mp4"]


def test_extraction_detects_attempt_fencing_loss(tmp_path) -> None:
    repository = FakeRepository(pending_work(), modified_count=0)
    result = asyncio.run(
        CreatorContentExtractionService(
            repository=repository,
            media_provider=FakeMediaProvider(tmp_path / "work.mp4"),
            transcriber=FakeTranscriber(),
        ).process_once()
    )

    assert result.stage == "lease_lost"
    assert result.success is False
