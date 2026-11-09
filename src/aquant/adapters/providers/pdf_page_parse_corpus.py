"""Read-only candidate loader for a parser-fingerprinted PDF page corpus.

This module never builds or updates a corpus. The caller supplies the reviewed
manifest SHA-256 anchor and the current parser/dependency fingerprint.
"""
from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path
import re
from threading import Lock
import zlib
from typing import Any

from .pdf_page_parse_corpus_anchor import MANIFEST_SHA256


SCHEMA = "aquant.pdf-pure-pages-corpus.v1"
MANIFEST_FILENAME = "manifest.json"
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_MANIFEST_FIELDS = {"schema", "parserFingerprint", "entries"}
_ENTRY_FIELDS = {"pdfBytes", "compressedSha256", "pageCount"}


class PageParseCorpusError(RuntimeError):
    """A pinned corpus or one of its indexed files is inconsistent."""


class PageParseCorpus:
    """Load immutable parsed pages from a SHA-pinned, read-only corpus.

    The manifest is read and validated once during construction. The manifest
    filename is fixed, and each gzip filename is constructed from the validated
    full PDF SHA-256. No file paths are taken from manifest content.
    """

    def __init__(self, root: str | Path,
                 expected_manifest_sha256: str) -> None:
        if (not isinstance(expected_manifest_sha256, str) or
                _SHA256_PATTERN.fullmatch(expected_manifest_sha256) is None):
            raise ValueError("expected_manifest_sha256 must be a lowercase SHA-256 hex string")
        try:
            self._root = Path(root).resolve(strict=True)
        except OSError as exc:
            raise PageParseCorpusError("corpus root is unavailable") from exc
        if not self._root.is_dir():
            raise PageParseCorpusError("corpus root is not a directory")

        manifest_path = self._root / MANIFEST_FILENAME
        manifest_bytes = self._read_confined(manifest_path, "manifest")
        actual_manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
        if actual_manifest_sha256 != expected_manifest_sha256:
            raise PageParseCorpusError(
                "manifest SHA-256 does not match the expected anchor")
        try:
            manifest = json.loads(manifest_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PageParseCorpusError("manifest is not valid UTF-8 JSON") from exc

        self._parser_fingerprint, self._entries = self._validate_manifest(manifest)

    def _read_confined(self, path: Path, description: str) -> bytes:
        if path.is_symlink():
            raise PageParseCorpusError(f"{description} path must not be a symlink")
        try:
            resolved = path.resolve(strict=True)
        except FileNotFoundError as exc:
            raise PageParseCorpusError(f"{description} file is missing") from exc
        except OSError as exc:
            raise PageParseCorpusError(f"{description} path cannot be resolved") from exc
        try:
            resolved.relative_to(self._root)
        except ValueError as exc:
            raise PageParseCorpusError(f"{description} path escapes the corpus root") from exc
        if not resolved.is_file():
            raise PageParseCorpusError(f"{description} path is not a regular file")
        try:
            return resolved.read_bytes()
        except OSError as exc:
            raise PageParseCorpusError(f"{description} file cannot be read") from exc

    @staticmethod
    def _validate_manifest(manifest: Any) -> tuple[str, dict[str, tuple[int, str, int]]]:
        if not isinstance(manifest, dict):
            raise PageParseCorpusError("manifest must be a JSON object")
        if set(manifest) != _MANIFEST_FIELDS:
            raise PageParseCorpusError("manifest fields do not match the fixed schema")
        if manifest["schema"] != SCHEMA:
            raise PageParseCorpusError("manifest schema is not supported")
        parser_fingerprint = manifest["parserFingerprint"]
        if not isinstance(parser_fingerprint, str) or not parser_fingerprint:
            raise PageParseCorpusError("manifest parserFingerprint must be a nonempty string")
        entries = manifest["entries"]
        if not isinstance(entries, dict):
            raise PageParseCorpusError("manifest entries must be a JSON object")

        validated: dict[str, tuple[int, str, int]] = {}
        for pdf_sha256, entry in entries.items():
            if (not isinstance(pdf_sha256, str) or
                    _SHA256_PATTERN.fullmatch(pdf_sha256) is None):
                raise PageParseCorpusError("manifest entry key must be a full lowercase PDF SHA-256")
            if not isinstance(entry, dict):
                raise PageParseCorpusError(f"entry {pdf_sha256} must be a JSON object")
            if set(entry) != _ENTRY_FIELDS:
                raise PageParseCorpusError(
                    f"entry {pdf_sha256} fields must be exactly pdfBytes, "
                    "compressedSha256, and pageCount")
            pdf_bytes = entry["pdfBytes"]
            page_count = entry["pageCount"]
            compressed_sha256 = entry["compressedSha256"]
            if isinstance(pdf_bytes, bool) or not isinstance(pdf_bytes, int) or pdf_bytes < 0:
                raise PageParseCorpusError(f"entry {pdf_sha256} pdfBytes must be a nonnegative integer")
            if isinstance(page_count, bool) or not isinstance(page_count, int) or page_count < 0:
                raise PageParseCorpusError(f"entry {pdf_sha256} pageCount must be a nonnegative integer")
            if (not isinstance(compressed_sha256, str) or
                    _SHA256_PATTERN.fullmatch(compressed_sha256) is None):
                raise PageParseCorpusError(
                    f"entry {pdf_sha256} compressedSha256 must be a full lowercase SHA-256")
            validated[pdf_sha256] = (pdf_bytes, compressed_sha256, page_count)
        return parser_fingerprint, validated

    def load_pages(self, body: bytes,
                   parser_fingerprint: str) -> tuple[str, ...] | None:
        """Return exact cached pages, or ``None`` for an ordinary cache miss.

        Missing source SHA values, body length mismatches, and parser
        fingerprint changes are misses so the caller can invoke its normal
        parser. Corrupt or missing files referenced by a matching entry raise
        ``PageParseCorpusError`` and are not silently treated as misses.
        """
        if not isinstance(body, bytes):
            raise TypeError("body must be bytes")
        if not isinstance(parser_fingerprint, str):
            raise TypeError("parser_fingerprint must be str")
        if parser_fingerprint != self._parser_fingerprint:
            return None

        pdf_sha256 = hashlib.sha256(body).hexdigest()
        entry = self._entries.get(pdf_sha256)
        if entry is None:
            return None
        pdf_bytes, expected_compressed_sha256, expected_page_count = entry
        if pdf_bytes != len(body):
            return None

        # The filename is derived only from the validated digest. Manifest
        # content cannot provide a path or filename.
        data_path = self._root / f"{pdf_sha256}.json.gz"
        compressed = self._read_confined(data_path, f"data {pdf_sha256}")
        actual_compressed_sha256 = hashlib.sha256(compressed).hexdigest()
        if actual_compressed_sha256 != expected_compressed_sha256:
            raise PageParseCorpusError(
                f"compressed data SHA-256 mismatch for {pdf_sha256}")
        try:
            page_json = gzip.decompress(compressed)
        except (OSError, EOFError, zlib.error) as exc:
            raise PageParseCorpusError(f"gzip data is corrupt for {pdf_sha256}") from exc
        try:
            pages = json.loads(page_json.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PageParseCorpusError(f"page JSON is corrupt for {pdf_sha256}") from exc
        if not isinstance(pages, list) or any(not isinstance(page, str) for page in pages):
            raise PageParseCorpusError(f"page JSON shape is invalid for {pdf_sha256}")
        if len(pages) != expected_page_count:
            raise PageParseCorpusError(
                f"pageCount mismatch for {pdf_sha256}: expected "
                f"{expected_page_count}, got {len(pages)}")
        return tuple(pages)


_corpus: PageParseCorpus | None = None
_corpus_lock = Lock()


def load_pages(body: bytes, *, parser_fingerprint: str) -> tuple[str, ...] | None:
    """Return matching immutable pages, or ``None`` when this build has no corpus."""
    if MANIFEST_SHA256 is None:
        return None

    global _corpus
    if _corpus is None:
        with _corpus_lock:
            if _corpus is None:
                _corpus = PageParseCorpus(
                    root=Path(__file__).with_name("_pdf_page_corpus_data"),
                    expected_manifest_sha256=MANIFEST_SHA256,
                )
    return _corpus.load_pages(body, parser_fingerprint=parser_fingerprint)
