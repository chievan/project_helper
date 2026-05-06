from sqlmodel import SQLModel, create_engine, Session
from app.core.config import settings

engine = create_engine(settings.DATABASE_URL, connect_args={"check_same_thread": False})

def init_db():
    # 修正文件名：从 repository 改为 repo
    from ..models.repo import Repository
    SQLModel.metadata.create_all(engine)

def get_session():
    with Session(engine) as session:
        yield session
