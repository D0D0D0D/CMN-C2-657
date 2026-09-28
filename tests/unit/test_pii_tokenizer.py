"""PII tokenizer tests (docs/02 §3 / §5-0 — adapter-only tokenization)."""

from src.services.pii_tokenizer import PIITokenizer


def test_tokenize_replaces_identifiers():
    tok = PIITokenizer()
    out = tok.tokenize("Candidate John Smith, email john@example.com")
    assert "John Smith" not in out
    assert "john@example.com" not in out
    assert "[CANDIDATE]" in out


def test_resolve_round_trip():
    tok = PIITokenizer()
    tokenized = tok.tokenize("Contact John Smith at john@example.com")
    resolved = tok.resolve(tokenized)
    assert "John Smith" in resolved
    assert "john@example.com" in resolved


def test_same_value_same_token():
    tok = PIITokenizer()
    out = tok.tokenize("John Smith and John Smith again")
    # the same identifier maps to the same token (consistency)
    assert out.count("[CANDIDATE]") == 2


def test_no_pii_unchanged():
    tok = PIITokenizer()
    text = "Built the payment platform and led the migration"
    assert tok.tokenize(text) == text


def test_map_not_persisted_in_output():
    """The token->value map lives only in the instance, never leaks into tokenized text."""
    tok = PIITokenizer()
    out = tok.tokenize("Jane Doe")
    assert "Jane" not in out and "Doe" not in out
