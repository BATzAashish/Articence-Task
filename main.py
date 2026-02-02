import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from app.models import init_db
from app.routes import router
from app.websocket import manager
from config import settings

# Configure logging
logging.basicConfig(
    level=getattr(logging, settings.log_level),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan context manager for startup/shutdown"""
    # Startup
    logger.info("Initializing database...")
    await init_db()
    logger.info("Application started")
    
    yield
    
    # Shutdown
    logger.info("Application shutting down")


# Create FastAPI app
app = FastAPI(
    title="Articence AI Call Processing Service",
    description="Microservice for ingesting audio metadata and orchestrating AI processing",
    version="1.0.0",
    lifespan=lifespan
)

# CORS middleware for dashboard
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Configure properly in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include routers
app.include_router(router)


@app.get("/")
async def root():
    """Health check endpoint"""
    return {
        "service": "Articence AI Call Processing",
        "status": "operational",
        "version": "1.0.0"
    }


@app.get("/health")
async def health():
    """Detailed health check"""
    return {
        "status": "healthy",
        "database": "connected"
    }


@app.websocket("/ws/dashboard")
async def websocket_endpoint(websocket: WebSocket):
    """
    WebSocket endpoint for real-time dashboard updates
    
    Message format from client:
    {
        "action": "subscribe",
        "call_id": "abc123"
    }
    
    Message format to client:
    {
        "type": "call_update",
        "call_id": "abc123",
        "state": "COMPLETED",
        "ai_result": {...}
    }
    """
    await manager.connect(websocket)
    
    try:
        while True:
            # Receive messages from client
            data = await websocket.receive_json()
            
            action = data.get("action")
            
            if action == "subscribe":
                call_id = data.get("call_id")
                if call_id:
                    manager.subscribe_to_call(websocket, call_id)
                    await websocket.send_json({
                        "type": "subscribed",
                        "call_id": call_id
                    })
            
            elif action == "ping":
                await websocket.send_json({"type": "pong"})
    
    except WebSocketDisconnect:
        manager.disconnect(websocket)
        logger.info("WebSocket client disconnected")
    
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
        manager.disconnect(websocket)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8080,
        reload=True,
        log_level=settings.log_level.lower()
    )
