"""API-only entrypoint for our MoneyPrinterTurbo video engine.

This wrapper intentionally keeps the upstream generation pipeline as the first
choice. It applies production-safe runtime overrides and, only when MoviePy
fails to produce an intermediate/final MP4 in the cloud container, falls back
to the system FFmpeg binary so a task does not remain stuck after a BrokenPipe.
"""

import os
import shutil
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
        detail = (result.stderr or result.stdout or "ffmpeg failed").strip()
        # Keep the useful tail without dumping megabytes of FFmpeg diagnostics.
        raise RuntimeError(f"{label}: {detail[-4000:]}")


def _arg(args: tuple[Any, ...], kwargs: dict[str, Any], name: str, index: int, default=None):
    if name in kwargs:
        return kwargs[name]
    if len(args) > index:
        return args[index]
    return default


def _ffmpeg_combine_fallback(*args, **kwargs) -> str:
    """Cloud-safe fallback for combine_videos when MoviePy pipe writing fails.

    This path is only reached after the upstream combine function failed to
    create its output. It deliberately favors reliability over transitions:
    it loops the first valid source to narration length, preserving aspect and
    playback speed. Normal successful upstream tasks are completely unchanged.
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

    Path(output_file).parent.mkdir(parents=True, exist_ok=True)
    command = [
        shutil.which("ffmpeg") or "ffmpeg",
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
        "veryfast",
        "-crf",
        "26",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        output_file,
    ]
    _run_ffmpeg(command, label="cloud combine fallback")
    if not _is_valid_mp4(output_file):
        raise RuntimeError("cloud combine fallback returned no usable MP4")
    logger.success("cloud FFmpeg fallback created combined video: {}", output_file)
    return output_file


def _escape_filter_path(path_value: str) -> str:
    # FFmpeg filter parser treats backslash, colon and single quote specially.
    return (
        str(Path(path_value).resolve())
        .replace("\\", "\\\\")
        .replace(":", "\\:")
        .replace("'", "\\'")
    )


def _ffmpeg_final_fallback(*args, **kwargs) -> bool:
    """Fallback final mux/burn-in using system FFmpeg.

    It retains the already-generated narration and SRT from MoneyPrinterTurbo.
    If libass subtitle burn-in is unavailable, it retries without burn-in so a
    real narrated MP4 is still returned rather than leaving the task at 75%.
    """
    video_path = str(_arg(args, kwargs, "video_path", 0, ""))
    audio_path = str(_arg(args, kwargs, "audio_path", 1, ""))
    subtitle_path = str(_arg(args, kwargs, "subtitle_path", 2, "") or "")
    output_file = str(_arg(args, kwargs, "output_file", 3, ""))
    params = _arg(args, kwargs, "params", 4, None)

    if not _is_valid_mp4(video_path):
        raise RuntimeError("cloud final fallback: combined video is missing")
    if not audio_path or not Path(audio_path).is_file():
        raise RuntimeError("cloud final fallback: narration audio is missing")

    base = [
        shutil.which("ffmpeg") or "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        video_path,
        "-i",
        audio_path,
    ]
    tail = [
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "24",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-shortest",
        "-movflags",
        "+faststart",
        output_file,
    ]

    subtitle_enabled = bool(getattr(params, "subtitle_enabled", False))
    can_burn_subtitle = subtitle_enabled and subtitle_path and Path(subtitle_path).is_file()
    if can_burn_subtitle:
        fonts_dir = Path(config.root_dir) / "resource" / "fonts"
        subtitle_filter = (
            f"subtitles='{_escape_filter_path(subtitle_path)}':"
            f"fontsdir='{_escape_filter_path(str(fonts_dir))}':"
            "force_style='FontSize=22,Outline=1,Shadow=0,Alignment=2,MarginV=55'"
        )
        try:
            _run_ffmpeg(base + ["-vf", subtitle_filter] + tail, label="cloud final fallback")
        except Exception as exc:
            logger.warning("subtitle burn-in fallback failed; retrying narrated MP4: {}", exc)
            _run_ffmpeg(base + tail, label="cloud final fallback without subtitle")
    else:
        _run_ffmpeg(base + tail, label="cloud final fallback")

    if not _is_valid_mp4(output_file):
        raise RuntimeError("cloud final fallback returned no usable MP4")
    logger.success("cloud FFmpeg fallback created final video: {}", output_file)
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

    # Pin MoviePy to the Debian FFmpeg installed in the container.
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
