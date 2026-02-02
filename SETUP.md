# Setup Instructions

## Prerequisites

- Python 3.12 or higher
- Docker Desktop (for PostgreSQL)
- Git

## Quick Start

### 1. Clone the Repository

```bash
git clone <your-repo-url>
cd Articence-Task
```

### 2. Install Dependencies

```bash
pip install -r requirements.txt
```

### 3. Start PostgreSQL Database

```bash
docker-compose up -d
```

Wait a few seconds for PostgreSQL to be ready.

### 4. Configure Environment (Optional)

Create a `.env` file in the root directory:

```env
DATABASE_URL=postgresql+asyncpg://user:password@localhost:5432/articence_db
LOG_LEVEL=INFO
MAX_AI_RETRIES=5
AI_FAILURE_RATE=0.25
```

### 5. Run the Application

#### Option A: Using Python directly

```bash
# On Windows PowerShell
$env:PYTHONPATH = "."
python main.py
```

```bash
# On Linux/Mac
export PYTHONPATH=.
python main.py
```

#### Option B: Using Uvicorn

```bash
uvicorn main:app --host 0.0.0.0 --port 8080 --reload
```

The API will be available at: http://localhost:8080

### 6. Access API Documentation

- **Swagger UI**: http://localhost:8080/docs
- **ReDoc**: http://localhost:8080/redoc

### 7. Run Tests

```bash
# On Windows PowerShell
$env:PYTHONPATH = "."
pytest tests/test_integration.py -v

# On Linux/Mac
export PYTHONPATH=.
pytest tests/test_integration.py -v
```

## API Endpoints

### Health Check
```bash
GET http://localhost:8080/
GET http://localhost:8080/health
```

### Ingest Audio Packet
```bash
POST http://localhost:8080/v1/call/stream/{call_id}

Body:
{
  "sequence": 0,
  "data": "audio_data_chunk",
  "timestamp": 1706745600.123
}

Response: 202 Accepted
```

### Get Call Status
```bash
GET http://localhost:8080/v1/call/{call_id}/status
```

### WebSocket (Real-time Updates)
```bash
WS ws://localhost:8080/ws/dashboard

Send:
{
  "action": "subscribe",
  "call_id": "your_call_id"
}
```

## Testing the API

### Using curl

```bash
# Ingest packets
curl -X POST http://localhost:8080/v1/call/stream/call_001 \
  -H "Content-Type: application/json" \
  -d '{"sequence": 0, "data": "audio_data_0", "timestamp": 1706745600.0}'

curl -X POST http://localhost:8080/v1/call/stream/call_001 \
  -H "Content-Type: application/json" \
  -d '{"sequence": 1, "data": "audio_data_1", "timestamp": 1706745601.0}'

# Check status
curl http://localhost:8080/v1/call/call_001/status
```

### Using Python

```python
import httpx
import asyncio

async def test_api():
    async with httpx.AsyncClient() as client:
        # Send packet
        response = await client.post(
            "http://localhost:8080/v1/call/stream/call_001",
            json={
                "sequence": 0,
                "data": "audio_data",
                "timestamp": 1706745600.0
            }
        )
        print(f"Status: {response.status_code}")
        print(f"Response: {response.json()}")
        
        # Wait a bit for processing
        await asyncio.sleep(2)
        
        # Check status
        status = await client.get(
            "http://localhost:8080/v1/call/call_001/status"
        )
        print(f"Call Status: {status.json()}")

asyncio.run(test_api())
```

## Database Management

### Connect to PostgreSQL

```bash
docker exec -it articence-postgres psql -U user -d articence_db
```

### View Tables

```sql
\dt
SELECT * FROM calls;
SELECT * FROM call_packets;
SELECT * FROM call_ai_results;
```

### Stop Database

```bash
docker-compose down
```

### Reset Database (Delete all data)

```bash
docker-compose down -v
docker-compose up -d
```

## Troubleshooting

### Port Already in Use

If port 8080 is already in use, modify the port in `main.py`:

```python
uvicorn.run("main:app", host="0.0.0.0", port=8081, reload=True)
```

### Database Connection Issues

1. Ensure Docker is running
2. Check PostgreSQL is healthy: `docker ps`
3. Wait 5-10 seconds after starting Docker Compose

### Import Errors

Ensure PYTHONPATH is set:

```bash
# Windows PowerShell
$env:PYTHONPATH = "."

# Linux/Mac
export PYTHONPATH=.
```

### Test Database Creation

Tests use a separate test database (`articence_test_db`). This is automatically created during testing.

## Development

### Project Structure

```
Articence-Task/
├── main.py              # FastAPI application entry point
├── config.py            # Configuration settings
├── requirements.txt     # Python dependencies
├── docker-compose.yml   # PostgreSQL container
├── pytest.ini          # Test configuration
├── app/
│   ├── __init__.py
│   ├── models.py       # SQLAlchemy models & state machine
│   ├── routes.py       # API endpoints
│   ├── schemas.py      # Pydantic schemas
│   ├── ai_service.py   # Mock AI service (25% failure)
│   ├── background_worker.py  # Retry logic with exponential backoff
│   └── websocket.py    # WebSocket connection manager
└── tests/
    └── test_integration.py  # Integration tests (11 tests)
```

### Adding New Features

1. Update models in `app/models.py`
2. Add routes in `app/routes.py`
3. Update schemas in `app/schemas.py`
4. Write tests in `tests/test_integration.py`
5. Run tests: `pytest tests/ -v`

## Performance Notes

- API response time target: < 50ms
- Database connection pool: 10 connections, max overflow 20
- AI service retry: Max 5 retries with exponential backoff
- Background processing: Non-blocking asyncio tasks

## Production Deployment

For production deployment:

1. Update `DATABASE_URL` to production database
2. Set `LOG_LEVEL=WARNING` or `ERROR`
3. Configure proper CORS origins in `main.py`
4. Use production-grade ASGI server (gunicorn + uvicorn workers)
5. Set up monitoring and logging
6. Enable database backups
7. Use environment variables for all secrets

```bash
# Production example
gunicorn main:app \
  --workers 4 \
  --worker-class uvicorn.workers.UvicornWorker \
  --bind 0.0.0.0:8080 \
  --access-logfile -
```
