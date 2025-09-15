from pymongo import MongoClient
import os
from dotenv import load_dotenv

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "").strip()
DB_NAME = "pravanshuosint"

client = MongoClient(MONGO_URI)
db = client[DB_NAME]

# Collections
history_col = db["history"]
favorites_col = db["favorites"]
