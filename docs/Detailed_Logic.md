# Detailed Logic & Data Models: GitHub ↔ Google Docs Sync System

## 1. Pythonデータモデル定義（データ構造の強制）

モジュール間（GitHub API層 ↔ Google Docs API層）でのデータ受け渡しには、以下の `dataclass` を厳密に使用すること。AIエージェントによる独自のプロパティの追加・変更は許可しない。

```python
from dataclasses import dataclass
from typing import Literal, Optional

@dataclass
class FileDiff:
    filename: str
    previous_filename: Optional[str]
    status: Literal['added', 'modified', 'removed', 'renamed']
    raw_content_url: Optional[str]
    commit_sha: str

    @property
    def target_tab_name(self) -> str:
        """フラットマッピング戦略に基づくタブ名の生成"""
        # フォルダ構造の階層 (/) や拡張子をアンダースコアに置換しフラットな第一階層タブ名を作成
        return self.filename.replace('/', '_').replace('.', '_')
```

---

## 2. GitHub Compare API の解析要件

- **APIエンドポイント**: `GET /repos/{owner}/{repo}/compare/{base}...{head}`
- **処理ロジック**:
  - `last_commit_sha.txt` に記録された前回実行時のコミットSHAを `base`、現在の最新コミットSHA（HEAD）を `head` として比較を実行する。
  - レスポンスの `files` 配列をループ処理し、上記定義の `FileDiff` オブジェクトのリストを生成する。

### 異常検知とフェイルセーフの強制

- 抽出した `FileDiff` リストを評価し、監視対象パスに紐づくファイルが**すべて `status: "removed"`** となっている場合（全消失）は、処理の続行をフラグ等でブロックし、後続の Google Docs API による「タブの全削除」を物理的に実行させない例外処理を実装すること。

---

## 3. Google Docs API 更新ロジックの厳格な仕様

Google Docs APIは、インデックスが狂うことによるドキュメントの破壊を防ぐため、以下のロジック仕様を徹底すること。

### ① タブ構造の完全取得

- `documents.get` メソッド呼び出し時は、必ずパラメータとして **`includeTabsContent=true`** を明示的に指定すること。
- これを怠ると、下位互換性のために最初のタブしか読み込まれず、他のタブ情報が完全に隠蔽（サイレントに無視）されてしまう。

### ② 後方からの処理（Write Backwards）戦略

- 複数のテキスト挿入や削除を一度に行う場合、インデックスのズレ（文字数がずれて意図しない場所を削除・上書きすること）を防ぐため、ドキュメントの末尾（インデックスの大きい方）から先頭に向かって逆順に処理（Write Backwards）を行うロジックを構築すること。

### ③ アトミックな更新処理（削除 & 挿入）

- 同一タブ内のコンテンツを更新（上書き）する際、**既存テキストの全範囲の削除（`deleteContentRange`）** と **新規テキストの挿入（`insertText`）** を、`documents.batchUpdate` 内の同一リクエストリスト（Requests配列）にまとめて**アトミックに送信・実行**すること。
- リクエストオブジェクトの `Location` オブジェクトには、必ず対象の **`tabId`** を指定すること。

### ④ APIペイロード（リクエスト）の構造例

以下の構造に従って `documents.batchUpdate` のリクエストを組み立てること。

```json
{
  "requests": [
    {
      "deleteContentRange": {
        "range": {
          "startIndex": 1,
          "endIndex": 500000,
          "tabId": "TAB_ID_HERE"
        }
      }
    },
    {
      "insertText": {
        "location": {
          "index": 1,
          "tabId": "TAB_ID_HERE"
        },
        "text": "# ここに新規テキストが挿入されます\n\nMarkdownのプレーンテキストが入ります。"
      }
    }
  ]
}
```

---

### ⑤ 孤立タブ（Orphan Tabs）の自動クリーンアップ（`--force` 時のみ）

GitHub 側で削除・リネームされたファイルのタブが Google Docs に残留するのを防ぐため、**`--force`（全量同期）実行時に限り**、GitHub 側に存在しないタブを `deleteTab` で削除する。通常の差分同期では実行しない。

- **特定ロジック** (`detect_orphan_tabs`):
  - `watch_folder.source_path` に `/`→`_`、`.`→`_` の置換を適用し、末尾に `_` を付けたものをプレフィックスとする（`build_tab_prefix`）。ハッシュ化タブ名（先頭41文字 + `_` + ハッシュ）に一致するよう、プレフィックスは最大41文字に切り詰める。
  - 既存タブ名のうち、このプレフィックスを持つものを候補とする。プレフィックスを持たない手動作成タブは対象外。
  - 同一ドキュメントの別 `watch_folder` のプレフィックスが自身を内包する場合（例: `docs_` と `docs_extra_`）、そのタブは除外する。
  - 今回同期対象（`added` / `modified` / `renamed`）の `target_tab_name` 集合 `expected_tab_names` を引いた残りを孤立タブとする。
- **Phase 1 への組み込み**: `generate_phase1_payload(..., orphan_cleanup=True, source_path=...)` が孤立タブの `deleteTab` を、`addDocumentTab` 等より前に生成する。100タブ上限の予測にも反映する。デフォルトは `orphan_cleanup=False`（挙動変更なし）。
- **フェイルセーフ**（いずれかに該当すると `RuntimeError` で中断し、削除は1件も行わない）:

  | ID | 条件 | 既定値 |
  |---|---|---|
  | G1 | 候補タブが存在するのに `expected_tab_names` が 0 件 | - |
  | G2 | 孤立タブ数 ÷ 候補タブ数 > `max_ratio` | 0.5 |
  | G3 | 孤立タブ数 > `max_count` | 20 |
  | G4 | GitHub tree API が `truncated: true`（一覧が不完全）の場合は孤立タブ削除自体を無効化 | - |

  - 上限値は `config.yaml` の任意項目 `orphan_cleanup.max_ratio` / `orphan_cleanup.max_count` で上書きできる。
  - 削除対象のタブ名は、削除前に警告ログへ出力する。

---

## 4. GitHub Actions ワークフロー自動化要件

- **トリガー**: `schedule` (cronによる1時間ごとの定期実行) および手動実行用の `workflow_dispatch`。
- **状態管理**: `actions/cache` を使用し、同期を完了したGitのコミットハッシュ（`last_commit_sha.txt`）をキャッシュとして永続化・管理する。キャッシュキーは `sync-sha-${{ github.run_id }}` などの動的なバリエーションを設定すること。
