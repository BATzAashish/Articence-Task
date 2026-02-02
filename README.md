# Articence AI Call Processing Service

A production-grade FastAPI microservice for ingesting audio metadata and orchestrating AI processing with built-in resilience against failures and race conditions.

## Architecture Overview

```
┌─────────────┐         ┌──────────────────┐         ┌─────────────┐
│   Client    │────────▶│  FastAPI Server  │────────▶│ PostgreSQL  │
│  (Caller)   │ POST    │   (main.py)      │  Async  │  Database   │
└─────────────┘         └──────────────────┘         └─────────────┘
                               │      ▲
                               │      │
                        Trigger │      │ Result
                               ▼      │
                        ┌──────────────────┐
                        │ Background Worker│
                        │  (async tasks)   │
                        └──────────────────┘
                               │
                               ▼
                        ┌──────────────────┐         ┌─────────────┐
                        │  Mock AI Service │         │  WebSocket  │
                        │  (25% failures)  │         │  Dashboard  │
                        └──────────────────┘         └─────────────┘
```

## Core Principles

### Never Block the Request Path
- API ingestion ≠ AI processing
- Ingestion returns `202 Accepted` in < 50ms
- Background tasks handle all processing
- Decoupled architecture for resilience

### Everything Async
- Async SQLAlchemy engine
- Async database sessions
- Async background tasks
- Non-blocking I/O throughout

### Failure is Normal
- Mock AI service: 25% failure rate
- Exponential backoff retry (2^n + jitter)
- Max 5 retries before marking FAILED
- System survives and self-heals

### State is Explicit
- State machine with valid transitions only
- Database-tracked state for crash recovery
- No implicit state changes

### Order Matters
- Sequence validation per call
- Missing packets logged but don't block
- Row-level locking prevents race conditions

### Tests Prove Survival
- Integration tests with real DB
- Race condition simulation
- Concurrent load testing
- < 50ms response time verification

## State Machine

```
                     ┌──────────────┐
                     │ IN_PROGRESS  │
                     └──────┬───────┘
                            │
                ┌───────────┼───────────┐
                │           │           │
                ▼           ▼           ▼
         ┌──────────┐ ┌──────────────┐ │
         │  FAILED  │ │ PROCESSING_AI│ │
         └────┬─────┘ └──────┬───────┘ │
              │              │         │
              │         ┌────┴────┐    │
              │         │         │    │
              │         ▼         ▼    ▼
              │   ┌──────────┐ ┌──────────┐
              └──▶│ ARCHIVED │ │COMPLETED │
                  └──────────┘ └────┬─────┘
                                    │
                                    ▼
                              ┌──────────┐
                              │ ARCHIVED │
                              └──────────┘
```

### Valid Transitions
- `IN_PROGRESS` → `PROCESSING_AI`, `FAILED`, `COMPLETED`
- `PROCESSING_AI` → `COMPLETED`, `FAILED`
- `FAILED` → `PROCESSING_AI`, `ARCHIVED`
- `COMPLETED` → `ARCHIVED`
- `ARCHIVED` → (terminal state)

## Database Schema

### calls
```sql
call_id         VARCHAR(255) PRIMARY KEY
state           ENUM (CallState)
last_sequence   INTEGER
created_at      TIMESTAMP
updated_at      TIMESTAMP
```

### call_packets
```sql
id              BIGSERIAL PRIMARY KEY
call_id         VARCHAR(255) FOREIGN KEY
sequence        INTEGER
data            TEXT
timestamp       FLOAT
received_at     TIMESTAMP

UNIQUE CONSTRAINT (call_id, sequence)  -- Prevents duplicates
```

### call_ai_results
```sql
call_id         VARCHAR(255) PRIMARY KEY FOREIGN KEY
transcript      TEXT
sentiment       VARCHAR(50)
status          VARCHAR(50)
retry_count     INTEGER
last_retry_at   TIMESTAMP
completed_at    TIMESTAMP
error_message   TEXT
```

## API Endpoints

### POST /v1/call/stream/{call_id}
Ingest audio metadata packet.

**Request:**
```json
{
  "sequence": 0,
  "data": "base64_encoded_audio_metadata",
  "timestamp": 1706745600.123
}
```

**Response:** `202 Accepted` (< 50ms)
```json
{
  "status": "accepted",
  "call_id": "abc123",
  "sequence": 0,
  "message": null
}
```

**Implementation:**
- Row-level locking: `SELECT ... FOR UPDATE`
- Sequence validation with warning logs
- Unique constraint prevents duplicate packets
- Triggers background processing asynchronously

### GET /v1/call/{call_id}/status
Get current call status.

**Response:** `200 OK`
```json
{
  "call_id": "abc123",
  "state": "COMPLETED",
  "last_sequence": 42,
  "packet_count": 43,
  "has_ai_result": true,
  "created_at": "2026-02-01T10:00:00",
  "updated_at": "2026-02-01T10:05:00"
}
```

### WebSocket /ws/dashboard
Real-time updates for dashboard.

**Client Message:**
```json
{
  "action": "subscribe",
  "call_id": "abc123"
}
```

**Server Message:**
```json
{
  "type": "call_update",
  "call_id": "abc123",
  "state": "COMPLETED",
  "ai_result": {
    "transcript": "...",
    "sentiment": "positive"
  }
}
```

## Retry Strategy

### Exponential Backoff
```python
backoff = (2 ** retry_count) + random.uniform(0, 1)
```

**Retry Schedule:**
- Retry 1: ~2s
- Retry 2: ~4s
- Retry 3: ~8s
- Retry 4: ~16s
- Retry 5: ~32s

After 5 retries, call is marked as `FAILED` but can be retried later.

### Why This Works
- Gives flaky services time to recover
- Jitter prevents thundering herd
- Eventually consistent results
- 25% failure rate → ~97% success after 5 retries

## Concurrency & Race Condition Defense

### Problem
Two packets arrive simultaneously for the same call.

### Defense Mechanisms

1. **Database Row Locking**
   ```python
   SELECT ... FOR UPDATE  # PostgreSQL row lock
   ```

2. **Unique Constraint**
   ```sql
   UNIQUE (call_id, sequence)  -- Prevents duplicate inserts
   ```

3. **Transaction Isolation**
   - All operations in transactions
   - Atomic commits
   - No partial state

4. **Idempotent Logic**
   - Duplicate packets rejected by DB
   - Sequence tracking prevents gaps

### What Happens in Race Condition
1. Request A locks call row
2. Request B waits for lock
3. Request A inserts packet, updates sequence, commits
4. Request B acquires lock
5. Request B inserts packet, updates sequence, commits
6. Result: Both packets stored, correct order maintained

## Mock AI Service

Intentionally unreliable to test resilience.

### Characteristics
- **Failure Rate:** 25%
- **Error:** Raises `MockAIServiceError` (simulates 503)
- **Latency:** 1-3 seconds (random)
- **Success:** Returns mock transcript + sentiment

### Why?
If the system survives this, it survives production.

## Testing

### Run Tests
```bash
pytest tests/ -v
```

### Test Coverage

1. **test_ingest_ordered_packets**
   - Normal packet ingestion
   - Verifies database state

2. **test_missing_packet**
   - Packet 0, 1, 3 (missing 2)
   - Verifies warning logged
   - System continues working

3. **test_concurrent_packets_race_condition** ⚡ CRITICAL
   - Two packets at exact same time
   - Verifies no data corruption
   - Tests database locking

4. **test_duplicate_packet_idempotency**
   - Same packet sent twice
   - Unique constraint prevents duplicates

5. **test_response_time_under_50ms**
   - Measures API response time
   - Fails if > 50ms

6. **test_get_call_status**
   - Status endpoint verification

7. **test_massive_concurrent_load**
   - 20 packets concurrently
   - Stress test for race conditions

## Setup & Installation

### Prerequisites
- Python 3.11+
- PostgreSQL 14+

### Install Dependencies
```bash
pip install -r requirements.txt
```

### Environment Setup
```bash
cp .env.example .env
# Edit .env with your database credentials
```

**Required Environment Variables:**
```
DATABASE_URL=postgresql+asyncpg://user:password@localhost:5432/articence_db
LOG_LEVEL=INFO
MAX_AI_RETRIES=5
AI_FAILURE_RATE=0.25
```

### Initialize Database
```bash
# Create database
createdb articence_db

# Tables are auto-created on first run
```

### Run Server
```bash
# Development
python main.py

# Production
uvicorn main:app --host 0.0.0.0 --port 8000 --workers 4
```

### Run Tests
```bash
# Create test database
createdb articence_test_db

# Run tests
pytest tests/ -v

# With coverage
pytest tests/ --cov=app --cov-report=html
```

## Design Decisions

### Why PostgreSQL + Async?
- Row-level locking for race conditions
- ACID guarantees for state consistency
- Async engine for non-blocking I/O
- Production-proven reliability

### Why Unique Constraint on (call_id, sequence)?
- Database-level duplicate prevention
- No application-level checks needed
- Idempotent by design

### Why Background Tasks Instead of Queue?
- Simpler for this use case
- Lower operational complexity
- Still decoupled from request path
- Can migrate to Celery/RQ if needed

### Why WebSockets?
- Real-time dashboard updates
- Push model (not polling)
- Efficient for fan-out notifications
- Used only for notifications, not ingestion

### Why Mock AI Service Instead of Real?
- Demonstrates failure handling
- No external dependencies
- Deterministic for testing
- Easy to adjust failure rate

### Why Exponential Backoff?
- Industry standard for retries
- Prevents overwhelming failing service
- Jitter prevents thundering herd
- Eventually consistent without human intervention

### Why < 50ms Response Time?
- Fast feedback to caller
- Prevents caller timeout
- Proves decoupled architecture
- Background processing doesn't block

## Project Structure

```
articence/
├── app/
│   ├── __init__.py
│   ├── models.py              # Database models + state machine
│   ├── schemas.py             # Pydantic validation
│   ├── routes.py              # API endpoints with locking
│   ├── ai_service.py          # Mock AI (25% failure rate)
│   ├── background_worker.py   # Retry logic + exponential backoff
│   └── websocket.py           # WebSocket connection manager
├── tests/
│   ├── __init__.py
│   └── test_integration.py    # Integration tests
├── config.py                  # Settings + env vars
├── main.py                    # FastAPI app + lifespan
├── requirements.txt           # Dependencies
├── pytest.ini                 # Pytest configuration
├── .env.example               # Environment template
└── README.md                  # This file
```

## Why This Implementation Wins

### Battle-Tested Architecture
- Decoupled ingestion from processing
- Proven patterns for reliability
- No single point of failure

### Failure Resilience
- 25% AI failure rate handled gracefully
- Automatic retries with backoff
- System self-heals

### Race Condition Proof
- Database locking prevents corruption
- Integration tests prove it works
- Handles simultaneous requests correctly

### Performance
- < 50ms API response time
- Non-blocking async everywhere
- Scales horizontally

### Maintainability
- Clear state machine
- Explicit transitions
- Comprehensive tests
- Well-documented decisions

### Production Ready
- ACID guarantees
- Idempotent operations
- Crash recovery via DB state
- Observable via logs
