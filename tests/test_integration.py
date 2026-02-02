import pytest
import asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy import select

from main import app
from app.models import Base, Call, CallPacket, CallState, AsyncSessionLocal
from config import settings

# Test database URL
TEST_DB_URL = "postgresql+asyncpg://user:password@localhost:5432/articence_test_db"

# Test engine with larger pool for parallel tests
test_engine = create_async_engine(
    TEST_DB_URL, 
    echo=False,
    pool_size=20,
    max_overflow=0,
    pool_pre_ping=True
)
TestSessionLocal = async_sessionmaker(test_engine, expire_on_commit=False)


@pytest.fixture(scope="function")
async def test_db():
    """Create test database tables before each test, drop after"""
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    
    yield
    
    # Cleanup database tables
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    
    # Dispose pool connections
    await test_engine.dispose()


# Override database dependency for tests
async def override_get_db_session():
    async with TestSessionLocal() as session:
        yield session


@pytest.fixture
async def client(test_db):
    """Create test client"""
    from app.models import get_db_session
    app.dependency_overrides[get_db_session] = override_get_db_session
    async with AsyncClient(app=app, base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_ingest_ordered_packets(client: AsyncClient, test_db):
    """Test normal packet ingestion in order"""
    call_id = "test_call_001"
    
    # Send 5 packets in order
    for seq in range(5):
        response = await client.post(
            f"/v1/call/stream/{call_id}",
            json={
                "sequence": seq,
                "data": f"packet_data_{seq}",
                "timestamp": 1706745600.0 + seq
            }
        )
        
        assert response.status_code == 202
        data = response.json()
        assert data["status"] == "accepted"
        assert data["call_id"] == call_id
        assert data["sequence"] == seq
    
    # Verify in database
    async with TestSessionLocal() as db:
        result = await db.execute(select(Call).where(Call.call_id == call_id))
        call = result.scalar_one()
        
        assert call.last_sequence == 4
        assert call.state == CallState.IN_PROGRESS
        
        packets = await db.execute(
            select(CallPacket).where(CallPacket.call_id == call_id).order_by(CallPacket.sequence)
        )
        packet_list = packets.scalars().all()
        assert len(packet_list) == 5


@pytest.mark.asyncio
async def test_missing_packet(client: AsyncClient, test_db):
    """Test missing packet handling - should log warning but not block"""
    call_id = "test_call_002"
    
    # Send packets 0, 1, 3 (missing 2)
    for seq in [0, 1, 3]:
        response = await client.post(
            f"/v1/call/stream/{call_id}",
            json={
                "sequence": seq,
                "data": f"packet_data_{seq}",
                "timestamp": 1706745600.0 + seq
            }
        )
        
        assert response.status_code == 202
        
        if seq == 3:
            # Should have warning message for sequence mismatch
            data = response.json()
            assert data["message"] is not None
    
    # Verify last_sequence is updated to highest received
    async with TestSessionLocal() as db:
        result = await db.execute(select(Call).where(Call.call_id == call_id))
        call = result.scalar_one()
        assert call.last_sequence == 3


@pytest.mark.asyncio
async def test_concurrent_packets_race_condition(client: AsyncClient, test_db):
    """
    Test race condition: two packets arrive simultaneously
    This is THE critical test for database locking
    """
    call_id = "test_call_003"
    
    # Send two packets at exactly the same time
    tasks = [
        client.post(
            f"/v1/call/stream/{call_id}",
            json={
                "sequence": 0,
                "data": "packet_0",
                "timestamp": 1706745600.0
            }
        ),
        client.post(
            f"/v1/call/stream/{call_id}",
            json={
                "sequence": 1,
                "data": "packet_1",
                "timestamp": 1706745601.0
            }
        )
    ]
    
    # Execute concurrently
    responses = await asyncio.gather(*tasks)
    
    # Both should succeed
    assert all(r.status_code == 202 for r in responses)
    
    # Verify database integrity
    async with TestSessionLocal() as db:
        # Should have exactly one call
        calls = await db.execute(select(Call).where(Call.call_id == call_id))
        call_list = calls.scalars().all()
        assert len(call_list) == 1
        
        call = call_list[0]
        assert call.last_sequence in [0, 1]  # One of them won
        
        # Should have both packets
        packets = await db.execute(
            select(CallPacket).where(CallPacket.call_id == call_id)
        )
        packet_list = packets.scalars().all()
        assert len(packet_list) == 2


@pytest.mark.asyncio
async def test_duplicate_packet_idempotency(client: AsyncClient, test_db):
    """Test that duplicate packets are handled idempotently"""
    call_id = "test_call_004"
    
    packet_data = {
        "sequence": 0,
        "data": "packet_data_0",
        "timestamp": 1706745600.0
    }
    
    # Send same packet twice
    response1 = await client.post(f"/v1/call/stream/{call_id}", json=packet_data)
    assert response1.status_code == 202
    
    # Second send should fail due to unique constraint, but gracefully
    response2 = await client.post(f"/v1/call/stream/{call_id}", json=packet_data)
    # Should return error or handle gracefully
    
    # Verify only one packet in database
    async with TestSessionLocal() as db:
        packets = await db.execute(
            select(CallPacket).where(CallPacket.call_id == call_id)
        )
        packet_list = packets.scalars().all()
        # Should have at most 1 packet (unique constraint)
        assert len(packet_list) <= 1


@pytest.mark.asyncio
async def test_response_time_under_50ms(client: AsyncClient, test_db):
    """Test that API response time is under 50ms"""
    import time
    
    call_id = "test_call_005"
    
    start_time = time.time()
    
    response = await client.post(
        f"/v1/call/stream/{call_id}",
        json={
            "sequence": 0,
            "data": "packet_data",
            "timestamp": 1706745600.0
        }
    )
    
    end_time = time.time()
    elapsed_ms = (end_time - start_time) * 1000
    
    assert response.status_code == 202
    assert elapsed_ms < 50, f"Response took {elapsed_ms}ms, expected < 50ms"


@pytest.mark.asyncio
async def test_get_call_status(client: AsyncClient, test_db):
    """Test retrieving call status"""
    call_id = "test_call_006"
    
    # Send a few packets
    for seq in range(3):
        await client.post(
            f"/v1/call/stream/{call_id}",
            json={
                "sequence": seq,
                "data": f"packet_{seq}",
                "timestamp": 1706745600.0 + seq
            }
        )
    
    # Get status
    response = await client.get(f"/v1/call/{call_id}/status")
    assert response.status_code == 200
    
    data = response.json()
    assert data["call_id"] == call_id
    assert data["state"] == "IN_PROGRESS"
    assert data["last_sequence"] == 2
    assert data["packet_count"] == 3


@pytest.mark.asyncio
async def test_massive_concurrent_load(client: AsyncClient, test_db):
    """Test system under massive concurrent load"""
    call_id = "test_call_007"
    num_packets = 20
    
    # Send many packets concurrently
    tasks = [
        client.post(
            f"/v1/call/stream/{call_id}",
            json={
                "sequence": i,
                "data": f"packet_{i}",
                "timestamp": 1706745600.0 + i
            }
        )
        for i in range(num_packets)
    ]
    
    responses = await asyncio.gather(*tasks, return_exceptions=True)
    
    # Count successful responses
    successful = sum(1 for r in responses if not isinstance(r, Exception) and r.status_code == 202)
    
    # At least most should succeed
    assert successful >= num_packets * 0.9, f"Only {successful}/{num_packets} succeeded"
    
    # Verify database state
    async with TestSessionLocal() as db:
        packets = await db.execute(
            select(CallPacket).where(CallPacket.call_id == call_id)
        )
        packet_list = packets.scalars().all()
        
        # Should have all unique packets (unique constraint prevents duplicates)
        assert len(packet_list) <= num_packets
