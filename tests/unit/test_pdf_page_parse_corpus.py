"""Pure page-cache reuse must not weaken byte or parser identity checks."""
import gzip
import hashlib
import json

import pytest

from aquant.adapters.providers import pdf_page_parse_corpus as pages


def _corpus(tmp_path):
    body = b"synthetic PDF bytes for a pure-cache contract test"
    digest = hashlib.sha256(body).hexdigest()
    compressed = gzip.compress(json.dumps(["synthetic page"]).encode(), mtime=0)
    (tmp_path / f"{digest}.json.gz").write_bytes(compressed)
    manifest = {
        "schema": pages.SCHEMA,
        "parserFingerprint": "synthetic-parser-v1",
        "entries": {digest: {
            "pdfBytes": len(body), "pageCount": 1,
            "compressedSha256": hashlib.sha256(compressed).hexdigest(),
        }},
    }
    raw = json.dumps(manifest).encode()
    (tmp_path / "manifest.json").write_bytes(raw)
    return body, digest, hashlib.sha256(raw).hexdigest()


def test_matching_source_and_parser_reuses_exact_page_tuple(tmp_path):
    body, _, anchor = _corpus(tmp_path)
    cache = pages.PageParseCorpus(tmp_path, anchor)
    assert cache.load_pages(body, "synthetic-parser-v1") == ("synthetic page",)


@pytest.mark.parametrize("mismatch", ["parser", "source"])
def test_source_or_parser_change_falls_back_to_normal_parser(tmp_path, mismatch):
    body, _, anchor = _corpus(tmp_path)
    cache = pages.PageParseCorpus(tmp_path, anchor)
    assert cache.load_pages(body + (b"changed" if mismatch == "source" else b""),
                            "changed" if mismatch == "parser" else "synthetic-parser-v1") is None


def test_tampered_page_data_is_an_error_instead_of_cached_success(tmp_path):
    body, digest, anchor = _corpus(tmp_path)
    (tmp_path / f"{digest}.json.gz").write_bytes(gzip.compress(b'["tampered"]'))
    with pytest.raises(pages.PageParseCorpusError, match="SHA-256"):
        pages.PageParseCorpus(tmp_path, anchor).load_pages(body, "synthetic-parser-v1")


def test_untrusted_manifest_is_rejected(tmp_path):
    _corpus(tmp_path)
    with pytest.raises(pages.PageParseCorpusError, match="anchor"):
        pages.PageParseCorpus(tmp_path, "0" * 64)


def test_checkout_without_private_corpus_uses_normal_parser(monkeypatch):
    monkeypatch.setattr(pages, "MANIFEST_SHA256", None)
    assert pages.load_pages(b"synthetic bytes", parser_fingerprint="synthetic-parser-v1") is None
