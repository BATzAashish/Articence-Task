import asyncio
import logging
import random
from datetime import datetime
from typing import Dict, Set

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models import Call, CallState, CallAIResult, AsyncSessionLocal
from app.ai_service import ai_service, MockAIServiceError
from app.websocket import notify_call_update
from config import settings

logger = logging.getLogger(__name__)

# Track calls currently being processed (prevent duplicate processing)
processing_calls: Set[str] = set()
processing_lock = asyncio.Lock()


async def trigger_ai_processing(call_id: str):
    """
    Trigger background AI processing for a call
    Non-blocking - just schedules the task
    """
    async with processing_lock:
        if call_id in processing_calls:
            # Already processing this call
            return
        processing_calls.add(call_id)
    
    # Schedule background task (fire and forget)
    asyncio.create_task(process_call_with_retry(call_id))


async def process_call_with_retry(call_id: str):
    """
    Process call with exponential backoff retry strategy
    
    Retry strategy:
    - Max retries: 5
    - Backoff: 2^n seconds + random jitter
    - Eventually consistent results
    """
    try:
        retry_count = 0
        max_retries = settings.max_ai_retries
        
        while retry_count <= max_retries:
            try:
                # Attempt processing
                success = await process_call(call_id, retry_count)
                
                if success:
                    logger.info(f"Successfully processed call_id={call_id} after {retry_count} retries")
                    return
                else:
                    # Call not ready for processing yet
                    return
            
            except MockAIServiceError as e:
                retry_count += 1
                
                if retry_count > max_retries:
                    # Max retries exceeded - mark as FAILED
                    logger.error(f"Max retries exceeded for call_id={call_id}")
                    await mark_call_failed(call_id, str(e))
                    return
                
                # Calculate backoff with jitter
                backoff = (2 ** retry_count) + random.uniform(0, 1)
                logger.info(
                    f"Retry {retry_count}/{max_retries} for call_id={call_id} "
                    f"after {backoff:.2f}s"
                )
                
                # Update retry count in database
                await update_retry_count(call_id, retry_count)
                
                # Wait before retrying
                await asyncio.sleep(backoff)
            
            except Exception as e:
                logger.error(f"Unexpected error processing call_id={call_id}: {str(e)}")
                await mark_call_failed(call_id, str(e))
                return
    
    finally:
        # Remove from processing set
        async with processing_lock:
            processing_calls.discard(call_id)


async def process_call(call_id: str, retry_attempt: int) -> bool:
    """
    Process a single call with AI service
    
    Returns:
        True if successful, False if call not ready
    
    Raises:
        MockAIServiceError: If AI service fails
    """
    async with AsyncSessionLocal() as db:
        # Get call with all packets
        stmt = (
            select(Call)
            .where(Call.call_id == call_id)
            .options(selectinload(Call.packets), selectinload(Call.ai_result))
        )
        result = await db.execute(stmt)
        call = result.scalar_one_or_none()
        
        if not call:
            logger.warning(f"Call {call_id} not found")
            return False
        
        # Check if call is in valid state for processing
        if call.state not in [CallState.IN_PROGRESS, CallState.FAILED]:
            logger.info(f"Call {call_id} already in state {call.state.value}, skipping")
            return False
        
        # Transition to PROCESSING_AI state
        if not call.transition_state(CallState.PROCESSING_AI):
            logger.warning(f"Invalid state transition for call {call_id}")
            return False
        await db.commit()
        
        # Notify via WebSocket
        await notify_call_update(call_id, CallState.PROCESSING_AI.value)
        
        # Concatenate all packet data
        audio_data = "".join(
            packet.data for packet in sorted(call.packets, key=lambda p: p.sequence)
        )
        
        # Call AI service (may raise MockAIServiceError)
        ai_result = await ai_service.transcribe(call_id, audio_data)
        
        # Store result and transition to COMPLETED
        # Refresh call state
        await db.refresh(call)
        
        # Create or update AI result
        if call.ai_result:
            call.ai_result.transcript = ai_result["transcript"]
            call.ai_result.sentiment = ai_result["sentiment"]
            call.ai_result.status = "completed"
            call.ai_result.retry_count = retry_attempt
            call.ai_result.completed_at = datetime.utcnow()
            call.ai_result.error_message = None
        else:
            db_result = CallAIResult(
                call_id=call_id,
                transcript=ai_result["transcript"],
                sentiment=ai_result["sentiment"],
                status="completed",
                retry_count=retry_attempt,
                completed_at=datetime.utcnow()
            )
            db.add(db_result)
        
        # Transition call to COMPLETED
        call.transition_state(CallState.COMPLETED)
        await db.commit()
        
        # Notify completion via WebSocket
        await notify_call_update(call_id, CallState.COMPLETED.value, ai_result)
        
        return True


async def update_retry_count(call_id: str, retry_count: int):
    """Update retry count in database"""
    async with AsyncSessionLocal() as db:
        stmt = select(Call).where(Call.call_id == call_id).options(selectinload(Call.ai_result))
        result = await db.execute(stmt)
        call = result.scalar_one_or_none()
        
        if call:
            if call.ai_result:
                call.ai_result.retry_count = retry_count
                call.ai_result.last_retry_at = datetime.utcnow()
            else:
                db_result = CallAIResult(
                    call_id=call_id,
                    retry_count=retry_count,
                    last_retry_at=datetime.utcnow()
                )
                db.add(db_result)
            
            await db.commit()


async def mark_call_failed(call_id: str, error_message: str):
    """Mark call as FAILED after max retries"""
    async with AsyncSessionLocal() as db:
        stmt = select(Call).where(Call.call_id == call_id).options(selectinload(Call.ai_result))
        result = await db.execute(stmt)
        call = result.scalar_one_or_none()
        
        if call:
            call.transition_state(CallState.FAILED)
            
            if call.ai_result:
                call.ai_result.status = "failed"
                call.ai_result.error_message = error_message
            else:
                db_result = CallAIResult(
                    call_id=call_id,
                    status="failed",
                    error_message=error_message
                )
                db.add(db_result)
            
            await db.commit()
    
    # Notify failure via WebSocket
    await notify_call_update(call_id, CallState.FAILED.value)
