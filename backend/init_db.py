from app.core.db import init_db
from app.models.repo import Repository # Ensure models are registered

if __name__ == "__main__":
    print("Initializing database...")
    init_db()
    print("Database initialized successfully.")
