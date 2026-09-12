"""API-only entrypoint for our MoneyPrinterTurbo video engine.

This wrapper intentionally keeps the upstream generation pipeline as the first
choice. It applies production-safe runtime overrides and, only when MoviePy
fails to produce an intermediate/final MP4 in the cloud container, falls back
to the system FFmpeg binary so a task does not remain stuck after a BrokenPipe.
"""

import os
import shutil
import signal
import subprocess
from pathlib import Path
from typing import Any

import uvicorn
from loguru import logger

from app.config import config


def _env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _ensure_demo_material() -> None:
    """Create one tiny local MP4 for a real zero-key smoke test."""
    if not _env_flag("MPT_CREATE_DEMO_MATERIAL", True):
        return

    target_dir = Path(config.root_dir) / "storage" / "local_videos"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / "mpt-demo-vertical.mp4"
    if target.is_file() and target.stat().st_size > 10_000:
        return

    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=1080x1920:rate=30",
        "-t",
        "8",
        "-an",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-crf",
        "32",
        "-threads",
        "1",
        "-pix_fmt",
        "yuv420p",
        str(target),
    ]
    try:
        subprocess.run(cmd, check=True, timeout=90)
        logger.info("created built-in demo material: {}", target.name)
    except Exception as exc:  # demo material must never block service startup
        logger.warning("could not create demo material: {}", exc)


def _is_valid_mp4(path_value: str | os.PathLike[str] | None) -> bool:
    if not path_value:
        return False
    try:
        path = Path(path_value)
        return path.is_file() and path.stat().st_size > 1_000
    except OSError:
        return False


def _probe_duration(path_value: str) -> float:
    ffprobe = shutil.which("ffprobe") or "ffprobe"
    result = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            path_value,
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "ffprobe failed").strip())
    return max(0.1, float(result.stdout.strip()))


def _run_ffmpeg(command: list[str], *, label: str, timeout: int = 180) -> None:
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        if result.returncode < 0:
            try:
                exit_reason = signal.Signals(-result.returncode).name
            except ValueError:
                exit_reason = f"signal {-result.returncode}"
        else:
            exit_reason = f"exit {result.returncode}"
        command_text = " ".join(command)
        if not detail:
            detail = "ffmpeg produced no stderr/stdout"
        logger.error(
            "{} failed ({}): {} | command={}",
            label,
            exit_reason,
            detail[-4000:],
            command_text,
        )
        raise RuntimeError(f"{label}: {exit_reason}: {detail[-4000:]}")


def _arg(args: tuple[Any, ...], kwargs: dict[str, Any], name: str, index: int, default=None):
    if name in kwargs:
        return kwargs[name]
    if len(args) > index:
        return args[index]
    return default


def _ffmpeg_combine_fallback(*args, **kwargs) -> str:
    """Low-memory cloud fallback for combine_videos.

    The upstream MoviePy path remains the first choice. If it fails, prefer an
    H.264 stream-copy loop, which avoids allocating a second 1080x1920 encoder
    and is enough to keep the real TTS/subtitle/video pipeline testable. Only if
    stream-copy cannot produce a usable file do we try a one-thread ultrafast
    re-encode as a last resort.
    """
    from app.models.schema import VideoAspect

    output_file = str(_arg(args, kwargs, "combined_video_path", 0, ""))
    video_paths = list(_arg(args, kwargs, "video_paths", 1, []) or [])
    audio_file = str(_arg(args, kwargs, "audio_file", 2, ""))
    video_aspect = _arg(args, kwargs, "video_aspect", 3, "9:16")
    clip_speed = float(_arg(args, kwargs, "clip_speed", 8, 1.0) or 1.0)
    fit_mode = _arg(args, kwargs, "video_fit_mode", 9, "cover")

    source = next((str(item) for item in video_paths if item and Path(item).is_file()), None)
    if not source:
        raise RuntimeError("cloud ffmpeg fallback: no readable source video")
    if not audio_file or not Path(audio_file).is_file():
        raise RuntimeError("cloud ffmpeg fallback: narration audio is missing")

    duration = _probe_duration(audio_file) + 0.12
    ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
    Path(output_file).parent.mkdir(parents=True, exist_ok=True)

    # Fast/reliable path: keep the source H.264 stream intact and only loop it.
    # This is especially important on small cloud instances where re-encoding a
    # 1080x1920 frame can be killed before FFmpeg has time to emit diagnostics.
    if abs(clip_speed - 1.0) < 1e-6:
        copy_command = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-stream_loop",
            "-1",
            "-i",
            source,
            "-t",
            f"{duration:.3f}",
            "-an",
            "-map",
            "0:v:0",
            "-c:v",
            "copy",
            "-movflags",
            "+faststart",
            output_file,
        ]
        try:
            _run_ffmpeg(copy_command, label="cloud combine stream-copy fallback")
            if _is_valid_mp4(output_file):
                logger.success("cloud stream-copy fallback created combined video: {}", output_file)
                return output_file
        except Exception as exc:
            logger.warning("stream-copy combine failed; trying low-memory encode: {}", exc)

    # Last-resort low-memory encode. One x264 thread + ultrafast greatly lowers
    # transient memory versus MoviePy/default x264 while preserving requested canvas.
    width, height = VideoAspect(video_aspect).to_resolution()
    fit_value = getattr(fit_mode, "value", fit_mode) or "cover"
    speed = min(4.0, max(0.25, clip_speed))
    if str(fit_value) == "contain":
        video_filter = (
            f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black"
        )
    else:
        video_filter = (
            f"scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height}"
        )
    if speed != 1.0:
        video_filter += f",setpts=PTS/{speed:.6f}"

    encode_command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-stream_loop",
        "-1",
        "-i",
        source,
        "-t",
        f"{duration:.3f}",
        "-an",
        "-vf",
        video_filter,
        "-r",
        "30",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-crf",
        "30",
        "-threads",
        "1",
        "-x264-params",
        "threads=1:ref=1:bframes=0",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        output_file,
    ]
    _run_ffmpeg(encode_command, label="cloud combine low-memory fallback")
    if not _is_valid_mp4(output_file):
        raise RuntimeError("cloud combine fallback returned no usable MP4")
    logger.success("cloud low-memory fallback created combined video: {}", output_file)
    return output_file


def _escape_filter_path(path_value: str) -> str:
    return (
        str(Path(path_value).resolve())
        .replace("\\", "\\\\")
        .replace(":", "\\:")
        .replace("'", "\\'")
    )


def _ffmpeg_final_fallback(*args, **kwargs) -> bool:
    """Low-memory final mux fallback.

    Upstream still gets the first chance to render subtitles and all effects. If
    that fails, the emergency path stream-copies the already-combined H.264 video
    and encodes only the narration audio. The SRT has already been generated and
    validated earlier in the pipeline; skipping burn-in in this emergency path
    is preferable to failing the whole quick test on a small cloud instance.
    """
    video_path = str(_arg(args, kwargs, "video_path", 0, ""))
    audio_path = str(_arg(args, kwargs, "audio_path", 1, ""))
    output_file = str(_arg(args, kwargs, "output_file", 3, ""))

    if not _is_valid_mp4(video_path):
        raise RuntimeError("cloud final fallback: combined video is missing")
    if not audio_path or not Path(audio_path).is_file():
        raise RuntimeError("cloud final fallback: narration audio is missing")

    ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
    Path(output_file).parent.mkdir(parents=True, exist_ok=True)
    copy_mux = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        video_path,
        "-i",
        audio_path,
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        "-b:a",
        "160k",
        "-shortest",
        "-movflags",
        "+faststart",
        output_file,
    ]
    try:
        _run_ffmpeg(copy_mux, label="cloud final stream-copy fallback")
        if _is_valid_mp4(output_file):
            logger.success("cloud stream-copy fallback created final video: {}", output_file)
            return True
    except Exception as exc:
        logger.warning("final stream-copy failed; trying one-thread encode: {}", exc)

    low_memory_mux = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        video_path,
        "-i",
        audio_path,
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-crf",
        "30",
        "-threads",
        "1",
        "-x264-params",
        "threads=1:ref=1:bframes=0",
        "-c:a",
        "aac",
        "-b:a",
        "160k",
        "-shortest",
        "-movflags",
        "+faststart",
        output_file,
    ]
    _run_ffmpeg(low_memory_mux, label="cloud final low-memory fallback")
    if not _is_valid_mp4(output_file):
        raise RuntimeError("cloud final fallback returned no usable MP4")
    logger.success("cloud low-memory fallback created final video: {}", output_file)
    return True


def _install_cloud_render_fallbacks() -> None:
    """Patch only the cloud runtime, leaving upstream source files untouched."""
    if not _env_flag("MPT_CLOUD_FFMPEG_FALLBACK", True):
        return

    from app.services import video as video_service

    original_combine = video_service.combine_videos
    original_generate = video_service.generate_video

    def combine_wrapper(*args, **kwargs):
        output_file = str(_arg(args, kwargs, "combined_video_path", 0, ""))
        try:
            result = original_combine(*args, **kwargs)
            if _is_valid_mp4(output_file):
                return result
            logger.warning(
                "upstream combine returned without a usable MP4; using system FFmpeg fallback"
            )
        except Exception as exc:
            logger.warning("upstream combine failed; using system FFmpeg fallback: {}", exc)
        return _ffmpeg_combine_fallback(*args, **kwargs)

    def generate_wrapper(*args, **kwargs):
        output_file = str(_arg(args, kwargs, "output_file", 3, ""))
        try:
            result = original_generate(*args, **kwargs)
            if _is_valid_mp4(output_file):
                return result
            logger.warning(
                "upstream final render returned without a usable MP4; using system FFmpeg fallback"
            )
        except Exception as exc:
            logger.warning("upstream final render failed; using system FFmpeg fallback: {}", exc)
        return _ffmpeg_final_fallback(*args, **kwargs)

    video_service.combine_videos = combine_wrapper
    video_service.generate_video = generate_wrapper
    logger.info("cloud-safe FFmpeg render fallbacks installed")


def configure_runtime() -> None:
    """Apply engine-only settings without writing secrets into config.toml."""
    api_key = os.getenv("MPT_API_KEY", "").strip()
    allow_insecure = _env_flag("MPT_ALLOW_INSECURE", False)

    if not api_key and not allow_insecure:
        raise RuntimeError(
            "MPT_API_KEY is required for engine mode. "
            "Set MPT_ALLOW_INSECURE=1 only for trusted local development."
        )

    if api_key:
        config.app["api_key"] = api_key

    system_ffmpeg = shutil.which("ffmpeg")
    if system_ffmpeg:
        os.environ["IMAGEIO_FFMPEG_EXE"] = system_ffmpeg
        os.environ["FFMPEG_BINARY"] = system_ffmpeg
        config.app["ffmpeg_path"] = system_ffmpeg
        logger.info("MoviePy FFmpeg pinned to {}", system_ffmpeg)

    config.listen_host = os.getenv("MPT_LISTEN_HOST", "0.0.0.0").strip() or "0.0.0.0"

    raw_port = os.getenv("MPT_LISTEN_PORT", "8080").strip()
    try:
        listen_port = int(raw_port)
    except ValueError as exc:
        raise RuntimeError("MPT_LISTEN_PORT must be an integer") from exc
    if not 1 <= listen_port <= 65535:
        raise RuntimeError("MPT_LISTEN_PORT must be between 1 and 65535")
    config.listen_port = listen_port


if __name__ == "__main__":
    configure_runtime()
    _ensure_demo_material()
    _install_cloud_render_fallbacks()
    logger.info(
        "start API-only video engine, host={}, port={}, auth={}",
        config.listen_host,
        config.listen_port,
        "enabled" if config.app.get("api_key") else "disabled",
    )
    uvicorn.run(
        app="app.asgi:app",
        host=config.listen_host,
        port=config.listen_port,
        reload=False,
        log_level="warning",
    )
