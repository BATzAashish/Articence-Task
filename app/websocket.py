import logging
from typing import Dict, Set, Optional
from fastapi import WebSocket, WebSocketDisconnect
import json

logger = logging.getLogger(__name__)


class ConnectionManager:
    """
    Manages WebSocket connections for real-time dashboard updates
    Allows fan-out notifications to all connected clients
    """
    
    def __init__(self):
        # Store active connections
        self.active_connections: Set[WebSocket] = set()
        # Map call_id to interested websockets
        self.call_subscribers: Dict[str, Set[WebSocket]] = {}
    
    async def connect(self, websocket: WebSocket):
        """Accept and register new WebSocket connection"""
        await websocket.accept()
        self.active_connections.add(websocket)
        logger.info(f"WebSocket connected. Total connections: {len(self.active_connections)}")
    
    def disconnect(self, websocket: WebSocket):
        """Remove WebSocket connection"""
        self.active_connections.discard(websocket)
        
        # Remove from all call subscriptions
        for subscribers in self.call_subscribers.values():
            subscribers.discard(websocket)
        
        logger.info(f"WebSocket disconnected. Total connections: {len(self.active_connections)}")
    
    def subscribe_to_call(self, websocket: WebSocket, call_id: str):
        """Subscribe a WebSocket to updates for a specific call"""
        if call_id not in self.call_subscribers:
            self.call_subscribers[call_id] = set()
        self.call_subscribers[call_id].add(websocket)
        logger.info(f"WebSocket subscribed to call_id={call_id}")
    
    async def broadcast_to_all(self, message: dict):
        """Send message to all connected clients"""
        disconnected = set()
        
        for connection in self.active_connections:
            try:
                await connection.send_json(message)
            except Exception as e:
                logger.error(f"Error broadcasting to WebSocket: {e}")
                disconnected.add(connection)
        
        # Clean up disconnected clients
        for connection in disconnected:
            self.disconnect(connection)
    
    async def broadcast_to_call(self, call_id: str, message: dict):
        """Send message to all clients subscribed to a specific call"""
        if call_id not in self.call_subscribers:
            return
        
        disconnected = set()
        
        for connection in self.call_subscribers[call_id]:
            try:
                await connection.send_json(message)
            except Exception as e:
                logger.error(f"Error sending to WebSocket: {e}")
                disconnected.add(connection)
        
        # Clean up disconnected clients
        for connection in disconnected:
            self.disconnect(connection)


# Singleton instance
manager = ConnectionManager()


async def notify_call_update(call_id: str, state: str, ai_result: Optional[dict] = None):
    """
    Notify dashboard about call state changes
    This is called from background worker to push updates in real-time
    """
    from datetime import datetime
    
    message = {
        "type": "call_update",
        "call_id": call_id,
        "state": state,
        "timestamp": datetime.utcnow().isoformat()
    }
    
    if ai_result:
        message["ai_result"] = ai_result
    
    # Broadcast to subscribers and all connections
    await manager.broadcast_to_call(call_id, message)
    await manager.broadcast_to_all(message)
