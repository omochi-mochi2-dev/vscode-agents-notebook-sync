import unittest
from unittest.mock import patch, MagicMock
import os
import sys
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.main import main

class TestStep3(unittest.TestCase):

    def setUp(self):
        # fetch_repository_variables が実ネットワークへ接続しないよう全テストでモックする
        patcher = patch('src.main.fetch_repository_variables', return_value={'product': {'prodname_vscode': 'Visual Studio Code'}})
        self.mock_fetch_vars = patcher.start()
        self.addCleanup(patcher.stop)

    @patch('src.main.sys.exit')
    @patch('src.main.init_docs_client')
    @patch('src.main.load_config')
    def test_main_no_sync_targets(self, mock_load_config, mock_init_docs_client, mock_exit):
        mock_load_config.return_value = {
            'GITHUB_TOKEN': 'fake',
            'sync_targets': []
        }
        main()
        mock_exit.assert_not_called()

    @patch('src.main.get_latest_commit_sha')
    @patch('src.main.get_previous_commit_sha')
    @patch('src.main.sys.exit')
    @patch('src.main.init_docs_client')
    @patch('src.main.load_config')
    def test_main_no_changes(self, mock_load_config, mock_init_docs_client, mock_exit, mock_prev_sha, mock_latest_sha):
        mock_load_config.return_value = {
            'GITHUB_TOKEN': 'fake',
            'sync_targets': [{'repository': 'owner/repo'}]
        }
        mock_prev_sha.return_value = "same_sha"
        mock_latest_sha.return_value = "same_sha"
        
        main()
        mock_exit.assert_not_called()

    @patch('src.main.fetch_document_tabs')
    @patch('src.main.requests.get')
    @patch('src.main.validate_failsafe')
    @patch('src.main.analyze_folder_rename')
    @patch('src.main.fetch_commit_diffs')
    @patch('src.main.get_latest_commit_sha')
    @patch('src.main.get_previous_commit_sha')
    @patch('src.main.sys.exit')
    @patch('src.main.init_docs_client')
    @patch('src.main.load_config')
    def test_main_with_changes(self, mock_load_config, mock_init_docs_client, mock_exit, mock_prev_sha, mock_latest_sha, mock_fetch_diffs, mock_analyze_rename, mock_validate_failsafe, mock_req_get, mock_fetch_tabs):
        mock_load_config.return_value = {
            'GITHUB_TOKEN': 'fake',
            'sync_targets': [{
                'repository': 'owner/repo',
                'watch_folders': [{'source_path': 'docs', 'google_doc_id': 'DOC123'}]
            }]
        }
        mock_prev_sha.return_value = "old_sha"
        mock_latest_sha.return_value = "new_sha"
        
        from src.models import FileDiff
        diff = FileDiff(filename="docs/file.md", previous_filename=None, status="modified", raw_content_url="url", commit_sha="new_sha")
        mock_fetch_diffs.return_value = [diff]
        
        mock_resp = MagicMock()
        mock_resp.text = "Welcome to {% data variables.product.prodname_vscode %}"
        mock_req_get.return_value = mock_resp
        
        mock_fetch_tabs.return_value = {"tabs": []}
        
        mock_docs_service = MagicMock()
        mock_init_docs_client.return_value = mock_docs_service
        
        with patch('builtins.open', unittest.mock.mock_open()) as mock_file:
            with patch('src.main.generate_phase1_payload') as mock_p1, \
                 patch('src.main.generate_phase2_payload') as mock_p2, \
                 patch('src.main.apply_batch_update') as mock_apply, \
                 patch('src.main.extract_tabs_info') as mock_extract:
                
                mock_p1.return_value = [{'addDocumentTab': {}}]
                mock_p2.return_value = [{'insertText': {}}]
                mock_extract.return_value = {}
                
                main()
                
                mock_exit.assert_not_called()
                self.assertEqual(mock_apply.call_count, 2)
                self.assertEqual(mock_fetch_tabs.call_count, 2)
                self.mock_fetch_vars.assert_called_once_with('owner', 'repo', 'new_sha', 'fake')
                # Liquid変数が解決された本文が Phase 2 に渡されること
                self.assertEqual(mock_p2.call_args[0][2], {"docs/file.md": "Welcome to Visual Studio Code"})
                mock_file.assert_called_with('last_commit_sha_owner_repo.txt', 'w', encoding='utf-8')

    @patch('src.main.sys.exit')
    @patch('src.main.init_docs_client')
    @patch('src.main.load_config')
    def test_main_exception_triggers_exit_and_protects_sha(self, mock_load_config, mock_init_docs_client, mock_exit):
        mock_load_config.side_effect = Exception("Config load error")
        with patch('builtins.open', unittest.mock.mock_open()) as mock_file:
            main()
            mock_exit.assert_called_once_with(1)
            mock_file.assert_not_called()

    @patch('src.main.fetch_all_files_as_added')
    @patch('src.main.get_latest_commit_sha')
    @patch('src.main.get_previous_commit_sha')
    @patch('src.main.init_docs_client')
    @patch('src.main.load_config')
    def test_main_cache_miss_fallback(self, mock_load_config, mock_init, mock_prev_sha, mock_latest_sha, mock_fetch_all):
        mock_load_config.return_value = {
            'GITHUB_TOKEN': 'fake',
            'sync_targets': [{'repository': 'owner/repo', 'watch_folders': []}]
        }
        mock_prev_sha.return_value = None  # Cache Miss
        mock_latest_sha.return_value = "new_sha"
        mock_fetch_all.return_value = []
        
        main()
        mock_fetch_all.assert_called_once_with('owner', 'repo', 'new_sha', 'fake')

    @patch('src.main.fetch_all_files_as_added')
    @patch('src.main.get_latest_commit_sha')
    @patch('src.main.get_previous_commit_sha')
    @patch('src.main.sys.exit')
    @patch('src.main.init_docs_client')
    @patch('src.main.load_config')
    def test_main_force_sync(self, mock_load_config, mock_init, mock_exit, mock_prev_sha, mock_latest_sha, mock_fetch_all):
        mock_load_config.return_value = {
            'GITHUB_TOKEN': 'fake',
            'sync_targets': [{'repository': 'owner/repo', 'watch_folders': []}]
        }
        mock_prev_sha.return_value = "same_sha"
        mock_latest_sha.return_value = "same_sha"
        mock_fetch_all.return_value = []
        
        with patch('builtins.open', unittest.mock.mock_open()) as mock_file:
            main(force=True)
            mock_file.assert_called_with('last_commit_sha_owner_repo.txt', 'w', encoding='utf-8')
        
        mock_fetch_all.assert_called_once_with('owner', 'repo', 'same_sha', 'fake')
        mock_exit.assert_not_called()

if __name__ == '__main__':
    unittest.main()
