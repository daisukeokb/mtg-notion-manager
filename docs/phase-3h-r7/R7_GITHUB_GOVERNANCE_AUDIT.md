# Phase 3H-R7 GitHub Governance Audit(G-1)

GitHubのlive設定を2026-11-15に `gh api` の読み取り(GET)だけで確認した。設定は一切変更していない(R7の範囲外)。

## 1. 実測値

| 評価項目 | 確認方法 | 実測値 |
|---|---|---|
| Repository | `GET repos/{owner}/{repo}` | public、default branch `main`、owner typeは個人(`User`)。merge方式はmerge・squash・rebaseすべて許可、auto-merge無効、merge後のbranch自動削除は無効 |
| Collaborator | `GET repos/{owner}/{repo}/collaborators` | 1名(role `admin`)。実行したtokenの権限もadmin |
| Branch Protection(classic) | `GET .../branches/main/protection` | `404 Branch not protected`(classicの保護は未設定) |
| Repository Rulesets | `GET .../rulesets` | 1件 `Protect main`(id 18841702、`enforcement: active`、対象 `refs/heads/main`) |
| Ruleset bypass actors | `GET .../rulesets/18841702` | `bypass_actors: []`、`current_user_can_bypass: never` |
| Mainに有効なrule | `GET .../rules/branches/main` | `deletion`、`non_fast_forward`、`pull_request`、`required_status_checks` |
| Required Pull Request | `pull_request` rule | 有効(mainへの変更はPR経由) |
| Required Approvals | 同 `required_approving_review_count` | **0** |
| CODEOWNERS | `git ls-files` に無し。`GET .../codeowners/errors` = 404 | 無し。`require_code_owner_review: false` |
| Review dismissal / Stale approval dismissal | `dismiss_stale_reviews_on_push` | `false` |
| Last push approval | `require_last_push_approval` | `false` |
| Required reviewers | `required_reviewers` | `[]` |
| Review thread resolution | `required_review_thread_resolution` | `true` |
| Required Status Checks | `required_status_checks` | `test`・`lint`・`type-check`(GitHub Actions、`strict_required_status_checks_policy: true`)。**`generation-workspace` は必須ではない** |
| Direct push restrictions | `pull_request` rule + bypass無し | mainへの直接pushはrulesetにより拒否される(bypass可能な主体なし) |
| Force push restrictions | `non_fast_forward` | 有効。repositoryの `allow_force_pushes` は `null`(classic保護が無いため) |
| Branch deletion | `deletion` | 有効(mainの削除を禁止) |
| Admin bypass | bypass actors | 無し。ただし**adminはrulesetそのものを編集・無効化できる**(GitHubの権限モデル。rulesetの変更は本auditでは確認していない操作) |
| Workflow permissions | `GET .../actions/permissions/workflow` | `default_workflow_permissions: read`、`can_approve_pull_request_reviews: false` |
| Actions policy | `GET .../actions/permissions` | 有効、`allowed_actions: all`、`sha_pinning_required: false`(SHA固定は `ci/static_guard.py` がworkflowの内容として強制) |

## 2. 評価

- **CIのPASSはGitHub GovernanceのPASSではない。** 必須checkは、PRの中身(`ci/static_guard.py` とworkflowそのもの)が同じPRで変更できる(G-1の原定義、PR #48)。
- **実効的なreview enforcementは無い。** 必須approvalが0、CODEOWNERS無し、collaboratorは1名だけ。PRは作成者本人がmergeでき、第三者の承認は技術的に要求されない。GitHubは作成者自身のapprovalを数えないため、collaboratorが1名の現状で必須approvalを1以上にすると、merge自体ができなくなる(構造的な制約)。
- 直接push・force push・mainの削除はrulesetで拒否され、bypass主体も無い。ただしadminはrulesetを変更できるため、これは「誤操作の防止」であり「adminに対する強制」ではない。
- `generation-workspace` jobは必須checkではない。失敗してもmerge可能(R7の新しい所見。Generation Workspaceは製品機能と無関係の独立package)。

## 3. 判定

| Gate | 判定 | 理由 |
|---|---|---|
| G7 GitHub Governance | **FAIL** | review enforcementが実効的でないことを、live設定で確認した(取得できない値は無かったため、UNVERIFIEDではない) |

**Cutover Blocker B-G1を設定する**: 本番移行の承認条件として、次のいずれかが必要。

1. reviewを技術的に強制する構成(第2のreviewer、必須approval ≥ 1、CODEOWNERS、stale approvalのdismiss)を導入する。GitHub設定の変更はR7の範囲外で、別の承認が要る。
2. または、人間の承認者が、collaborator 1名の構造上の制約を理解したうえで、GitHub外のreview(本R7のHuman Acceptanceの記録そのもの)を補償統制として明示的に受け入れる。

どちらを選ぶかは人間の判断であり、R7は代わりに判断しない。

## 4. 本auditの限界

- 設定は確認時点の値で、以後の変更は検知しない。
- rulesetの変更履歴(audit log)は確認していない(個人repositoryではAPIで取得できない範囲がある)。
- 確認に使った `gh` の認証はadmin権限を持つ。読み取りのGETだけを実行した。
