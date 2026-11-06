import os
import logging
import requests
import yaml
from typing import Dict, List, Optional

from src.models import FileDiff

logger = logging.getLogger(__name__)

def get_previous_commit_sha(owner: str, repo: str) -> Optional[str]:
    """
    last_commit_sha_{owner}_{repo}.txt から前回実行時のコミットSHAを読み込む。
    """
    file_path = f'last_commit_sha_{owner}_{repo}.txt'
    if not os.path.exists(file_path):
        logger.warning(f"{file_path} not found. This might be the first run.")
        return None
    with open(file_path, 'r', encoding='utf-8') as f:
        sha = f.read().strip()
        if not sha:
            logger.warning(f"{file_path} is empty.")
            return None
        return sha

def get_latest_commit_sha(owner: str, repo: str, token: str, branch: str = 'main') -> str:
    url = f"https://api.github.com/repos/{owner}/{repo}/commits/{branch}"
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28"
    }
    logger.info(f"Fetching latest commit SHA from {url}")
    try:
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        return response.json().get('sha')
    except requests.exceptions.RequestException as e:
        logger.error(f"GitHub API request failed: {e}")
        if e.response is not None:
            logger.error(f"Response body: {e.response.text}")
        raise ValueError(f"Failed to fetch latest commit sha: {e}")

class FullSyncDiffs(List[FileDiff]):
    """
    全量同期で取得した FileDiff のリスト。
    GitHub tree API のレスポンスが切り詰められた場合 (truncated=true) は truncated=True となる。
    """
    truncated: bool = False


def fetch_all_files_as_added(owner: str, repo: str, head_sha: str, token: str) -> List[FileDiff]:
    url = f"https://api.github.com/repos/{owner}/{repo}/git/trees/{head_sha}?recursive=1"
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28"
    }
    
    logger.info(f"Performing full sync: fetching all files from tree {head_sha}")
    try:
        response = requests.get(url, headers=headers)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        logger.error(f"GitHub API request failed: {e}")
        if e.response is not None:
            logger.error(f"Response body: {e.response.text}")
        raise ValueError(f"Failed to fetch tree: {e}")
        
    response_json = response.json()
    tree = response_json.get('tree', [])
    
    diffs = FullSyncDiffs()
    diffs.truncated = bool(response_json.get('truncated', False))
    if diffs.truncated:
        logger.warning(f"Git tree response for {owner}/{repo}@{head_sha} is truncated. File list may be incomplete.")
    for item in tree:
        if item.get('type') == 'blob':
            filename = item.get('path')
            if not (filename.endswith('.md') or filename.endswith('.txt')):
                continue
                
            raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/{head_sha}/{filename}"
            diffs.append(FileDiff(
                filename=filename,
                previous_filename=None,
                status='added',
                raw_content_url=raw_url,
                commit_sha=head_sha
            ))
            
    return diffs

def fetch_commit_diffs(base_sha: str, head_sha: str, owner: str, repo: str, token: str) -> List[FileDiff]:
    """
    GitHub Compare APIを呼び出し、差分ファイルをFileDiffのリストとして返す。
    """
    url = f"https://api.github.com/repos/{owner}/{repo}/compare/{base_sha}...{head_sha}"
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28"
    }
    
    logger.info(f"Fetching commit diffs from {url}")
    try:
        response = requests.get(url, headers=headers)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        logger.error(f"GitHub API request failed: {e}")
        if e.response is not None:
            logger.error(f"Response body: {e.response.text}")
        raise ValueError(f"Failed to fetch commit diffs: {e}")

    data = response.json()
    files = data.get('files', [])
    
    diffs = []
    for f in files:
        filename = f.get('filename', '')
        if not (filename.endswith('.md') or filename.endswith('.txt')):
            continue
            
        status = f.get('status')
        if status not in ['added', 'modified', 'removed', 'renamed']:
            logger.warning(f"Unexpected status '{status}' for file {filename}. Treating as modified.")
            status = 'modified'
            
        diffs.append(FileDiff(
            filename=filename,
            previous_filename=f.get('previous_filename'),
            status=status,
            raw_content_url=f.get('raw_url'),
            commit_sha=head_sha
        ))
        
    return diffs

def fetch_repository_variables(owner: str, repo: str, ref: str, token: str, variables_dir: str = 'data/variables') -> Dict[str, Dict[str, str]]:
    """
    リポジトリの data/variables/ 配下の .yml/.yaml を取得・パースし、
    {"product": {...}, "copilot": {...}} 形式の二重辞書として返す。
    取得・パースに失敗した場合は警告/エラーログを出力し、該当分をスキップする。
    """
    headers: Dict[str, str] = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28"
    }
    list_url = f"https://api.github.com/repos/{owner}/{repo}/contents/{variables_dir}"
    logger.info(f"Fetching variable definitions from {list_url}")
    try:
        response = requests.get(list_url, headers=headers, params={"ref": ref})
        if response.status_code == 404:
            logger.warning(f"{variables_dir} not found in {owner}/{repo}@{ref}. No variables will be resolved.")
            return {}
        response.raise_for_status()
        entries = response.json()
    except requests.exceptions.RequestException as e:
        logger.error(f"GitHub API request failed: {e}")
        if e.response is not None:
            logger.error(f"Status code: {e.response.status_code}, Response body: {e.response.text}")
        return {}
    except ValueError as e:
        logger.error(f"Invalid JSON response when listing {variables_dir}: {e}")
        return {}

    if not isinstance(entries, list):
        logger.warning(f"Unexpected response when listing {variables_dir}; expected a directory.")
        return {}

    result: Dict[str, Dict[str, str]] = {}
    for entry in entries:
        name: str = entry.get('name', '')
        if entry.get('type') != 'file' or not name.endswith(('.yml', '.yaml')):
            continue
        download_url: Optional[str] = entry.get('download_url')
        if not download_url:
            logger.warning(f"No download_url for {name}. Skipping.")
            continue
        key = name.rsplit('.', 1)[0]
        try:
            file_resp = requests.get(download_url, headers=headers)
            file_resp.raise_for_status()
            parsed = yaml.safe_load(file_resp.text)
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to fetch variable file {name}: {e}")
            if e.response is not None:
                logger.error(f"Status code: {e.response.status_code}, Response body: {e.response.text}")
            continue
        except yaml.YAMLError as e:
            logger.error(f"Failed to parse YAML file {name}: {e}")
            continue
        if not isinstance(parsed, dict):
            logger.warning(f"Variable file {name} is not a mapping. Skipping.")
            continue
        result[key] = parsed

    logger.info(f"Loaded variable files: {sorted(result.keys())}")
    return result

def analyze_folder_rename(diffs: List[FileDiff], repository: str, config_path: str = 'config.yaml') -> None:
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f) or {}
    except FileNotFoundError:
        logger.warning(f"Config file not found for rename analysis: {config_path}")
        return
    except yaml.YAMLError as e:
        logger.error(f"Failed to parse config file for rename analysis: {e}")
        return

    sync_targets = config.get('sync_targets', [])
    target = next((t for t in sync_targets if t.get('repository') == repository), None)
    if not target:
        logger.warning(f"Repository {repository} not found in config.")
        return

    watch_folders = target.get('watch_folders', [])
    config_updated = False

    for folder_info in watch_folders:
        watch_folder = folder_info.get('source_path')
        if not watch_folder:
            continue

        watch_prefix = watch_folder if watch_folder.endswith('/') else f"{watch_folder}/"

        files_originally_in_watch = [
            d for d in diffs 
            if (d.previous_filename and d.previous_filename.startswith(watch_prefix)) 
            or (d.filename.startswith(watch_prefix) and d.status != 'renamed')
        ]
        
        if not files_originally_in_watch:
            continue

        renamed_from_watch = [
            d for d in diffs 
            if d.status == 'renamed' and d.previous_filename and d.previous_filename.startswith(watch_prefix)
        ]
        
        ratio = len(renamed_from_watch) / len(files_originally_in_watch)
        if ratio >= 0.8 and len(renamed_from_watch) > 0:
            from collections import Counter
            new_folders = []
            for d in renamed_from_watch:
                rel_path = d.previous_filename[len(watch_prefix):]
                if d.filename.endswith(rel_path):
                    new_prefix = d.filename[:-len(rel_path)]
                    new_prefix = new_prefix.rstrip('/')
                    new_folders.append(new_prefix)
                else:
                    new_folders.append(os.path.dirname(d.filename))
                    
            most_common_new_folder = Counter(new_folders).most_common(1)[0][0]
            
            logger.warning(f"Detected folder rename from {watch_folder} to {most_common_new_folder} (Ratio: {ratio:.2f}). Auto-healing config.")
            
            folder_info['source_path'] = most_common_new_folder
            config_updated = True

    if config_updated:
        try:
            with open(config_path, 'w', encoding='utf-8') as f:
                yaml.safe_dump(config, f, allow_unicode=True, sort_keys=False)
            logger.info("config.yaml has been successfully auto-healed.")
        except Exception as e:
            logger.error(f"Failed to auto-heal config.yaml: {e}")
            raise

def validate_failsafe(diffs: List[FileDiff], watch_folder: str) -> bool:
    watch_prefix = watch_folder if watch_folder.endswith('/') else f"{watch_folder}/"
    
    relevant_diffs = [
        d for d in diffs 
        if d.filename.startswith(watch_prefix) or (d.previous_filename and d.previous_filename.startswith(watch_prefix))
    ]
    
    if not relevant_diffs:
        return True
        
    all_removed = all(d.status == 'removed' for d in relevant_diffs)
    
    if all_removed and len(relevant_diffs) > 0:
        logger.error(f"Failsafe triggered: All {len(relevant_diffs)} relevant files in '{watch_folder}' are marked as 'removed'. Aborting to prevent destructive deletion.")
        raise ValueError(f"Failsafe triggered: All files in '{watch_folder}' removed.")
        
    return True
