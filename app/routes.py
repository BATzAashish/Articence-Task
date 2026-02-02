import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy.exc import IntegrityError

from app.models import Call, CallPacket, CallState, get_db_session
from app.schemas import PacketPayload, PacketResponse, CallStatusResponse
from app.background_worker import trigger_ai_processing

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/call", tags=["calls"])


@router.post("/stream/{call_id}", status_code=status.HTTP_202_ACCEPTED, response_model=PacketResponse)
async def ingest_packet(
    call_id: str,
    payload: PacketPayload,
    db: AsyncSession = Depends(get_db_session)
) -> PacketResponse:
    """
    Ingest audio metadata packet
    
    Critical requirements:
    - Response time < 50ms
    - Never block on missing packets
    - Handle race conditions with row locking
    - Validate sequence ordering
    - Return 202 Accepted immediately
    """
    try:
        # Begin transaction with row-level locking
        async with db.begin():
            # Try to get existing call with lock, or create if doesn't exist
            # This handles race conditions during creation
            stmt = select(Call).where(Call.call_id == call_id).with_for_update(skip_locked=False)
            result = await db.execute(stmt)
            call = result.scalar_one_or_none()
            
            # Create call if it doesn't exist
            if call is None:
                try:
                    call = Call(
                        call_id=call_id,
                        state=CallState.IN_PROGRESS,
                        last_sequence=-1
                    )
                    db.add(call)
                    await db.flush()  # Get the call into the session
                except Exception as e:
                    # Another transaction created it, rollback and retry with lock
                    await db.rollback()
                    stmt = select(Call).where(Call.call_id == call_id).with_for_update()
                    result = await db.execute(stmt)
                    call = result.scalar_one_or_none()
                    if call is None:
                        raise e  # Something went wrong
            
            # Validate sequence ordering
            expected_sequence = call.last_sequence + 1
            if payload.sequence != expected_sequence:
                # Log warning but DO NOT BLOCK
                logger.warning(
                    f"Sequence mismatch for call_id={call_id}: "
                    f"expected={expected_sequence}, received={payload.sequence}"
                )
            
            # Check if packet already exists (idempotency)
            check_stmt = select(CallPacket).where(
                CallPacket.call_id == call_id,
                CallPacket.sequence == payload.sequence
            )
            existing_packet = (await db.execute(check_stmt)).scalar_one_or_none()
            
            if existing_packet is None:
                # Insert new packet
                packet = CallPacket(
                    call_id=call_id,
                    sequence=payload.sequence,
                    data=payload.data,
                    timestamp=payload.timestamp
                )
                db.add(packet)
                
                # Update last sequence only if this is newer
                if payload.sequence > call.last_sequence:
                    call.last_sequence = payload.sequence
                    call.updated_at = datetime.utcnow()
            else:
                # Duplicate packet - silently skip (idempotency)
                logger.debug(f"Duplicate packet for call_id={call_id}, sequence={payload.sequence} - silently accepted")
            
            # Commit happens automatically at end of context manager
        
        # Trigger background AI processing (non-blocking)
        # This happens AFTER commit, so ingestion is fast
        await trigger_ai_processing(call_id)
        
        message = None
        if payload.sequence != expected_sequence:
            message = f"Packet accepted but sequence mismatch (expected {expected_sequence})"
        
        return PacketResponse(
            status="accepted",
            call_id=call_id,
            sequence=payload.sequence,
            message=message
        )
    
    except Exception as e:
        logger.error(f"Error ingesting packet for call_id={call_id}: {str(e)}")
        # Even on error, return 202 if possible (fail gracefully)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to ingest packet"
        )


@router.get("/{call_id}/status", response_model=CallStatusResponse)
async def get_call_status(
    call_id: str,
    db: AsyncSession = Depends(get_db_session)
) -> CallStatusResponse:
    """Get current status of a call"""
    stmt = (
        select(Call)
        .where(Call.call_id == call_id)
        .options(selectinload(Call.packets), selectinload(Call.ai_result))
    )
    result = await db.execute(stmt)
    call = result.scalar_one_or_none()
    
    if not call:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Call {call_id} not found"
        )
    
    return CallStatusResponse(
        call_id=call.call_id,
        state=call.state.value,
        last_sequence=call.last_sequence,
        packet_count=len(call.packets),
        has_ai_result=call.ai_result is not None,
        created_at=call.created_at.isoformat(),
        updated_at=call.updated_at.isoformat()
    )
