from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
from pathlib import Path
from typing import Any

from app.core.config import PROJECT_ROOT
from app.models.douyin_creator_work import (
    DouyinTranscript,
    DouyinTranscriptSegment,
)


CN_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
# 本机固定使用的 faster-whisper 模型目录；模型文件由部署脚本提前准备。
DOUYIN_ASR_MODEL_PATH = PROJECT_ROOT / ".local" / "models" / "faster-whisper-small"
# 当前服务器没有为该 worker 分配 GPU，因此固定在 CPU 上执行语音识别。
DOUYIN_ASR_DEVICE = "cpu"
# CPU 推理固定使用 int8 量化，以控制内存占用和单视频处理时间。
DOUYIN_ASR_COMPUTE_TYPE = "int8"
# 固定启用画面字幕 OCR，用于补充 ASR 容易识别错误的数字和财经术语。
DOUYIN_OCR_ENABLED = True
logger = logging.getLogger(__name__)


class FasterWhisperTranscriber:
    """
    为抖音分析 worker 提供延迟加载的本地语音识别和字幕 OCR。

    faster-whisper 负责语音正文和时间分段，RapidOCR 负责画面底部字幕；两路
    结果可独立成功并会标明来源。重量级模型只在首次实际转写时加载，避免调度器
    或未启用 worker 的进程占用模型内存。
    """

    def __init__(
        self,
        *,
        model_size: str | Path = DOUYIN_ASR_MODEL_PATH,
        device: str = DOUYIN_ASR_DEVICE,
        compute_type: str = DOUYIN_ASR_COMPUTE_TYPE,
        download_root: str | Path | None = None,
        enable_ocr: bool = DOUYIN_OCR_ENABLED,
    ) -> None:
        """
        保存 ASR 运行参数并初始化尚未加载的模型句柄。

        `model_size` 可以是模型名称或本地路径；`device` 与 `compute_type` 控制
        推理设备和量化方式；`download_root` 仅在需要下载模型时使用。
        """
        # faster-whisper 模型名称或本地模型目录。
        self.model_size = str(model_size)
        # ASR 推理设备，例如 cpu 或 cuda。
        self.device = device
        # ASR 计算精度或量化类型，例如 int8。
        self.compute_type = compute_type
        # 模型下载缓存目录；为空时沿用 faster-whisper 默认位置。
        self.download_root = None if download_root is None else str(download_root)
        # 是否额外抽帧识别视频画面中的硬字幕。
        self.enable_ocr = enable_ocr
        # 延迟创建的 faster-whisper 模型实例。
        self._model: Any | None = None
        # 延迟创建的 RapidOCR 实例。
        self._ocr: Any | None = None

    def transcribe(self, media_path: str | Path) -> DouyinTranscript:
        """
        对本地视频执行中文 ASR，并按配置合并画面字幕 OCR。

        方法校验媒体文件，生成毫秒级语音分段，并在 OCR 失败时记录日志后继续
        使用 ASR。两路都没有文本才报错；返回结果会保留独立原文、组合文本、
        识别来源、模型名称和完成时间，供后续 LLM 与盘前可用性检查使用。
        """
        path = Path(media_path)
        if not path.is_file() or path.stat().st_size == 0:
            raise RuntimeError("待转写的抖音媒体文件不存在或为空")
        model = self._get_model()
        segments_iter, info = model.transcribe(
            str(path),
            language="zh",
            beam_size=5,
            vad_filter=True,
            condition_on_previous_text=False,
        )
        segments: list[DouyinTranscriptSegment] = []
        text_parts: list[str] = []
        for segment in segments_iter:
            text = str(segment.text or "").strip()
            if not text:
                continue
            text_parts.append(text)
            segments.append(
                DouyinTranscriptSegment(
                    start_ms=max(int(float(segment.start) * 1000), 0),
                    end_ms=max(int(float(segment.end) * 1000), 0),
                    text=text,
                )
            )
        asr_text = "".join(text_parts).strip()
        ocr_text = ""
        if self.enable_ocr:
            try:
                ocr_text = self._extract_subtitles(path)
            except Exception:
                logger.exception("douyin subtitle OCR failed; falling back to ASR")
        if not asr_text and not ocr_text:
            raise RuntimeError("ASR 与字幕 OCR 均未识别出有效文本")
        if ocr_text and asr_text:
            full_text = f"【视频字幕 OCR】\n{ocr_text}\n【语音 ASR】\n{asr_text}"
            provider = "rapidocr+faster-whisper"
        elif ocr_text:
            full_text = ocr_text
            provider = "rapidocr"
        else:
            full_text = asr_text
            provider = "faster-whisper"
        return DouyinTranscript(
            text=full_text,
            asr_text=asr_text,
            ocr_text=ocr_text,
            segments=segments,
            language=str(getattr(info, "language", None) or "zh"),
            provider=provider,
            model=self.model_size,
            transcribed_at=datetime.now(CN_TZ),
        )

    def _get_model(self) -> Any:
        """
        按需创建并缓存 faster-whisper 模型实例。

        依赖缺失时给出明确安装错误；后续转写复用同一实例，避免每个视频重复
        加载模型权重。
        """
        if self._model is None:
            try:
                from faster_whisper import WhisperModel
            except ImportError as exc:
                raise RuntimeError(
                    "缺少 faster-whisper，请先安装 requirements.txt"
                ) from exc
            kwargs: dict[str, Any] = {
                "device": self.device,
                "compute_type": self.compute_type,
            }
            if self.download_root:
                kwargs["download_root"] = self.download_root
            self._model = WhisperModel(self.model_size, **kwargs)
        return self._model

    def _extract_subtitles(self, media_path: Path) -> str:
        """
        从视频底部字幕区域按约每秒一帧执行 OCR，并合并连续结果。

        仅截取画面下方的常见字幕带，过滤低置信度文字；视频句柄无论成功失败
        都会释放。返回文本用于补充 ASR 对数字和财经术语的识别。
        """
        import cv2

        if self._ocr is None:
            from rapidocr import RapidOCR

            self._ocr = RapidOCR()
        capture = cv2.VideoCapture(str(media_path))
        if not capture.isOpened():
            raise RuntimeError("OpenCV 无法打开抖音视频")
        fps = float(capture.get(cv2.CAP_PROP_FPS) or 0)
        sample_every = self._ocr_sample_every(fps)
        frame_index = 0
        detected_lines: list[str] = []
        try:
            while True:
                ok, frame = capture.read()
                if not ok:
                    break
                if frame_index % sample_every:
                    frame_index += 1
                    continue
                height, width = frame.shape[:2]
                crop = frame[
                    int(height * 0.68) : int(height * 0.87),
                    int(width * 0.05) : int(width * 0.95),
                ]
                output = self._ocr(crop)
                texts = list(output.txts or [])
                scores = list(output.scores or [])
                line = "".join(
                    str(text).strip()
                    for text, score in zip(texts, scores)
                    if str(text).strip() and float(score) >= 0.55
                )
                if line:
                    detected_lines.append(line)
                frame_index += 1
        finally:
            capture.release()
        return "\n".join(self._merge_ocr_lines(detected_lines))

    @staticmethod
    def _ocr_sample_every(fps: float) -> int:
        """计算约每秒抽取一帧的间隔；缺少帧率元数据时按 25 FPS 估算。"""
        return max(round(fps if fps > 0 else 25), 1)

    @staticmethod
    def _merge_ocr_lines(lines: list[str]) -> list[str]:
        """
        合并连续 OCR 帧中的重复字幕和逐字扩展字幕。

        完全重复、被上一行包含的文本会丢弃；新行包含上一行或共享足够长前缀时
        更新上一条，减少同一句字幕在最终提示词中反复出现。
        """
        merged: list[str] = []
        for raw_line in lines:
            line = raw_line.strip()
            if not line:
                continue
            if not merged:
                merged.append(line)
                continue
            previous = merged[-1]
            if line == previous or line in previous:
                continue
            if previous in line:
                merged[-1] = line
                continue
            common_prefix = 0
            for left, right in zip(previous, line):
                if left != right:
                    break
                common_prefix += 1
            if len(line) > len(previous) and common_prefix >= max(
                min(len(previous) - 1, 4), 2
            ):
                merged[-1] = line
                continue
            merged.append(line)
        return merged
