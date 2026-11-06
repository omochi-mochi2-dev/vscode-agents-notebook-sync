from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

from src.docs_client import split_requests_into_chunks
from src.main import main
from src.models import FileDiff


def _tab_reqs(n: int) -> List[Dict[str, Any]]:
    reqs: List[Dict[str, Any]] = []
    for i in range(n):
        tab = f"t{i}"
        reqs.append({"deleteContentRange": {"range": {"startIndex": 1, "endIndex": 5, "tabId": tab}}})
        reqs.append({"insertText": {"location": {"index": 1, "tabId": tab}, "text": "x"}})
    return reqs


def test_split_by_tab_count_keeps_pairs_atomic() -> None:
    chunks = split_requests_into_chunks(_tab_reqs(25), max_tabs=10, max_requests=100)
    assert [len(c) for c in chunks] == [20, 20, 10]
    for c in chunks:
        assert c[0].keys() == {"deleteContentRange"}
        assert len(c) % 2 == 0
        for j in range(0, len(c), 2):
            assert c[j]["deleteContentRange"]["range"]["tabId"] == c[j + 1]["insertText"]["location"]["tabId"]


def test_split_by_request_count() -> None:
    chunks = split_requests_into_chunks(_tab_reqs(10), max_tabs=100, max_requests=6)
    assert [len(c) for c in chunks] == [6, 6, 6, 2]


def test_split_empty_and_single_tab_without_delete() -> None:
    assert split_requests_into_chunks([]) == []
    single = [{"insertText": {"location": {"index": 1, "tabId": "a"}, "text": "x"}}]
    assert split_requests_into_chunks(single) == [single]


@patch("src.main.apply_batch_update")
@patch("src.main.extract_tabs_info")
@patch("src.main.fetch_document_tabs")
@patch("src.main.generate_phase1_payload", return_value=[])
@patch("src.main.requests.get")
@patch("src.main.validate_failsafe")
@patch("src.main.analyze_folder_rename")
@patch("src.main.fetch_commit_diffs")
@patch("src.main.fetch_repository_variables", return_value={})
@patch("src.main.get_latest_commit_sha", return_value="new")
@patch("src.main.get_previous_commit_sha", return_value="old")
@patch("src.main.init_docs_client")
@patch("src.main.load_config")
def test_main_phase2_called_in_multiple_batches(
    mock_cfg: MagicMock, mock_init: MagicMock, mock_prev: MagicMock, mock_latest: MagicMock,
    mock_vars: MagicMock, mock_diffs: MagicMock, mock_rename: MagicMock, mock_failsafe: MagicMock,
    mock_get: MagicMock, mock_p1: MagicMock, mock_fetch_tabs: MagicMock, mock_extract: MagicMock,
    mock_apply: MagicMock,
) -> None:
    mock_cfg.return_value = {
        "GITHUB_TOKEN": "t",
        "sync_targets": [{"repository": "o/r", "watch_folders": [{"source_path": "docs", "google_doc_id": "D"}]}],
    }
    diffs = [FileDiff(f"docs/f{i}.md", None, "modified", "u", "new") for i in range(25)]
    mock_diffs.return_value = diffs
    mock_get.return_value = MagicMock(text="body")
    mock_fetch_tabs.return_value = {"tabs": []}
    mock_extract.return_value = {d.target_tab_name: {"tabId": f"id{i}", "endIndex": 10} for i, d in enumerate(diffs)}

    with patch("builtins.open", MagicMock()):
        main()

    # Phase 1 は空のため、全呼び出しが Phase 2 のチャンク (25タブ / 10タブ = 3バッチ)
    assert mock_apply.call_count == 3
    sizes = [len(c.args[2]) for c in mock_apply.call_args_list]
    assert sizes == [20, 20, 10]

