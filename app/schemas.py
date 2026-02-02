from pydantic import BaseModel, Field, validator
from typing import Optional


class PacketPayload(BaseModel):
    """Incoming packet payload validation"""
    sequence: int = Field(..., ge=0, description="Packet sequence number (0-based)")
    data: str = Field(..., min_length=1, description="Audio metadata content")
    timestamp: float = Field(..., gt=0, description="Unix timestamp")
    
    class Config:
        json_schema_extra = {
            "example": {
                "sequence": 0,
                "data": "base64_encoded_audio_metadata",
                "timestamp": 1706745600.123
            }
        }


class PacketResponse(BaseModel):
    """Response for packet ingestion"""
    status: str = "accepted"
    call_id: str
    sequence: int
    message: Optional[str] = None


class CallStatusResponse(BaseModel):
    """Call status for dashboard"""
    call_id: str
    state: str
    last_sequence: int
    packet_count: int
    has_ai_result: bool
    created_at: str
    updated_at: str


class AIResultResponse(BaseModel):
    """AI processing result"""
    call_id: str
    transcript: Optional[str]
    sentiment: Optional[str]
    status: str
    retry_count: int
    completed_at: Optional[str]
