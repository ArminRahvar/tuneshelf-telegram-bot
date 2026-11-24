import tempfile
import unittest
from pathlib import Path

from app.database.download_repository import DownloadRepository
from app.database.migrations import backfill_legacy_library, upgrade_schema
from app.database.engine import create_database_engine


class DownloadJobRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.database_url = (
            "sqlite:///"
            + str(Path(self.directory.name) / "downloads.sqlite3")
        )
        upgrade_schema(self.database_url)
        backfill_legacy_library(self.database_url)
        self.repository = DownloadRepository(self.database_url)

    def tearDown(self) -> None:
        self.repository.close()
        self.directory.cleanup()

    def test_enqueue_claim_cancel_and_retry(self) -> None:
        job_id = self.repository.enqueue(42, "https://example.com/audio")
        self.assertIsNotNone(job_id)
        self.assertIsNone(
            self.repository.enqueue(42, "https://example.com/audio")
        )

        claimed = self.repository.claim_next()
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed["id"], job_id)
        self.assertEqual(claimed["status"], "processing")
        self.assertEqual(claimed["attempt_count"], 1)

        self.assertTrue(self.repository.cancel(42, int(job_id)))
        cancelled = self.repository.get(42, int(job_id))
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertTrue(self.repository.retry(42, int(job_id)))
        retried = self.repository.get(42, int(job_id))
        self.assertEqual(retried["status"], "pending")
        self.assertIsNone(retried["started_at"])

    def test_batch_and_owned_status_queries(self) -> None:
        ids = self.repository.enqueue_batch(
            42,
            [
                "https://example.com/one",
                "https://example.com/two",
            ],
        )
        self.assertEqual(len(ids), 2)
        self.assertEqual(len(self.repository.list_for_user(42)), 2)
        self.assertEqual(self.repository.list_for_user(99), [])

        self.assertTrue(self.repository.set_status(ids[0], "converting", progress=75))
        job = self.repository.get(42, ids[0])
        self.assertEqual(job["status"], "converting")
        self.assertEqual(job["progress"], 75)

    def test_failed_job_can_be_retried(self) -> None:
        job_id = self.repository.enqueue(42, "https://example.com/failure")
        self.repository.claim_next()
        self.assertTrue(
            self.repository.set_status(
                int(job_id),
                "failed",
                error="network unavailable",
            )
        )
        failed = self.repository.get(42, int(job_id))
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["error"], "network unavailable")
        self.assertTrue(self.repository.retry(42, int(job_id)))


if __name__ == "__main__":
    unittest.main()
