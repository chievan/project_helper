from sqlmodel import SQLModel, create_engine, Session
from app.core.config import settings

engine = create_engine(settings.DATABASE_URL, connect_args={"check_same_thread": False})

def init_db():
    # 必须导入模型，SQLModel 才能发现表结构
    from app.models.repository import Repository
    SQLModel.metadata.create_all(engine)

def get_session():
    with Session(engine) as session:
        yield session
