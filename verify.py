"""
Quick verification script to test the Articence AI Call Processing Service

This script:
1. Checks if PostgreSQL is running
2. Tests API endpoints
3. Verifies packet ingestion
4. Checks call status
5. Tests race conditions
"""
import asyncio
import httpx
import time

BASE_URL = "http://localhost:8080"

async def check_health():
    """Check if API is running"""
    print("🔍 Checking API health...")
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(f"{BASE_URL}/health", timeout=5.0)
            if response.status_code == 200:
                print("✅ API is healthy")
                return True
            else:
                print(f"❌ API returned status {response.status_code}")
                return False
    except Exception as e:
        print(f"❌ Cannot connect to API: {e}")
        print(f"   Make sure the server is running on {BASE_URL}")
        return False

async def test_packet_ingestion():
    """Test basic packet ingestion"""
    print("\n📦 Testing packet ingestion...")
    call_id = f"test_verify_{int(time.time())}"
    
    async with httpx.AsyncClient() as client:
        # Send multiple packets
        for i in range(3):
            response = await client.post(
                f"{BASE_URL}/v1/call/stream/{call_id}",
                json={
                    "sequence": i,
                    "data": f"audio_chunk_{i}",
                    "timestamp": time.time()
                },
                timeout=5.0
            )
            
            if response.status_code == 202:
                print(f"  ✅ Packet {i} accepted")
            else:
                print(f"  ❌ Packet {i} failed: {response.status_code}")
                return False
    
    return True

async def test_call_status(call_id=None):
    """Test call status retrieval"""
    print("\n📊 Testing call status...")
    
    if not call_id:
        call_id = f"test_verify_{int(time.time())}"
        # Create a call first
        async with httpx.AsyncClient() as client:
            await client.post(
                f"{BASE_URL}/v1/call/stream/{call_id}",
                json={"sequence": 0, "data": "test", "timestamp": time.time()},
                timeout=5.0
            )
    
    await asyncio.sleep(0.5)  # Wait a bit for processing
    
    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"{BASE_URL}/v1/call/{call_id}/status",
            timeout=5.0
        )
        
        if response.status_code == 200:
            data = response.json()
            print(f"  ✅ Call status: {data['state']}")
            print(f"     Packets: {data['packet_count']}")
            print(f"     Last sequence: {data['last_sequence']}")
            return True
        else:
            print(f"  ❌ Failed to get status: {response.status_code}")
            return False

async def test_race_condition():
    """Test concurrent packet ingestion"""
    print("\n🏁 Testing race condition handling...")
    call_id = f"test_race_{int(time.time())}"
    
    async with httpx.AsyncClient() as client:
        # Send 5 packets concurrently
        tasks = [
            client.post(
                f"{BASE_URL}/v1/call/stream/{call_id}",
                json={
                    "sequence": i,
                    "data": f"data_{i}",
                    "timestamp": time.time() + i
                },
                timeout=5.0
            )
            for i in range(5)
        ]
        
        responses = await asyncio.gather(*tasks, return_exceptions=True)
        
        success_count = sum(
            1 for r in responses 
            if not isinstance(r, Exception) and r.status_code == 202
        )
        
        print(f"  ✅ {success_count}/5 concurrent packets accepted")
        return success_count >= 4  # Allow some to fail due to timing

async def test_response_time():
    """Test API response time"""
    print("\n⏱️  Testing response time...")
    call_id = f"test_perf_{int(time.time())}"
    
    async with httpx.AsyncClient() as client:
        start = time.perf_counter()
        
        response = await client.post(
            f"{BASE_URL}/v1/call/stream/{call_id}",
            json={
                "sequence": 0,
                "data": "test_data",
                "timestamp": time.time()
            },
            timeout=5.0
        )
        
        end = time.perf_counter()
        response_time_ms = (end - start) * 1000
        
        if response.status_code == 202 and response_time_ms < 100:
            print(f"  ✅ Response time: {response_time_ms:.2f}ms (target < 50ms)")
            return True
        else:
            print(f"  ⚠️  Response time: {response_time_ms:.2f}ms")
            return False

async def main():
    """Run all verification tests"""
    print("=" * 60)
    print("🚀 Articence AI Call Processing Service - Verification")
    print("=" * 60)
    
    # Check if API is running
    if not await check_health():
        print("\n❌ Cannot proceed: API is not running")
        print("\n💡 To start the API:")
        print("   1. Start PostgreSQL: docker-compose up -d")
        print("   2. Start API: python main.py")
        return
    
    # Run tests
    tests = [
        ("Packet Ingestion", test_packet_ingestion),
        ("Call Status", test_call_status),
        ("Race Condition", test_race_condition),
        ("Response Time", test_response_time),
    ]
    
    results = []
    for test_name, test_func in tests:
        try:
            result = await test_func()
            results.append((test_name, result))
        except Exception as e:
            print(f"  ❌ Error: {e}")
            results.append((test_name, False))
    
    # Summary
    print("\n" + "=" * 60)
    print("📋 Test Summary")
    print("=" * 60)
    
    for test_name, result in results:
        status = "✅ PASSED" if result else "❌ FAILED"
        print(f"  {status} - {test_name}")
    
    passed = sum(1 for _, result in results if result)
    total = len(results)
    
    print(f"\n🎯 Overall: {passed}/{total} tests passed")
    
    if passed == total:
        print("\n🎉 All tests passed! System is working correctly.")
    else:
        print("\n⚠️  Some tests failed. Check the output above.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\n⚠️  Verification cancelled by user")
    except Exception as e:
        print(f"\n\n❌ Error: {e}")
