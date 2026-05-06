from sqlmodel import SQLModel, Field, Column, JSON
from typing import Optional, Dict, Any
from datetime import datetime

class Repository(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    url: str = Field(index=True, unique=True)
    name: str
    owner: str
    description: Optional[str] = None
    tech_stack: Optional[Dict[str, Any]] = Field(default={}, sa_column=Column(JSON))
    analysis_report: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    status: str = "pending"  # pending, cloning, analyzing, completed, failed
    progress: float = 0.0
