import sys
import logging
import os
import requests
import argparse

from src.config import load_config
from src.github_client import (
    get_previous_commit_sha,
    get_latest_commit_sha,
    fetch_commit_diffs,
    fetch_all_files_as_added,
    analyze_folder_rename,
    validate_failsafe,
    fetch_repository_variables
)
from src.liquid_resolver import resolve_liquid_variables
from src.docs_client import (
    init_docs_client,
    fetch_document_tabs,
    extract_tabs_info,
    generate_phase1_payload,
    generate_phase2_payload,
    split_requests_into_chunks,
    apply_batch_update
)

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

def main(force: bool = False) -> None:
    try:
        config = load_config()
        github_token = config['GITHUB_TOKEN']
        
        credentials_path = os.environ.get('GOOGLE_APPLICATION_CREDENTIALS', 'credentials.json')
        docs_service = init_docs_client(credentials_path)

        sync_targets = config.get('sync_targets', [])
        
        if not sync_targets:
            logger.warning("No sync targets defined in config.")
            return

        for target in sync_targets:
            repository = target.get('repository')
            if not repository:
                continue
                
            owner, repo = repository.split('/')
            
            base_sha = get_previous_commit_sha(owner, repo)
            head_sha = get_latest_commit_sha(owner, repo, github_token)
            
            if force:
                logger.info(f"Force sync triggered. Ignoring base_sha and performing full sync for {repository}.")
                diffs = fetch_all_files_as_added(owner, repo, head_sha, github_token)
            elif base_sha == head_sha:
                logger.info(f"No changes detected for {repository} (SHA: {head_sha}). Skipping.")
                continue
            elif not base_sha:
                logger.info(f"No previous commit SHA found. Performing full sync for {repository}. (Fallback)")
                diffs = fetch_all_files_as_added(owner, repo, head_sha, github_token)
            else:
                diffs = fetch_commit_diffs(base_sha, head_sha, owner, repo, github_token)
            
            # Liquid変数定義の取得 (Phase 2 の前処理で使用)
            repo_variables = fetch_repository_variables(owner, repo, head_sha, github_token)

            # 6. 自己修復の連動
            analyze_folder_rename(diffs, repository, 'config.yaml')
            
            # Since config might have been updated by auto-healing, reload watch_folders
            config = load_config()
            target = next((t for t in config.get('sync_targets', []) if t.get('repository') == repository), target)
            watch_folders = target.get('watch_folders', [])
            
            for wf in watch_folders:
                source_path = wf.get('source_path')
                doc_id = wf.get('google_doc_id')
                
                if not source_path or not doc_id:
                    continue
                    
                validate_failsafe(diffs, source_path)
                
                prefix = source_path if source_path.endswith('/') else f"{source_path}/"
                folder_diffs = [
                    d for d in diffs 
                    if d.filename.startswith(prefix) or (d.previous_filename and d.previous_filename.startswith(prefix))
                ]
                
                if not folder_diffs:
                    logger.info(f"No relevant changes for folder {source_path}. Skipping.")
                    continue
                
                file_contents = {}
                for d in folder_diffs:
                    if d.status in ['added', 'modified', 'renamed'] and d.raw_content_url:
                        headers = {"Authorization": f"Bearer {github_token}"}
                        try:
                            resp = requests.get(d.raw_content_url, headers=headers)
                            resp.raise_for_status()
                            file_contents[d.filename] = resolve_liquid_variables(resp.text, repo_variables)
                        except Exception as e:
                            logger.error(f"Failed to fetch raw content for {d.filename}: {e}")
                            raise

                # 7. Google Docs同期 (2段階プロセス)
                # Phase 1: 構造の同期（タブの作成・削除）
                current_doc_state = fetch_document_tabs(docs_service, doc_id)
                current_tab_map = extract_tabs_info(current_doc_state)
                
                phase1_reqs = generate_phase1_payload(folder_diffs, current_tab_map)
                if phase1_reqs:
                    logger.info(f"Phase 1: Executing structural changes (tabs creation/deletion) for {doc_id}.")
                    apply_batch_update(docs_service, doc_id, phase1_reqs)
                    
                    # 構造が変更されたため、再フェッチして新しいtabIdを取得する
                    logger.info(f"Re-fetching document tabs to get updated tabIds.")
                    latest_doc_state = fetch_document_tabs(docs_service, doc_id)
                    latest_tab_map = extract_tabs_info(latest_doc_state)
                else:
                    logger.info(f"Phase 1: No structural changes required for {source_path}.")
                    latest_tab_map = current_tab_map
                
                # Phase 2: コンテンツの同期（削除と挿入のアトミック処理）
                phase2_reqs = generate_phase2_payload(folder_diffs, latest_tab_map, file_contents)
                if phase2_reqs:
                    chunks = split_requests_into_chunks(phase2_reqs)
                    total_chunks = len(chunks)
                    logger.info(f"Phase 2: Executing content updates for {doc_id} ({len(phase2_reqs)} requests in {total_chunks} batches).")
                    for i, chunk in enumerate(chunks, start=1):
                        logger.info(f"Applying Phase 2 updates (batch {i}/{total_chunks}, {len(chunk)} requests)...")
                        apply_batch_update(docs_service, doc_id, chunk)
                else:
                    logger.info(f"Phase 2: No content updates required for {source_path}.")
                    
            # 8. 状態の保存 (リポジトリ単位のファイル名へ変更)
            sha_filename = f'last_commit_sha_{owner}_{repo}.txt'
            with open(sha_filename, 'w', encoding='utf-8') as f:
                f.write(head_sha)
            logger.info(f"Sync completed successfully. Saved head_sha: {head_sha} to {sha_filename}")
                
    except Exception as e:
        logger.error(f"Sync process failed: {e}", exc_info=True)
        sys.exit(1)

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="GitHub to Google Docs Sync")
    parser.add_argument('--force', action='store_true', help="Force full synchronization ignoring SHA cache")
    args = parser.parse_args()
    main(force=args.force)
