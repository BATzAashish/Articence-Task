import asyncio
import logging
import random
from typing import Dict, Optional
from fastapi import HTTPException

from config import settings

logger = logging.getLogger(__name__)


class MockAIServiceError(Exception):
    """Custom exception for AI service failures"""
    pass


class MockAIService:
    """
    Mock AI transcription service
    Intentionally unreliable to test retry logic
    
    Characteristics:
    - 25% failure rate (returns 503)
    - Variable latency: 1-3 seconds
    - Simulates real-world flaky external APIs
    """
    
    def __init__(self, failure_rate: float = 0.25):
        self.failure_rate = failure_rate
        self.call_count = 0
        self.failure_count = 0
    
    async def transcribe(self, call_id: str, audio_data: str) -> Dict[str, str]:
        """
        Simulate AI transcription
        
        Args:
            call_id: Unique call identifier
            audio_data: Concatenated audio metadata
        
        Returns:
            Dict with transcript and sentiment
        
        Raises:
            MockAIServiceError: On simulated failure (25% chance)
        """
        self.call_count += 1
        
        # Simulate variable latency (1-3 seconds)
        latency = random.uniform(1.0, 3.0)
        await asyncio.sleep(latency)
        
        # Simulate failure (25% chance)
        if random.random() < self.failure_rate:
            self.failure_count += 1
            logger.warning(
                f"AI service failure for call_id={call_id} "
                f"(failure rate: {self.failure_count}/{self.call_count})"
            )
            raise MockAIServiceError("503 Service Unavailable - AI service temporarily down")
        
        # Success - return mock results
        logger.info(f"AI service success for call_id={call_id} (latency: {latency:.2f}s)")
        
        # Generate mock transcript based on call_id for deterministic testing
        sentiments = ["positive", "negative", "neutral", "mixed"]
        sentiment = sentiments[hash(call_id) % len(sentiments)]
        
        return {
            "transcript": f"Mock transcript for call {call_id}: Customer and agent conversation...",
            "sentiment": sentiment
        }


# Singleton instance
ai_service = MockAIService(failure_rate=settings.ai_failure_rate)
