from shared.llm import chat


def generate_paper(repo_data: dict, analysis: dict, vector_db):
    sections = {}

    title_prompt = f"""
Generate a concise IEEE-style paper title.

Project: {repo_data.get('name')}
Domain: {analysis.get('TARGET_DOMAIN')}
Technologies: {', '.join(analysis.get('KEY_TECHNOLOGIES', [])[:3])}

Return only the title.
"""
    sections["title"] = chat(
        title_prompt,
        model="llama-3.1-8b-instant",
        max_tokens=80,
        temperature=0.2,
    ).strip()

    def section(name, query, words, k=4):
        docs = vector_db.similarity_search(query, k=k)
        context = "\n\n".join(d.page_content for d in docs)
        prompt = f"""
Write an IEEE-style {name} section (~{words} words).

Context:
{context}
"""
        return chat(
            prompt,
            model="llama-3.3-70b-versatile",
            max_tokens=words * 2,
            temperature=0.25,
        )

    sections["abstract"] = section(
        "Abstract",
        "high-level summary and purpose",
        200,
        k=2,
    )

    sections["introduction"] = section(
        "Introduction",
        "problem statement, motivation, contributions",
        600,
    )

    sections["methodology"] = section(
        "Methodology",
        "architecture, algorithms, data flow",
        700,
    )

    sections["implementation"] = section(
        "Implementation",
        "core modules and system design",
        600,
    )

    sections["results"] = section(
        "Results and Evaluation",
        "performance and expected outcomes",
        600,
    )

    sections["conclusion"] = section(
        "Conclusion",
        "summary and future work",
        300,
    )

    refs_prompt = f"""
Generate 10 IEEE-style references relevant to:
{sections['title']}
"""
    sections["references"] = chat(
        refs_prompt,
        model="llama-3.3-70b-versatile",
        max_tokens=400,
        temperature=0.2,
    )

    return sections
