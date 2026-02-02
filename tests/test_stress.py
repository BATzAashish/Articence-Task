"""
Stress Tests - Push the system to its limits
These tests verify the system can handle extreme loads
"""
import pytest
import asyncio
from httpx import AsyncClient
from sqlalchemy import select

from main import app
from app.models import Call, CallPacket, Base
from tests.test_integration import test_engine, TestSessionLocal


@pytest.fixture(scope="function")
async def test_db():
    """Create test database tables before each test, drop after"""
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    
    yield
    
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    
    await test_engine.dispose()


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
async def test_100_concurrent_calls(client: AsyncClient, test_db):
    """
    Stress test: 100 different calls with 10 packets each
    Tests system scalability and database connection pooling
    """
    print("\n🔥 STRESS TEST: 100 concurrent calls with 10 packets each")
    
    # Create 100 calls with 10 packets each
    tasks = []
    for call_num in range(100):
        call_id = f"stress_call_{call_num:03d}"
        for seq in range(10):
            tasks.append(
                client.post(
                    f"/v1/call/stream/{call_id}",
                    json={
                        "sequence": seq,
                        "data": f"packet_{seq}",
                        "timestamp": 1706745600.0 + seq
                    }
                )
            )
    
    # Execute all 1000 requests concurrently
    print(f"   Sending 1000 requests (100 calls × 10 packets)...")
    responses = await asyncio.gather(*tasks, return_exceptions=True)
    
    # Check results
    success_count = sum(1 for r in responses if not isinstance(r, Exception) and r.status_code == 202)
    error_count = len(responses) - success_count
    
    print(f"   ✅ Success: {success_count}/1000")
    print(f"   ❌ Errors: {error_count}/1000")
    
    # At least 95% should succeed
    assert success_count >= 950, f"Only {success_count} requests succeeded"
    
    # Verify database integrity
    async with TestSessionLocal() as db:
        result = await db.execute(select(Call))
        calls = result.scalars().all()
        print(f"   📊 Total calls in DB: {len(calls)}")
        
        result = await db.execute(select(CallPacket))
        packets = result.scalars().all()
        print(f"   📦 Total packets in DB: {len(packets)}")
        
        assert len(calls) == 100
        # Should have close to 1000 packets (allowing for some duplicates being rejected)
        assert len(packets) >= 950


@pytest.mark.asyncio
async def test_extreme_race_condition(client: AsyncClient, test_db):
    """
    Extreme race condition test: 50 simultaneous requests for same call
    Tests that locking mechanism holds up under extreme contention
    """
    print("\n🔥 EXTREME RACE CONDITION: 50 simultaneous requests")
    
    call_id = "extreme_race_call"
    
    # Send 50 packets simultaneously
    tasks = [
        client.post(
            f"/v1/call/stream/{call_id}",
            json={
                "sequence": i,
                "data": f"packet_{i}",
                "timestamp": 1706745600.0 + i
            }
        )
        for i in range(50)
    ]
    
    print(f"   Sending 50 concurrent requests for single call...")
    responses = await asyncio.gather(*tasks)
    
    # All should return 202
    success_count = sum(1 for r in responses if r.status_code == 202)
    print(f"   ✅ All requests accepted: {success_count}/50")
    assert all(r.status_code == 202 for r in responses)
    
    # Verify database integrity
    async with TestSessionLocal() as db:
        # Should have exactly ONE call
        result = await db.execute(select(Call).where(Call.call_id == call_id))
        calls = result.scalars().all()
        print(f"   📊 Calls in DB: {len(calls)} (should be 1)")
        assert len(calls) == 1
        
        # Should have exactly 50 packets (no duplicates)
        result = await db.execute(
            select(CallPacket).where(CallPacket.call_id == call_id)
        )
        packets = result.scalars().all()
        print(f"   📦 Packets in DB: {len(packets)} (should be 50)")
        assert len(packets) == 50
        
        # Verify all sequences are present
        sequences = sorted([p.sequence for p in packets])
        expected = list(range(50))
        print(f"   🔢 All sequences present: {sequences == expected}")
        assert sequences == expected


@pytest.mark.asyncio
async def test_rapid_fire_single_call(client: AsyncClient, test_db):
    """
    Rapid fire test: Send 100 packets as fast as possible to single call
    Tests sequential packet handling at high speed
    """
    print("\n🔥 RAPID FIRE: 100 packets to single call")
    
    call_id = "rapid_fire_call"
    
    import time
    start_time = time.time()
    
    # Send 100 packets sequentially but as fast as possible
    for i in range(100):
        response = await client.post(
            f"/v1/call/stream/{call_id}",
            json={
                "sequence": i,
                "data": f"packet_{i}",
                "timestamp": 1706745600.0 + i
            }
        )
        assert response.status_code == 202
    
    elapsed = time.time() - start_time
    avg_per_request = (elapsed / 100) * 1000  # ms
    
    print(f"   ⏱️  Total time: {elapsed:.2f}s")
    print(f"   📊 Average per request: {avg_per_request:.2f}ms")
    print(f"   🚀 Throughput: {100/elapsed:.1f} requests/sec")
    
    # Each request should still be fast
    assert avg_per_request < 100, f"Average {avg_per_request}ms is too slow"
    
    # Verify all packets stored
    async with TestSessionLocal() as db:
        result = await db.execute(
            select(CallPacket).where(CallPacket.call_id == call_id)
        )
        packets = result.scalars().all()
        print(f"   ✅ All 100 packets stored")
        assert len(packets) == 100


@pytest.mark.asyncio
async def test_duplicate_flood(client: AsyncClient, test_db):
    """
    Duplicate flood: Send same packet 20 times concurrently
    Tests idempotency under high duplicate load
    """
    print("\n🔥 DUPLICATE FLOOD: Same packet sent 20 times")
    
    call_id = "duplicate_flood_call"
    
    # Send the same packet 20 times concurrently
    tasks = [
        client.post(
            f"/v1/call/stream/{call_id}",
            json={
                "sequence": 0,
                "data": "duplicate_packet",
                "timestamp": 1706745600.0
            }
        )
        for _ in range(20)
    ]
    
    print(f"   Sending same packet 20 times concurrently...")
    responses = await asyncio.gather(*tasks)
    
    # All should be accepted (202)
    success_count = sum(1 for r in responses if r.status_code == 202)
    print(f"   ✅ All accepted: {success_count}/20")
    assert all(r.status_code == 202 for r in responses)
    
    # But only ONE should be stored
    async with TestSessionLocal() as db:
        result = await db.execute(
            select(CallPacket).where(CallPacket.call_id == call_id)
        )
        packets = result.scalars().all()
        print(f"   📦 Packets stored: {len(packets)} (should be 1)")
        assert len(packets) == 1, "Idempotency failed - duplicates were stored"


@pytest.mark.asyncio
async def test_mixed_order_chaos(client: AsyncClient, test_db):
    """
    Mixed order chaos: Send packets completely out of order
    Tests system robustness with chaotic ordering
    """
    print("\n🔥 MIXED ORDER CHAOS: Packets sent randomly")
    
    call_id = "chaos_call"
    
    # Send packets in random order
    import random
    sequences = list(range(30))
    random.shuffle(sequences)
    
    print(f"   Sending 30 packets in random order...")
    for seq in sequences:
        response = await client.post(
            f"/v1/call/stream/{call_id}",
            json={
                "sequence": seq,
                "data": f"packet_{seq}",
                "timestamp": 1706745600.0 + seq
            }
        )
        assert response.status_code == 202
    
    # All should be stored
    async with TestSessionLocal() as db:
        result = await db.execute(
            select(CallPacket).where(CallPacket.call_id == call_id)
        )
        packets = result.scalars().all()
        print(f"   ✅ All 30 packets stored despite chaos")
        assert len(packets) == 30
        
        # Verify all sequences present
        stored_sequences = sorted([p.sequence for p in packets])
        assert stored_sequences == list(range(30))


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
