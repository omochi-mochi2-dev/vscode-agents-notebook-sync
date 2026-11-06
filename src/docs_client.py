import os
import logging
from typing import Dict, Any, List, Optional, Sequence, Tuple
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from src.models import FileDiff, format_tab_title

logger = logging.getLogger(__name__)

SCOPES = ['https://www.googleapis.com/auth/documents']

def init_docs_client(credentials_path: str = 'credentials.json') -> Any:
    """
    credentials.json を使用して、Google Docs APIクライアントの認証初期化を行う。
    """
    if not os.path.exists(credentials_path):
        logger.error(f"Credentials file not found: {credentials_path}")
        raise FileNotFoundError(f"Credentials file not found: {credentials_path}")
        
    try:
        credentials = Credentials.from_service_account_file(
            credentials_path, scopes=SCOPES)
        service = build('docs', 'v1', credentials=credentials)
        logger.info("Google Docs API client initialized successfully.")
        return service
    except Exception as e:
        logger.error(f"Failed to initialize Google Docs API client: {e}")
        raise

def fetch_document_tabs(service: Any, document_id: str) -> Dict[str, Any]:
    """
    documents.get を呼び出し、パラメータとして includeTabsContent=true を明示的に指定して
    既存のタブ一覧とコンテンツを含むドキュメント構造を取得する。
    """
    logger.info(f"Fetching document tabs for document ID: {document_id}")
    try:
        doc = service.documents().get(
            documentId=document_id,
            includeTabsContent=True
        ).execute()
        return doc
    except HttpError as e:
        logger.error(f"Google Docs API HTTP Error fetching document {document_id}: {e}")
        raise

def extract_tabs_info(document_content: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """
    取得したドキュメント構造からタブ名とtabId、および現在のendIndexを抽出するユーティリティ関数。
    戻り値は { "tab_title": {"tabId": "...", "endIndex": 123} } の辞書。
    """
    tab_info_map = {}
    tabs = document_content.get('tabs', [])
    for tab in tabs:
        tab_props = tab.get('tabProperties', {})
        tab_id = tab_props.get('tabId')
        title = tab_props.get('title')
        
        # Calculate end_index (length of the content)
        end_index = 2
        try:
            body_content = tab.get('documentTab', {}).get('body', {}).get('content', [])
            if body_content:
                end_index = body_content[-1].get('endIndex', 2)
        except Exception as e:
            logger.warning(f"Could not determine end index for tab {title}: {e}")
            
        if tab_id and title:
            tab_info_map[title] = {
                'tabId': tab_id,
                'endIndex': end_index
            }
            
    return tab_info_map

ORPHAN_MAX_RATIO: float = 0.5
ORPHAN_MAX_COUNT: int = 20
_TAB_TITLE_HASH_PREFIX_LENGTH: int = 41


def build_tab_prefix(source_path: str) -> str:
    """
    watch_folder の source_path から、そのフォルダ配下のタブ名が共有するプレフィックスを生成する。
    format_tab_title と同じ置換規則を適用し、末尾に '_' を付与する。
    50文字超でハッシュ化されたタブ名にも一致するよう、先頭41文字までに切り詰める。
    """
    base = source_path.strip('/').replace('/', '_').replace('.', '_')
    return f"{base}_"[:_TAB_TITLE_HASH_PREFIX_LENGTH]


def detect_orphan_tabs(
    diffs: List[FileDiff],
    current_tab_map: Dict[str, Dict[str, Any]],
    source_path: str,
    other_prefixes: Sequence[str] = (),
    max_ratio: float = ORPHAN_MAX_RATIO,
    max_count: int = ORPHAN_MAX_COUNT,
) -> List[str]:
    """
    全量同期時に、GitHub 側に存在しない孤立タブ名を特定して返す。

    - 対象は source_path のプレフィックスを持つ既存タブのみ（手動作成タブ等は対象外）。
    - 他の watch_folder のプレフィックス（より長く、かつ自身を内包するもの）に該当するタブは除外する。
    - 安全対策: 期待タブが0件、孤立比率 > max_ratio、孤立件数 > max_count の場合は RuntimeError で中断する。
    """
    prefix = build_tab_prefix(source_path)
    expected_tab_names = {
        d.target_tab_name for d in diffs if d.status in ('added', 'modified', 'renamed')
    }

    candidates: List[str] = []
    for tab_name in current_tab_map:
        if not tab_name.startswith(prefix):
            continue
        if any(
            other != prefix and other.startswith(prefix) and tab_name.startswith(other)
            for other in other_prefixes
        ):
            continue
        candidates.append(tab_name)

    if not candidates:
        return []

    if not expected_tab_names:
        logger.error(f"Fail-safe triggered: No expected tabs for '{source_path}' but {len(candidates)} existing tabs found. Aborting orphan cleanup.")
        raise RuntimeError("Fail-safe: 同期対象ファイルが0件のため、孤立タブの削除を中断します。")

    orphans = [name for name in candidates if name not in expected_tab_names]
    if not orphans:
        return []

    ratio = len(orphans) / len(candidates)
    if ratio > max_ratio:
        logger.error(f"Fail-safe triggered: Orphan ratio {ratio:.2%} ({len(orphans)}/{len(candidates)}) exceeds limit {max_ratio:.2%} for '{source_path}'.")
        raise RuntimeError(f"Fail-safe: 孤立タブの比率({ratio:.0%})が上限({max_ratio:.0%})を超えるため、削除を中断します。")
    if len(orphans) > max_count:
        logger.error(f"Fail-safe triggered: Orphan count {len(orphans)} exceeds limit {max_count} for '{source_path}'.")
        raise RuntimeError(f"Fail-safe: 孤立タブ数({len(orphans)})が上限({max_count})を超えるため、削除を中断します。")

    logger.warning(f"Orphan tabs detected for '{source_path}' ({len(orphans)}): {sorted(orphans)}")
    return orphans


def generate_phase1_payload(
    diffs: List[FileDiff],
    current_tab_map: Dict[str, Dict[str, Any]],
    orphan_cleanup: bool = False,
    source_path: Optional[str] = None,
    other_prefixes: Sequence[str] = (),
    max_ratio: float = ORPHAN_MAX_RATIO,
    max_count: int = ORPHAN_MAX_COUNT,
) -> List[Dict[str, Any]]:
    """
    Phase 1: タブの作成と削除を行うリクエストを生成する。
    orphan_cleanup=True の場合（--force 時のみ）、GitHub 側に存在しない孤立タブの deleteTab も生成する。
    """
    requests: List[Dict[str, Any]] = []

    orphan_names: List[str] = []
    if orphan_cleanup:
        if not source_path:
            raise ValueError("source_path is required when orphan_cleanup is enabled.")
        orphan_names = detect_orphan_tabs(
            diffs, current_tab_map, source_path, other_prefixes, max_ratio, max_count
        )
    
    if diffs and all(diff.status == 'removed' for diff in diffs):
        logger.error("Fail-safe triggered: All detected diffs are 'removed'. Aborting sync to prevent total document deletion.")
        raise RuntimeError("Fail-safe: 監視対象パス内の全ファイルが削除対象になっています。不正な全削除を防ぐため処理を中断します。")

    # 事前に最終的なタブ数をシミュレーションして上限チェック
    expected_tab_names = set(current_tab_map.keys()) - set(orphan_names)
    for diff in diffs:
        target_tab_name = diff.target_tab_name
        if diff.status == 'removed':
            expected_tab_names.discard(target_tab_name)
        elif diff.status in ['added', 'modified']:
            expected_tab_names.add(target_tab_name)
        elif diff.status == 'renamed':
            if diff.previous_filename:
                old_tab_name = format_tab_title(diff.previous_filename)
                expected_tab_names.discard(old_tab_name)
            expected_tab_names.add(target_tab_name)

    if len(expected_tab_names) > 100:
        logger.error(f"Fail-safe triggered: Estimated tab count ({len(expected_tab_names)}) exceeds the Google Docs limit of 100.")
        raise RuntimeError(f"1ドキュメントのタブ数上限(100)を超過するため処理を中断します（予測タブ数: {len(expected_tab_names)}）。対象ファイルを減らすか設定を分割してください。")

    already_deleted: set = set()
    for orphan_name in orphan_names:
        requests.append({
            "deleteTab": {
                "tabId": current_tab_map[orphan_name]['tabId']
            }
        })
        already_deleted.add(orphan_name)

    for diff in diffs:
        target_tab_name = diff.target_tab_name
        
        if diff.status == 'removed':
            if target_tab_name in already_deleted:
                continue
            if target_tab_name in current_tab_map:
                requests.append({
                    "deleteTab": {
                        "tabId": current_tab_map[target_tab_name]['tabId']
                    }
                })
            else:
                logger.warning(f"Skipping deletion for '{target_tab_name}': Tab does not exist.")
        elif diff.status in ['added', 'modified', 'renamed']:
            # 追加、または既存タブマップに存在しない場合は新規作成 (addDocumentTab)
            if target_tab_name not in current_tab_map:
                requests.append({
                    "addDocumentTab": {
                        "tabProperties": {
                            "title": target_tab_name
                        }
                    }
                })
                
            # renamedの場合は古いタブの削除も同時に行う
            if diff.status == 'renamed' and diff.previous_filename:
                old_tab_name = format_tab_title(diff.previous_filename)
                if old_tab_name in current_tab_map and old_tab_name not in already_deleted:
                    requests.append({
                        "deleteTab": {
                            "tabId": current_tab_map[old_tab_name]['tabId']
                        }
                    })
                    
    return requests

def generate_phase2_payload(diffs: List[FileDiff], latest_tab_map: Dict[str, Dict[str, Any]], file_contents: Dict[str, str]) -> List[Dict[str, Any]]:
    """
    Phase 2: 最新のtabIdを用いて、コンテンツの全置換(deleteContentRange + insertText)を行うリクエストを生成する。
    """
    requests = []
    
    for diff in diffs:
        if diff.status in ['added', 'modified', 'renamed']:
            target_tab_name = diff.target_tab_name
            content = file_contents.get(diff.filename, "")
            
            if target_tab_name not in latest_tab_map:
                logger.error(f"Tab '{target_tab_name}' not found in Phase 2 despite Phase 1 execution. Skipping content update.")
                continue
                
            tab_id = latest_tab_map[target_tab_name]['tabId']
            end_index = latest_tab_map[target_tab_name].get('endIndex', 2)
            delete_end_index = end_index - 1
            
            # ① 先に既存コンテンツを全削除（空タブの場合はスキップ）
            if delete_end_index > 1:
                requests.append({
                    "deleteContentRange": {
                        "range": {
                            "startIndex": 1,
                            "endIndex": delete_end_index,
                            "tabId": tab_id
                        }
                    }
                })
            
            # ② インデックス 1 の位置から新規テキストを挿入
            if content:
                requests.append({
                    "insertText": {
                        "location": {
                            "index": 1,
                            "tabId": tab_id
                        },
                        "text": content
                    }
                })
                
    return requests

def _request_tab_id(request: Dict[str, Any]) -> Any:
    """リクエストが対象とする tabId を返す（deleteContentRange / insertText 用）。"""
    body: Dict[str, Any] = next(iter(request.values()), {})
    if 'range' in body:
        return body['range'].get('tabId')
    if 'location' in body:
        return body['location'].get('tabId')
    return None

def split_requests_into_chunks(
    requests: List[Dict[str, Any]],
    max_tabs: int = 10,
    max_requests: int = 50
) -> List[List[Dict[str, Any]]]:
    """
    Phase 2 のリクエストをタブ単位でグループ化し、1チャンクあたり最大 max_tabs タブ /
    max_requests リクエストに収まるよう分割する。
    同一タブの deleteContentRange と insertText は必ず同一チャンクに含め、アトミック性を維持する。
    """
    groups: List[List[Dict[str, Any]]] = []
    last_tab_id: Any = object()
    for req in requests:
        tab_id = _request_tab_id(req)
        if groups and tab_id is not None and tab_id == last_tab_id:
            groups[-1].append(req)
        else:
            groups.append([req])
        last_tab_id = tab_id

    chunks: List[List[Dict[str, Any]]] = []
    current: List[Dict[str, Any]] = []
    current_groups = 0
    for group in groups:
        if current and (current_groups >= max_tabs or len(current) + len(group) > max_requests):
            chunks.append(current)
            current = []
            current_groups = 0
        current.extend(group)
        current_groups += 1
    if current:
        chunks.append(current)
    return chunks

def apply_batch_update(service: Any, document_id: str, requests: List[Dict[str, Any]]) -> None:
    """
    リクエストが存在する場合に batchUpdate を実行する。
    """
    if not requests:
        return
        
    try:
        service.documents().batchUpdate(
            documentId=document_id,
            body={'requests': requests}
        ).execute()
        logger.info(f"Successfully applied batch update with {len(requests)} requests for document {document_id}.")
    except HttpError as e:
        logger.error(f"Google Docs API HTTP Error during batchUpdate on document {document_id}: {e}")
        raise
