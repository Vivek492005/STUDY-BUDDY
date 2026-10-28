from langchain_community.vectorstores import FAISS
from langchain_core.documents import Document

from shared.embeddings import get_embeddings
from shared.text_splitter import default_splitter


def build_vectorstore(repo_data: dict):
    docs = []
    for f in repo_data.get("files", []):
        docs.append(
            Document(
                page_content=f["content"],
                metadata={"source": f["path"]},
            )
        )

    splitter = default_splitter()
    chunks = splitter.split_documents(docs)

    embeddings = get_embeddings()
    return FAISS.from_documents(chunks, embeddings)
