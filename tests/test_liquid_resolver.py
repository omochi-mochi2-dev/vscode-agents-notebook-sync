from typing import Any, Dict
from unittest.mock import MagicMock, patch

from src.github_client import fetch_repository_variables
from src.liquid_resolver import resolve_liquid_variables

VARIABLES: Dict[str, Dict[str, str]] = {
    "product": {"prodname_vscode": "Visual Studio Code"},
    "copilot": {"chat_view": "Chat view"},
}


def test_resolve_liquid_variables_success() -> None:
    assert resolve_liquid_variables("{% data variables.product.prodname_vscode %}", VARIABLES) == "Visual Studio Code"
    assert resolve_liquid_variables("{% data variables.copilot.chat_view %}", VARIABLES) == "Chat view"
    content = (
        "Open {% data variables.copilot.chat_view %} in {%data variables.product.prodname_vscode%}.\n"
        "{%  data  variables.product.prodname_vscode  %} again."
    )
    assert resolve_liquid_variables(content, VARIABLES) == (
        "Open Chat view in Visual Studio Code.\nVisual Studio Code again."
    )


def test_resolve_liquid_variables_missing_key() -> None:
    unknown_var = "{% data variables.product.unknown %}"
    unknown_file = "{% data variables.nofile.x %}"
    content = f"A {unknown_var} B {unknown_file} C {{% data variables.copilot.chat_view %}}"
    assert resolve_liquid_variables(content, VARIABLES) == f"A {unknown_var} B {unknown_file} C Chat view"
    assert resolve_liquid_variables("text", {}) == "text"


def test_resolve_liquid_variables_key_with_spaces() -> None:
    variables: Dict[str, Dict[str, str]] = {
        "copilot": {"next edit suggestions": "next edit suggestions (NES)", "chat_view": "Chat view"},
    }
    content = "Use {% data variables.copilot.next edit suggestions %} and {%data variables.copilot.next edit suggestions%} with {% data variables.copilot.chat_view %}."
    assert resolve_liquid_variables(content, variables) == (
        "Use next edit suggestions (NES) and next edit suggestions (NES) with Chat view."
    )
    missing = "{% data variables.copilot.no such key %}"
    assert resolve_liquid_variables(missing, variables) == missing


def _resp(status: int = 200, json_data: Any = None, text: str = "") -> MagicMock:
    m = MagicMock()
    m.status_code = status
    m.json.return_value = json_data
    m.text = text
    m.raise_for_status.return_value = None
    return m


def test_fetch_repository_variables_success_and_bad_yaml() -> None:
    listing = [
        {"name": "product.yml", "type": "file", "download_url": "https://raw/product.yml"},
        {"name": "copilot.yaml", "type": "file", "download_url": "https://raw/copilot.yaml"},
        {"name": "broken.yml", "type": "file", "download_url": "https://raw/broken.yml"},
        {"name": "README.md", "type": "file", "download_url": "https://raw/README.md"},
    ]
    responses: Dict[str, MagicMock] = {
        "https://raw/product.yml": _resp(text="prodname_vscode: Visual Studio Code\n"),
        "https://raw/copilot.yaml": _resp(text="chat_view: Chat view\n"),
        "https://raw/broken.yml": _resp(text="a: [unclosed\n"),
    }

    def fake_get(url: str, **kwargs: Any) -> MagicMock:
        if url.endswith("/contents/data/variables"):
            return _resp(json_data=listing)
        return responses[url]

    with patch("src.github_client.requests.get", side_effect=fake_get):
        result = fetch_repository_variables("o", "r", "sha", "tok")
    assert result == {
        "product": {"prodname_vscode": "Visual Studio Code"},
        "copilot": {"chat_view": "Chat view"},
    }


def test_fetch_repository_variables_not_found() -> None:
    with patch("src.github_client.requests.get", return_value=_resp(status=404)):
        assert fetch_repository_variables("o", "r", "sha", "tok") == {}

