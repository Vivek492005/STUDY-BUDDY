"""
Text Cleaning Module - Normalizes and cleans resume text for processing.
"""
import re
import unicodedata


def clean_text(text):
    """
    Clean and normalize resume text.
    
    Args:
        text: Raw text extracted from resume
    
    Returns:
        str: Cleaned text
    """
    if not text:
        return ""

    # Normalize unicode characters
    text = unicodedata.normalize('NFKD', text)

    # Normalize newlines and whitespace
    text = re.sub(r'\r\n', '\n', text)
    text = re.sub(r'\r', '\n', text)
    text = re.sub(r'[ \t]+', ' ', text)  # Collapse horizontal whitespace
    text = re.sub(r'\n{3,}', '\n\n', text) # Limit excessive newlines

    # Remove special characters but keep useful punctuation.
    # NOTE (2026-10-25): % $ ₹ € £ are kept on purpose — the ATS scorer's
    # quantified-achievement detection needs them. An earlier version
    # stripped them, which silently zeroed the metrics component for
    # every resume (metrics were counted on cleaned text).
    text = re.sub(r'[^\w\s@.,:;/\-+#()&|%$₹€£]', '', text)

    # Fix common encoding issues
    text = text.replace('â€™', "'")
    text = text.replace('â€"', "-")
    text = text.replace('â€œ', '"')
    text = text.replace('â€', '"')

    # Remove extra spaces around punctuation, but NOT inside digit groups:
    # "$240,000" must survive as one number, not become "$240, 000".
    # The lookahead must guard the whole ", spaces, digit" sequence:
    # a plain (?!\d) backtracks past the spaces and still inserts one.
    text = re.sub(r'\s*,(?!\s*\d)\s*', ', ', text)
    text = re.sub(r'\s*([:;])\s*', r'\1 ', text)

    # Remove leading/trailing whitespace
    text = text.strip()

    return text


def normalize_text(text):
    """
    Normalize text for comparison (lowercase, remove extra spaces).
    
    Args:
        text: Text to normalize
    
    Returns:
        str: Normalized text
    """
    if not text:
        return ""
    text = text.lower().strip()
    text = re.sub(r'\s+', ' ', text)
    return text


def extract_email(text):
    """Extract email addresses from text."""
    pattern = r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}'
    emails = re.findall(pattern, text)
    return emails[0] if emails else None


def extract_phone(text):
    """Extract phone numbers from text (India 10-digit + US/intl formats)."""
    pattern = (r'(?:\+?91[-.\s]?)?[6-9]\d{4}[-.\s]?\d{5}'
               r'|(?:\+?\d{1,3}[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}')
    phones = re.findall(pattern, text)
    return phones[0] if phones else None


def extract_links(text):
    """Extract URLs and links from text."""
    pattern = r'https?://[^\s<>"{}|\\^`\[\]]+'
    links = re.findall(pattern, text)
    return links
