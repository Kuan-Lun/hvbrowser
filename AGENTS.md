# AGENTS.md

## 政策來源

- 本檔是此 repository 的唯一代理開發政策來源。
- 其他代理入口只能要求完整閱讀本檔，不得複製另一份政策。
- 可執行規則以 repository 內的 scripts 與設定檔為準。

## 溝通

- 最終回覆一律使用繁體中文。
- 程式碼、識別字、命令、檔名與 commit message 可使用英文。
- 不得為了承載回覆而新增 Markdown 文件。
- 移除 compatibility path、改變公開行為或採用例外時，必須在對話及
  最終回覆中明確說明。

## 設計與修改原則

- 除使用者在目前任務明確指定限制外，設計時不以最小修改或向後相容
  作為限制；在已授權任務範圍內以程式碼品質優先，選擇架構、可讀性與
  可測試性最好的完整結果。
- 開始實作時須說明上述設計原則；完成時須如實說明是否遵守，以及具體
  的設計取捨。若實際受到最小修改或向後相容限制，須揭露限制來源及
  影響，不得宣稱未受限制。
- 綜合考慮 SOLID、KISS、YAGNI、內聚性與低耦合。
- 必要的局部重構可直接納入任務。
- 若會實質擴大任務範圍、改變原要求未涵蓋的公開行為，或引入資料遷移，
  必須先取得使用者同意。
- 任務直接涉及的 legacy compatibility code 應移除，不保留 shim；不全面
  清理與任務無關的 legacy code。
- generated output 不得直接修改；必須修改 generator 或 source 後重新產生。

## Issue 與任務範圍

- 每批開始先在既有 issue 或任務紀錄明列目標、包含與不包含的範圍、必要驗收及
  完成條件。品質優先適用於這個範圍；完成不要求清空 repository 的 issue。
  發現、登記或評估 issue 不代表接手實作，也不自動擴大目前任務。
- 每個 issue 聚焦一個可獨立追蹤的問題，以未參與對話的接手者為讀者。標題說明
  問題或預期成果，開頭交代受影響功能、現況與影響，不以對話經過代替背景。
- Bug 記錄環境、重現方式、預期與實際結果；未能重現時明列已知證據與限制。
  改善工作列明能力缺口、範圍與完成條件；調查工作列明待解問題、已知證據及
  結束調查的判準，不把推測或未完成審查的線索寫成已確認故障。
- 背景、範圍與可觀察的驗收結果各寫在適當位置，不重複清單或強制套滿模板。
  只列有助接手的檔案、符號、版本及契約參照，區分既定需求、建議與待決事項；
  一般實作選擇不包裝成使用者必須決定的取捨。發布前確認內容不靠聊天或不可
  存取的暫存日誌也能理解，查核既有 issue 去重並保留必要證據與相依關係。
- 有 GitHub 的 repository 以 GitHub Issues 為唯一問題追蹤來源；同一 repository
  的其他 checkout 或 NAS 副本不另建一套。純本機 repository 使用納入版本控制的
  `issues/`，每項一份檔案，保留穩定編號、狀態、範圍、驗收及證據；關閉後保留
  紀錄。沒有已知問題時不建立空 issue。尚未發布的草稿須明列待發布狀態。
- 審查須區分「本次阻擋缺陷」與「另案觀察」。前者是本次變更引入或惡化的問題，
  或有具體證據顯示未達事先約定的交付條件；必須修復並驗證。既存且與本次交付
  無關的缺陷、改善及安全疑慮另開 issue，不因發現它們就阻止本次合併或擴大工作。
  分類須核對基準、受查版本、觸發條件及影響；無法釐清是否影響本次交付時，
  明示未完成核對，不假稱無關。若需要改變已約定範圍，先說明因果關係及選項。
- 完整保留原始審查結果，分別記錄本次阻擋缺陷與另案觀察；每項包含位置、
  觸發、影響、基準依據與未驗證限制。Reviewer 維持
  唯讀，不發布 issue。主代理在本輪收尾前將觀察連到 GitHub issue／本機編號；
  重複、已修復或不成立的項目記錄對應連結、修正版本或理由，逐項核對去向。
  發布失敗保留待發布項目，不宣稱成功；不得刪掉 finding 或改寫舊 receipt 以通過。
- 本次阻擋問題整批修復、相關檢查通過後再審查最終候選；沒有本次未解缺陷且
  必要 gate 通過才合併。另案 issue 的優先順序與實作另行選定；不得把 backlog
  清空當成這次工作的完成條件。

## 工作樹與 Git

- 唯讀分析不建立 branch。
- 凡會修改 tracked files 的任務，使用
  `scripts/detect-primary-branch.sh` 判定 primary，並建立專用 task branch。
- 不得 stash、reset、clean、覆寫或混入既有使用者修改。
- 工作樹不乾淨時，從 committed primary 建立獨立 worktree。
- task branch 可包含多個邏輯 Conventional Commits。避免巨大 commit；小而
  內聚的任務仍可只有一個 commit。
- 任務完成後執行 `scripts/git-flow-merge.sh`。該腳本負責完整 gate、
  `--no-ff` merge、安全移除 task worktree，以及以 `git branch -d` 刪除
  已合併的本機 branch。
- merge conflict 或 gate failure 時必須 abort merge並保留 task branch。
- merge 後收到的任何 follow-up 都建立新的 task branch。
- 本機 task branch、commit、`--no-ff` merge與 `branch -d` 已獲預先授權。
- fetch、pull、push、remote branch、tag、release、publish、deploy與任何
  force 操作仍須逐次明確授權。
- 不得使用 `--no-verify`。

## 提交格式

- 所有非 merge commit 必須符合 Conventional Commits。
- Breaking change 使用 `type!:` 或 `BREAKING CHANGE:` footer。
- project version 更新使用獨立 commit：
  `chore(release): bump version to X.Y.Z`。

## 版本政策

- `pyproject.toml` 的 `[project].version` 是唯一 project version source。
- project version 固定使用 `X.Y.Z`。
- 1.0 前，`Y` 是 compatibility lane，`Z` 是同一 lane 內的相容 release
  counter。相容修正或功能遞增 `Z`；breaking change遞增 `Y` 並將 `Z`
  歸零。
- 1.0 後使用標準 Semantic Versioning。
- 整個 task branch只在整合前更新一次 project version。
- shipped runtime或 deployment surface 有變更時，至少需要相容升版。
- Breaking API、CLI、config、schema、protocol、資料格式或 Python/platform
  support變更必須提高 compatibility lane或 major。
- tests、一般文件、IDE、hooks、CI與 dev-only tooling 單獨變更時不升版。
- 未分類路徑必須明確判定 impact，不得靜默當作 `none`。
- `Version-Impact: none` 必須附具體理由，並在最終回覆揭露。
- project version變更必須觸發完整 direct dependency audit。
- 先更新候選版本，再執行
  `scripts/audit-dependencies.py --review-note "相容性驗證摘要"`；升版 commit
  必須包含與候選版本及 dependency manifest相符的
  `.release/dependency-audit.json`。
- `scripts/check-version.py`驗證整個 task branch；pre-merge gate以
  `--index`驗證實際 staged merge candidate。

## 依賴與環境

- repository 必須能從單一乾淨 checkout重建，不得依賴固定 sibling clone
  路徑。
- 明確跨 repository任務可使用傳入的 wheel、Git URL/ref或 repository
  path；sibling discovery只能是選擇性的效能優化。
- Python registry dependencies原則上使用 `>=` lower bound；合理 upper
  bound與 `!=` 可以保留，但必須有相容性依據。
- 精確版本只允許經驗證且有文件理由的特殊契約。
- dependency audit必須涵蓋 build、runtime、optional與 development direct
  dependencies，並搜尋現有 upper bound之外的候選版本。
- 有新版時必須檢查 release notes、驗證相容性並嘗試修正問題。
- `uv.lock` 與 `package-lock.json` 不得成為 committed或重建輸入。
  `scripts/rebuild-env.sh` 可使用 `uv venv` 與 `uv pip`，但不得使用會依賴
  project lockfile的同步流程。
- Node tooling使用 `npm install --package-lock=false`。
- 不得依賴 system-wide lint、format、type-check或 Markdown工具。
- `requires-python` 使用 `>=3.14`；只有經驗證的壞版本可使用 `!=`。

## 品質工具

- `pyproject.toml` 是 Ruff與 mypy的唯一規則來源。
- 使用 Ruff lint與 Ruff formatter，不使用 Black。
- Ruff使用適合專案的嚴格規則集，不從 `ALL` 出發；每個停用規則必須
  記錄理由。
- mypy使用標準 `strict = true`。不得保留 `mypy.ini`。
- module例外使用精確 TOML overrides。
- `type: ignore` 必須指定 error code並附理由。
- `noqa` 必須指定 rule code並附理由。
- Markdown使用 repository-local `markdownlint-cli2`。
- VS Code使用相同設定與 repository-local environment；CLI gate是最終
  權威，IDE diagnostics為即時輔助。

## 檢查分層

- `scripts/format.sh`：明確執行會修改檔案的 formatter或 fixer。
- `scripts/check-fast.sh`：離線、唯讀的 Ruff、format check、mypy與
  markdownlint；每次非 merge commit執行。
- `scripts/check-full.sh`：fast gate、完整測試、適用時的 build與wheel smoke及本
  repository的特殊檢查；整合候選只跑一次。
- dependency audit可連網，但 hooks只驗證本機 receipt，不在 commit過程
  連網。
- GitHub Actions只呼叫相同 scripts，並保留 trusted publishing、平台特有
  或本機無法可靠重現的檢查。
- 不使用 Claude、Codex或其他 provider-specific Stop hooks重複檢查。

## 測試與例外

- runtime行為變更必須新增或更新測試；bug fix必須有 regression test。
- 新功能涵蓋正常、邊界與錯誤路徑。
- 數值測試固定隨機種子；容許誤差需有依據。
- flaky test視為失敗，不得以重跑掩蓋。
- 不設定跨 repository的統一 coverage百分比。
- live account、network、production或 destructive probe不得進入 hooks、
  一般 pytest或自動 merge gate。
- `skip` 或 `xfail` 必須有理由；`xfail` 原則上使用 `strict=True`。
- 不得為通過檢查而全域放寬工具設定。

## 完成回報

最終回覆必須包含：

- 明確說明本次是否在不考慮最小修改、也不以向後相容為限制的條件下，
  遵守程式碼品質優先要求，並提供具體依據。
- 明確判定本次是「不向後相容」或「品質優先後恰好向後相容」，說明受
  影響的介面、行為或資料格式及判定依據；不得只回覆「相容修正」或
  版本分類。若使用者明確要求保留相容，須如實說明此限制，不得稱為
  「恰好相容」。純文件或政策變更須說明沒有執行期介面或資料格式變更。
- 若不向後相容，進一步明確判定「可提供一次性相容性轉換工具」或
  「無法提供一次性相容性轉換工具」，並說明理由。尚未確認可行性時，
  須明示待確認事項，不得把未調查視為不可行。
- 轉換工具可行時，分開回報「可提供」與「已提供」；在已授權範圍內提供
  並驗證工具，附位置、使用方式、適用範圍及限制。尚未提供時須說明
  原因與待辦，不得宣稱相容性問題已解決。
- 無法提供轉換工具時，列出資料影響與確切的人工處理、刪除或重建範圍，
  並明示可保留的項目；不得只籠統要求「全部重建」。
- 實作及公開行為變化。
- 移除的 compatibility path。
- project version與 dependency audit結果。
- commits與完整檢查結果。
- primary branch與 merge commit。
- branch/worktree是否已清除。
- 是否仍未 push、publish或 deploy。

## Repository-specific policy

`hvbrowser` 是 HentaiVerse高階 browser API library。底層browser ownership
由`hbrowser`提供；本 repository擁有realm/account context、maintenance、
market與其他 HentaiVerse navigation/mutation boundaries。

- committed dependency固定指向registry release，不得硬編 sibling checkout。
  明確跨repository任務可在乾淨重建後由使用者提供local wheel/path/ref。
- 本 package直接import `zendriver.cdp`，因此必須宣告direct Zendriver
  dependency，並與`hbrowser`已驗證的exact lifecycle cohort一致。
- account、realm、document與browser identity必須在任何mutation前重新驗證；
  stale page、realm mismatch、ambiguous receipt或cleanup failure一律fail closed。
- 維持browser context/driver lifecycle單一owner，不得為便利而重複啟動、
  隱式接管或在timeout後重用可能仍有CDP operation的browser。
- live network probes不是一般品質檢查。`scripts/live_readonly_smoke.py`只能在
  使用者明確要求、提供isolated credentials並確認安全前置條件後人工執行。
- live smoke不得由pytest、hooks、`check-full.sh`、merge或CI自動執行，也不得
  進行購買、餵食、market submission、repair、recovery、start/flee battle等
  mutation。
- 一般測試必須離線，以fakes驗證正常、boundary、failure與no-mutation paths。
- full gate必須執行offline deterministic pytest、sdist/wheel build，以及從
  新建wheel path import `hvbrowser`的 smoke test；該smoke不得連網。
