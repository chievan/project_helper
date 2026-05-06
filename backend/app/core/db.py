from sqlmodel import SQLModel, create_engine, Session
from app.core.config import settings

engine = create_engine(settings.DATABASE_URL, connect_args={"check_same_thread": False})

def init_db():
    # 使用相对导入，增强路径鲁棒性
    from ..models.repository import Repository
    SQLModel.metadata.create_all(engine)

def get_session():
    with Session(engine) as session:
        yield session
