from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
import io


def build_pdf(sections: dict, author: str, institution: str):
    buffer = io.BytesIO()

    doc = SimpleDocTemplate(
        buffer,
        pagesize=letter,
        leftMargin=0.75 * inch,
        rightMargin=0.75 * inch,
        topMargin=1 * inch,
        bottomMargin=1 * inch,
    )

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "Title",
        parent=styles["Title"],
        alignment=1,
        fontSize=24,
        spaceAfter=20,
    )
    body = styles["Normal"]

    story = []

    story.append(Paragraph(sections["title"], title_style))
    story.append(Paragraph(author, styles["Heading2"]))
    story.append(Paragraph(institution, styles["Italic"]))
    story.append(Spacer(1, 20))

    def add_section(name, text):
        story.append(Paragraph(name, styles["Heading1"]))
        for p in text.split("\n"):
            if p.strip():
                story.append(Paragraph(p, body))
        story.append(Spacer(1, 12))

    add_section("Abstract", sections.get("abstract", ""))
    add_section("Introduction", sections.get("introduction", ""))
    add_section("Methodology", sections.get("methodology", ""))
    add_section("Implementation", sections.get("implementation", ""))
    add_section("Results and Evaluation", sections.get("results", ""))
    add_section("Conclusion", sections.get("conclusion", ""))
    add_section("References", sections.get("references", ""))

    doc.build(story)
    buffer.seek(0)
    return buffer
