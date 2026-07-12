from pymongo import MongoClient

# Connect to MongoDB
client = MongoClient("mongodb://root:example@localhost:27017/?authSource=admin")
db = client['userdb']
collection = db['titanic']

# Count the number of documents in the collection
document_count = collection.count_documents({})
print(f"Number of documents in userdb.titanic: {document_count}")

# Fetch and print some sample records
sample_records = list(collection.find().limit(5))
for record in sample_records:
    print(record)