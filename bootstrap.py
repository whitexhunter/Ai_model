import os
import chromadb
from dotenv import load_dotenv

load_dotenv()

DATA_DIR = "data"

def bootstrap_base_memory(file_path):
    os.makedirs(DATA_DIR, exist_ok=True)
    chroma_client = chromadb.PersistentClient(path=f"{DATA_DIR}/relationship_vector_space")
    vector_memory = chroma_client.get_or_create_collection(name="relationship_history")

    if not os.path.exists(file_path):
        print(f"Error: [{file_path}] not found. Put your relationship_base.txt in the same folder.")
        return
        
    if vector_memory.count() > 0:
        print(f"Vector space already contains {vector_memory.count()} records. Skipping bootstrap.")
        return

    print("Parsing background logs into permanent vector space for Sanskruti...")
    with open(file_path, 'r', encoding='utf-8') as file:
        lines = file.readlines()

    documents, ids, metadatas = [], [], []
    for i, line in enumerate(lines):
        line = line.strip()
        if not line: continue
        
        if line.startswith("[Aditya]:"):
            speaker = "Aditya"
            content = line.replace("[Aditya]:", "").strip()
        elif line.startswith("[Sanskruti]:"):
            speaker = "Sanskruti"
            content = line.replace("[Sanskruti]:", "").strip()
        else:
            speaker = "context"
            content = line

        documents.append(f"[{speaker}]: {content}")
        metadatas.append({"speaker": speaker})
        ids.append(f"hist_{i}")

        if len(documents) >= 100:
            vector_memory.add(documents=documents, metadatas=metadatas, ids=ids)
            documents, ids, metadatas = [], [], []

    if documents:
        vector_memory.add(documents=documents, metadatas=metadatas, ids=ids)
    print("Historical indexing completed successfully. Sanskruti remembers everything.")

if __name__ == '__main__':
    bootstrap_base_memory('relationship_base.txt')
