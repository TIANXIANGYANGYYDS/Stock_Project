from motor.motor_asyncio import AsyncIOMotorClient
from app.core.config import Settings


MONGO_URI = Settings().mongo_uri
MONGO_DB_NAME = Settings().mongo_db_name

client = AsyncIOMotorClient(MONGO_URI)
db = client[MONGO_DB_NAME]