"""
Shared utilities for Feishu message formatting.
Converts Markdown to Feishu rich text (post) format.
"""

import json


def markdown_to_rich_text(text):
    """Convert markdown text to Feishu rich text paragraphs.

    Supports: headings (#, ##, ###), code blocks (```), inline code (`),
    bold (**text**), and plain text.

    Returns a list of paragraphs, each a list of element dicts.
    """
    lines = text.split("\n")
    paragraphs = []
    in_code_block = False
    code_lines = []

    for line in lines:
        if line.strip().startswith("```"):
            if in_code_block:
                paragraphs.append([{"tag": "text", "text": "\n".join(code_lines), "style": ["code_block"]}])
                code_lines = []
                in_code_block = False
            else:
                in_code_block = True
            continue

        if in_code_block:
            code_lines.append(line)
            continue

        if not line.strip():
            paragraphs.append([{"tag": "text", "text": ""}])
            continue

        if line.startswith("### "):
            paragraphs.append([{"tag": "text", "text": line[4:], "style": ["bold"]}])
        elif line.startswith("## "):
            paragraphs.append([{"tag": "text", "text": line[3:], "style": ["bold"]}])
        elif line.startswith("# "):
            paragraphs.append([{"tag": "text", "text": line[2:], "style": ["bold"]}])
        else:
            paragraphs.append(_parse_inline(line))

    if in_code_block and code_lines:
        paragraphs.append([{"tag": "text", "text": "\n".join(code_lines), "style": ["code_block"]}])

    return paragraphs


def _parse_inline(line):
    """Parse inline markdown: **bold**, `code`, regular text."""
    elements = []
    i = 0
    buf = ""

    while i < len(line):
        if line[i] == "`" and not line[i:].startswith("```"):
            if buf:
                elements.append({"tag": "text", "text": buf})
                buf = ""
            end = line.find("`", i + 1)
            if end != -1:
                elements.append({"tag": "text", "text": line[i + 1:end], "style": ["code_inline"]})
                i = end + 1
            else:
                buf += line[i]
                i += 1
        elif line[i:i + 2] == "**":
            if buf:
                elements.append({"tag": "text", "text": buf})
                buf = ""
            end = line.find("**", i + 2)
            if end != -1:
                elements.append({"tag": "text", "text": line[i + 2:end], "style": ["bold"]})
                i = end + 2
            else:
                buf += line[i]
                i += 1
        else:
            buf += line[i]
            i += 1

    if buf:
        elements.append({"tag": "text", "text": buf})
    return elements if elements else [{"tag": "text", "text": ""}]


def build_post_content(title, text):
    """Build a Feishu post (rich text) message content dict."""
    paragraphs = markdown_to_rich_text(text)
    return {"zh_cn": {"title": title, "content": paragraphs}}
