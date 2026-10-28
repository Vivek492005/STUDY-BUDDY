from langchain_community.document_loaders import PyMuPDFLoader
from langchain_text_splitters import RecursiveCharacterTextSplitter

CHROMA_DIR = "./data/chroma"


def ingest_pdfs(vectorstore, session_id: str, files):
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=1000,
        chunk_overlap=150,
    )

    docs = []

    for f in files:
        path = f.get("path")
        if not path:
            continue

        loader = PyMuPDFLoader(path)
        try:
            loaded_docs = loader.load()
            docs.extend(splitter.split_documents(loaded_docs))
        except Exception as e:
            print(f"Error loading {f.get('filename')}: {e}")
            continue

    if docs:
        vectorstore.add_documents(docs)

    return vectorstore    
