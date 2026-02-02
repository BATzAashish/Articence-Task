import enum
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger, Column, DateTime, Enum, Float, 
    ForeignKey, Integer, String, Text, UniqueConstraint
)
from sqlalchemy.ext.asyncio import AsyncAttrs, create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, relationship

from config import settings


# Base class for all models
class Base(AsyncAttrs, DeclarativeBase):
    pass


class CallState(str, enum.Enum):
    """Call state machine - strictly enforced transitions"""
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    PROCESSING_AI = "PROCESSING_AI"
    FAILED = "FAILED"
    ARCHIVED = "ARCHIVED"
    
    @classmethod
    def valid_transitions(cls):
        """Define all valid state transitions"""
        return {
            cls.IN_PROGRESS: [cls.PROCESSING_AI, cls.FAILED, cls.COMPLETED],
            cls.PROCESSING_AI: [cls.COMPLETED, cls.FAILED],
            cls.FAILED: [cls.PROCESSING_AI, cls.ARCHIVED],
            cls.COMPLETED: [cls.ARCHIVED],
            cls.ARCHIVED: []
        }
    
    def can_transition_to(self, new_state: "CallState") -> bool:
        """Check if transition is valid"""
        return new_state in self.valid_transitions().get(self, [])


class Call(Base):
    """
    Main call tracking table
    Tracks state and last received sequence to prevent race conditions
    """
    __tablename__ = "calls"
    
    call_id = Column(String(255), primary_key=True, index=True)
    state = Column(Enum(CallState), nullable=False, default=CallState.IN_PROGRESS)
    last_sequence = Column(Integer, nullable=False, default=-1)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relationships
    packets = relationship("CallPacket", back_populates="call", cascade="all, delete-orphan")
    ai_result = relationship("CallAIResult", back_populates="call", uselist=False, cascade="all, delete-orphan")
    
    def transition_state(self, new_state: CallState) -> bool:
        """
        Attempt to transition to new state
        Returns True if successful, False if invalid transition
        """
        if not self.state.can_transition_to(new_state):
            return False
        self.state = new_state
        self.updated_at = datetime.utcnow()
        return True


class CallPacket(Base):
    """
    Individual audio metadata packets
    Unique constraint on (call_id, sequence) prevents duplicate inserts
    """
    __tablename__ = "call_packets"
    
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    call_id = Column(String(255), ForeignKey("calls.call_id", ondelete="CASCADE"), nullable=False)
    sequence = Column(Integer, nullable=False)
    data = Column(Text, nullable=False)
    timestamp = Column(Float, nullable=False)
    received_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    
    # Relationship
    call = relationship("Call", back_populates="packets")
    
    # Prevent duplicate packets
    __table_args__ = (
        UniqueConstraint("call_id", "sequence", name="uq_call_sequence"),
    )


class CallAIResult(Base):
    """
    AI processing results for each call
    Tracks retry attempts for failure resilience
    """
    __tablename__ = "call_ai_results"
    
    call_id = Column(String(255), ForeignKey("calls.call_id", ondelete="CASCADE"), primary_key=True)
    transcript = Column(Text, nullable=True)
    sentiment = Column(String(50), nullable=True)
    status = Column(String(50), nullable=False, default="pending")
    retry_count = Column(Integer, nullable=False, default=0)
    last_retry_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    error_message = Column(Text, nullable=True)
    
    # Relationship
    call = relationship("Call", back_populates="ai_result")


# Database engine and session factory
engine = create_async_engine(
    settings.database_url,
    echo=False,
    pool_size=10,
    max_overflow=20,
    pool_pre_ping=True
)

AsyncSessionLocal = async_sessionmaker(
    engine,
    expire_on_commit=False,
    class_=None
)


async def init_db():
    """Initialize database tables"""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_db_session():
    """Dependency for getting database session"""
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()
