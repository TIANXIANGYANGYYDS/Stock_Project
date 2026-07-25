from types import SimpleNamespace

from app.services.douyin_asr_service import FasterWhisperTranscriber


def test_ocr_line_merge_removes_repeats_and_keeps_longer_transition() -> None:
    assert FasterWhisperTranscriber._merge_ocr_lines(
        ["今晚老美大跌", "今晚老美大跌", "业绩不预", "业绩不及预期", "承压"]
    ) == ["今晚老美大跌", "业绩不及预期", "承压"]


def test_ocr_can_supply_text_when_asr_is_empty(tmp_path) -> None:
    media_path = tmp_path / "subtitles-only.mp4"
    media_path.write_bytes(b"video")
    transcriber = FasterWhisperTranscriber(enable_ocr=True)
    transcriber._model = SimpleNamespace(
        transcribe=lambda *args, **kwargs: (iter(()), SimpleNamespace(language="zh"))
    )
    transcriber._extract_subtitles = lambda path: "字幕里的行业观点"  # type: ignore[method-assign]

    result = transcriber.transcribe(media_path)

    assert result.text == "字幕里的行业观点"
    assert result.asr_text == ""
    assert result.ocr_text == "字幕里的行业观点"
    assert result.provider == "rapidocr"


def test_ocr_sampling_uses_safe_fallback_when_fps_is_missing() -> None:
    assert FasterWhisperTranscriber._ocr_sample_every(0) == 25
    assert FasterWhisperTranscriber._ocr_sample_every(29.97) == 30
