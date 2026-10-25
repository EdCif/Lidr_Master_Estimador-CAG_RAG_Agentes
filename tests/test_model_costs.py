"""Tests de la tabla de precios (sesión 06, ``app/services/model_costs.py``).

Se ejecutan con:

    uv run python -m pytest tests/test_model_costs.py -v
"""

import pytest

from app.services.model_costs import estimate_cost_usd, normalize_model


def test_opus_cost_is_tokens_times_price_per_million():
    # 1.000 × 4 $ / 1M + 100 × 20 $ / 1M = 0,004 + 0,002
    assert estimate_cost_usd("claude-opus-5-5", 1_000, 100) == pytest.approx(0.006)


def test_haiku_uses_the_cheap_tier_up_to_100k_input_tokens():
    # 50.000 × 0,10 $ / 1M + 2.000 × 0,50 $ / 1M = 0,005 + 0,001
    assert estimate_cost_usd("claude-haiku-5-5", 50_000, 2_000) == pytest.approx(0.006)
    # Justo en el límite todavía es el tramo barato.
    assert estimate_cost_usd("claude-haiku-5-5", 100_000, 0) == pytest.approx(0.01)


def test_haiku_charges_the_whole_request_at_the_long_tier_above_100k():
    # 150.000 × 0,50 $ / 1M + 2.000 × 2,50 $ / 1M = 0,075 + 0,005
    assert estimate_cost_usd("claude-haiku-5-5", 150_000, 2_000) == pytest.approx(0.08)


def test_model_names_with_provider_prefix_or_date_are_recognised():
    assert normalize_model("anthropic/claude-haiku-5-5") == "claude-haiku-5-5"
    assert normalize_model("claude-haiku-5-5-20261001") == "claude-haiku-5-5"
    assert normalize_model("Claude-Opus-5-5") == "claude-opus-5-5"
    # Un modelo más antiguo no se confunde con el nuevo por empezar igual.
    assert normalize_model("claude-opus-5") is None


def test_unknown_model_or_missing_tokens_give_none_not_zero():
    """Un 0 haría creer que el turno salió gratis: preferimos "no se sabe"."""
    assert estimate_cost_usd("gpt-4o-mini", 1_000, 100) is None
    assert estimate_cost_usd("claude-haiku-5-5", None, 100) is None
    assert estimate_cost_usd("claude-haiku-5-5", 1_000, None) is None
    assert estimate_cost_usd(None, 1_000, 100) is None
