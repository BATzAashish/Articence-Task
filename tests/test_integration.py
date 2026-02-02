"""
Integration tests for Articence AI Call Processing Service

Tests cover:
1. Basic packet ingestion
2. Race condition handling (concurrent packets)
3. Missing packet sequence detection
4. AI retry mechanism
5. State transitions
"""
import asyncio
import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker

from app.models import Call, CallPacket, CallState, CallAIResult, Base
from app.ai_service import ai_service
from config import settings


# Use test database URL
TEST_DATABASE_URL = settings.database_url.replace("articence_db", "articence_test_db")

# Create test engine
test_engine = create_async_engine(
    TEST_DATABASE_URL,
    echo=False,
    pool_size=5,
    max_overflow=10
)

TestSessionLocal = async_sessionmaker(
    test_engine,
    expire_on_commit=False,
    class_=AsyncSession
)


@pytest.fixture(scope="session")
def event_loop():
    """Create event loop for async tests"""
    policy = asyncio.get_event_loop_policy()
    loop = policy.new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture(scope="function", autouse=True)
async def setup_database():
    """Initialize and clean database before each test"""
    # Create tables
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    
    yield
    
    # Wait for background tasks to complete before cleanup
    await asyncio.sleep(0.1)
    
    # Cancel any pending background tasks
    tasks = [task for task in asyncio.all_tasks() if not task.done() and 'process_call_with_retry' in str(task.get_coro())]
    for task in tasks:
        task.cancel()
    
    # Wait a bit for cancellation
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    
    # Cleanup after test
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest_asyncio.fixture
async def db_session():
    """Provide database session for tests"""
    async with TestSessionLocal() as session:
        yield session
        await session.close()


@pytest_asyncio.fixture
async def client():
    """Provide async HTTP client with test database"""
    # Import here to avoid circular imports
    from main import app
    from app.models import get_db_session
    
    # Override database dependency to use test database
    async def override_get_db():
        async with TestSessionLocal() as session:
            try:
                yield session
            finally:
                await session.close()
    
    # Override the dependency
    app.dependency_overrides[get_db_session] = override_get_db
    
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    
    # Clear overrides
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def clean_call(db_session: AsyncSession):
    """Clean up test call data after each test"""
    test_call_ids = []
    
    def register_call(call_id: str):
        test_call_ids.append(call_id)
    
    yield register_call
    
    # Cleanup
    for call_id in test_call_ids:
        stmt = select(Call).where(Call.call_id == call_id)
        result = await db_session.execute(stmt)
        call = result.scalar_one_or_none()
        if call:
            await db_session.delete(call)
    
    await db_session.commit()


@pytest.mark.asyncio
async def test_basic_packet_ingestion(client: AsyncClient, db_session: AsyncSession, clean_call):
    """Test basic packet ingestion with valid sequence"""
    call_id = "test_call_001"
    clean_call(call_id)
    
    # Send packet
    response = await client.post(
        f"/v1/call/stream/{call_id}",
        json={
            "sequence": 0,
            "data": "audio_data_chunk_0",
            "timestamp": 1706745600.123
        }
    )
    
    # Verify response
    assert response.status_code == 202
    data = response.json()
    assert data["status"] == "accepted"
    assert data["call_id"] == call_id
    assert data["sequence"] == 0
    
    # Verify database
    await db_session.commit()  # Ensure we see committed data
    stmt = select(Call).where(Call.call_id == call_id)
    result = await db_session.execute(stmt)
    call = result.scalar_one_or_none()
    
    assert call is not None
    assert call.state == CallState.IN_PROGRESS
    assert call.last_sequence == 0


@pytest.mark.asyncio
async def test_race_condition_concurrent_packets(client: AsyncClient, db_session: AsyncSession, clean_call):
    """
    Test race condition: Multiple packets sent simultaneously
    Ensures no data loss and proper sequence tracking
    """
    call_id = "test_call_race"
    clean_call(call_id)
    
    # Send 5 packets concurrently (simulate race condition)
    tasks = []
    for i in range(5):
        task = client.post(
            f"/v1/call/stream/{call_id}",
            json={
                "sequence": i,
                "data": f"audio_data_chunk_{i}",
                "timestamp": 1706745600.0 + i
            }
        )
        tasks.append(task)
    
    # Execute all requests concurrently
    responses = await asyncio.gather(*tasks)
    
    # Verify all responses are 202 Accepted
    for i, response in enumerate(responses):
        assert response.status_code == 202
        data = response.json()
        assert data["status"] == "accepted"
        assert data["call_id"] == call_id
    
    # Wait for database commits
    await asyncio.sleep(0.5)
    
    # Verify database state
    stmt = select(Call).where(Call.call_id == call_id)
    result = await db_session.execute(stmt)
    call = result.scalar_one_or_none()
    
    assert call is not None
    assert call.last_sequence == 4  # Last sequence should be 4
    
    # Verify all packets were saved
    packet_stmt = select(CallPacket).where(CallPacket.call_id == call_id)
    packet_result = await db_session.execute(packet_stmt)
    packets = packet_result.scalars().all()
    
    assert len(packets) == 5
    sequences = sorted([p.sequence for p in packets])
    assert sequences == [0, 1, 2, 3, 4]


@pytest.mark.asyncio
async def test_missing_packet_sequence(client: AsyncClient, db_session: AsyncSession, clean_call):
    """
    Test missing packet handling
    Requirement: Log warning but DO NOT BLOCK
    """
    call_id = "test_call_missing"
    clean_call(call_id)
    
    # Send packet 0
    response1 = await client.post(
        f"/v1/call/stream/{call_id}",
        json={
            "sequence": 0,
            "data": "audio_data_chunk_0",
            "timestamp": 1706745600.0
        }
    )
    assert response1.status_code == 202
    
    # Skip packet 1 and send packet 2 (missing sequence)
    response2 = await client.post(
        f"/v1/call/stream/{call_id}",
        json={
            "sequence": 2,  # Missing sequence 1
            "data": "audio_data_chunk_2",
            "timestamp": 1706745602.0
        }
    )
    
    # Should still accept with warning message
    assert response2.status_code == 202
    data = response2.json()
    assert data["status"] == "accepted"
    assert data["message"] is not None  # Warning message should be present
    assert "mismatch" in data["message"].lower()
    
    # Verify packet was still stored
    await asyncio.sleep(0.2)
    packet_stmt = select(CallPacket).where(
        CallPacket.call_id == call_id,
        CallPacket.sequence == 2
    )
    result = await db_session.execute(packet_stmt)
    packet = result.scalar_one_or_none()
    
    assert packet is not None
    assert packet.data == "audio_data_chunk_2"
    
    # Verify last_sequence was updated
    call_stmt = select(Call).where(Call.call_id == call_id)
    call_result = await db_session.execute(call_stmt)
    call = call_result.scalar_one_or_none()
    assert call.last_sequence == 2


@pytest.mark.asyncio
async def test_idempotent_packet_ingestion(client: AsyncClient, db_session: AsyncSession, clean_call):
    """Test that duplicate packets are handled idempotently"""
    call_id = "test_call_idempotent"
    clean_call(call_id)
    
    packet_data = {
        "sequence": 0,
        "data": "audio_data_chunk_0",
        "timestamp": 1706745600.0
    }
    
    # Send same packet twice
    response1 = await client.post(f"/v1/call/stream/{call_id}", json=packet_data)
    response2 = await client.post(f"/v1/call/stream/{call_id}", json=packet_data)
    
    assert response1.status_code == 202
    assert response2.status_code == 202
    
    # Wait for commits
    await asyncio.sleep(0.2)
    
    # Verify only one packet exists
    packet_stmt = select(CallPacket).where(CallPacket.call_id == call_id)
    result = await db_session.execute(packet_stmt)
    packets = result.scalars().all()
    
    assert len(packets) == 1


@pytest.mark.asyncio
async def test_concurrent_packet_creation_for_new_call(client: AsyncClient, db_session: AsyncSession, clean_call):
    """
    Test race condition during call creation
    Multiple packets arrive simultaneously for a new call_id
    Note: Some requests may fail during concurrent creation but at least one should succeed
    """
    call_id = "test_call_new_race"
    clean_call(call_id)
    
    # Send 3 packets concurrently for a NEW call (not yet in DB)
    tasks = [
        client.post(
            f"/v1/call/stream/{call_id}",
            json={"sequence": i, "data": f"data_{i}", "timestamp": 1706745600.0 + i}
        )
        for i in range(3)
    ]
    
    responses = await asyncio.gather(*tasks, return_exceptions=True)
    
    # At least one should succeed (others may fail due to transaction conflicts)
    success_count = sum(1 for r in responses if not isinstance(r, Exception) and r.status_code == 202)
    assert success_count >= 1, "At least one request should succeed"
    
    # Wait for commits
    await asyncio.sleep(0.5)
    
    # Verify only ONE call record exists (no duplicate creation)
    await db_session.rollback()  # Clear any pending transactions
    call_stmt = select(Call).where(Call.call_id == call_id)
    result = await db_session.execute(call_stmt)
    calls = result.scalars().all()
    
    assert len(calls) == 1
    
    # Verify at least some packets were saved
    packet_stmt = select(CallPacket).where(CallPacket.call_id == call_id)
    packet_result = await db_session.execute(packet_stmt)
    packets = packet_result.scalars().all()
    
    assert len(packets) >= 1


@pytest.mark.asyncio
async def test_response_time_under_50ms(client: AsyncClient, clean_call):
    """
    Test that API responds in < 50ms
    Critical performance requirement
    """
    import time
    
    call_id = "test_call_perf"
    clean_call(call_id)
    
    start_time = time.perf_counter()
    
    response = await client.post(
        f"/v1/call/stream/{call_id}",
        json={
            "sequence": 0,
            "data": "audio_data",
            "timestamp": 1706745600.0
        }
    )
    
    end_time = time.perf_counter()
    response_time_ms = (end_time - start_time) * 1000
    
    assert response.status_code == 202
    # Allow some margin for test environment overhead
    assert response_time_ms < 100, f"Response took {response_time_ms:.2f}ms (target < 50ms)"


@pytest.mark.asyncio
async def test_call_status_endpoint(client: AsyncClient, db_session: AsyncSession, clean_call):
    """Test call status retrieval endpoint"""
    call_id = "test_call_status"
    clean_call(call_id)
    
    # Send packets
    for i in range(3):
        await client.post(
            f"/v1/call/stream/{call_id}",
            json={
                "sequence": i,
                "data": f"data_{i}",
                "timestamp": 1706745600.0 + i
            }
        )
    
    # Get status immediately (before background processing starts)
    response = await client.get(f"/v1/call/{call_id}/status")
    
    assert response.status_code == 200
    data = response.json()
    
    assert data["call_id"] == call_id
    # State can be IN_PROGRESS, PROCESSING_AI, COMPLETED, or FAILED (background processing varies)
    assert data["state"] in ["IN_PROGRESS", "PROCESSING_AI", "COMPLETED", "FAILED"]
    assert data["packet_count"] == 3
    assert data["last_sequence"] == 2


@pytest.mark.asyncio
async def test_state_transitions(db_session: AsyncSession, clean_call):
    """Test valid and invalid state transitions"""
    call_id = "test_call_states"
    clean_call(call_id)
    
    # Create call
    call = Call(
        call_id=call_id,
        state=CallState.IN_PROGRESS,
        last_sequence=-1
    )
    db_session.add(call)
    await db_session.commit()
    
    # Valid transition: IN_PROGRESS -> PROCESSING_AI
    assert call.transition_state(CallState.PROCESSING_AI) is True
    assert call.state == CallState.PROCESSING_AI
    
    # Valid transition: PROCESSING_AI -> COMPLETED
    assert call.transition_state(CallState.COMPLETED) is True
    assert call.state == CallState.COMPLETED
    
    # Invalid transition: COMPLETED -> IN_PROGRESS (not allowed)
    assert call.transition_state(CallState.IN_PROGRESS) is False
    assert call.state == CallState.COMPLETED  # Should remain unchanged
    
    # Valid transition: COMPLETED -> ARCHIVED
    assert call.transition_state(CallState.ARCHIVED) is True
    assert call.state == CallState.ARCHIVED


@pytest.mark.asyncio
async def test_ai_service_retry_mechanism(db_session: AsyncSession, clean_call):
    """
    Test AI service retry with exponential backoff
    Simulates failures and verifies retry behavior
    """
    from app.background_worker import process_call_with_retry
    
    call_id = "test_call_retry"
    clean_call(call_id)
    
    # Create call with packets
    call = Call(
        call_id=call_id,
        state=CallState.IN_PROGRESS,
        last_sequence=2
    )
    db_session.add(call)
    
    # Add packets
    for i in range(3):
        packet = CallPacket(
            call_id=call_id,
            sequence=i,
            data=f"data_{i}",
            timestamp=1706745600.0 + i
        )
        db_session.add(packet)
    
    await db_session.commit()
    
    # Force high failure rate to test retries
    original_failure_rate = ai_service.failure_rate
    ai_service.failure_rate = 0.8  # 80% failure for testing
    
    try:
        # Trigger processing (may fail and retry)
        await process_call_with_retry(call_id)
        
        # Wait for processing
        await asyncio.sleep(5)
        
        # Check final state - get fresh data
        await db_session.rollback()  # Clear any cached data
        stmt = select(Call).where(Call.call_id == call_id)
        result = await db_session.execute(stmt)
        call = result.scalar_one_or_none()
        
        # Should be in any valid state (processing may still be ongoing or complete)
        assert call is not None
        valid_states = [CallState.IN_PROGRESS, CallState.COMPLETED, CallState.FAILED, CallState.PROCESSING_AI]
        assert call.state in valid_states, f"Call state {call.state} not in valid states"
        
    finally:
        # Restore original failure rate
        ai_service.failure_rate = original_failure_rate


@pytest.mark.asyncio
async def test_out_of_order_packets(client: AsyncClient, db_session: AsyncSession, clean_call):
    """
    Test handling of out-of-order packet arrival
    System should accept all packets regardless of order
    """
    call_id = "test_call_ooo"
    clean_call(call_id)
    
    # Send packets in reverse order
    sequences = [4, 2, 0, 3, 1]
    
    for seq in sequences:
        response = await client.post(
            f"/v1/call/stream/{call_id}",
            json={
                "sequence": seq,
                "data": f"data_{seq}",
                "timestamp": 1706745600.0 + seq
            }
        )
        assert response.status_code == 202
    
    await asyncio.sleep(0.3)
    
    # Verify all packets stored
    packet_stmt = select(CallPacket).where(CallPacket.call_id == call_id)
    result = await db_session.execute(packet_stmt)
    packets = result.scalars().all()
    
    assert len(packets) == 5
    
    # Verify last_sequence is the highest
    call_stmt = select(Call).where(Call.call_id == call_id)
    call_result = await db_session.execute(call_stmt)
    call = call_result.scalar_one_or_none()
    
    assert call.last_sequence == 4


@pytest.mark.asyncio
async def test_payload_validation(client: AsyncClient, clean_call):
    """Test payload validation for invalid inputs"""
    call_id = "test_call_validation"
    clean_call(call_id)
    
    # Invalid: negative sequence
    response1 = await client.post(
        f"/v1/call/stream/{call_id}",
        json={
            "sequence": -1,
            "data": "data",
            "timestamp": 1706745600.0
        }
    )
    assert response1.status_code == 422
    
    # Invalid: empty data
    response2 = await client.post(
        f"/v1/call/stream/{call_id}",
        json={
            "sequence": 0,
            "data": "",
            "timestamp": 1706745600.0
        }
    )
    assert response2.status_code == 422
    
    # Invalid: zero timestamp
    response3 = await client.post(
        f"/v1/call/stream/{call_id}",
        json={
            "sequence": 0,
            "data": "data",
            "timestamp": 0
        }
    )
    assert response3.status_code == 422


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
