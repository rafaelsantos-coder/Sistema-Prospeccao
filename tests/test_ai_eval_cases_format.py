"""Os casos da avaliação com modelo ficam válidos mesmo sem rodar o modelo."""

from __future__ import annotations

import json
from pathlib import Path

from tests.ai_eval.test_ai_eval import CASES, _context


def test_ai_eval_cases_build_context():
    assert CASES
    for path in CASES:
        case = json.loads(Path(path).read_text(encoding="utf-8"))
        assert {"id", "description", "history", "latest", "expect"} <= case.keys(), path.name
        context = _context(case)
        assert context["latest_customer_message"] == case["latest"]
        assert context["history"][-1]["body"] == case["latest"]
        assert context["knowledge"]


def test_sdr_tools_work_without_database():
    from types import SimpleNamespace

    from app.services.ai_sdr_tools import load_manual

    manual = load_manual(None, SimpleNamespace(id=0))
    assert any(item["title"] == "Condições contratuais FTTH" for item in manual)
