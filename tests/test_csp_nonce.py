"""CSP sem 'unsafe-inline' em scripts: nonce por resposta e nenhum script/handler embutido sem nonce."""

import re
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app

TEMPLATES = Path(__file__).resolve().parents[1] / "app" / "templates"


def _script_src(csp: str) -> list[str]:
    for directive in csp.split(";"):
        parts = directive.split()
        if parts and parts[0] == "script-src":
            return parts[1:]
    return []


def test_csp_usa_nonce_novo_por_resposta_e_sem_unsafe_inline_em_script():
    with TestClient(app) as client:
        first = client.get("/login").headers["content-security-policy"]
        second = client.get("/login").headers["content-security-policy"]
    a, b = _script_src(first), _script_src(second)
    assert "'unsafe-inline'" not in a
    nonce_a = [x for x in a if x.startswith("'nonce-")]
    nonce_b = [x for x in b if x.startswith("'nonce-")]
    assert len(nonce_a) == 1 and len(nonce_b) == 1 and nonce_a != nonce_b


def test_todo_script_embutido_tem_nonce_e_nao_ha_handlers_inline():
    for path in TEMPLATES.rglob("*.html"):
        text = path.read_text(encoding="utf-8")
        for tag in re.findall(r"<script\b[^>]*>", text):
            if "src=" in tag or 'type="application/json"' in tag:
                continue
            assert 'nonce="{{ csp_nonce }}"' in tag, f"{path.name}: script embutido sem nonce"
        assert not re.search(r"\son[a-z]+\s*=", text), f"{path.name}: handler inline bloqueado pela CSP"
