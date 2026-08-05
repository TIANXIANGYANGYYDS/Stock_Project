from types import SimpleNamespace

from app.services.creator_media_transcription_service import (
    CREATOR_ASR_CPU_THREADS,
    CREATOR_OCR_CPU_THREADS,
    FasterWhisperTranscriber,
    LazyRapidOCREngine,
)


def test_default_media_cpu_threads_are_limited_to_two() -> None:
    """验证生产默认 ASR 和 OCR 都只使用两个 CPU 线程。"""

    assert CREATOR_ASR_CPU_THREADS == 2
    assert CREATOR_OCR_CPU_THREADS == 2


def test_ocr_line_merge_removes_repeats_and_keeps_longer_transition() -> None:
    """验证连续字幕去重，并保留逐步补全后的较长文本。"""

    assert FasterWhisperTranscriber._merge_ocr_lines(
        ["今晚老美大跌", "今晚老美大跌", "业绩不预", "业绩不及预期", "承压"]
    ) == ["今晚老美大跌", "业绩不及预期", "承压"]


def test_ocr_can_supply_text_when_asr_is_empty(tmp_path) -> None:
    """验证语音识别为空时，字幕 OCR 可独立生成有效转写结果。"""

    media_path = tmp_path / "subtitles-only.mp4"
    media_path.write_bytes(b"video")
    transcriber = FasterWhisperTranscriber(enable_ocr=True)
    transcriber._model = SimpleNamespace(
        transcribe=lambda *args, **kwargs: (iter(()), SimpleNamespace(language="zh"))
    )
    transcriber._extract_subtitles = (  # type: ignore[method-assign]
        lambda path: "字幕里的行业观点"
    )

    result = transcriber.transcribe(media_path)

    assert result.text == "字幕里的行业观点"
    assert result.asr_text == ""
    assert result.ocr_text == "字幕里的行业观点"
    assert result.provider == "rapidocr"


def test_audio_only_media_skips_subtitle_ocr(tmp_path) -> None:
    """验证 B 站音频 m4s 只做 ASR，不再触发 OpenCV 字幕提取。"""

    media_path = tmp_path / "audio-only.m4s"
    media_path.write_bytes(b"audio")
    transcriber = FasterWhisperTranscriber(enable_ocr=True)
    transcriber._model = SimpleNamespace(
        transcribe=lambda *args, **kwargs: (
            iter([SimpleNamespace(start=0, end=1, text="语音内容")]),
            SimpleNamespace(language="zh"),
        )
    )
    transcriber._extract_subtitles = (  # type: ignore[method-assign]
        lambda path: (_ for _ in ()).throw(AssertionError("audio must skip OCR"))
    )

    result = transcriber.transcribe(media_path)

    assert result.text == "语音内容"
    assert result.asr_text == "语音内容"
    assert result.ocr_text == ""
    assert result.provider == "faster-whisper"


def test_ocr_sampling_uses_safe_fallback_when_fps_is_missing() -> None:
    """验证视频帧率缺失时使用两秒安全间隔，并对正常帧率取整。"""

    assert FasterWhisperTranscriber._ocr_sample_every(0) == 50
    assert FasterWhisperTranscriber._ocr_sample_every(29.97) == 60


def test_whisper_model_limits_cpu_threads_and_internal_workers(monkeypatch) -> None:
    """验证模型初始化显式限制 CPU 线程和内部 worker，防止占满服务器核心。"""

    captured: dict[str, object] = {}

    def fake_model(model_size: str, **kwargs):
        """记录模型路径和构造参数，并返回无需加载权重的测试对象。"""

        captured["model_size"] = model_size
        captured.update(kwargs)
        return SimpleNamespace()

    monkeypatch.setattr("faster_whisper.WhisperModel", fake_model)
    transcriber = FasterWhisperTranscriber(
        model_size="local-model",
        cpu_threads=4,
        num_workers=1,
    )

    transcriber._get_model()

    assert captured["model_size"] == "local-model"
    assert captured["cpu_threads"] == 4
    assert captured["num_workers"] == 1


def test_release_resources_drops_loaded_model_and_shared_ocr(monkeypatch) -> None:
    """验证队列空闲释放会清空 Whisper，并通知共享 OCR 丢弃原生模型。"""

    released: list[bool] = []
    trimmed: list[bool] = []
    ocr = SimpleNamespace(release=lambda: released.append(True))
    monkeypatch.setattr(
        "app.services.creator_media_transcription_service._trim_native_heap",
        lambda: trimmed.append(True),
    )
    transcriber = FasterWhisperTranscriber(ocr_engine=ocr)
    transcriber._model = SimpleNamespace()

    transcriber.release_resources()

    assert transcriber._model is None
    assert released == [True]
    assert trimmed == [True]


def test_shared_rapidocr_engine_limits_onnx_threads(monkeypatch) -> None:
    """验证共享 OCR 引擎向 ONNX Runtime 传入明确的算子线程上限。"""

    captured: dict[str, object] = {}

    class FakeRapidOCR:
        """记录 RapidOCR 初始化参数，并模拟一次可调用的 OCR 引擎。"""

        def __init__(self, *, params) -> None:
            """保存待断言的 ONNX Runtime 参数。"""

            captured.update(params)

        def __call__(self, source):
            """记录输入并返回轻量测试结果。"""

            captured["source"] = source
            return "ocr-result"

    monkeypatch.setattr("rapidocr.RapidOCR", FakeRapidOCR)
    engine = LazyRapidOCREngine(cpu_threads=4, inter_op_threads=1)

    assert engine("image") == "ocr-result"
    assert captured == {
        "EngineConfig.onnxruntime.intra_op_num_threads": 4,
        "EngineConfig.onnxruntime.inter_op_num_threads": 1,
        "source": "image",
    }
