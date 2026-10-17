import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import Transaction
from app.schemas import TransactionOut, WebhookAck, WebhookPayload

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1")


@router.post(
    "/webhooks/transactions",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=WebhookAck,
)
async def receive_transaction_webhook(
    payload: WebhookPayload,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    stmt = (
        insert(Transaction)
        .values(**payload.model_dump())
        .on_conflict_do_nothing(index_elements=[Transaction.transaction_id])
        .returning(Transaction.transaction_id)
    )
    try:
        inserted_id = (await session.execute(stmt)).scalar_one_or_none()
        await session.commit()
    except SQLAlchemyError:
        # Not stored means not accepted: a 503 makes the sender retry later.
        logger.exception("Failed to store webhook for %s", payload.transaction_id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Unable to accept the webhook right now, please retry",
        )

    duplicate = inserted_id is None
    if duplicate:
        logger.info("Duplicate webhook for %s ignored", payload.transaction_id)
    else:
        request.app.state.worker.notify()

    return WebhookAck(transaction_id=payload.transaction_id, duplicate=duplicate)


@router.get("/transactions/{transaction_id}", response_model=list[TransactionOut])
async def get_transaction(
    transaction_id: str,
    session: AsyncSession = Depends(get_session),
):
    transaction = await session.get(Transaction, transaction_id)
    if transaction is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Transaction not found")
    return [transaction]
