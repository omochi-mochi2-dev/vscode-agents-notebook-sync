import os
import logging
from typing import Dict, Any, List, Tuple
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

def generate_phase1_payload(diffs: List[FileDiff], current_tab_map: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Phase 1: タブの作成と削除を行うリクエストを生成する。
    """
    requests = []
    
    if diffs and all(diff.status == 'removed' for diff in diffs):
        logger.error("Fail-safe triggered: All detected diffs are 'removed'. Aborting sync to prevent total document deletion.")
        raise RuntimeError("Fail-safe: 監視対象パス内の全ファイルが削除対象になっています。不正な全削除を防ぐため処理を中断します。")

    # 事前に最終的なタブ数をシミュレーションして上限チェック
    expected_tab_names = set(current_tab_map.keys())
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

    for diff in diffs:
        target_tab_name = diff.target_tab_name
        
        if diff.status == 'removed':
            if target_tab_name in current_tab_map:
                tab_id = current_tab_map[target_tab_name]['tabId']
                requests.append({
                    "deleteTab": {
                        "tabId": tab_id
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
                if old_tab_name in current_tab_map:
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
