from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_first_request_uses_personal_token_after_managed_bootstrap() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "eggpool users token USER --show-secret" in readme
    assert 'export EGGPOOL_API_KEY="$(eggpool getkey)"' not in readme
    assert "it does not grant managed-user access" in readme

