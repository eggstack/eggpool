from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_first_request_uses_personal_token_after_managed_bootstrap() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "Set the personal token from onboarding" in readme
    assert "export EGGPOOL_API_KEY='<personal-token-shown-once>'" in readme
    assert 'export EGGPOOL_API_KEY="$(eggpool getkey)"' not in readme
    assert "It does not grant dashboard or administrator access" in readme
