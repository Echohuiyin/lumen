"""Lightweight knowledge base search for vendor/community fixes."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional

import httpx

from .config import AppConfig


LOG = logging.getLogger(__name__)

# Words too generic to be useful in Red Hat KB queries.
_GENERIC_WORDS = frozenset({
    "a", "an", "the", "in", "on", "at", "to", "for", "of", "and", "or",
    "is", "was", "are", "with", "from", "by", "not", "no", "but",
    "null", "double", "free", "error", "bug", "crash", "kernel", "issue",
    "problem", "failure", "invalid",
})


@dataclass
class KnowledgeMatch:
    """A matched advisory or article from the knowledge base."""

    score: float
    title: str
    summary: str
    source: Path | str
    metadata: Optional[dict] = None

    def format_brief(self) -> str:
        data = {
            "title": self.title,
            "summary": self.summary,
            "score": round(self.score, 3),
            "source": str(self.source),
            "metadata": self.metadata or {},
        }
        return json.dumps(data, ensure_ascii=False)


class KnowledgeBase:
    """Minimal filesystem-backed knowledge base."""

    def __init__(self, config: Optional[AppConfig] = None) -> None:
        self.config = config or AppConfig()
        self.paths = [p for p in self.config.knowledge_base_paths if p.exists()]
        if not self.paths:
            LOG.info("No knowledge base directories configured")

        base_url = (self.config.redhat_kb_base_url or "").strip()
        self._redhat_base_url = base_url or None
        if self._redhat_base_url:
            LOG.info("Red Hat KB lookups enabled via %s", self._redhat_base_url)
        else:
            LOG.info("Red Hat KB lookups disabled (no base URL configured)")

    # ------------------------------------------------------------------
    def _candidate_files(self) -> Iterable[Path]:
        for root in self.paths:
            yield from root.rglob("*.md")
            yield from root.rglob("*.txt")
            yield from root.rglob("*.json")

    # ------------------------------------------------------------------
    def search(self, query_terms: List[str], limit: int = 5) -> List[KnowledgeMatch]:
        query_terms = [term.lower() for term in query_terms if term and term.strip()]
        if not query_terms:
            return []

        matches: List[KnowledgeMatch] = []

        # Local filesystem search
        if self.paths:
            for candidate in self._candidate_files():
                try:
                    payload = candidate.read_text(encoding="utf-8")
                except OSError as exc:  # pragma: no cover - filesystem dependent
                    LOG.warning("Failed to read %s: %s", candidate, exc)
                    continue

                lowered = payload.lower()
                hits = sum(lowered.count(term) for term in query_terms)
                if hits == 0:
                    continue

                title = candidate.stem.replace("_", " ")
                summary = payload.splitlines()[0][:240] if payload else ""
                matches.append(
                    KnowledgeMatch(
                        score=float(hits),
                        title=title,
                        summary=summary,
                        source=candidate,
                    )
                )

        # Remote Red Hat KB search
        matches.extend(self._search_redhat_kb(query_terms, limit))

        if not matches:
            return []

        matches.sort(key=lambda item: item.score, reverse=True)
        return matches[:limit]

    # ------------------------------------------------------------------
    @staticmethod
    def _build_query(query_terms: List[str]) -> str:
        """Build a Solr-friendly query string from raw query terms.

        Strategy:
        1. Multi-word terms are quoted to preserve phrase semantics.
        2. Single words that look like kernel symbols (contain _ or .)
           are kept as-is — they are high-signal.
        3. Generic / low-signal words are dropped.
        4. Terms are joined with OR so the API returns partial matches
           (the server scores relevance; more matching terms = higher score).
        """
        parts: List[str] = []
        seen: set[str] = set()

        for raw in query_terms:
            raw = raw.strip()
            if not raw:
                continue

            words = raw.split()

            if len(words) > 1:
                # Multi-word phrase — quote it for exact phrase matching,
                # and also add individual significant words as OR terms.
                phrase = " ".join(words)
                if phrase not in seen:
                    parts.append(f'"{phrase}"')
                    seen.add(phrase)
                for w in words:
                    wl = w.lower()
                    if wl not in _GENERIC_WORDS and wl not in seen and len(wl) > 2:
                        parts.append(w)
                        seen.add(wl)
            else:
                wl = raw.lower()
                if wl in seen:
                    continue
                # Drop generic words, but keep kernel symbols (contain _ or .)
                if wl in _GENERIC_WORDS and "_" not in raw and "." not in raw:
                    continue
                parts.append(raw)
                seen.add(wl)

        return " OR ".join(parts) if parts else " ".join(query_terms)

    # ------------------------------------------------------------------
    def _query_redhat_kb(
        self, query: str, rows: int
    ) -> List[dict]:
        """Execute a single Red Hat KB API query, return the docs list."""
        params = {
            "q": query,
            "rows": str(rows),
            "fl": "*,score",
        }

        try:
            response = httpx.get(
                self._redhat_base_url,
                params=params,
                headers={"Accept": "application/json"},
                timeout=self.config.redhat_kb_timeout_seconds,
            )
            response.raise_for_status()
        except (httpx.HTTPError, OSError) as exc:  # pragma: no cover - network dependent
            LOG.warning("Red Hat KB lookup failed: %s", exc)
            return []

        try:
            payload = response.json()
        except ValueError as exc:  # pragma: no cover - depends on remote API
            LOG.warning("Red Hat KB returned non-JSON payload: %s", exc)
            return []

        docs: List[dict] = []
        if isinstance(payload, dict):
            response_block = payload.get("response")
            if isinstance(response_block, dict):
                items = response_block.get("docs")
                if isinstance(items, list):
                    docs = items
            if not docs:
                alt_docs = payload.get("docs") or payload.get("result")
                if isinstance(alt_docs, list):
                    docs = alt_docs

        return docs

    # ------------------------------------------------------------------
    @staticmethod
    def _as_text(value: object) -> str:
        """Coerce an API field value to a plain string."""
        if isinstance(value, str):
            return value
        if isinstance(value, (list, tuple)):
            return " ".join(str(item) for item in value if item)
        if value is None:
            return ""
        return str(value)

    # ------------------------------------------------------------------
    @staticmethod
    def _extract_title(doc: dict) -> str:
        """Extract the best title from a Red Hat KB document."""
        raw = (
            doc.get("title")
            or doc.get("publishedTitle")
            or doc.get("allTitle")
            or doc.get("documentTitle")
        )
        title = KnowledgeBase._as_text(raw)
        return title if title else "Red Hat KB Article"

    # ------------------------------------------------------------------
    @staticmethod
    def _extract_summary(doc: dict) -> str:
        """Extract the best summary from a Red Hat KB document."""
        raw = (
            doc.get("summary")
            or doc.get("publishedAbstract")
            or doc.get("abstract")
            or doc.get("snippet")
            or doc.get("issue")
            or doc.get("solution_rootcause")
        )
        return KnowledgeBase._as_text(raw)[:500]

    # ------------------------------------------------------------------
    @staticmethod
    def _extract_url(doc: dict) -> str:
        """Extract the canonical URL from a Red Hat KB document."""
        url = (
            doc.get("view_uri")
            or doc.get("view_uri_browse")
            or doc.get("uri")
            or doc.get("link")
            or doc.get("url")
            or doc.get("path")
        )
        if isinstance(url, str) and url.startswith("/"):
            return f"https://access.redhat.com{url}"
        return url or ""

    # ------------------------------------------------------------------
    @staticmethod
    def _extract_score(doc: dict) -> float:
        """Extract or infer a relevance score from a Red Hat KB document."""
        score_raw = doc.get("score")
        if score_raw is None:
            score_raw = (
                doc.get("relevance")
                or doc.get("boostBaseVersion")
                or doc.get("boostProduct")
                or doc.get("caseCount")
                or doc.get("caseCount_365")
                or 1.0
            )
        try:
            return float(score_raw)
        except (TypeError, ValueError):
            return 1.0

    # ------------------------------------------------------------------
    @staticmethod
    def _extract_metadata(doc: dict) -> dict:
        """Build a metadata dict from a Red Hat KB document."""
        return {
            "id": doc.get("id") or doc.get("documentId") or doc.get("solution.id"),
            "product": doc.get("product"),
            "created": doc.get("created"),
            "createdDate": doc.get("createdDate"),
            "last_modified": doc.get("last_modified") or doc.get("lastModifiedDate"),
            "publication_state": doc.get("publication_state"),
            "requires_subscription": doc.get("requires_subscription"),
            "access_state": doc.get("accessState"),
        }

    # ------------------------------------------------------------------
    def _parse_doc(self, doc: dict) -> Optional[KnowledgeMatch]:
        """Parse a single Red Hat KB API doc into a KnowledgeMatch."""
        if not isinstance(doc, dict):
            return None

        return KnowledgeMatch(
            score=self._extract_score(doc),
            title=self._extract_title(doc),
            summary=self._extract_summary(doc),
            source=self._extract_url(doc) or "Red Hat KB",
            metadata=self._extract_metadata(doc),
        )

    # ------------------------------------------------------------------
    def _fallback_query(self, query_terms: List[str], rows: int) -> List[dict]:
        """Try a symbol-only fallback query when the primary returns nothing."""
        symbol_terms = [
            t for t in query_terms
            if "_" in t or "." in t or re.search(r"[A-Z].*[a-z]|[a-z].*[A-Z]", t)
        ]
        if symbol_terms and symbol_terms != query_terms:
            fallback_q = " OR ".join(
                f'"{t}"' if " " in t else t for t in symbol_terms
            )
            LOG.debug("Red Hat KB fallback query: %s", fallback_q)
            return self._query_redhat_kb(fallback_q, rows)
        return []

    # ------------------------------------------------------------------
    def _search_redhat_kb(self, query_terms: List[str], limit: int) -> List[KnowledgeMatch]:
        if not self._redhat_base_url:
            return []

        rows = min(limit, self.config.redhat_kb_max_results)

        # --- Primary query: OR-joined with phrases preserved ---
        query = self._build_query(query_terms)
        LOG.debug("Red Hat KB query: %s", query)
        docs = self._query_redhat_kb(query, rows)

        # --- Fallback: if zero results, pick only symbol-like terms ---
        if not docs:
            docs = self._fallback_query(query_terms, rows)

        # --- Parse docs into KnowledgeMatch objects ---
        matches: List[KnowledgeMatch] = []
        for doc in docs:
            match = self._parse_doc(doc)
            if match is not None:
                matches.append(match)

        return matches


__all__ = ["KnowledgeBase", "KnowledgeMatch"]
