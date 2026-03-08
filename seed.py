import os
from pymongo import MongoClient

client = MongoClient(os.getenv("MONGODB_URI"))
db = client["hrms_db"]

collections = [
    "users",
    "documents",
    "leave_requests",
    "visa_applications",
    "flight_requests",
    "insurance_applications",
    "cab_requests"
]

for col in collections:
    if col not in db.list_collection_names():
        db.create_collection(col)
        print(f"{col} collection created")
    else:
        print(f"{col} already exists")

print("Database setup completed ✅")