"""Lightweight markdown rendering for chat bubbles."""

from __future__ import annotations

import html
import re
from urllib.parse import quote

_BOLD = re.compile(r"\*\*(.+?)\*\*")
_ITALIC = re.compile(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)")
_CODE_FENCE = re.compile(r"```(?:\w+)?\n(.*?)```", re.DOTALL)
_INLINE_CODE = re.compile(r"`([^`]+)`")
_WI_REF = re.compile(r"(?<![\w/])#(\d{2,})")
_URL = re.compile(r"(https?://[^\s<]+)")
_ADO_WI = re.compile(r"(https?://dev\.azure\.com/[^\s]+/_workitems/edit/(\d+))", re.I)


def render_chat_markdown(text: str, *, ado_org: str | None = None, ado_project: str | None = None) -> str:
    """Render a constrained markdown subset to safe HTML for chat bubbles."""

    if not text:
        return ""

    escaped = html.escape(text)

    # Fenced code blocks
    def _fence(match: re.Match[str]) -> str:
        body = match.group(1).rstrip("\n")
        return f'<pre class="as-pre mb-2 small"><code>{body}</code></pre>'

    escaped = _CODE_FENCE.sub(_fence, escaped)

    # Split into paragraphs on blank lines
    blocks = re.split(r"\n\s*\n", escaped)
    rendered_blocks: list[str] = []
    for block in blocks:
        lines = block.split("\n")
        if all(line.strip().startswith(("- ", "* ")) or not line.strip() for line in lines if line.strip()):
            items = []
            for line in lines:
                stripped = line.strip()
                if stripped.startswith(("- ", "* ")):
                    items.append(f"<li>{_inline(stripped[2:], ado_org, ado_project)}</li>")
            if items:
                rendered_blocks.append("<ul class='mb-2'>" + "".join(items) + "</ul>")
                continue
        if all(re.match(r"^\d+\.\s+", line.strip()) for line in lines if line.strip()):
            items = []
            for line in lines:
                stripped = line.strip()
                m = re.match(r"^\d+\.\s+(.*)$", stripped)
                if m:
                    items.append(f"<li>{_inline(m.group(1), ado_org, ado_project)}</li>")
            if items:
                rendered_blocks.append("<ol class='mb-2'>" + "".join(items) + "</ol>")
                continue
        if lines and lines[0].startswith("### "):
            rendered_blocks.append(
                f"<h6 class='fw-semibold mb-1'>{_inline(lines[0][4:], ado_org, ado_project)}</h6>"
            )
            rest = "<br>".join(_inline(line, ado_org, ado_project) for line in lines[1:] if line.strip())
            if rest:
                rendered_blocks.append(f"<p class='mb-2'>{rest}</p>")
            continue
        if lines and lines[0].startswith("## "):
            rendered_blocks.append(
                f"<h5 class='fw-semibold mb-1'>{_inline(lines[0][3:], ado_org, ado_project)}</h5>"
            )
            rest = "<br>".join(_inline(line, ado_org, ado_project) for line in lines[1:] if line.strip())
            if rest:
                rendered_blocks.append(f"<p class='mb-2'>{rest}</p>")
            continue
        joined = "<br>".join(_inline(line, ado_org, ado_project) for line in lines)
        rendered_blocks.append(f"<p class='mb-2'>{joined}</p>")

    return "\n".join(rendered_blocks)


def _inline(text: str, ado_org: str | None, ado_project: str | None) -> str:
    text = _INLINE_CODE.sub(r"<code>\1</code>", text)
    text = _BOLD.sub(r"<strong>\1</strong>", text)
    text = _ITALIC.sub(r"<em>\1</em>", text)

    def _url(match: re.Match[str]) -> str:
        url = match.group(1).rstrip(").,;")
        return f'<a href="{url}" target="_blank" rel="noopener">{url}</a>'

    text = _URL.sub(_url, text)

    def _wi(match: re.Match[str]) -> str:
        wi_id = match.group(1)
        if ado_org and ado_project:
            project_seg = quote(ado_project)
            href = f"https://dev.azure.com/{ado_org}/{project_seg}/_workitems/edit/{wi_id}"
            return f'<a href="{href}" target="_blank" rel="noopener">#{wi_id}</a>'
        return f"#{wi_id}"

    return _WI_REF.sub(_wi, text)
