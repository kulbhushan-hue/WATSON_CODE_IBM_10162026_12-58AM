"""Best-effort extraction of maintenance details from linked vendor pages."""

from __future__ import annotations

import io
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin, urlparse

import requests
import streamlit as st
from bs4 import BeautifulSoup
from pypdf import PdfReader
from pypdf.errors import PdfReadError

ALLOWED_HOSTS = (
    "ibm.com",
    "broadcom.com",
    "bmc.com",
    "rocketsoftware.com",
    "precisely.com",
)
MAX_DOWNLOAD_BYTES = 8 * 1024 * 1024
MAX_TEXT_CHARS = 750_000
REQUEST_TIMEOUT = (5, 12)
MAX_REDIRECTS = 4

MAINTENANCE_TERMS = re.compile(
    r"\b(ptf|ptfs|usermod|usermods|sysmod|sysmods|apar|solutions? required|maintenance)\b",
    re.IGNORECASE,
)
PREREQUISITE_TERMS = re.compile(
    r"\b(pre[\s-]?requisite|pre[\s-]?requisites|corequisite|corequisites|"
    r"co[\s-]?requisite|co[\s-]?requisites|requires?|required by|"
    r"dependency|dependencies)\b",
    re.IGNORECASE,
)
FIX_ID = re.compile(
    r"\b(?:[A-Z]{1,3}\d{4,8}|PTF\s*[A-Z0-9-]{4,20}|"
    r"USERMOD\s*[A-Z0-9-]{3,20}|SYSMOD\s*[A-Z0-9-]{3,20})\b",
    re.IGNORECASE,
)


def _allowed_url(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        return False
    if parsed.port not in (None, 443):
        return False
    host = parsed.hostname.lower().rstrip(".")
    return any(host == allowed or host.endswith(f".{allowed}") for allowed in ALLOWED_HOSTS)


def _download(url: str) -> tuple[bytes, str, str]:
    current = url
    headers = {"User-Agent": "zOS-Compatibility-Dashboard/1.0 (vendor support lookup)"}
    for redirect_count in range(MAX_REDIRECTS + 1):
        if not _allowed_url(current):
            return b"", "", "Blocked non-approved vendor URL or redirect"
        try:
            with requests.get(
                current,
                headers=headers,
                timeout=REQUEST_TIMEOUT,
                stream=True,
                allow_redirects=False,
            ) as response:
                if response.is_redirect or response.is_permanent_redirect:
                    location = response.headers.get("Location", "")
                    if not location or redirect_count == MAX_REDIRECTS:
                        return b"", "", "Too many or invalid redirects"
                    current = urljoin(current, location)
                    continue
                if response.status_code != 200:
                    return b"", "", f"Vendor page returned HTTP {response.status_code}"

                chunks = []
                size = 0
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    size += len(chunk)
                    if size > MAX_DOWNLOAD_BYTES:
                        return b"", "", "Vendor page exceeds the 8 MB extraction limit"
                    chunks.append(chunk)
                return (
                    b"".join(chunks),
                    response.headers.get("Content-Type", "").lower(),
                    "Fetched",
                )
        except requests.RequestException as exc:
            return b"", "", f"Fetch failed: {exc.__class__.__name__}"
    return b"", "", "Too many redirects"


def _html_lines(payload: bytes) -> str:
    soup = BeautifulSoup(payload, "html.parser")
    for element in soup(["script", "style", "noscript", "svg"]):
        element.decompose()

    lines = []
    for element in soup.find_all(
        ["h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "tr", "pre"]
    ):
        if element.name == "tr":
            cells = []
            for cell in element.find_all(["th", "td"], recursive=False):
                value = cell.get_text(" ", strip=True)
                links = [
                    f"{anchor.get_text(' ', strip=True)} [{anchor.get('href')}]"
                    for anchor in cell.find_all("a", href=True)
                ]
                if links:
                    value = " ".join([value, *links]).strip()
                cells.append(value)
            line = " | ".join(cells)
        else:
            line = element.get_text(" ", strip=True)
        line = re.sub(r"\s+", " ", line).strip()
        if not line:
            continue
        if element.name and re.fullmatch(r"h[1-6]", element.name):
            line = f"## {line}"
        if not lines or lines[-1] != line:
            lines.append(line)
    for element in soup.find_all(id=True):
        identifier = str(element.get("id", "")).strip()
        if not identifier:
            continue
        context = element.parent.get_text(" ", strip=True) if element.parent else element.get_text(" ", strip=True)
        context = re.sub(r"\s+", " ", context).strip()
        if context:
            lines.append(f"@@ID:{identifier}@@ {context}")
    return "\n".join(lines)[:MAX_TEXT_CHARS]


def _pdf_text(payload: bytes) -> str:
    reader = PdfReader(io.BytesIO(payload), strict=False)
    pages = []
    for page in reader.pages[:100]:
        text = page.extract_text() or ""
        if text:
            pages.append(text)
        if sum(map(len, pages)) >= MAX_TEXT_CHARS:
            break
    return "\n".join(pages)[:MAX_TEXT_CHARS]


def _plain_text(payload: bytes, content_type: str, url: str) -> tuple[str, str]:
    is_pdf = "pdf" in content_type or urlparse(url).path.lower().endswith(".pdf")
    try:
        if is_pdf:
            text, status = _pdf_text(payload), "Fetched PDF"
        elif "html" in content_type or not content_type or payload.lstrip().startswith(b"<"):
            text, status = _html_lines(payload), "Fetched HTML"
        elif "text/" in content_type or "json" in content_type:
            text, status = payload.decode("utf-8", errors="replace")[:MAX_TEXT_CHARS], "Fetched text"
        else:
            return "", f"Unsupported page content type: {content_type or 'unknown'}"
        if not text.strip():
            return "", f"{status}, but no readable static text"
        return text, status
    except (ValueError, OSError, EOFError, PdfReadError) as exc:
        return "", f"Could not parse vendor document: {exc.__class__.__name__}"


@st.cache_data(ttl=6 * 60 * 60, max_entries=64, show_spinner=False)
def fetch_vendor_page(url: str) -> tuple[str, str]:
    """Fetch one approved source page; successful and failed results cache for six hours."""
    if not _allowed_url(url):
        return "", "Blocked non-approved vendor URL"
    payload, content_type, status = _download(url)
    if status != "Fetched":
        return "", status
    text, parse_status = _plain_text(payload, content_type, url)
    return text, parse_status


def _normalise(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()


def _matching_sections(page_text: str, product: str, vrm: str) -> list[str]:
    target = _normalise(product)
    if not target:
        return []
    lines = [line.strip() for line in page_text.splitlines() if line.strip()]
    version = _normalise(vrm)
    matching_rows = [
        line for line in lines
        if "|" in line and target in _normalise(line)
        and (not version or version in _normalise(line))
    ]
    id_sections = {
        match.group(1): line.split(" ", 1)[1] if " " in line else ""
        for line in lines
        if (match := re.match(r"@@ID:([A-Za-z0-9_.:-]+)@@(?:\s+(.*))?$", line))
    }
    if matching_rows:
        sections = []
        for line in matching_rows:
            section = [line]
            for identifier in re.findall(r"#([A-Za-z0-9_.:-]+)", line):
                if identifier in id_sections:
                    section.append(id_sections[identifier])
            sections.append("\n".join(section))
        return sections

    matching = [
        i for i, line in enumerate(lines)
        if not line.startswith("@@ID:")
        and target in _normalise(line.removeprefix("##"))
    ]
    if not matching:
        return []
    version_matches = [i for i in matching if version and version in _normalise(lines[i])]
    starts = version_matches or matching
    sections = []
    for start in starts:
        end = start + 1
        while end < len(lines) and not lines[end].startswith("## "):
            end += 1
        sections.append("\n".join(lines[start:end]))
    return sections


def _details_from_section(section: str) -> str:
    lines = [line.strip() for line in section.splitlines() if line.strip()]
    maintenance = []
    prerequisites = []
    for index, line in enumerate(lines):
        normal_line = line.removeprefix("## ").strip()
        if MAINTENANCE_TERMS.search(normal_line) or FIX_ID.search(normal_line):
            maintenance.append(normal_line)
            # Some vendor matrices put the fix IDs on the line after the label.
            for following in lines[index + 1:index + 4]:
                candidate = following.removeprefix("## ").strip()
                if candidate.startswith("## "):
                    break
                if FIX_ID.search(candidate) or MAINTENANCE_TERMS.search(candidate):
                    maintenance.append(candidate)
        if PREREQUISITE_TERMS.search(normal_line):
            prerequisites.append(normal_line)
            prerequisites.extend(
                following.removeprefix("## ").strip()
                for following in lines[index + 1:index + 3]
                if FIX_ID.search(following) or PREREQUISITE_TERMS.search(following)
            )

    maintenance = list(dict.fromkeys(maintenance))
    prerequisites = list(dict.fromkeys(prerequisites))
    sections = []
    if maintenance:
        sections.append("PTF / USERMOD / SYSMOD: " + " | ".join(maintenance))
    if prerequisites:
        sections.append("Prerequisite / corequisite: " + " | ".join(prerequisites))
    return "\n".join(sections)[:4000]


def extract_row_details(page_text: str, page_status: str, product: str, vrm: str) -> str:
    if not page_text:
        return f"{page_status}. Verify the vendor source manually."
    sections = _matching_sections(page_text, product, vrm)
    if not sections:
        return (
            f"{page_status}; no product-specific section matched {product} {vrm}. "
            "Verify the vendor source manually."
        )
    details = list(dict.fromkeys(
        detail for section in sections
        if (detail := _details_from_section(section))
    ))
    if not details:
        return (
            f"{page_status}; no matching PTF, USERMOD, SYSMOD, prerequisite, "
            "or corequisite details found. This does not mean none are required."
        )
    return "\n".join(details)


def enrich_vendor_details(df):
    """Fetch each distinct linked vendor page once and annotate each data row."""
    result = df.copy()
    urls = sorted({
        str(url).strip()
        for url in result["link"]
        if str(url).strip()
    })
    pages: dict[str, tuple[str, str]] = {}
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {pool.submit(fetch_vendor_page, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            pages[url] = future.result()

    details = []
    for row in result.itertuples(index=False):
        url = str(getattr(row, "link", "")).strip()
        product = str(getattr(row, "product", ""))
        vrm = str(getattr(row, "vrm", ""))
        if not url:
            details.append("No vendor URL supplied. Verify maintenance requirements manually.")
            continue
        page_text, page_status = pages[url]
        details.append(extract_row_details(page_text, page_status, product, vrm))
    result["maintenance_details"] = details
    result["maintenance_details"] = details
    readable = sum(bool(text.strip()) and status.startswith("Fetched") for text, status in pages.values())
    return result, {
        "urls_total": len(urls),
        "urls_readable": readable,
        "urls_unavailable": len(urls) - readable,
    }
