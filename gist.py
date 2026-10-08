"""Deterministic HTML extraction and bounded gist input preparation."""
from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.request import Request, urlopen

MAX_INPUT_CHARS = 12000
PROMPT_VERSION = "gist_v2"
GIST_PROMPT = """You create a neutral, factual display headline and gist of one insurance-related source item for rapid human scanning.
Use only the supplied source metadata and text. Do not judge interest, score, rank, recommend, or suggest promotion.
Do not invent facts. If a field is unsupported, omit it or say unclear.
Return only valid JSON with exactly these string fields: {"display_headline":"...","gist":"..."}
The display_headline should usually be 8-18 words and state the concrete event, action, ruling, allegation, proposal, or change. Identify the important actor when known, include useful insurance or regulatory context, and preserve central dollar amounts, injuries, penalties, coverage issues, or unusual conduct. Avoid generic agency language, clickbait, interpretation, outrage, significance judgments, and speculation.
The gist should be 50-100 words in this compact format:
What happened: [concrete event]
Who/what is involved: [people, entities, or subject]
Stakes: [money, legal, regulatory, consumer, or operational consequence if supported]
Notable detail: [one useful specific detail if supported]

SOURCE ITEM:
"""


class _ArticleParser(HTMLParser):
    REMOVE = {"script", "style", "nav", "header", "footer", "aside", "form", "noscript", "svg"}
    BLOCK = {"p", "div", "article", "section", "h1", "h2", "h3", "h4", "li", "blockquote", "br"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.stack: list[str] = []
        self.in_article = False

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        self.stack.append(tag)
        attrs_map = dict(attrs)
        if tag == "article" or "article" in (attrs_map.get("class") or "").lower() or attrs_map.get("role") == "main":
            self.in_article = True
        if tag in self.BLOCK and self.stack:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in self.BLOCK:
            self.parts.append("\n")
        if self.stack:
            self.stack.pop()
        if tag == "article" and not any(x == "article" for x in self.stack):
            self.in_article = False

    def handle_data(self, data):
        if not any(tag in self.REMOVE for tag in self.stack):
            self.parts.append(data)

    def text(self) -> str:
        value = html.unescape("".join(self.parts))
        value = re.sub(r"[ \t\r\f\v]+", " ", value)
        return re.sub(r"\n\s*\n+", "\n", value).strip()


def extract_html_text(content: str) -> tuple[str, str]:
    parser = _ArticleParser()
    parser.feed(content)
    text = parser.text()
    if not text:
        raise ValueError("empty extracted text")
    method = "html_article" if parser.in_article or re.search(r"<article\b", content, re.I) else "html_body"
    return text, method


def fetch_url(url: str, *, timeout: int = 20) -> str:
    request = Request(url, headers={"User-Agent": "StoriesDashboard/3.0"})
    with urlopen(request, timeout=timeout) as response:
        content_type = response.headers.get_content_type()
        if content_type not in {"text/html", "application/xhtml+xml"}:
            raise ValueError(f"unsupported content type: {content_type}")
        return response.read(2_000_000).decode(response.headers.get_content_charset() or "utf-8", errors="replace")


@dataclass(frozen=True)
class BoundedInput:
    input_text: str
    extracted_char_count: int
    input_char_count: int


def build_bounded_input(item: dict, extracted_text: str, max_chars: int = MAX_INPUT_CHARS) -> BoundedInput:
    metadata = "\n".join(str(item.get(key) or "") for key in ("headline", "source_name", "published_at", "raw_url"))
    value = (metadata + "\n\n" + extracted_text)[:max_chars]
    return BoundedInput(value, len(extracted_text), len(value))


def prompt_for(input_text: str) -> str:
    return GIST_PROMPT + input_text


def parse_summary_output(output: str) -> dict[str, str]:
    """Parse the model's single-call JSON, tolerating a markdown code fence."""
    value = output.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)\s*```", value, re.I | re.S)
    if fenced:
        value = fenced.group(1).strip()
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {"display_headline": "", "gist": output.strip()}
    if not isinstance(parsed, dict):
        raise ValueError("summarizer output must be a JSON object")
    return {
        "display_headline": str(parsed.get("display_headline") or "").strip(),
        "gist": str(parsed.get("gist") or "").strip(),
    }
