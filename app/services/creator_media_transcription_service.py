from __future__ import annotations

import ctypes
from datetime import datetime
import gc
import logging
from pathlib import Path
import resource
import time
from typing import Any

from app.core.config import PROJECT_ROOT
from app.models.creator_monitoring import (
    CN_TZ,
    CreatorMediaTranscript,
    CreatorTranscriptSegment,
)


# 部署流程预先准备的本地 faster-whisper 模型目录。
CREATOR_ASR_MODEL_PATH = PROJECT_ROOT / ".local" / "models" / "faster-whisper-small"
# 当前部署使用 CPU 对博主媒体执行转写。
CREATOR_ASR_DEVICE = "cpu"
# Int8 推理可将 CPU 内存占用和处理耗时控制在工作进程预算内。
CREATOR_ASR_COMPUTE_TYPE = "int8"
# 限制单次 CPU 转写最多使用两个推理线程，耗时换取更平稳的整机负载。
CREATOR_ASR_CPU_THREADS = 2
# 单个常驻模型只允许一个内部转写 worker，博主作品由外层队列串行调度。
CREATOR_ASR_NUM_WORKERS = 1
# 视频帧 OCR 用于补充字幕中出现、但 ASR 可能遗漏的数字和金融术语。
CREATOR_OCR_ENABLED = True
# RapidOCR 的 ONNX Runtime 单个算子最多使用两个 CPU 线程。
CREATOR_OCR_CPU_THREADS = 2
# OCR 图中的不同算子只使用一个调度线程，避免额外并行放大 CPU 峰值。
CREATOR_OCR_INTER_OP_THREADS = 1
# 每两秒识别一帧字幕，在保留连续字幕覆盖的同时降低视频解码和 OCR 开销。
CREATOR_OCR_SAMPLE_INTERVAL_SECONDS = 2.0
# 只有这些容器会进入视频帧字幕 OCR；音频流继续交给 Whisper，但不调用 OpenCV。
CREATOR_OCR_VIDEO_SUFFIXES = frozenset({".avi", ".mkv", ".mov", ".mp4", ".webm"})
logger = logging.getLogger(__name__)


class LazyRapidOCREngine:
    """延迟创建并可主动释放一个进程内共享的 RapidOCR 引擎。

    视频字幕和图片正文识别均可调用同一实例。引擎只在首次真正执行 OCR 时加载，
    队列空闲后由 worker 释放其原生模型内存，下一批作品到达时再自动重新创建。
    """

    def __init__(
        self,
        *,
        cpu_threads: int = CREATOR_OCR_CPU_THREADS,
        inter_op_threads: int = CREATOR_OCR_INTER_OP_THREADS,
    ) -> None:
        """保存 OCR 线程上限，并初始化尚未加载模型的共享引擎句柄。"""

        if cpu_threads <= 0 or inter_op_threads <= 0:
            raise ValueError("OCR 算子线程数必须大于 0")
        # ONNX Runtime 执行单个 OCR 算子时允许使用的最大 CPU 线程数。
        self.cpu_threads = cpu_threads
        # ONNX Runtime 并行调度不同 OCR 算子时允许使用的线程数。
        self.inter_op_threads = inter_op_threads
        # 当前进程已加载的 RapidOCR 实例；空值表示尚未使用或已经释放。
        self._engine: Any | None = None

    def __call__(self, source: Any) -> Any:
        """对路径或图像矩阵执行 OCR，并原样返回 RapidOCR 的结构化结果。"""

        if self._engine is None:
            from rapidocr import RapidOCR

            self._engine = RapidOCR(
                params={
                    "EngineConfig.onnxruntime.intra_op_num_threads": self.cpu_threads,
                    "EngineConfig.onnxruntime.inter_op_num_threads": (
                        self.inter_op_threads
                    ),
                }
            )
        return self._engine(source)

    def release(self) -> None:
        """丢弃已加载的 OCR 模型句柄，使原生运行时可以回收其内存。"""

        self._engine = None


def _current_process_rss_mib() -> float | None:
    """读取当前 Linux 进程常驻内存并换算为 MiB，读取失败时返回空值。

    ASR 与 OCR 的主要开销位于原生库中，Python 对象统计无法反映真实占用；部署
    服务器提供的 ``/proc`` 数值可以直接观察当前 RSS，且不会引入额外监控依赖。
    """

    try:
        for line in Path("/proc/self/status").read_text(encoding="ascii").splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) / 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def _peak_process_rss_mib() -> float:
    """返回当前进程生命周期内的最大常驻内存，单位为 MiB。"""

    return float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss) / 1024


def _trim_native_heap() -> bool:
    """请求 glibc 将空闲原生堆页归还操作系统，并返回是否成功执行。

    Whisper、ONNX Runtime 和 OpenCV 释放模型后可能把大块内存留在分配器缓存中；
    worker 队列已确认空闲时调用 ``malloc_trim`` 可以降低常驻 RSS。非 glibc 系统
    或缺少该符号时安全返回假值，不影响后续作品重新加载模型。
    """

    try:
        libc = ctypes.CDLL("libc.so.6")
        malloc_trim = libc.malloc_trim
        malloc_trim.argtypes = [ctypes.c_size_t]
        malloc_trim.restype = ctypes.c_int
        return bool(malloc_trim(0))
    except (OSError, AttributeError):
        return False


class FasterWhisperTranscriber:
    """使用延迟加载的 faster-whisper 和可选字幕 OCR 转写博主视频。

    语音识别生成主要文本和带时间戳片段。RapidOCR 会采样视频下方字幕区域，
    即使没有语音也能独立提供可用文本。重量级模型对象仅在首次使用时创建，
    避免调度器和非媒体工作进程意外加载模型。
    """

    def __init__(
        self,
        *,
        model_size: str | Path = CREATOR_ASR_MODEL_PATH,
        device: str = CREATOR_ASR_DEVICE,
        compute_type: str = CREATOR_ASR_COMPUTE_TYPE,
        cpu_threads: int = CREATOR_ASR_CPU_THREADS,
        num_workers: int = CREATOR_ASR_NUM_WORKERS,
        download_root: str | Path | None = None,
        enable_ocr: bool = CREATOR_OCR_ENABLED,
        ocr_sample_interval_seconds: float = CREATOR_OCR_SAMPLE_INTERVAL_SECONDS,
        ocr_engine: Any | None = None,
    ) -> None:
        """保存推理设置，并初始化用于延迟加载模型的空句柄。

        ``model_size`` 可接收 faster-whisper 模型名称或本地模型目录；``device``、
        ``compute_type``、``cpu_threads`` 和 ``num_workers`` 控制推理资源；OCR
        采样间隔必须大于零；仅在明确允许下载模型时才传递 ``download_root``。
        """

        if cpu_threads <= 0 or num_workers <= 0:
            raise ValueError("ASR CPU 线程数和内部 worker 数必须大于 0")
        if ocr_sample_interval_seconds <= 0:
            raise ValueError("OCR 采样间隔必须大于 0")
        # 传递给 faster-whisper 的模型标识或本地目录。
        self.model_size = str(model_size)
        # 推理设备，例如 ``cpu`` 或 ``cuda``。
        self.device = device
        # 模型精度或量化模式，例如 ``int8``。
        self.compute_type = compute_type
        # 单次 CPU 转写允许使用的最大推理线程数。
        self.cpu_threads = cpu_threads
        # faster-whisper 模型内部允许并行服务的转写请求数。
        self.num_workers = num_workers
        # 可选的 faster-whisper 下载缓存目录。
        self.download_root = None if download_root is None else str(download_root)
        # 是否采样视频帧以识别硬字幕。
        self.enable_ocr = enable_ocr
        # 相邻两次字幕 OCR 采样之间的目标秒数。
        self.ocr_sample_interval_seconds = ocr_sample_interval_seconds
        # 延迟初始化并在当前进程内复用的 faster-whisper 模型实例。
        self._model: Any | None = None
        # 可与图片提取器共享、支持空闲释放的延迟 RapidOCR 引擎。
        self._ocr: Any = ocr_engine or LazyRapidOCREngine()

    def transcribe(self, media_path: str | Path) -> CreatorMediaTranscript:
        """识别一个本地视频，返回合并后的 ASR/OCR 文本及审计数据。

        输入文件必须存在且非空。OCR 失败只会记录日志，不会丢弃有效的 ASR
        输出；只有 OCR 文本的视频同样可用。仅当两个来源均无文本时方法才失败，
        并会保留两类来源文本以及毫秒级 ASR 片段，供下游诊断。
        """

        path = Path(media_path)
        if not path.is_file() or path.stat().st_size == 0:
            raise RuntimeError("待转写的博主媒体文件不存在或为空")
        started_at = time.monotonic()
        rss_before = _current_process_rss_mib()
        model = self._get_model()
        segments_iter, info = model.transcribe(
            str(path),
            language="zh",
            beam_size=5,
            vad_filter=True,
            condition_on_previous_text=False,
        )
        segments: list[CreatorTranscriptSegment] = []
        text_parts: list[str] = []
        for segment in segments_iter:
            text = str(segment.text or "").strip()
            if not text:
                continue
            text_parts.append(text)
            segments.append(
                CreatorTranscriptSegment(
                    start_ms=max(int(float(segment.start) * 1000), 0),
                    end_ms=max(int(float(segment.end) * 1000), 0),
                    text=text,
                )
            )
        asr_text = "".join(text_parts).strip()
        asr_finished_at = time.monotonic()
        ocr_text = ""
        if self.enable_ocr and path.suffix.lower() in CREATOR_OCR_VIDEO_SUFFIXES:
            try:
                ocr_text = self._extract_subtitles(path)
            except Exception:
                logger.exception("creator subtitle OCR failed; falling back to ASR")
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
        finished_at = time.monotonic()
        logger.info(
            "博主媒体转写完成 file=%s size_mib=%.2f asr_seconds=%.2f "
            "ocr_seconds=%.2f total_seconds=%.2f rss_before_mib=%s "
            "rss_after_mib=%s peak_rss_mib=%.2f",
            path.name,
            path.stat().st_size / (1024 * 1024),
            asr_finished_at - started_at,
            finished_at - asr_finished_at,
            finished_at - started_at,
            self._format_optional_mib(rss_before),
            self._format_optional_mib(_current_process_rss_mib()),
            _peak_process_rss_mib(),
        )
        return CreatorMediaTranscript(
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
        """仅创建一次配置的 faster-whisper 模型，并在后续调用中复用。

        可选依赖会延迟到实际处理媒体时才导入。工作进程未安装该依赖时会抛出
        明确的运行时错误，而不会破坏无关调度器或 API 的导入。
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
                "cpu_threads": self.cpu_threads,
                "num_workers": self.num_workers,
            }
            if self.download_root:
                kwargs["download_root"] = self.download_root
            self._model = WhisperModel(self.model_size, **kwargs)
        return self._model

    def _extract_subtitles(self, media_path: Path) -> str:
        """采样视频下方字幕区域，并合并高置信度 OCR 文本行。

        按配置的秒级间隔检查一帧，只将常见的屏幕下方字幕区域送入 RapidOCR，并过滤
        低置信度文本；即使 OCR 抛出异常，也会释放 OpenCV 视频捕获资源。
        """

        import cv2

        capture = cv2.VideoCapture(str(media_path))
        if not capture.isOpened():
            raise RuntimeError("OpenCV 无法打开博主视频")
        fps = float(capture.get(cv2.CAP_PROP_FPS) or 0)
        sample_every = self._ocr_sample_every(
            fps,
            sample_interval_seconds=self.ocr_sample_interval_seconds,
        )
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
    def _ocr_sample_every(
        fps: float,
        *,
        sample_interval_seconds: float = CREATOR_OCR_SAMPLE_INTERVAL_SECONDS,
    ) -> int:
        """根据视频帧率和目标秒数返回至少为一帧的安全采样间隔。"""

        safe_fps = fps if fps > 0 else 25
        return max(round(safe_fps * sample_interval_seconds), 1)

    def release_resources(self) -> None:
        """释放已加载的 Whisper 与共享 OCR 模型，并触发 Python 垃圾回收。

        该方法只在作品提取和分析队列同时为空时调用，不会中断正在进行的转写；
        保留全部轻量配置，后续新作品到达时仍可按相同参数延迟重建模型。
        """

        self._model = None
        release_ocr = getattr(self._ocr, "release", None)
        if callable(release_ocr):
            release_ocr()
        gc.collect()
        _trim_native_heap()

    @staticmethod
    def _format_optional_mib(value: float | None) -> str:
        """将可选 MiB 数值格式化为稳定日志文本，缺失时返回 ``unknown``。"""

        return "unknown" if value is None else f"{value:.2f}"

    @staticmethod
    def _merge_ocr_lines(lines: list[str]) -> list[str]:
        """按视频帧顺序合并重复及逐步扩展显示的字幕。

        完全重复或被上一行包含的字符串会被丢弃。较长文本包含上一行或共享足够
        长的前缀时，会替换上一条记录，避免把同一句字幕动画的每一帧都发送给
        单作品观点分析器。
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


__all__ = ["FasterWhisperTranscriber", "LazyRapidOCREngine"]
