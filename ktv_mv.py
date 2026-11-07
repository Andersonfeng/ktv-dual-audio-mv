#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""KTV 双音轨 MV 制作工具。

把单音轨 MP4/MKV/FLV 转换成包含两条音轨的 MKV：
    音轨 1：原唱（直接从源文件复制，不重新编码）
    音轨 2：伴唱（用 Demucs 分离人声，或使用内置的声道相消法）

只依赖 Python 标准库；人声分离推荐额外安装 Demucs。
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
import wave
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional, Sequence

try:
    import audioop
except ImportError:  # Python 3.13+ 会移除 audioop；检测功能自动跳过
    audioop = None  # type: ignore[assignment]

APP_NAME = "KTV 双音轨 MV 工具"
VERSION = "1.0.2"
SUPPORTED_EXTS = {".mp4", ".mkv", ".flv", ".f4v"}
DEFAULT_SUFFIX = ""
LEGACY_SUFFIXES = (".dual",)
GENERATED_TAG = "KTVMV"
GENERATED_TAG_VALUE = "dual_audio"
PROJECT_DIR = Path(__file__).resolve().parent

Emit = Callable[[str, str], None]
Progress = Callable[[float, str], None]


def preferred_python() -> str:
    """优先使用项目自带 .venv 中的 Python（Demucs 安装在这里）。"""
    candidates = [
        PROJECT_DIR / ".venv" / "Scripts" / "python.exe",
        PROJECT_DIR / ".venv" / "bin" / "python",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return sys.executable


def demucs_install_hint() -> str:
    venv_python = PROJECT_DIR / ".venv" / "Scripts" / "python.exe"
    if venv_python.is_file():
        return r'".venv\Scripts\python.exe" -m pip install -U demucs -i https://pypi.org/simple/'
    return "python -m pip install -U demucs -i https://pypi.org/simple/"



# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------


class ProcessingError(RuntimeError):
    """可以展示给用户的处理错误。"""


class DependencyError(ProcessingError):
    """缺少外部程序或 Python 包。"""


class ProcessingCancelled(ProcessingError):
    """用户取消了任务。"""


def configure_piped_console() -> None:
    """在输出被重定向到管道/文件时使用 UTF-8，避免 Windows 代码页乱码。"""
    for stream in (sys.stdout, sys.stderr):
        if stream is None:
            continue
        try:
            if not stream.isatty():
                stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            pass


def console_emit(level: str, message: str) -> None:
    prefixes = {
        "info": "[信息]",
        "ok": "[完成]",
        "warn": "[注意]",
        "error": "[错误]",
        "detail": "[详细]",
    }
    stream = sys.stderr if level == "error" else sys.stdout
    for index, line in enumerate(str(message).splitlines() or [""]):
        prefix = prefixes.get(level, "[信息]") if index == 0 else "      "
        print(f"{prefix} {line}", file=stream, flush=True)


def noop_emit(level: str, message: str) -> None:
    pass


def noop_progress(percent: float, stage: str) -> None:
    pass


def format_duration(seconds: Optional[float]) -> str:
    if seconds is None or seconds < 0:
        return "未知"
    total = int(round(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def format_size(size: int) -> str:
    value = float(max(0, size))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{size} B"


def _to_float(value: object) -> Optional[float]:
    try:
        result = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if result != result or result in (float("inf"), float("-inf")):
        return None
    return result


def _creation_flags() -> int:
    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0)) if os.name == "nt" else 0


def _quote_command(command: Sequence[object]) -> str:
    return subprocess.list2cmdline([str(part) for part in command])


@dataclass(frozen=True)
class StereoStats:
    rms: float
    difference_ratio: float


CENTER_DUAL_MONO_RATIO = 0.01
MIN_INSTRUMENTAL_RMS = 4.0


def analyze_stereo_wav(path: Path) -> Optional[StereoStats]:
    """分析 16-bit 立体声 WAV 的整体 RMS 和左右声道差异比例。

    difference_ratio 接近 0 表示左右声道完全相同，属于“双声道单声道”。
    这类音源无法用声道相消去除人声，否则会连伴奏一起消掉。
    """
    if audioop is None:
        return None
    try:
        with wave.open(str(path), "rb") as handle:
            if handle.getnchannels() != 2 or handle.getsampwidth() != 2:
                return None
            sample_rate = handle.getframerate()
            chunk_frames = max(sample_rate * 10, 1)
            total_square = 0.0
            total_samples = 0
            max_ratio = 0.0

            while True:
                data = handle.readframes(chunk_frames)
                if not data:
                    break
                sample_count = len(data) // 2
                if sample_count == 0:
                    continue
                rms = float(audioop.rms(data, 2))
                total_square += rms * rms * sample_count
                total_samples += sample_count
                if rms < 16:
                    continue
                difference = audioop.tomono(data, 2, 0.5, -0.5)
                difference_rms = float(audioop.rms(difference, 2))
                max_ratio = max(max_ratio, difference_rms / rms)
    except (OSError, wave.Error, EOFError):
        return None

    if total_samples == 0:
        return None
    overall_rms = (total_square / total_samples) ** 0.5
    return StereoStats(rms=overall_rms, difference_ratio=max_ratio)



# ---------------------------------------------------------------------------
# 外部进程
# ---------------------------------------------------------------------------


class ProcessRegistry:
    """记录正在运行的子进程，以便取消任务时一起结束。"""

    def __init__(self) -> None:
        self.cancelled = threading.Event()
        self._lock = threading.Lock()
        self._processes: set[subprocess.Popen[str]] = set()

    def add(self, process: subprocess.Popen[str]) -> None:
        with self._lock:
            self._processes.add(process)

    def discard(self, process: subprocess.Popen[str]) -> None:
        with self._lock:
            self._processes.discard(process)

    def cancel_all(self) -> None:
        self.cancelled.set()
        with self._lock:
            processes = list(self._processes)
        for process in processes:
            try:
                process.kill()
            except OSError:
                pass

    def reset(self) -> None:
        self.cancelled.clear()


def run_command(
    command: Sequence[object],
    *,
    registry: ProcessRegistry,
    emit: Emit,
    verbose: bool = False,
    cwd: Optional[Path] = None,
    env: Optional[dict[str, str]] = None,
) -> None:
    """运行命令并收集错误输出。取消时抛出 ProcessingCancelled。"""
    if registry.cancelled.is_set():
        raise ProcessingCancelled("任务已取消")

    args = [str(part) for part in command]
    if verbose:
        emit("detail", f"执行命令：{_quote_command(args)}")

    try:
        process = subprocess.Popen(
            args,
            cwd=str(cwd) if cwd else None,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=_creation_flags(),
        )
    except OSError as exc:
        raise ProcessingError(f"无法启动外部程序：{args[0]}\n{exc}") from exc

    registry.add(process)
    if registry.cancelled.is_set():
        try:
            process.kill()
        except OSError:
            pass

    tail: deque[str] = deque(maxlen=30)
    try:
        if process.stdout is not None:
            for raw_line in process.stdout:
                line = raw_line.rstrip("\r\n")
                if line:
                    tail.append(line)
                    if verbose:
                        emit("detail", line)
                if registry.cancelled.is_set():
                    try:
                        process.kill()
                    except OSError:
                        pass
                    break
        return_code = process.wait()
    finally:
        registry.discard(process)

    if registry.cancelled.is_set():
        raise ProcessingCancelled("任务已取消")

    if return_code != 0:
        detail = "\n".join(tail)
        message = f"外部命令执行失败（退出码 {return_code}）"
        if detail:
            message += f"\n{detail}"
        raise ProcessingError(message)


# ---------------------------------------------------------------------------
# 媒体探测
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MediaInfo:
    path: Path
    duration: Optional[float]
    audio_duration: Optional[float]
    video_streams: int
    audio_streams: int
    video_codec: str = ""
    width: int = 0
    height: int = 0
    audio_codec: str = ""
    audio_channels: int = 0
    audio_layout: str = ""
    audio_sample_rate: int = 0
    audio_offset: float = 0.0
    audio_titles: tuple[str, ...] = ()
    audio_defaults: tuple[bool, ...] = ()
    size_bytes: int = 0
    generated: bool = False

    @property
    def has_video(self) -> bool:
        return self.video_streams > 0

    @property
    def has_audio(self) -> bool:
        return self.audio_streams > 0


@dataclass
class Tools:
    ffmpeg: str
    ffprobe: str
    python: str
    python_candidates: tuple[str, ...] = ()


@dataclass(frozen=True)
class RunConfig:
    backend: str = "auto"
    demucs_model: str = "htdemucs"
    demucs_device: str = "auto"
    instrumental_codec: str = "aac"
    bitrate: str = "192k"
    suffix: str = DEFAULT_SUFFIX
    overwrite: bool = False
    keep_temp: bool = False
    temp_dir: Optional[Path] = None
    verbose: bool = False


def _find_executable(explicit: Optional[str], name: str) -> Optional[str]:
    if explicit:
        candidate = Path(explicit).expanduser()
        if candidate.is_file():
            return str(candidate)
        found = shutil.which(explicit)
        if found:
            return found
        return None
    return shutil.which(name)


def check_tools(
    ffmpeg_path: Optional[str] = None,
    ffprobe_path: Optional[str] = None,
) -> Tools:
    ffmpeg = _find_executable(ffmpeg_path, "ffmpeg")
    if not ffmpeg:
        raise DependencyError(
            "未找到 FFmpeg。请安装 FFmpeg，并让 ffmpeg.exe 出现在 PATH 中，"
            "或者使用 --ffmpeg 指定完整路径。\n"
            "Windows 可试用：winget install Gyan.FFmpeg"
        )

    ffprobe = _find_executable(ffprobe_path, "ffprobe")
    if not ffprobe:
        sibling = Path(ffmpeg).with_name("ffprobe.exe")
        if sibling.is_file():
            ffprobe = str(sibling)
    if not ffprobe:
        raise DependencyError(
            "未找到 ffprobe。请把 ffprobe.exe 和 ffmpeg.exe 放在同一目录，"
            "或使用 --ffprobe 指定完整路径。"
        )

    primary_python = preferred_python()
    python_candidates: list[str] = []
    seen: set[str] = set()
    for candidate in (primary_python, sys.executable):
        key = os.path.normcase(os.path.abspath(candidate))
        if key not in seen:
            seen.add(key)
            python_candidates.append(candidate)

    return Tools(
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        python=primary_python,
        python_candidates=tuple(python_candidates),
    )


def probe_media(path: Path, ffprobe: str) -> MediaInfo:
    command = [
        ffprobe,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    try:
        result = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=90,
            creationflags=_creation_flags(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProcessingError(f"无法读取媒体信息：{path.name}\n{exc}") from exc

    if result.returncode != 0:
        detail = (result.stderr or "").strip()
        raise ProcessingError(
            f"无法读取媒体信息：{path.name}" + (f"\n{detail}" if detail else "")
        )

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ProcessingError(f"FFprobe 返回了无法解析的数据：{path.name}") from exc

    streams = data.get("streams") or []
    format_info = data.get("format") or {}

    video_streams = [
        stream
        for stream in streams
        if stream.get("codec_type") == "video"
        and not (stream.get("disposition") or {}).get("attached_pic")
    ]
    audio_streams = [
        stream for stream in streams if stream.get("codec_type") == "audio"
    ]

    main_video = video_streams[0] if video_streams else {}
    first_audio = audio_streams[0] if audio_streams else {}

    format_duration = _to_float(format_info.get("duration"))
    video_duration = _to_float(main_video.get("duration"))
    audio_duration = _to_float(first_audio.get("duration"))
    duration = format_duration or video_duration or audio_duration

    format_start = _to_float(format_info.get("start_time")) or 0.0
    audio_start = _to_float(first_audio.get("start_time")) or 0.0
    audio_offset = max(0.0, audio_start - format_start)

    audio_titles = tuple(
        str((stream.get("tags") or {}).get("title")
            or (stream.get("tags") or {}).get("handler_name")
            or "")
        for stream in audio_streams
    )
    audio_defaults = tuple(
        bool((stream.get("disposition") or {}).get("default"))
        for stream in audio_streams
    )

    try:
        size_bytes = int(format_info.get("size") or path.stat().st_size)
    except (TypeError, ValueError, OSError):
        size_bytes = 0

    global_tags = {
        str(key).lower(): str(value)
        for key, value in (format_info.get("tags") or {}).items()
    }
    generated = global_tags.get(GENERATED_TAG.lower()) == GENERATED_TAG_VALUE

    return MediaInfo(
        path=path,
        duration=duration,
        audio_duration=audio_duration or duration,
        video_streams=len(video_streams),
        audio_streams=len(audio_streams),
        video_codec=str(main_video.get("codec_name") or ""),
        width=int(main_video.get("width") or 0),
        height=int(main_video.get("height") or 0),
        audio_codec=str(first_audio.get("codec_name") or ""),
        audio_channels=int(first_audio.get("channels") or 0),
        audio_layout=str(first_audio.get("channel_layout") or ""),
        audio_sample_rate=int(first_audio.get("sample_rate") or 0),
        audio_offset=audio_offset,
        audio_titles=audio_titles,
        audio_defaults=audio_defaults,
        size_bytes=size_bytes,
        generated=generated,
    )


# ---------------------------------------------------------------------------
# 人声分离
# ---------------------------------------------------------------------------


def demucs_version(python_executable: str) -> Optional[str]:
    """实际导入一次 Demucs / torch，避免只检测到安装记录但运行库损坏。"""
    code = (
        "import importlib.util as u\n"
        "if u.find_spec('demucs') is None:\n"
        "    raise SystemExit(3)\n"
        "import demucs, torch\n"
        "print(demucs.__version__)\n"
    )
    try:
        result = subprocess.run(
            [python_executable, "-c", code],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            creationflags=_creation_flags(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return (result.stdout or "").strip().splitlines()[-1] or "未知版本"


def resolve_backend(requested: str, tools: Tools, emit: Emit) -> str:
    if requested == "center":
        emit("warn", "使用内置快速消音：只适合人声位于正中央的立体声歌曲，效果有限。")
        return "center"

    candidates = tools.python_candidates or (tools.python,)
    version: Optional[str] = None
    for candidate in candidates:
        version = demucs_version(candidate)
        if version:
            if candidate != tools.python:
                emit("info", f"检测到 Demucs 安装在：{candidate}")
            tools.python = candidate
            break

    if requested == "demucs":
        if not version:
            raise DependencyError(
                "未检测到 Demucs（或 torch）。请先安装：\n"
                f"  {demucs_install_hint()}\n"
                "如果已经安装，请确认安装到了当前 Python，"
                "或把安装路径加入 PATH；也可以先用 --backend center 试用内置消音。"
            )
        emit("info", f"人声分离引擎：Demucs {version}")
        return "demucs"

    if version:
        emit("info", f"自动选择 Demucs {version} 进行人声分离。")
        return "demucs"

    emit(
        "warn",
        "未检测到 Demucs，自动改用内置快速消音。"
        "它只能消除位于正中央的人声，且可能损失立体声；追求质量请安装 Demucs：\n"
        f"  {demucs_install_hint()}",
    )
    return "center"


# ---------------------------------------------------------------------------
# 单个文件的处理
# ---------------------------------------------------------------------------


@dataclass
class FileResult:
    source: Path
    output: Optional[Path] = None
    status: str = "failed"  # ok / skipped / failed / cancelled
    message: str = ""
    elapsed: float = 0.0
    size_bytes: int = 0


@dataclass
class BatchSummary:
    results: list[FileResult] = field(default_factory=list)
    cancelled: bool = False
    fatal_error: str = ""
    prefiltered_count: int = 0

    @property
    def ok_count(self) -> int:
        return sum(1 for item in self.results if item.status == "ok")

    @property
    def skipped_count(self) -> int:
        return self.prefiltered_count + sum(
            1 for item in self.results if item.status == "skipped"
        )

    @property
    def failed_count(self) -> int:
        return sum(1 for item in self.results if item.status == "failed")

    @property
    def output_paths(self) -> list[Path]:
        return [item.output for item in self.results if item.status == "ok" and item.output]


class Processor:
    def __init__(
        self,
        tools: Tools,
        config: RunConfig,
        backend: str,
        registry: ProcessRegistry,
        emit: Emit,
    ) -> None:
        self.tools = tools
        self.config = config
        self.backend = backend
        self.registry = registry
        self.emit = emit

    # -- 小工具 ---------------------------------------------------------

    def _log(self, level: str, message: str, source: Optional[Path] = None) -> None:
        prefix = f"{source.name}：" if source else ""
        self.emit(level, prefix + message)

    def _check_cancel(self) -> None:
        if self.registry.cancelled.is_set():
            raise ProcessingCancelled("任务已取消")

    def output_path_for(self, source: Path, output_dir: Optional[Path]) -> Path:
        base = output_dir if output_dir else source.parent
        return base / f"{source.stem}{self.config.suffix}.mkv"

    # -- 主流程 ---------------------------------------------------------

    def process(
        self,
        source: Path,
        output: Path,
        progress: Progress = noop_progress,
    ) -> FileResult:
        started = time.monotonic()
        self._check_cancel()

        if output.exists() and not self.config.overwrite:
            self._log("warn", f"输出已存在，跳过：{output}", source)
            return FileResult(
                source=source,
                output=output,
                status="skipped",
                message="输出文件已存在",
            )

        if source.resolve() == output.resolve():
            raise ProcessingError("输出路径不能与源文件相同")

        progress(2, "正在分析视频")
        info = probe_media(source, self.tools.ffprobe)
        if info.generated and info.audio_streams >= 2:
            self._log("warn", "这是本工具已经生成的双音轨文件，跳过。", source)
            return FileResult(
                source=source,
                output=output,
                status="skipped",
                message="已经是生成的双音轨文件",
            )
        if not info.has_video:
            raise ProcessingError("源文件中没有视频轨道")
        if not info.has_audio:
            raise ProcessingError("源文件中没有音频轨道")
        if self.backend == "center" and info.audio_channels == 1:
            raise ProcessingError(
                "内置快速消音需要立体声音频，但源文件是单声道。"
                "请安装 Demucs 后使用 --backend demucs。"
            )

        dimension = f"{info.width}x{info.height}" if info.width and info.height else "未知尺寸"
        self._log(
            "info",
            f"时长 {format_duration(info.duration)}，{dimension}，"
            f"音频 {info.audio_codec or '未知'} / {info.audio_channels or '?'} 声道，"
            f"{format_size(info.size_bytes)}",
            source,
        )
        if info.audio_streams > 1:
            self._log(
                "warn",
                f"源文件已有 {info.audio_streams} 条音轨，只把第 1 条作为原唱。",
                source,
            )

        try:
            output.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ProcessingError(f"无法创建输出文件夹：{output.parent}\n{exc}") from exc
        temp_parent = self.config.temp_dir or output.parent
        temp_dir = temp_parent / (
            f".ktv-mv-{os.getpid()}-{threading.get_ident()}-{uuid.uuid4().hex[:8]}"
        )
        try:
            temp_parent.mkdir(parents=True, exist_ok=True)
            temp_dir.mkdir(mode=0o700)
        except OSError as exc:
            raise ProcessingError(
                f"无法创建临时文件夹：{temp_dir}\n{exc}"
            ) from exc
        temp_output: Optional[Path] = None

        try:
            original_wav = temp_dir / "original.wav"
            raw_instrumental = temp_dir / "instrumental_raw.wav"
            instrumental_wav = temp_dir / "instrumental.wav"

            progress(8, "正在提取原唱音频")
            self._extract_audio(source, original_wav)
            self._check_cancel()

            if self.backend == "center":
                progress(14, "正在检查声道结构")
                stereo = analyze_stereo_wav(original_wav)
                if stereo and stereo.difference_ratio < CENTER_DUAL_MONO_RATIO:
                    raise ProcessingError(
                        "检测到这首歌的左右声道几乎完全相同（双声道单声道）。"
                        "快速消音会把伴奏也一起消掉，导致伴唱静音；"
                        "请安装 Demucs 后使用 --backend demucs，"
                        "或在图形界面选择「Demucs 最佳质量」。"
                    )

            if self.backend == "demucs":
                progress(20, "Demucs 分离中（通常几十秒到几分钟，GPU 更快）")
                self._separate_demucs(original_wav, raw_instrumental, temp_dir)
            else:
                progress(20, "正在分离人声")
                self._separate_center(original_wav, raw_instrumental)
            self._check_cancel()

            progress(72, "正在校准伴唱音轨长度")
            self._conform_audio(
                raw_instrumental,
                instrumental_wav,
                info.audio_duration or info.duration,
            )
            self._check_cancel()

            stereo = analyze_stereo_wav(instrumental_wav)
            if stereo and stereo.rms < MIN_INSTRUMENTAL_RMS:
                raise ProcessingError(
                    "生成的伴唱音轨接近静音，已停止封装。"
                    "请改用 Demucs 人声分离后重新处理。"
                )

            progress(82, "正在封装双音轨 MKV")
            temp_output = output.parent / (
                f".{output.stem}.{os.getpid()}.{threading.get_ident()}.part.mkv"
            )
            self._mux(source, instrumental_wav, temp_output, info)
            self._check_cancel()

            progress(96, "正在校验输出文件")
            self._validate_output(source, temp_output, info)
            os.replace(temp_output, output)
            temp_output = None
        except ProcessingCancelled:
            self._log("warn", "已取消", source)
            raise
        finally:
            if temp_output is not None:
                try:
                    temp_output.unlink(missing_ok=True)
                except OSError:
                    pass
            if self.config.keep_temp:
                self._log("info", f"临时文件保留在：{temp_dir}", source)
            else:
                shutil.rmtree(temp_dir, ignore_errors=True)

        elapsed = time.monotonic() - started
        try:
            size = output.stat().st_size
        except OSError:
            size = 0
        self._log(
            "ok",
            f"完成：{output.name}（{format_size(size)}，耗时 {format_duration(elapsed)}）",
            source,
        )
        progress(100, "完成")
        return FileResult(
            source=source,
            output=output,
            status="ok",
            elapsed=elapsed,
            size_bytes=size,
        )

    # -- 各阶段 ---------------------------------------------------------

    def _extract_audio(self, source: Path, output_wav: Path) -> None:
        command = [
            self.tools.ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            source,
            "-map",
            "0:a:0",
            "-vn",
            "-sn",
            "-dn",
            "-ac",
            "2",
            "-ar",
            "44100",
            "-c:a",
            "pcm_s16le",
            output_wav,
        ]
        run_command(
            command,
            registry=self.registry,
            emit=self.emit,
            verbose=self.config.verbose,
        )
        if not output_wav.is_file() or output_wav.stat().st_size == 0:
            raise ProcessingError("提取音频失败，没有得到有效音频数据")

    def _separate_center(self, audio_wav: Path, output_wav: Path) -> None:
        # 左右声道相减可以抵消位于正中央的人声。除以 2 避免削波，
        # 再把同一个反相结果复制到左右声道，避免产生反相立体声。
        audio_filter = (
            "pan=stereo|"
            "c0=0.5*c0-0.5*c1|"
            "c1=0.5*c0-0.5*c1"
        )
        command = [
            self.tools.ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            audio_wav,
            "-af",
            audio_filter,
            "-ar",
            "44100",
            "-ac",
            "2",
            "-c:a",
            "pcm_s16le",
            output_wav,
        ]
        run_command(
            command,
            registry=self.registry,
            emit=self.emit,
            verbose=self.config.verbose,
        )
        self._require_audio(output_wav, "内置消音")

    def _separate_demucs(
        self, audio_wav: Path, output_wav: Path, temp_dir: Path
    ) -> None:
        demucs_out = temp_dir / "demucs"
        command = [
            self.tools.python,
            "-m",
            "demucs",
            "--two-stems=vocals",
            "-n",
            self.config.demucs_model,
            "--out",
            demucs_out,
        ]
        if self.config.demucs_device != "auto":
            command.extend(["-d", self.config.demucs_device])
        command.append(audio_wav)

        self._log(
            "info",
            "正在运行 Demucs。处理一首 3～4 分钟的歌通常需要几十秒到几分钟，"
            "GPU 会明显更快；首次运行还要加载/下载模型。期间进度条会停在分离阶段，"
            "属于正常现象。",
        )
        cache_root = PROJECT_DIR / ".cache"
        try:
            cache_root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ProcessingError(
                f"无法创建 Demucs 模型缓存目录：{cache_root}\n{exc}"
            ) from exc
        env = os.environ.copy()
        env["TORCH_HOME"] = str(cache_root / "torch")

        stop_heartbeat = threading.Event()
        started = time.monotonic()

        def heartbeat() -> None:
            while not stop_heartbeat.wait(15):
                elapsed = int(time.monotonic() - started)
                self._log("info", f"Demucs 仍在处理，已用 {elapsed} 秒，请耐心等待……")

        heartbeat_thread = threading.Thread(target=heartbeat, daemon=True)
        heartbeat_thread.start()
        try:
            run_command(
                command,
                registry=self.registry,
                emit=self.emit,
                verbose=self.config.verbose,
                env=env,
            )
        finally:
            stop_heartbeat.set()
        self._check_cancel()

        candidates = sorted(
            demucs_out.rglob("no_vocals.wav"),
            key=lambda item: item.stat().st_size,
            reverse=True,
        )
        if not candidates:
            raise ProcessingError(
                "Demucs 运行结束，但没有找到伴唱文件 no_vocals.wav。"
                "请用 --verbose 查看详细输出，或改用 --backend center。"
            )
        shutil.copy2(candidates[0], output_wav)
        self._require_audio(output_wav, "Demucs")

    def _require_audio(self, path: Path, stage: str) -> None:
        if not path.is_file() or path.stat().st_size == 0:
            raise ProcessingError(f"{stage}没有生成有效的伴唱音频")

    def _conform_audio(
        self,
        source_wav: Path,
        output_wav: Path,
        target_duration: Optional[float],
    ) -> None:
        """把伴唱精确裁/补到原音频长度，防止封装后慢慢跑偏。"""
        filters = ["asetpts=PTS-STARTPTS"]
        command = [
            self.tools.ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            source_wav,
        ]
        if target_duration and target_duration > 0:
            filters.insert(0, f"atrim=start=0:end={target_duration:.6f}")
            filters.append("apad")
            command.extend(["-af", ",".join(filters), "-t", f"{target_duration:.6f}"])
        else:
            command.extend(["-af", ",".join(filters)])

        command.extend(
            [
                "-ar",
                "44100",
                "-ac",
                "2",
                "-c:a",
                "pcm_s16le",
                output_wav,
            ]
        )
        run_command(
            command,
            registry=self.registry,
            emit=self.emit,
            verbose=self.config.verbose,
        )
        self._require_audio(output_wav, "音轨校准")

    def _mux(
        self,
        source: Path,
        instrumental_wav: Path,
        temp_output: Path,
        info: MediaInfo,
    ) -> None:
        command: list[object] = [
            self.tools.ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            source,
        ]

        # 如果原音频相对视频有起始偏移，让伴唱音轨也保持同样的偏移。
        if info.audio_offset >= 0.02:
            command.extend(["-itsoffset", f"{info.audio_offset:.6f}"])
            self._log(
                "info",
                f"检测到原音频起始偏移 {info.audio_offset:.3f} 秒，已同步伴唱音轨。",
            )

        command.extend(
            [
                "-i",
                instrumental_wav,
                "-map",
                "0:V:0",
                "-map",
                "0:a:0",
                "-map",
                "1:a:0",
                "-map_metadata",
                "0",
                "-map_chapters",
                "0",
                "-c",
                "copy",
                "-c:a:1",
                self.config.instrumental_codec,
            ]
        )
        if self.config.instrumental_codec == "aac":
            command.extend(["-b:a:1", self.config.bitrate])

        command.extend(
            [
                "-metadata:s:a:0",
                "title=原唱",
                "-metadata:s:a:1",
                "title=伴唱",
                "-metadata:s:a:1",
                "language=und",
                "-disposition:a:0",
                "default",
                "-disposition:a:1",
                "0",
                "-metadata",
                f"{GENERATED_TAG}={GENERATED_TAG_VALUE}",
                "-max_muxing_queue_size",
                "4096",
                "-f",
                "matroska",
                temp_output,
            ]
        )
        run_command(
            command,
            registry=self.registry,
            emit=self.emit,
            verbose=self.config.verbose,
        )
        if not temp_output.is_file() or temp_output.stat().st_size == 0:
            raise ProcessingError("封装 MKV 失败，输出文件为空")

    def _validate_output(
        self, source: Path, output: Path, source_info: MediaInfo
    ) -> None:
        info = probe_media(output, self.tools.ffprobe)
        if info.video_streams < 1:
            raise ProcessingError("输出文件缺少视频轨道")
        if info.audio_streams < 2:
            raise ProcessingError("输出文件没有生成两条音轨")

        if source_info.duration and info.duration:
            difference = abs(source_info.duration - info.duration)
            tolerance = max(1.0, source_info.duration * 0.01)
            if difference > tolerance:
                self._log(
                    "warn",
                    f"输出时长与源文件相差 {difference:.2f} 秒，请检查播放是否同步。",
                    source,
                )

        titles = tuple(title for title in info.audio_titles[:2] if title)
        if len(titles) >= 2 and ("原唱" not in titles[0] or "伴唱" not in titles[1]):
            self._log("warn", "音轨名称写入异常，请在播放器中确认两条音轨。", source)
        if len(info.audio_defaults) >= 2 and info.audio_defaults[:2] != (True, False):
            self._log("warn", "默认音轨标记异常，请在播放器中确认默认音轨。", source)
        self._log("info", f"校验通过：1 条视频 + {info.audio_streams} 条音轨", source)


# ---------------------------------------------------------------------------
# 批量处理
# ---------------------------------------------------------------------------


def normalize_suffix(value: str) -> str:
    suffix = (value or "").strip()
    if not suffix:
        return ""
    if any(char in suffix for char in '\\/:*?"<>|'):
        raise ProcessingError("输出后缀不能包含路径字符或 Windows 非法字符")
    if not suffix.startswith("."):
        suffix = "." + suffix
    if suffix in (".", "..") or suffix.endswith((".", " ")):
        raise ProcessingError("输出后缀格式不正确")
    return suffix


def _is_generated(path: Path, suffix: str) -> bool:
    stem = path.stem.lower()
    if any(stem.endswith(item) for item in LEGACY_SUFFIXES):
        return True
    return bool(suffix) and stem.endswith(suffix.lower())


def filter_generated_sources(
    sources: Sequence[Path],
    ffprobe: str,
    emit: Emit = noop_emit,
) -> list[Path]:
    """移除以前生成的 MKV，避免再次处理自己的输出。

    只探测 .mkv，不增加 MP4/FLV 源文件的处理开销。探测失败的文件保留下来，
    交给正式处理阶段给出更完整的错误。
    """
    real_sources: list[Path] = []
    for source in sources:
        if source.suffix.lower() != ".mkv":
            real_sources.append(source)
            continue
        try:
            info = probe_media(source, ffprobe)
        except ProcessingError:
            real_sources.append(source)
            continue
        if info.generated and info.audio_streams >= 2:
            emit("warn", f"跳过本工具生成的双音轨文件：{source.name}")
            continue
        real_sources.append(source)
    return real_sources


def collect_sources(
    inputs: Iterable[str | Path],
    *,
    recursive: bool = True,
    suffix: str = DEFAULT_SUFFIX,
    emit: Emit = noop_emit,
) -> list[Path]:
    found: list[Path] = []
    seen: set[str] = set()

    def add(path: Path, explicit: bool) -> None:
        if path.suffix.lower() not in SUPPORTED_EXTS:
            if explicit:
                emit("warn", f"不支持的格式，已忽略：{path}")
            return
        if _is_generated(path, suffix):
            if explicit:
                emit("warn", f"看起来是已经生成的输出文件，已忽略：{path.name}")
            return
        key = os.path.normcase(os.path.abspath(path))
        if key not in seen:
            seen.add(key)
            found.append(path)

    for raw in inputs:
        path = Path(raw).expanduser()
        if not path.exists():
            emit("warn", f"路径不存在，已忽略：{path}")
            continue
        if path.is_file():
            add(path, True)
            continue
        if path.is_dir():
            iterator = path.rglob("*") if recursive else path.glob("*")
            for child in iterator:
                try:
                    if child.is_file():
                        add(child, False)
                except OSError:
                    continue
            continue
        emit("warn", f"无法识别该路径，已忽略：{path}")

    found.sort(key=lambda item: (str(item.parent).lower(), item.name.lower()))
    return found


def make_output_plan(
    sources: Sequence[Path],
    output_dir: Optional[Path],
    suffix: str,
) -> list[Path]:
    used: set[str] = set()
    source_keys = {
        os.path.normcase(str(source.resolve())) for source in sources
    }
    plan: list[Path] = []

    def next_safe_name(base: Path, preferred_stem: str) -> Path:
        candidate = base / f"{preferred_stem}.mkv"
        counter = 2
        while True:
            key = os.path.normcase(str(candidate.resolve()))
            if key not in used and key not in source_keys:
                return candidate
            candidate = base / f"{preferred_stem} ({counter}).mkv"
            counter += 1

    for source in sources:
        base = output_dir if output_dir else source.parent
        preferred_stem = f"{source.stem}{suffix}"
        candidate = base / f"{preferred_stem}.mkv"
        key = os.path.normcase(str(candidate.resolve()))
        if key in source_keys or key in used:
            # 避免覆盖源文件（例如 MKV 转 MKV），或两个源文件同名。
            candidate = next_safe_name(base, f"{preferred_stem} (双音轨)")
        used.add(os.path.normcase(str(candidate.resolve())))
        plan.append(candidate)
    return plan


def run_batch(
    sources: Sequence[Path],
    output_dir: Optional[Path],
    processor: Processor,
    *,
    emit: Emit = noop_emit,
    progress: Progress = noop_progress,
) -> BatchSummary:
    summary = BatchSummary()
    original_total = len(sources)
    sources = filter_generated_sources(sources, processor.tools.ffprobe, emit)
    summary.prefiltered_count = original_total - len(sources)
    total = len(sources)
    if total == 0:
        emit("warn", "没有需要处理的新视频。")
        return summary

    outputs = make_output_plan(sources, output_dir, processor.config.suffix)
    emit("info", f"共 {total} 个文件，输出格式为 MKV。")
    for source, output in zip(sources, outputs):
        base = output_dir if output_dir else source.parent
        expected = base / f"{source.stem}{processor.config.suffix}.mkv"
        if os.path.normcase(str(output.resolve())) != os.path.normcase(str(expected.resolve())):
            emit(
                "warn",
                f"{source.name}：为避免覆盖源文件或同名冲突，"
                f"输出文件名调整为 {output.name}",
            )

    for index, (source, output) in enumerate(zip(sources, outputs), start=1):
        if processor.registry.cancelled.is_set():
            summary.cancelled = True
            break

        emit("info", f"[{index}/{total}] {source.name}")

        def on_progress(percent: float, stage: str, idx: int = index, name: str = source.name) -> None:
            overall = ((idx - 1) + max(0.0, min(100.0, percent)) / 100.0) / total * 100.0
            progress(overall, f"[{idx}/{total}] {stage} - {name}")

        try:
            result = processor.process(source, output, on_progress)
        except ProcessingCancelled:
            summary.cancelled = True
            summary.results.append(
                FileResult(source=source, output=output, status="cancelled", message="已取消")
            )
            break
        except ProcessingError as exc:
            emit("error", f"{source.name}：{exc}")
            result = FileResult(
                source=source, output=output, status="failed", message=str(exc)
            )
        except Exception as exc:  # 防御意外错误，保证批次继续
            emit("error", f"{source.name}：未预期错误：{exc}")
            result = FileResult(
                source=source, output=output, status="failed", message=str(exc)
            )
        summary.results.append(result)

    completed = len(summary.results)
    if summary.cancelled:
        emit("warn", f"任务已取消，已处理 {completed}/{total} 个文件。")
    else:
        message = (
            f"处理结束：成功 {summary.ok_count}，跳过 {summary.skipped_count}，"
            f"失败 {summary.failed_count}。"
        )
        emit("ok" if summary.failed_count == 0 else "warn", message)
    return summary


# ---------------------------------------------------------------------------
# 图形界面
# ---------------------------------------------------------------------------


def run_gui(initial_inputs: Optional[Sequence[str]] = None) -> int:
    try:
        import tkinter as tk
        from tkinter import filedialog, messagebox, ttk
    except ImportError as exc:
        console_emit("error", f"当前 Python 没有 Tkinter，无法启动图形界面：{exc}")
        return 1

    class App:
        def __init__(self, root: tk.Tk) -> None:
            self.root = root
            self.paths: list[Path] = []
            self.queue: queue.Queue[tuple] = queue.Queue()
            self.worker: Optional[threading.Thread] = None
            self.registry = ProcessRegistry()
            self.last_output: Optional[Path] = None

            self.backend_var = tk.StringVar(value="自动（推荐）")
            self.codec_var = tk.StringVar(value="AAC（兼容性好）")
            self.output_var = tk.StringVar(value="")
            self.overwrite_var = tk.BooleanVar(value=False)
            self.status_var = tk.StringVar(value="等待添加视频")
            self.count_var = tk.StringVar(value="尚未添加文件")
            self.progress_var = tk.DoubleVar(value=0)

            self._setup_window()
            self._build_ui()
            if initial_inputs:
                self._add_paths([Path(item) for item in initial_inputs])

        # -- 界面 -------------------------------------------------------

        def _setup_window(self) -> None:
            self.root.title(f"{APP_NAME}  v{VERSION}")
            self.root.geometry("920x700")
            self.root.minsize(760, 580)
            self.root.configure(background="#f3f5f7")
            try:
                self.root.option_add("*Font", "{Microsoft YaHei UI} 9")
            except Exception:
                pass
            try:
                self.root.iconname(APP_NAME)
            except Exception:
                pass

            style = ttk.Style(self.root)
            try:
                style.theme_use("clam")
            except tk.TclError:
                pass
            style.configure("TFrame", background="#f3f5f7")
            style.configure("Card.TFrame", background="#ffffff")
            style.configure("TLabel", background="#f3f5f7", foreground="#1f2937")
            style.configure("Card.TLabel", background="#ffffff", foreground="#1f2937")
            style.configure(
                "Title.TLabel",
                background="#f3f5f7",
                foreground="#111827",
                font=("Microsoft YaHei UI", 18, "bold"),
            )
            style.configure(
                "Sub.TLabel",
                background="#f3f5f7",
                foreground="#6b7280",
                font=("Microsoft YaHei UI", 9),
            )
            style.configure(
                "Accent.TButton",
                background="#2563eb",
                foreground="#ffffff",
                padding=(18, 9),
                borderwidth=0,
                font=("Microsoft YaHei UI", 10, "bold"),
            )
            style.map(
                "Accent.TButton",
                background=[("active", "#1d4ed8"), ("disabled", "#93c5fd")],
            )
            style.configure("TButton", padding=(10, 6))
            style.configure(
                "Horizontal.TProgressbar",
                troughcolor="#e5e7eb",
                background="#2563eb",
                lightcolor="#2563eb",
                darkcolor="#2563eb",
            )

        def _build_ui(self) -> None:
            outer = ttk.Frame(self.root, padding=22)
            outer.pack(fill="both", expand=True)

            ttk.Label(outer, text=APP_NAME, style="Title.TLabel").pack(anchor="w")
            ttk.Label(
                outer,
                text="批量把 MP4 / MKV / FLV 转成「原唱 + 伴唱」双音轨 MKV，视频画面无损保留。",
                style="Sub.TLabel",
            ).pack(anchor="w", pady=(3, 16))

            source_card = ttk.Frame(outer, style="Card.TFrame", padding=14)
            source_card.pack(fill="both", expand=True)
            header = ttk.Frame(source_card, style="Card.TFrame")
            header.pack(fill="x")
            ttk.Label(header, text="1. 选择视频", style="Card.TLabel").pack(side="left")
            ttk.Label(header, textvariable=self.count_var, style="Card.TLabel", foreground="#6b7280").pack(side="right")

            buttons = ttk.Frame(source_card, style="Card.TFrame")
            buttons.pack(fill="x", pady=(10, 8))
            ttk.Button(buttons, text="添加视频文件", command=self._choose_files).pack(side="left")
            ttk.Button(buttons, text="添加文件夹", command=self._choose_folder).pack(side="left", padx=8)
            ttk.Button(buttons, text="移除选中", command=self._remove_selected).pack(side="left")
            ttk.Button(buttons, text="清空列表", command=self._clear_paths).pack(side="left", padx=8)

            list_frame = ttk.Frame(source_card, style="Card.TFrame")
            list_frame.pack(fill="both", expand=True)
            self.listbox = tk.Listbox(
                list_frame,
                selectmode="extended",
                activestyle="none",
                background="#fbfdff",
                foreground="#1f2937",
                highlightthickness=1,
                highlightbackground="#dbe3ec",
                relief="flat",
                font=("Consolas", 9),
                height=7,
            )
            scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=self.listbox.yview)
            self.listbox.configure(yscrollcommand=scrollbar.set)
            self.listbox.pack(side="left", fill="both", expand=True)
            scrollbar.pack(side="right", fill="y")

            options = ttk.Frame(outer, style="Card.TFrame", padding=14)
            options.pack(fill="x", pady=12)
            ttk.Label(options, text="2. 输出与消音设置", style="Card.TLabel").grid(
                row=0, column=0, columnspan=4, sticky="w", pady=(0, 10)
            )

            ttk.Label(options, text="输出文件夹", style="Card.TLabel").grid(
                row=1, column=0, sticky="w"
            )
            output_entry = ttk.Entry(options, textvariable=self.output_var)
            output_entry.grid(row=1, column=1, sticky="ew", padx=8)
            ttk.Button(options, text="浏览...", command=self._choose_output).grid(
                row=1, column=2, sticky="w"
            )
            ttk.Label(
                options,
                text="默认：原视频旁 output 文件夹；留空则写在原视频旁边",
                style="Card.TLabel",
                foreground="#6b7280",
            ).grid(row=1, column=3, sticky="w", padx=(10, 0))

            ttk.Label(options, text="人声分离", style="Card.TLabel").grid(
                row=2, column=0, sticky="w", pady=(10, 0)
            )
            backend_box = ttk.Combobox(
                options,
                textvariable=self.backend_var,
                values=(
                    "自动（推荐）",
                    "Demucs 最佳质量",
                    "快速消音（无需安装）",
                ),
                state="readonly",
                width=18,
            )
            backend_box.grid(row=2, column=1, sticky="w", padx=8, pady=(10, 0))
            ttk.Label(
                options,
                text="auto = 已安装 Demucs 时用 Demucs，否则用快速消音",
                style="Card.TLabel",
                foreground="#6b7280",
            ).grid(row=2, column=3, sticky="w", padx=(10, 0), pady=(10, 0))

            ttk.Label(options, text="伴唱格式", style="Card.TLabel").grid(
                row=3, column=0, sticky="w", pady=(10, 0)
            )
            codec_box = ttk.Combobox(
                options,
                textvariable=self.codec_var,
                values=("AAC（兼容性好）", "FLAC（无损，文件更大）"),
                state="readonly",
                width=18,
            )
            codec_box.grid(row=3, column=1, sticky="w", padx=8, pady=(10, 0))
            ttk.Checkbutton(
                options,
                text="覆盖已有输出文件",
                variable=self.overwrite_var,
            ).grid(row=3, column=3, sticky="w", padx=(10, 0), pady=(10, 0))
            options.columnconfigure(1, weight=1)

            action = ttk.Frame(outer)
            action.pack(fill="x")
            self.start_button = ttk.Button(
                action, text="开始制作", style="Accent.TButton", command=self._start
            )
            self.start_button.pack(side="left")
            self.cancel_button = ttk.Button(
                action, text="取消", command=self._cancel, state="disabled"
            )
            self.cancel_button.pack(side="left", padx=8)
            self.open_button = ttk.Button(
                action, text="打开输出文件夹", command=self._open_output, state="disabled"
            )
            self.open_button.pack(side="left", padx=8)

            progress_frame = ttk.Frame(outer)
            progress_frame.pack(fill="x", pady=(14, 0))
            self.progress = ttk.Progressbar(
                progress_frame,
                variable=self.progress_var,
                maximum=100,
                mode="determinate",
            )
            self.progress.pack(fill="x")
            ttk.Label(
                outer,
                textvariable=self.status_var,
                style="Sub.TLabel",
            ).pack(anchor="w", pady=(5, 8))

            log_card = ttk.Frame(outer, style="Card.TFrame", padding=10)
            log_card.pack(fill="both", expand=True)
            log_header = ttk.Frame(log_card, style="Card.TFrame")
            log_header.pack(fill="x")
            ttk.Label(log_header, text="运行日志", style="Card.TLabel").pack(side="left")
            ttk.Button(log_header, text="清空日志", command=self._clear_log).pack(side="right")
            self.log_text = tk.Text(
                log_card,
                height=9,
                wrap="word",
                background="#0f172a",
                foreground="#dbeafe",
                insertbackground="#dbeafe",
                relief="flat",
                padx=10,
                pady=8,
                font=("Consolas", 9),
                state="disabled",
            )
            log_scroll = ttk.Scrollbar(log_card, orient="vertical", command=self.log_text.yview)
            self.log_text.configure(yscrollcommand=log_scroll.set)
            self.log_text.pack(side="left", fill="both", expand=True, pady=(8, 0))
            log_scroll.pack(side="right", fill="y", pady=(8, 0))
            self.log_text.tag_configure("error", foreground="#fca5a5")
            self.log_text.tag_configure("warn", foreground="#fcd34d")
            self.log_text.tag_configure("ok", foreground="#86efac")
            self.log_text.tag_configure("info", foreground="#dbeafe")
            self.log_text.tag_configure("detail", foreground="#94a3b8")

            self.root.protocol("WM_DELETE_WINDOW", self._on_close)
            self.root.after(100, self._poll_queue)

        # -- 文件列表 ---------------------------------------------------

        def _add_paths(self, paths: Sequence[Path]) -> None:
            sources = collect_sources(
                paths,
                recursive=True,
                suffix=DEFAULT_SUFFIX,
                emit=lambda level, message: self.queue.put(("log", level, message)),
            )
            existing = {os.path.normcase(os.path.abspath(path)) for path in self.paths}
            added = 0
            for path in sources:
                key = os.path.normcase(os.path.abspath(path))
                if key not in existing:
                    existing.add(key)
                    self.paths.append(path)
                    added += 1
            self._refresh_list()
            if added:
                if not self.output_var.get().strip():
                    self.output_var.set(str(self._suggest_output_dir()))
                self.queue.put(("log", "info", f"已添加 {added} 个视频文件。"))

        def _suggest_output_dir(self) -> Path:
            parents = [str(path.parent) for path in self.paths]
            if not parents:
                return Path.cwd() / "output"
            try:
                common = Path(os.path.commonpath(parents))
            except ValueError:
                common = self.paths[0].parent
            return common / "output"

        def _choose_files(self) -> None:
            files = filedialog.askopenfilenames(
                title="选择 MP4 / MKV / FLV 视频",
                filetypes=[
                    ("视频文件", "*.mp4 *.mkv *.flv *.f4v"),
                    ("所有文件", "*.*"),
                ],
            )
            if files:
                self._add_paths([Path(item) for item in files])

        def _choose_folder(self) -> None:
            folder = filedialog.askdirectory(title="选择包含视频的文件夹")
            if folder:
                self._add_paths([Path(folder)])

        def _refresh_list(self) -> None:
            self.listbox.delete(0, "end")
            for path in self.paths:
                self.listbox.insert("end", str(path))
            if self.paths:
                self.count_var.set(f"已选择 {len(self.paths)} 个文件")
                self.status_var.set("准备就绪")
            else:
                self.count_var.set("尚未添加文件")
                self.status_var.set("等待添加视频")

        def _remove_selected(self) -> None:
            selected = set(self.listbox.curselection())
            if not selected:
                return
            self.paths = [path for index, path in enumerate(self.paths) if index not in selected]
            self._refresh_list()

        def _clear_paths(self) -> None:
            self.paths.clear()
            self._refresh_list()

        def _choose_output(self) -> None:
            folder = filedialog.askdirectory(title="选择输出文件夹")
            if folder:
                self.output_var.set(folder)

        # -- 日志 -------------------------------------------------------

        def _append_log(self, level: str, message: str) -> None:
            self.log_text.configure(state="normal")
            prefix = {
                "error": "错误  ",
                "warn": "注意  ",
                "ok": "完成  ",
            }.get(level, "")
            self.log_text.insert("end", f"{time.strftime('%H:%M:%S')}  {prefix}{message}\n", level)
            self.log_text.see("end")
            line_count = int(self.log_text.index("end-1c").split(".")[0])
            if line_count > 1800:
                self.log_text.delete("1.0", "200.0")
            self.log_text.configure(state="disabled")

        def _clear_log(self) -> None:
            self.log_text.configure(state="normal")
            self.log_text.delete("1.0", "end")
            self.log_text.configure(state="disabled")

        def _set_busy(self, busy: bool) -> None:
            if busy:
                self.start_button.configure(state="disabled")
                self.cancel_button.configure(state="normal")
            else:
                self.start_button.configure(state="normal")
                self.cancel_button.configure(state="disabled")

        def _stop_indeterminate(self) -> None:
            if str(self.progress.cget("mode")) == "indeterminate":
                self.progress.stop()
                self.progress.configure(mode="determinate")
                self.progress_var.set(0)

        def _update_progress(self, percent: float, stage: str) -> None:
            if "Demucs 分离中" in stage:
                if str(self.progress.cget("mode")) != "indeterminate":
                    self.progress.configure(mode="indeterminate")
                    self.progress.start(15)
                self.status_var.set(stage)
                return
            self._stop_indeterminate()
            self.progress_var.set(percent)
            self.status_var.set(stage)

        # -- 运行任务 ---------------------------------------------------

        def _start(self) -> None:
            if self.worker and self.worker.is_alive():
                return
            if not self.paths:
                messagebox.showwarning("还没有视频", "请先添加至少一个 MP4、MKV 或 FLV 文件。")
                return

            output_dir: Optional[Path] = None
            raw_output = self.output_var.get().strip().strip('"')
            if raw_output:
                output_dir = Path(raw_output).expanduser()
                if output_dir.exists() and not output_dir.is_dir():
                    messagebox.showerror("输出路径错误", "输出路径不是文件夹。")
                    return
                try:
                    output_dir.mkdir(parents=True, exist_ok=True)
                except OSError as exc:
                    messagebox.showerror("无法创建输出文件夹", str(exc))
                    return

            suffix = DEFAULT_SUFFIX
            try:
                sources = collect_sources(
                    self.paths,
                    recursive=False,
                    suffix=suffix,
                    emit=lambda level, message: self.queue.put(("log", level, message)),
                )
            except Exception as exc:
                messagebox.showerror("读取文件失败", str(exc))
                return
            if not sources:
                messagebox.showwarning("没有可处理的文件", "列表中没有可处理的 MP4 / MKV / FLV 文件。")
                return

            config = RunConfig(
                backend={
                    "自动（推荐）": "auto",
                    "Demucs 最佳质量": "demucs",
                    "快速消音（无需安装）": "center",
                }.get(self.backend_var.get(), "auto"),
                instrumental_codec={
                    "AAC（兼容性好）": "aac",
                    "FLAC（无损，文件更大）": "flac",
                }.get(self.codec_var.get(), "aac"),
                suffix=suffix,
                overwrite=self.overwrite_var.get(),
                verbose=False,
            )
            self.output_var.set(str(output_dir) if output_dir else "")
            self.registry = ProcessRegistry()
            self._stop_indeterminate()
            self.progress_var.set(0)
            self.status_var.set("正在准备...")
            self.last_output = None
            self._clear_log()
            self._set_busy(True)
            self.open_button.configure(state="disabled")

            self.worker = threading.Thread(
                target=self._worker,
                args=(sources, output_dir, config, self.registry),
                daemon=True,
            )
            self.worker.start()

        def _worker(
            self,
            sources: Sequence[Path],
            output_dir: Optional[Path],
            config: RunConfig,
            registry: ProcessRegistry,
        ) -> None:
            def emit(level: str, message: str) -> None:
                self.queue.put(("log", level, message))

            def progress(percent: float, stage: str) -> None:
                self.queue.put(("progress", percent, stage))

            try:
                tools = check_tools()
                emit("info", f"FFmpeg：{tools.ffmpeg}")
                backend = resolve_backend(config.backend, tools, emit)
                processor = Processor(tools, config, backend, registry, emit)
                summary = run_batch(
                    sources,
                    output_dir,
                    processor,
                    emit=emit,
                    progress=progress,
                )
                self.queue.put(("done", summary))
            except ProcessingCancelled:
                self.queue.put(("done", BatchSummary(cancelled=True)))
            except Exception as exc:
                emit("error", str(exc))
                self.queue.put(("done", BatchSummary(fatal_error=str(exc))))

        def _cancel(self) -> None:
            self.registry.cancel_all()
            self.status_var.set("正在停止，请等待当前进程退出...")
            self.cancel_button.configure(state="disabled")

        def _open_output(self) -> None:
            target = self.last_output
            if target is None or not target.exists():
                target = Path(self.output_var.get().strip()) if self.output_var.get().strip() else None
            if target is None or not target.exists():
                messagebox.showinfo("输出文件夹", "还没有可打开的输出文件夹。")
                return
            try:
                if os.name == "nt":
                    os.startfile(str(target))  # type: ignore[attr-defined]
                elif sys.platform == "darwin":
                    subprocess.Popen(["open", str(target)])
                else:
                    subprocess.Popen(["xdg-open", str(target)])
            except OSError as exc:
                messagebox.showerror("无法打开文件夹", str(exc))

        def _poll_queue(self) -> None:
            try:
                while True:
                    event = self.queue.get_nowait()
                    kind = event[0]
                    if kind == "log":
                        self._append_log(event[1], event[2])
                    elif kind == "progress":
                        self._update_progress(float(event[1]), str(event[2]))
                    elif kind == "done":
                        self._finish(event[1])
            except queue.Empty:
                pass
            self.root.after(100, self._poll_queue)

        def _finish(self, summary: BatchSummary) -> None:
            self._set_busy(False)
            self._stop_indeterminate()
            if summary.output_paths:
                self.last_output = summary.output_paths[0].parent
                self.open_button.configure(state="normal")
            if summary.cancelled:
                self.status_var.set("已取消")
                self.progress_var.set(0)
                return

            if summary.fatal_error:
                self.status_var.set("启动失败，请查看日志")
                messagebox.showerror("处理失败", summary.fatal_error)
                return

            self.progress_var.set(100 if summary.failed_count == 0 else self.progress_var.get())
            self.status_var.set(
                f"处理结束：成功 {summary.ok_count}，跳过 {summary.skipped_count}，"
                f"失败 {summary.failed_count}"
            )
            if summary.failed_count:
                messagebox.showwarning(
                    "部分文件失败",
                    f"成功 {summary.ok_count} 个，失败 {summary.failed_count} 个。\n"
                    "请查看运行日志了解原因。",
                )

        def _on_close(self) -> None:
            if self.worker and self.worker.is_alive():
                if not messagebox.askyesno("确认退出", "任务仍在进行，确定要停止并退出吗？"):
                    return
                self.registry.cancel_all()
            self.root.destroy()

    root = tk.Tk()
    App(root)
    root.mainloop()
    return 0


# ---------------------------------------------------------------------------
# 命令行
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ktv_mv.py",
        description=(
            "把单音轨 MP4/MKV/FLV 制作成双音轨 MKV：音轨 1 原唱，音轨 2 伴唱。"
            "视频和原唱音轨直接复制，不重新编码。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python ktv_mv.py \"D:\\MV\" -o \"D:\\MV\\双音轨\"\n"
            "  python ktv_mv.py a.mp4 b.mkv c.flv --backend demucs --overwrite\n"
            "  python ktv_mv.py . --backend center --no-recursive\n"
            "  python ktv_mv.py --gui\n\n"
            "支持输入：MP4、MKV、FLV、F4V；输出统一为 MKV。\n\n"
            "推荐安装 Demucs 以获得可用的人声分离效果：\n"
            f"  {demucs_install_hint()}\n"
        ),
    )
    parser.add_argument("inputs", nargs="*", help="视频文件或文件夹，可一次指定多个")
    parser.add_argument("-o", "--output-dir", help="输出文件夹；省略时写到原视频旁边")
    parser.add_argument(
        "--backend",
        choices=("auto", "demucs", "center"),
        default="auto",
        help="人声分离方式：auto（默认）、demucs（质量最好）或 center（快速消音）",
    )
    parser.add_argument(
        "--demucs-model",
        default="htdemucs",
        help="Demucs 模型名称，默认 htdemucs",
    )
    parser.add_argument(
        "--demucs-device",
        choices=("auto", "cpu", "cuda"),
        default="auto",
        help="Demucs 运算设备，默认 auto",
    )
    parser.add_argument(
        "--instrumental-codec",
        choices=("aac", "flac"),
        default="aac",
        help="伴唱音轨编码，默认 aac（兼容性好）",
    )
    parser.add_argument(
        "--bitrate",
        default="192k",
        help="伴唱 AAC 码率，默认 192k",
    )
    parser.add_argument(
        "--suffix",
        default=DEFAULT_SUFFIX,
        help="附加在输出文件名后的后缀，默认不加；例如 --suffix _ktv",
    )
    parser.add_argument(
        "--recursive",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="递归扫描子文件夹，默认开启",
    )
    parser.add_argument("--overwrite", action="store_true", help="覆盖已有输出文件")
    parser.add_argument("--keep-temp", action="store_true", help="保留临时文件，便于排查问题")
    parser.add_argument("--temp-dir", help="临时文件目录，默认放在输出目录内")
    parser.add_argument("--ffmpeg", help="ffmpeg.exe 的完整路径")
    parser.add_argument("--ffprobe", help="ffprobe.exe 的完整路径")
    parser.add_argument("--dry-run", action="store_true", help="只显示处理计划，不生成文件")
    parser.add_argument("-v", "--verbose", action="store_true", help="显示外部命令和详细输出")
    parser.add_argument("--gui", action="store_true", help="启动图形界面")
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    return parser


def _validate_config(args: argparse.Namespace) -> RunConfig:
    suffix = normalize_suffix(args.suffix)
    if args.instrumental_codec == "aac":
        if not re.fullmatch(r"\d+[kKmM]", args.bitrate.strip()):
            raise ProcessingError("--bitrate 格式不正确，请使用带单位的数值，例如：192k")
    if not (args.demucs_model or "").strip():
        raise ProcessingError("--demucs-model 不能为空")
    temp_dir = Path(args.temp_dir).expanduser() if args.temp_dir else None
    if temp_dir and temp_dir.exists() and not temp_dir.is_dir():
        raise ProcessingError("--temp-dir 不是文件夹")

    return RunConfig(
        backend=args.backend,
        demucs_model=args.demucs_model.strip(),
        demucs_device=args.demucs_device,
        instrumental_codec=args.instrumental_codec,
        bitrate=args.bitrate.strip(),
        suffix=suffix,
        overwrite=bool(args.overwrite),
        keep_temp=bool(args.keep_temp),
        temp_dir=temp_dir,
        verbose=bool(args.verbose),
    )


def _print_dry_run(
    sources: Sequence[Path],
    outputs: Sequence[Path],
    config: RunConfig,
    backend: str,
    tools: Tools,
) -> int:
    failures = 0
    console_emit("info", f"处理计划（引擎：{backend}，输出后缀：{config.suffix}）：")
    for index, (source, output) in enumerate(zip(sources, outputs), start=1):
        try:
            info = probe_media(source, tools.ffprobe)
            if info.generated and info.audio_streams >= 2:
                action = "跳过（本工具已生成）"
            else:
                action = "跳过（已存在）" if output.exists() and not config.overwrite else "生成"
            print(
                f"  [{index}] {source}\n"
                f"      时长 {format_duration(info.duration)} · "
                f"{info.width}x{info.height} · "
                f"{info.audio_codec}/{info.audio_channels}声道 · "
                f"{format_size(info.size_bytes)}\n"
                f"      -> {output}  [{action}]"
            )
        except ProcessingError as exc:
            failures += 1
            console_emit("error", f"{source.name}：{exc}")
    return 1 if failures else 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    configure_piped_console()
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.gui:
        return run_gui(args.inputs)

    if not args.inputs:
        parser.error("请指定至少一个视频文件或文件夹；也可以使用 --gui 打开图形界面")

    registry = ProcessRegistry()
    try:
        config = _validate_config(args)
        tools = check_tools(args.ffmpeg, args.ffprobe)
        emit = console_emit
        backend = resolve_backend(config.backend, tools, emit)

        sources = collect_sources(
            args.inputs,
            recursive=bool(args.recursive),
            suffix=config.suffix,
            emit=emit,
        )
        if not sources:
            console_emit("warn", "没有找到可处理的 MP4 / MKV / FLV 文件。")
            return 1

        output_dir = Path(args.output_dir).expanduser() if args.output_dir else None
        if output_dir and output_dir.exists() and not output_dir.is_dir():
            raise ProcessingError("--output-dir 指向的不是文件夹")

        if args.dry_run:
            dry_sources = filter_generated_sources(sources, tools.ffprobe, emit)
            if not dry_sources:
                console_emit("warn", "没有需要处理的新视频。")
                return 0
            dry_outputs = make_output_plan(dry_sources, output_dir, config.suffix)
            return _print_dry_run(dry_sources, dry_outputs, config, backend, tools)

        processor = Processor(tools, config, backend, registry, emit)
        summary = run_batch(sources, output_dir, processor, emit=emit)
        if summary.cancelled:
            return 130
        return 0 if summary.failed_count == 0 else 1
    except KeyboardInterrupt:
        registry.cancel_all()
        console_emit("warn", "用户中断，已停止。")
        return 130
    except ProcessingError as exc:
        console_emit("error", str(exc))
        return 1
    except Exception as exc:
        console_emit("error", f"未预期错误：{exc}")
        if getattr(args, "verbose", False):
            import traceback

            traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
