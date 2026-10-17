import asyncio
import logging
from contextlib import suppress

from sqlalchemy import text

from app.db import WorkerSessionLocal

logger = logging.getLogger(__name__)

CLAIM_BATCH = text("""
    UPDATE transactions
    SET locked_until = now() + make_interval(secs => :lease_seconds),
        attempts = attempts + 1
    WHERE transaction_id IN (
        SELECT transaction_id
        FROM transactions
        WHERE status = 'PROCESSING'
          AND (locked_until IS NULL OR locked_until < now())
        ORDER BY created_at
        LIMIT :batch_size
        FOR UPDATE SKIP LOCKED
    )
    RETURNING transaction_id, attempts
""")

MARK_PROCESSED = text("""
    UPDATE transactions
    SET status = 'PROCESSED', processed_at = now(), locked_until = NULL, last_error = NULL
    WHERE transaction_id = :transaction_id AND status = 'PROCESSING'
""")

RECORD_FAILURE = text("""
    UPDATE transactions
    SET status = CASE WHEN attempts >= :max_attempts THEN 'FAILED' ELSE status END,
        locked_until = CASE
            WHEN attempts >= :max_attempts THEN NULL
            ELSE now() + make_interval(secs => :retry_in)
        END,
        last_error = :error
    WHERE transaction_id = :transaction_id AND status = 'PROCESSING'
    RETURNING status
""")

RELEASE_LEASES = text("""
    UPDATE transactions
    SET locked_until = NULL, attempts = attempts - 1
    WHERE transaction_id = ANY(:transaction_ids) AND status = 'PROCESSING'
""")


class TransactionWorker:
    def __init__(
        self,
        *,
        processing_delay: float,
        concurrency: int,
        idle_poll_seconds: float,
        max_attempts: int,
        claim_batch_size: int = 100,
    ):
        self.processing_delay = processing_delay
        self.idle_poll_seconds = idle_poll_seconds
        self.max_attempts = max_attempts
        self.concurrency = concurrency
        self.claim_batch_size = claim_batch_size
        # Long enough to cover the simulated call plus slow DB round trips.
        self.lease_seconds = processing_delay + 90

        self._wake = asyncio.Event()
        self._slot_freed = asyncio.Event()
        self._in_flight: dict[str, asyncio.Task] = {}
        self._runner: asyncio.Task | None = None

    def start(self) -> None:
        self._runner = asyncio.create_task(self._run(), name="transaction-worker")

    def notify(self) -> None:
        self._wake.set()

    async def stop(self) -> None:
        if self._runner:
            self._runner.cancel()
            with suppress(asyncio.CancelledError):
                await self._runner

        interrupted = list(self._in_flight)
        for task in self._in_flight.values():
            task.cancel()
        await asyncio.gather(*self._in_flight.values(), return_exceptions=True)

        if interrupted:
            try:
                async with WorkerSessionLocal() as session:
                    await session.execute(RELEASE_LEASES, {"transaction_ids": interrupted})
                    await session.commit()
                logger.info("Released %d in-flight transactions on shutdown", len(interrupted))
            except Exception:
                logger.exception("Could not release leases; they will expire on their own")

    async def _run(self) -> None:
        while True:
            free_slots = self.concurrency - len(self._in_flight)
            if free_slots <= 0:
                self._slot_freed.clear()
                await self._slot_freed.wait()
                continue

            # Clear before looking, so a webhook arriving mid-claim still wakes us.
            self._wake.clear()
            try:
                claimed = await self._claim_batch(min(free_slots, self.claim_batch_size))
            except Exception:
                logger.exception("Worker could not reach the database, retrying in 5s")
                await asyncio.sleep(5)
                continue

            if not claimed:
                with suppress(TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), timeout=self.idle_poll_seconds)
                continue

            for transaction_id, attempt in claimed:
                task = asyncio.create_task(self._process(transaction_id, attempt))
                self._in_flight[transaction_id] = task
                task.add_done_callback(lambda _, tid=transaction_id: self._done(tid))

    def _done(self, transaction_id: str) -> None:
        self._in_flight.pop(transaction_id, None)
        self._slot_freed.set()

    async def _claim_batch(self, batch_size: int) -> list[tuple[str, int]]:
        async with WorkerSessionLocal() as session:
            rows = (
                await session.execute(
                    CLAIM_BATCH,
                    {"lease_seconds": self.lease_seconds, "batch_size": batch_size},
                )
            ).all()
            await session.commit()
        return [(row.transaction_id, row.attempts) for row in rows]

    async def _process(self, transaction_id: str, attempt: int) -> None:
        logger.info("Processing %s (attempt %d)", transaction_id, attempt)
        try:
            await self._call_external_service(transaction_id)
            async with WorkerSessionLocal() as session:
                await session.execute(MARK_PROCESSED, {"transaction_id": transaction_id})
                await session.commit()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Processing failed for %s", transaction_id)
            await self._record_failure(transaction_id, attempt, exc)
            return
        logger.info("Processed %s", transaction_id)

    async def _call_external_service(self, transaction_id: str) -> None:
        await asyncio.sleep(self.processing_delay)

    async def _record_failure(self, transaction_id: str, attempt: int, exc: Exception) -> None:
        retry_in = min(300, 5 * 2 ** (attempt - 1))
        try:
            async with WorkerSessionLocal() as session:
                new_status = (
                    await session.execute(
                        RECORD_FAILURE,
                        {
                            "transaction_id": transaction_id,
                            "max_attempts": self.max_attempts,
                            "retry_in": retry_in,
                            "error": f"{type(exc).__name__}: {exc}"[:500],
                        },
                    )
                ).scalar_one_or_none()
                await session.commit()
        except Exception:
            logger.exception("Could not record failure for %s; lease expiry will retry it", transaction_id)
            return

        if new_status == "FAILED":
            logger.error("Giving up on %s after %d attempts", transaction_id, attempt)
        else:
            asyncio.get_running_loop().call_later(retry_in + 1, self.notify)
