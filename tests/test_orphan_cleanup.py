import os
import sys
import unittest
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.docs_client import (
    ORPHAN_MAX_COUNT,
    ORPHAN_MAX_RATIO,
    build_tab_prefix,
    detect_orphan_tabs,
    generate_phase1_payload,
)
from src.github_client import FullSyncDiffs, fetch_all_files_as_added
from src.main import main
from src.models import FileDiff


def make_diff(filename: str, status: str = 'added') -> FileDiff:
    return FileDiff(
        filename=filename,
        previous_filename=None,
        status=status,  # type: ignore[arg-type]
        raw_content_url='url',
        commit_sha='sha',
    )


def make_tab_map(names: List[str]) -> Dict[str, Dict[str, Any]]:
    return {name: {'tabId': f't.{name}', 'endIndex': 2} for name in names}


class TestBuildTabPrefix(unittest.TestCase):
    def test_basic(self) -> None:
        self.assertEqual(build_tab_prefix('docs'), 'docs_')
        self.assertEqual(build_tab_prefix('docs/'), 'docs_')
        self.assertEqual(build_tab_prefix('docs/editor.v2'), 'docs_editor_v2_')

    def test_long_prefix_is_truncated_for_hashed_titles(self) -> None:
        prefix = build_tab_prefix('a' * 60)
        self.assertEqual(len(prefix), 41)


class TestDetectOrphanTabs(unittest.TestCase):
    def test_detects_only_prefixed_missing_tabs(self) -> None:
        diffs = [make_diff('docs/a.md'), make_diff('docs/b.md')]
        tabs = make_tab_map(['docs_a_md', 'docs_b_md', 'docs_old_md', 'manual_tab'])
        orphans = detect_orphan_tabs(diffs, tabs, 'docs')
        self.assertEqual(orphans, ['docs_old_md'])

    def test_manual_tabs_without_prefix_are_untouched(self) -> None:
        diffs = [make_diff('docs/a.md')]
        tabs = make_tab_map(['docs_a_md', 'memo', 'README'])
        self.assertEqual(detect_orphan_tabs(diffs, tabs, 'docs'), [])

    def test_no_orphans(self) -> None:
        diffs = [make_diff('docs/a.md')]
        tabs = make_tab_map(['docs_a_md'])
        self.assertEqual(detect_orphan_tabs(diffs, tabs, 'docs'), [])

    def test_nested_watch_folder_prefix_is_excluded(self) -> None:
        # 'docs' と 'docs_extra' のように、一方のプレフィックスが他方を内包するケース
        diffs = [make_diff('docs/a.md'), make_diff('docs/b.md'), make_diff('docs/c.md')]
        tabs = make_tab_map(['docs_a_md', 'docs_b_md', 'docs_c_md', 'docs_extra_x_md', 'docs_gone_md'])
        orphans = detect_orphan_tabs(
            diffs, tabs, 'docs', other_prefixes=[build_tab_prefix('docs_extra')]
        )
        self.assertEqual(orphans, ['docs_gone_md'])

    def test_shorter_other_prefix_does_not_exclude_own_tabs(self) -> None:
        diffs = [make_diff('docs_extra/a.md'), make_diff('docs_extra/b.md'), make_diff('docs_extra/c.md')]
        tabs = make_tab_map(['docs_extra_a_md', 'docs_extra_b_md', 'docs_extra_c_md', 'docs_extra_gone_md'])
        orphans = detect_orphan_tabs(
            diffs, tabs, 'docs_extra', other_prefixes=[build_tab_prefix('docs')]
        )
        self.assertEqual(orphans, ['docs_extra_gone_md'])

    def test_failsafe_zero_expected_tabs(self) -> None:
        tabs = make_tab_map(['docs_a_md', 'docs_b_md'])
        with self.assertRaises(RuntimeError):
            detect_orphan_tabs([], tabs, 'docs')

    def test_zero_expected_and_zero_candidates_is_noop(self) -> None:
        self.assertEqual(detect_orphan_tabs([], make_tab_map(['memo']), 'docs'), [])

    def test_failsafe_ratio_exceeded(self) -> None:
        diffs = [make_diff('docs/a.md')]
        tabs = make_tab_map(['docs_a_md', 'docs_x_md', 'docs_y_md'])  # 2/3 > 50%
        with self.assertRaises(RuntimeError):
            detect_orphan_tabs(diffs, tabs, 'docs')

    def test_ratio_exactly_at_limit_is_allowed(self) -> None:
        diffs = [make_diff('docs/a.md')]
        tabs = make_tab_map(['docs_a_md', 'docs_x_md'])  # 1/2 == 50%
        self.assertEqual(detect_orphan_tabs(diffs, tabs, 'docs'), ['docs_x_md'])

    def test_failsafe_count_exceeded(self) -> None:
        expected = [make_diff(f'docs/keep{i}.md') for i in range(100)]
        names = [d.target_tab_name for d in expected]
        orphan_names = [f'docs_old{i}_md' for i in range(ORPHAN_MAX_COUNT + 1)]
        tabs = make_tab_map(names + orphan_names)
        with self.assertRaises(RuntimeError):
            detect_orphan_tabs(expected, tabs, 'docs')

    def test_count_exactly_at_limit_is_allowed(self) -> None:
        expected = [make_diff(f'docs/keep{i}.md') for i in range(60)]
        names = [d.target_tab_name for d in expected]
        orphan_names = [f'docs_old{i}_md' for i in range(ORPHAN_MAX_COUNT)]
        tabs = make_tab_map(names + orphan_names)
        self.assertEqual(
            sorted(detect_orphan_tabs(expected, tabs, 'docs')), sorted(orphan_names)
        )

    def test_custom_limits(self) -> None:
        diffs = [make_diff('docs/a.md'), make_diff('docs/b.md')]
        tabs = make_tab_map(['docs_a_md', 'docs_b_md', 'docs_x_md'])
        with self.assertRaises(RuntimeError):
            detect_orphan_tabs(diffs, tabs, 'docs', max_count=0)
        self.assertEqual(ORPHAN_MAX_RATIO, 0.5)
        self.assertEqual(ORPHAN_MAX_COUNT, 20)


class TestPhase1Orphans(unittest.TestCase):
    def test_default_does_not_delete_orphans(self) -> None:
        diffs = [make_diff('docs/a.md')]
        tabs = make_tab_map(['docs_a_md', 'docs_old_md'])
        self.assertEqual(generate_phase1_payload(diffs, tabs), [])

    def test_orphan_cleanup_generates_delete_tab(self) -> None:
        diffs = [make_diff('docs/a.md'), make_diff('docs/b.md')]
        tabs = make_tab_map(['docs_a_md', 'docs_b_md', 'docs_old_md'])
        reqs = generate_phase1_payload(diffs, tabs, orphan_cleanup=True, source_path='docs')
        self.assertEqual(reqs, [{'deleteTab': {'tabId': 't.docs_old_md'}}])

    def test_orphan_cleanup_with_new_file_adds_and_deletes(self) -> None:
        diffs = [make_diff('docs/a.md'), make_diff('docs/new.md')]
        tabs = make_tab_map(['docs_a_md', 'docs_old_md'])
        reqs = generate_phase1_payload(diffs, tabs, orphan_cleanup=True, source_path='docs')
        self.assertEqual(reqs[0], {'deleteTab': {'tabId': 't.docs_old_md'}})
        self.assertIn(
            {'addDocumentTab': {'tabProperties': {'title': 'docs_new_md'}}}, reqs
        )

    def test_orphan_cleanup_requires_source_path(self) -> None:
        with self.assertRaises(ValueError):
            generate_phase1_payload([make_diff('docs/a.md')], {}, orphan_cleanup=True)

    def test_orphan_not_deleted_twice_when_also_removed(self) -> None:
        diffs = [make_diff('docs/a.md'), make_diff('docs/gone.md', status='removed')]
        tabs = make_tab_map(['docs_a_md', 'docs_gone_md'])
        reqs = generate_phase1_payload(diffs, tabs, orphan_cleanup=True, source_path='docs')
        self.assertEqual(reqs, [{'deleteTab': {'tabId': 't.docs_gone_md'}}])

    def test_failsafe_propagates_from_phase1(self) -> None:
        diffs = [make_diff('docs/a.md')]
        tabs = make_tab_map(['docs_a_md', 'docs_x_md', 'docs_y_md'])
        with self.assertRaises(RuntimeError):
            generate_phase1_payload(diffs, tabs, orphan_cleanup=True, source_path='docs')


class TestFetchAllTruncated(unittest.TestCase):
    @patch('src.github_client.requests.get')
    def test_truncated_flag_propagated(self, mock_get: MagicMock) -> None:
        resp = MagicMock()
        resp.json.return_value = {
            'truncated': True,
            'tree': [{'type': 'blob', 'path': 'docs/a.md'}],
        }
        mock_get.return_value = resp
        diffs = fetch_all_files_as_added('o', 'r', 'sha', 'tok')
        self.assertIsInstance(diffs, FullSyncDiffs)
        self.assertTrue(diffs.truncated)
        self.assertEqual(len(diffs), 1)

    @patch('src.github_client.requests.get')
    def test_not_truncated_by_default(self, mock_get: MagicMock) -> None:
        resp = MagicMock()
        resp.json.return_value = {'tree': [{'type': 'blob', 'path': 'docs/a.md'}]}
        mock_get.return_value = resp
        self.assertFalse(fetch_all_files_as_added('o', 'r', 'sha', 'tok').truncated)


class TestMainIntegration(unittest.TestCase):
    def setUp(self) -> None:
        patcher = patch('src.main.fetch_repository_variables', return_value={})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _run(self, force: bool, tabs: List[str], truncated: bool = False,
             extra_config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        diffs = FullSyncDiffs([make_diff('docs/a.md'), make_diff('docs/b.md')])
        diffs.truncated = truncated
        config: Dict[str, Any] = {
            'GITHUB_TOKEN': 'fake',
            'sync_targets': [{
                'repository': 'owner/repo',
                'watch_folders': [{'source_path': 'docs', 'google_doc_id': 'DOC'}],
            }],
        }
        config.update(extra_config or {})
        doc = {'tabs': [{'tabProperties': {'tabId': f't.{n}', 'title': n}} for n in tabs]}
        resp = MagicMock()
        resp.text = 'body'

        with patch('src.main.load_config', return_value=config), \
             patch('src.main.init_docs_client'), \
             patch('src.main.get_previous_commit_sha', return_value='same'), \
             patch('src.main.get_latest_commit_sha', return_value='same'), \
             patch('src.main.fetch_all_files_as_added', return_value=diffs), \
             patch('src.main.fetch_commit_diffs', return_value=[]), \
             patch('src.main.analyze_folder_rename'), \
             patch('src.main.validate_failsafe'), \
             patch('src.main.requests.get', return_value=resp), \
             patch('src.main.fetch_document_tabs', return_value=doc), \
             patch('src.main.generate_phase2_payload', return_value=[]), \
             patch('src.main.apply_batch_update') as mock_apply, \
             patch('src.main.sys.exit') as mock_exit, \
             patch('builtins.open', unittest.mock.mock_open()):
            main(force=force)
        return {'apply': mock_apply, 'exit': mock_exit}

    def test_force_deletes_orphans(self) -> None:
        result = self._run(True, ['docs_a_md', 'docs_b_md', 'docs_old_md'])
        result['exit'].assert_not_called()
        phase1 = result['apply'].call_args_list[0][0][2]
        self.assertEqual(phase1, [{'deleteTab': {'tabId': 't.docs_old_md'}}])

    def test_without_force_no_orphan_deletion(self) -> None:
        diffs = [make_diff('docs/a.md', status='modified')]
        config: Dict[str, Any] = {
            'GITHUB_TOKEN': 'fake',
            'sync_targets': [{
                'repository': 'owner/repo',
                'watch_folders': [{'source_path': 'docs', 'google_doc_id': 'DOC'}],
            }],
        }
        doc = {'tabs': [{'tabProperties': {'tabId': f't.{n}', 'title': n}}
                        for n in ['docs_a_md', 'docs_old_md']]}
        with patch('src.main.load_config', return_value=config), \
             patch('src.main.init_docs_client'), \
             patch('src.main.get_previous_commit_sha', return_value='old'), \
             patch('src.main.get_latest_commit_sha', return_value='new'), \
             patch('src.main.fetch_commit_diffs', return_value=diffs), \
             patch('src.main.analyze_folder_rename'), \
             patch('src.main.validate_failsafe'), \
             patch('src.main.requests.get', return_value=MagicMock(text='body')), \
             patch('src.main.fetch_document_tabs', return_value=doc), \
             patch('src.main.generate_phase2_payload', return_value=[]), \
             patch('src.main.apply_batch_update') as mock_apply, \
             patch('src.main.sys.exit') as mock_exit, \
             patch('builtins.open', unittest.mock.mock_open()):
            main(force=False)
        mock_exit.assert_not_called()
        mock_apply.assert_not_called()

    def test_truncated_tree_disables_cleanup(self) -> None:
        result = self._run(True, ['docs_a_md', 'docs_b_md', 'docs_old_md'], truncated=True)
        result['exit'].assert_not_called()
        result['apply'].assert_not_called()

    def test_failsafe_aborts_before_any_update(self) -> None:
        result = self._run(True, ['docs_a_md', 'docs_x_md', 'docs_y_md', 'docs_z_md'])
        result['exit'].assert_called_once_with(1)
        result['apply'].assert_not_called()

    def test_config_overrides_limits(self) -> None:
        result = self._run(
            True,
            ['docs_a_md', 'docs_b_md', 'docs_old_md'],
            extra_config={'orphan_cleanup': {'max_count': 0}},
        )
        result['exit'].assert_called_once_with(1)
        result['apply'].assert_not_called()


if __name__ == '__main__':
    unittest.main()
