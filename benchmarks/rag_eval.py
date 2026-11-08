"""RAG-01: Evaluation harness for retrieval and answers.

Corpus: 8 synthetic documents (distinct topics, 6-8 facts each), 56
questions (48 answerable, 6 per doc, + 8 unanswerable).

Metrics: Recall@1/3/5, MRR, answer accuracy (expected fact substring in
the answer), citation validity, abstention accuracy.
Usage: python benchmarks/rag_eval.py [--quick]
Writes benchmarks/rag_results.json.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from studybuddy.pdf_qa import (  # noqa: E402
    answer_question,
    chunk_pages,
    retrieve,
    verify_citations,
)

GOLD_DIR = os.path.join(os.path.dirname(__file__), "rag_gold")


def recall_at_k(ranked_ids, gold_ids, k):
    topk = set(ranked_ids[:k])
    hits = len(topk & set(gold_ids))
    return hits / len(gold_ids) if gold_ids else 1.0


def mrr(ranked_ids, gold_ids):
    gold = set(gold_ids)
    for i, rid in enumerate(ranked_ids):
        if rid in gold:
            return 1.0 / (i + 1)
    return 0.0


# ---------------------------------------------------------------------------
# Corpus: 8 docs, each a set of distinct factual sentences. The text is
# repeated x3 (as before) so chunking produces several chunks per doc.
# ---------------------------------------------------------------------------
DOCS = {
    "paper_a": (
        "Machine learning models require large datasets for training. "
        "The transformer architecture revolutionized natural language processing in 2017. "
        "Attention mechanisms allow models to focus on relevant input parts. "
        "BERT and GPT are prominent transformer-based models. "
        "Fine-tuning adapts a pre-trained model to a specific task. "
        "The attention mechanism weighs each word by its relevance to the others. "
        "Training a large model can cost millions of dollars in compute. "
    ),
    "paper_b": (
        "Climate change accelerates polar ice melt. "
        "Rising sea levels threaten coastal cities worldwide. "
        "Carbon capture technology shows promise for emission reduction. "
        "Renewable energy adoption grew 12 percent last year. "
        "Methane traps far more heat than carbon dioxide per tonne. "
        "The Paris Agreement aims to limit warming to one and a half degrees Celsius. "
        "Glaciers in the Himalayas feed rivers used by a billion people. "
    ),
    "paper_c": (
        "Quantum computing uses qubits for parallel computation. "
        "Shor's algorithm factors large numbers efficiently. "
        "Quantum supremacy was claimed by Google in 2019. "
        "Error correction remains a major challenge. "
        "Entanglement links qubits so measuring one affects the other. "
        "Quantum computers need temperatures near absolute zero to operate. "
        "IBM and Google both run cloud-accessible quantum processors. "
    ),
    "paper_d": (
        "A B-tree index speeds up lookups but slows down writes. "
        "ACID transactions guarantee atomicity, consistency, isolation and durability. "
        "Normalisation reduces duplication by splitting tables into related pieces. "
        "PostgreSQL supports JSON columns alongside relational tables. "
        "A database deadlock happens when two transactions wait on each other forever. "
        "Replication copies data to standby servers for high availability. "
    ),
    "paper_e": (
        "Phishing emails trick people into revealing passwords. "
        "Multi-factor authentication blocks most account-takeover attacks. "
        "Zero trust assumes no network segment is safe by default. "
        "Ransomware encrypts files and demands payment for the key. "
        "A VPN tunnels traffic through an encrypted connection. "
        "Patching known vulnerabilities within 30 days cuts breach risk sharply. "
    ),
    "paper_f": (
        "The Perseverance rover landed on Mars in February 2021. "
        "The International Space Station orbits Earth every 90 minutes. "
        "Artemis aims to return astronauts to the Moon. "
        "A Falcon 9 first stage can land and fly again. "
        "The James Webb telescope observes infrared light from the early universe. "
        "Mars has two small moons, Phobos and Deimos. "
    ),
    "paper_g": (
        "Inflation measures how fast prices rise over a year. "
        "Central banks raise interest rates to cool inflation. "
        "GDP counts the value of all goods and services produced in a country. "
        "A recession is commonly defined as two quarters of shrinking GDP. "
        "Unemployment benefits support workers between jobs. "
        "Compound interest grows savings faster than simple interest. "
    ),
    "paper_h": (
        "CRISPR lets scientists edit DNA at precise locations. "
        "Messenger RNA vaccines teach cells to make a harmless spike protein. "
        "Mitochondria generate most of the energy a cell uses. "
        "The human genome contains roughly twenty thousand protein-coding genes. "
        "Antibiotics kill bacteria but do nothing against viruses. "
        "Photosynthesis converts sunlight, water and carbon dioxide into glucose. "
    ),
}

# Gold Q&A: (question, gold_doc, answerable, expected_fact_substring).
# The substring must appear verbatim (case-insensitive) in the gold doc's
# text; it is what answer_accuracy checks for in the produced answer.
QA = [
    # paper_a: machine learning (first 4 are the --quick set)
    ("What architecture revolutionized NLP in 2017?", "paper_a", True,
     "transformer architecture"),
    ("Which models are transformer-based?", "paper_a", True, "BERT and GPT"),
    ("What do attention mechanisms allow models to do?", "paper_a", True,
     "focus on relevant input parts"),
    ("What does fine-tuning do to a pre-trained model?", "paper_a", True,
     "adapts a pre-trained model to a specific task"),
    ("How much can training a large model cost?", "paper_a", True,
     "millions of dollars"),
    ("What does the attention mechanism weigh each word by?", "paper_a",
     True, "relevance to the others"),
    # paper_b: climate
    ("What threatens coastal cities?", "paper_b", True, "Rising sea levels"),
    ("How much did renewable adoption grow?", "paper_b", True, "12 percent"),
    ("What shows promise for emission reduction?", "paper_b", True,
     "Carbon capture"),
    ("What traps more heat than carbon dioxide per tonne?", "paper_b", True,
     "Methane"),
    ("What temperature limit does the Paris Agreement aim for?", "paper_b",
     True, "one and a half degrees Celsius"),
    ("Which glaciers feed rivers used by a billion people?", "paper_b", True,
     "Himalayas"),
    # paper_c: quantum
    ("What does Shor's algorithm do?", "paper_c", True,
     "factors large numbers"),
    ("When was quantum supremacy claimed?", "paper_c", True, "2019"),
    ("What remains a major challenge in quantum computing?", "paper_c", True,
     "Error correction"),
    ("What does entanglement do to qubits?", "paper_c", True,
     "measuring one affects the other"),
    ("What temperatures do quantum computers need to operate?", "paper_c",
     True, "near absolute zero"),
    ("Which companies run cloud-accessible quantum processors?", "paper_c",
     True, "IBM and Google"),
    # paper_d: databases
    ("What does a B-tree index speed up?", "paper_d", True, "lookups"),
    ("What do ACID transactions guarantee?", "paper_d", True,
     "atomicity, consistency, isolation and durability"),
    ("What does normalisation reduce?", "paper_d", True, "duplication"),
    ("What does PostgreSQL support alongside relational tables?", "paper_d",
     True, "JSON columns"),
    ("When does a database deadlock happen?", "paper_d", True,
     "two transactions wait on each other"),
    ("What does replication copy data to?", "paper_d", True,
     "standby servers"),
    # paper_e: cybersecurity
    ("What do phishing emails trick people into revealing?", "paper_e", True,
     "passwords"),
    ("What blocks most account-takeover attacks?", "paper_e", True,
     "Multi-factor authentication"),
    ("What does zero trust assume about network segments?", "paper_e", True,
     "no network segment is safe"),
    ("What does ransomware demand payment for?", "paper_e", True,
     "the key"),
    ("What does a VPN tunnel traffic through?", "paper_e", True,
     "encrypted connection"),
    ("What does patching within 30 days cut?", "paper_e", True,
     "breach risk"),
    # paper_f: space
    ("When did the Perseverance rover land on Mars?", "paper_f", True,
     "February 2021"),
    ("How often does the International Space Station orbit Earth?",
     "paper_f", True, "every 90 minutes"),
    ("What is the goal of the Artemis programme?", "paper_f", True,
     "return astronauts to the Moon"),
    ("What can a Falcon 9 first stage do?", "paper_f", True,
     "land and fly again"),
    ("What kind of light does the James Webb telescope observe?", "paper_f",
     True, "infrared light"),
    ("What are the names of Mars's two moons?", "paper_f", True,
     "Phobos and Deimos"),
    # paper_g: economics
    ("What does inflation measure?", "paper_g", True,
     "how fast prices rise"),
    ("What do central banks raise to cool inflation?", "paper_g", True,
     "interest rates"),
    ("What does GDP count?", "paper_g", True,
     "value of all goods and services produced"),
    ("How is a recession commonly defined?", "paper_g", True,
     "two quarters of shrinking GDP"),
    ("What do unemployment benefits support?", "paper_g", True,
     "workers between jobs"),
    ("What grows savings faster than simple interest?", "paper_g", True,
     "Compound interest"),
    # paper_h: biology
    ("What does CRISPR let scientists edit?", "paper_h", True,
     "DNA at precise locations"),
    ("What do mRNA vaccines teach cells to make?", "paper_h", True,
     "harmless spike protein"),
    ("What generates most of the energy a cell uses?", "paper_h", True,
     "Mitochondria"),
    ("How many protein-coding genes does the human genome contain?",
     "paper_h", True, "twenty thousand"),
    ("What do antibiotics do nothing against?", "paper_h", True, "viruses"),
    ("What does photosynthesis convert sunlight, water and carbon dioxide into?",
     "paper_h", True, "glucose"),
    # Unanswerable (not in the corpus). Two groups, by design:
    #  - CLEAN (5): zero token overlap with the corpus; the retriever
    #    returns no match and answer_question abstains.
    #  - NEAR-MISS (3): each shares one token with the corpus ("mars",
    #    "year", "how"). These DOCUMENT a real retriever limitation:
    #    retrieve() normalises scores, so a single shared token gives the
    #    top chunk a combined score of 1.0, which clears the 0.05
    #    relevance threshold and produces a (wrong) answer instead of an
    #    abstention. Expected abstention is therefore 5/8 = 0.625, not 1.0.
    ("Who invented the telephone?", None, False, ""),
    ("What is the population of Tokyo?", None, False, ""),
    ("What is the recipe for chocolate cake?", None, False, ""),
    ("Who painted the Mona Lisa?", None, False, ""),
    ("What is the chemical symbol for gold?", None, False, ""),
    ("What is the capital of Mars?", None, False, ""),
    ("In which year was Shakespeare born?", None, False, ""),
    ("How do dolphins communicate?", None, False, ""),
]


def evaluate(quick=False):
    docs = {doc_id: text * 3 for doc_id, text in DOCS.items()}
    # Chunk each doc
    chunked = {}
    for doc_id, text in docs.items():
        chunks = chunk_pages([text], chunk_chars=400, overlap=50)
        for i, c in enumerate(chunks):
            c["chunk_id"] = f"{doc_id}#{i}"
        chunked[doc_id] = chunks

    all_chunks = [c for cl in chunked.values() for c in cl]

    qa = list(QA)
    if quick:
        qa = qa[:4]

    recalls = {1: [], 3: [], 5: []}
    mrrs = []
    abstain_ok = 0
    abstain_total = 0
    cite_ok = 0
    cite_total = 0
    ans_ok = 0
    ans_total = 0
    failures = []

    for question, gold_doc, answerable, expected in qa:
        ranked = retrieve(all_chunks, question, top_k=5)
        ranked_ids = [c["chunk_id"].split("#")[0] for c in ranked]

        if answerable:
            gold_ids = [gold_doc]
            for k in recalls:
                recalls[k].append(recall_at_k(ranked_ids, gold_ids, k))
            mrrs.append(mrr(ranked_ids, gold_ids))
        else:
            abstain_total += 1

        # Answer + citation check
        ans = answer_question(question, "eval", all_chunks, top_k=3)
        if not answerable:
            if "couldn't find" in ans["answer"]:
                abstain_ok += 1
            else:
                failures.append({"question": question, "kind": "abstain",
                                 "answer": ans["answer"][:200]})
        else:
            cite_total += 1
            v = verify_citations(ans)
            if v["ok"]:
                cite_ok += 1
            else:
                failures.append({"question": question, "kind": "citation",
                                 "answer": ans["answer"][:200]})
            ans_total += 1
            if expected and expected.lower() in ans["answer"].lower():
                ans_ok += 1
            else:
                failures.append({"question": question, "kind": "answer",
                                 "expected": expected,
                                 "answer": ans["answer"][:200]})

    results = {
        "n_questions": len(qa),
        "n_docs": len(docs),
        "n_answerable": ans_total,
        "n_unanswerable": abstain_total,
        "recall@1": sum(recalls[1]) / len(recalls[1]) if recalls[1] else 0,
        "recall@3": sum(recalls[3]) / len(recalls[3]) if recalls[3] else 0,
        "recall@5": sum(recalls[5]) / len(recalls[5]) if recalls[5] else 0,
        "mrr": sum(mrrs) / len(mrrs) if mrrs else 0,
        "answer_accuracy": ans_ok / ans_total if ans_total else 0,
        "abstention_accuracy": abstain_ok / abstain_total if abstain_total else 0,
        "citation_validity": cite_ok / cite_total if cite_total else 0,
    }
    if failures:
        results["failures"] = failures
    return results


def main():
    quick = "--quick" in sys.argv
    results = evaluate(quick=quick)
    print(json.dumps(results, indent=2))
    out = os.path.join(os.path.dirname(__file__), "rag_results.json")
    with open(out, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nWrote {out}")


if __name__ == "__main__":
    main()
