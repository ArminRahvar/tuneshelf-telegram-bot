"""Interfaces and subprocess adapters for downloading authorized media."""

from collections.abc import Callable
from pathlib import Path
from typing import Protocol
import shutil
import subprocess


class AudioDownloader(Protocol):
    def download(self, url: str, output_dir: Path, progress: Callable[[int], None]) -> Path:
        ...


class AudioProcessor(Protocol):
    def convert(
        self,
        input_path: Path,
        output_path: Path,
        *,
        format: str = "mp3",
    ) -> Path:
        ...


class YtDlpDownloader:
    def download(self, url: str, output_dir: Path, progress: Callable[[int], None]) -> Path:
        try:
            import yt_dlp
        except ImportError as exc:
            raise RuntimeError("yt-dlp is not installed") from exc

        output_dir.mkdir(parents=True, exist_ok=True)

        def report_status(update: dict) -> None:
            downloaded = update.get("downloaded_bytes")
            total = update.get("total_bytes") or update.get("total_bytes_estimate")
            if downloaded is not None and total:
                progress(max(0, min(100, int(downloaded * 100 / total))))

        options = {
            "format": "bestaudio/best",
            "outtmpl": str(output_dir / "%(title)s [%(id)s].%(ext)s"),
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "progress_hooks": [report_status],
        }
        with yt_dlp.YoutubeDL(options) as client:
            result = client.extract_info(url, download=True)
        requested = (result or {}).get("requested_downloads") or []
        if requested and requested[0].get("filepath"):
            return Path(requested[0]["filepath"])
        prepared = client.prepare_filename(result)
        return Path(prepared)


class FFmpegProcessor:
    def convert(
        self,
        input_path: Path,
        output_path: Path,
        *,
        format: str = "mp3",
    ) -> Path:
        executable = shutil.which("ffmpeg")
        if executable is None:
            raise RuntimeError("FFmpeg is not installed")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [
                executable,
                "-y",
                "-v",
                "error",
                "-i",
                str(input_path),
                "-vn",
                "-codec:a",
                "libmp3lame" if format.lower() == "mp3" else "aac",
                str(output_path),
            ],
            check=True,
            capture_output=True,
        )
        return output_path
