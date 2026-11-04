from __future__ import annotations

from dataclasses import replace

import pytest

from app import config


def _production(**overrides):
    base = replace(
        config.settings,
        app_env="production",
        secret_key="x" * 32,
        session_signing_key="y" * 32,
        data_encryption_key="z" * 32,
        bootstrap_admin_password="SenhaProducaoForte1!",
        database_url="postgresql+psycopg://sulnet:pass@db:5432/sulnet_prospecta",
        ai_seller_enabled=False,
        ai_autonomous_enabled=False,
        ai_engineer_enabled=False,
        ai_engineer_autonomous_pr_enabled=False,
        zapi_instance_id="",
        zapi_instance_token="",
        openai_api_key="",
        sulnet_crm_base_url="",
        sulnet_ai_integration_key="",
        sulnet_crm_bearer_token="",
        zapsign_webhook_secret="",
        ai_engineer_github_repository="",
        ai_engineer_github_token="",
    )
    return replace(base, **overrides)


def test_production_settings_accept_baseline(monkeypatch):
    monkeypatch.setattr(config, "settings", _production())
    config.validate_production_settings()


def test_production_rejects_default_bootstrap_password(monkeypatch):
    monkeypatch.setattr(
        config,
        "settings",
        _production(bootstrap_admin_password="Sulnet@2026!"),
    )
    with pytest.raises(RuntimeError, match="BOOTSTRAP_ADMIN_PASSWORD"):
        config.validate_production_settings()


def test_production_seller_requires_autonomy_openai_crm_and_zapsign(monkeypatch):
    monkeypatch.setattr(
        config,
        "settings",
        _production(
            ai_seller_enabled=True,
            ai_autonomous_enabled=False,
            openai_api_key="sk-test",
            sulnet_crm_base_url="https://crm.example",
            sulnet_ai_integration_key="k" * 24,
            zapsign_webhook_secret="w" * 24,
        ),
    )
    with pytest.raises(RuntimeError, match="AI_AUTONOMOUS_ENABLED"):
        config.validate_production_settings()

    monkeypatch.setattr(
        config,
        "settings",
        _production(
            ai_seller_enabled=True,
            ai_autonomous_enabled=True,
            openai_api_key="sk-test",
            sulnet_crm_base_url="https://crm.example",
            sulnet_ai_integration_key="k" * 24,
            zapsign_webhook_secret="w" * 24,
        ),
    )
    config.validate_production_settings()


def test_production_blocks_engineer_autonomous_pr_without_allow_flag(monkeypatch):
    monkeypatch.setattr(
        config,
        "settings",
        _production(
            ai_engineer_enabled=True,
            ai_engineer_autonomous_pr_enabled=True,
            ai_engineer_github_repository="org/repo",
            ai_engineer_github_token="ghp_test_token",
        ),
    )
    monkeypatch.delenv("AI_ENGINEER_ALLOW_PRODUCTION_PR", raising=False)
    with pytest.raises(RuntimeError, match="AI_ENGINEER_ALLOW_PRODUCTION_PR"):
        config.validate_production_settings()

    monkeypatch.setenv("AI_ENGINEER_ALLOW_PRODUCTION_PR", "true")
    config.validate_production_settings()


def test_rewrite_engine_observe_and_rewrite_modes():
    from app.services.ai_sdr import AIDecision, QualificationData
    from app.services.ai_sdr_rewrite import apply_rewrite_policies

    def rewrite(decision, _context):
        decision.reply_text = "Texto da regra fixa"
        return decision

    decision = AIDecision(
        reply_text="Original",
        intent="INTEREST",
        qualification_score=50,
        temperature="MORNO",
        recommended_stage="QUALIFICADO",
        handoff_recommended=False,
        confidence=0.9,
        summary="",
        next_action="",
        qualification=QualificationData(),
    )
    observed, observations = apply_rewrite_policies(
        decision,
        {},
        (("router_information", rewrite),),
        observe={"router_information"},
    )
    assert observed.reply_text == "Original"
    assert observations[0]["mode"] == "observe"

    applied, observations = apply_rewrite_policies(
        decision,
        {},
        (("router_information", rewrite),),
        observe=set(),
    )
    assert applied.reply_text == "Texto da regra fixa"
    assert observations[0]["mode"] == "rewrite"
