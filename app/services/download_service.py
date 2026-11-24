"""Application service for enqueueing and processing download jobs."""

from pathlib import Path
from tempfile import TemporaryDirectory
from collections.abc import Callable

from app.database.download_repository import DownloadRepository
from app.integrations.downloader import (
    AudioDownloader,
    AudioProcessor,
)


class DownloadService:
    def __init__(
        self,
        repository: DownloadRepository,
        downloader: AudioDownloader,
        processor: AudioProcessor,
    ) -> None:
        self.repository = repository
        self.downloader = downloader
        self.processor = processor

    def enqueue(
        self,
        user_id: int,
        source_url: str,
        *,
        track_id: int | None = None,
        batch_id: str | None = None,
    ) -> int | None:
        return self.repository.enqueue(
            user_id,
            source_url,
            track_id=track_id,
            batch_id=batch_id,
        )

    def cancel(self, user_id: int, job_id: int) -> bool:
        return self.repository.cancel(user_id, job_id)

    def retry(self, user_id: int, job_id: int) -> bool:
        return self.repository.retry(user_id, job_id)

    def process_next(
        self,
        consume: Callable[[Path, dict], None] | None = None,
    ) -> bool:
        job = self.repository.claim_next()
        if job is None:
            return False
        job_id = int(job["id"])

        with TemporaryDirectory(prefix="tuneshelf-download-") as temporary:
            output_dir = Path(temporary)
            try:
                downloaded = self.downloader.download(
                    str(job["source_url"]),
                    output_dir,
                    lambda progress: self.repository.set_status(
                        job_id,
                        "processing",
                        progress=min(progress, 90),
                    ),
                )
                self.repository.set_status(
                    job_id,
                    "converting",
                    progress=90,
                )
                converted = self.processor.convert(
                    downloaded,
                    output_dir / "processed.mp3",
                    format="mp3",
                )
                if consume is not None:
                    self.repository.set_status(
                        job_id,
                        "uploading",
                        progress=95,
                    )
                    consume(converted, job)
                self.repository.set_status(
                    job_id,
                    "completed",
                    progress=100,
                )
                return True
            except Exception as exc:
                self.repository.set_status(
                    job_id,
                    "failed",
                    error=f"{type(exc).__name__}: {exc}",
                )
                return False
