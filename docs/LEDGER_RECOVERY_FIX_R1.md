# 账本恢复一致性修复 R1 登记簿

状态：**IMPLEMENTED / PENDING INDEPENDENT REVIEW**。本文是修复完成申报，不签发 PASS，不登记新冻结点。独立复审在本提交上复跑全部测试并裁定。

| 项 | 值 |
|---|---|
| 修复基线（修复前 HEAD） | `7108eb48682c2a2a51725e2e71ccfc4f11d60a2a` |
| 修复提交 | 本文档所在提交 |
| 改动 | `src/cts/ledger/journal.py`，`tests/test_ledger_recovery.py`（新增） |
| 测试 | 全量通过：222 项（原 201 项不变，新增 21 项） |
| 范围外（本轮不动） | UTC 规范哈希及历史兼容、杠杆参数合同、交易能力（`reduce_only` 禁令、UNKNOWN TTL、发单事务边界） |
| 网络与授权 | 未访问币安。网络重跑暂停，TESTNET 试单未授权，实盘 `HOLD`，`main` 不合并 |

## 1. 修复清单（对应用户独立复核确认的六项）

1. **逐仓手续费重放遗漏**：FEE 事件记录 `from_isolated`；重放投影按分录事实（`ISOLATED_MARGIN` 贷方金额）扣减仓位保证金并递增版本。
2. **盈亏入钱包语义丢失**：FILL 事件记录 `credit_isolated`；重放按分录判定入账账户（见第 3 节规则）。
3. **零净值入金破坏原子性**：净值 ≤ 0 时的出入金在持久化之前拒绝（`LEDGER_IMBALANCE`），流水与内存均不变；新库不再可被投毒，重开正常。
4. **`replay()` 净值不等价**：根因是回放与加载的每事件序列不同（净值刷新与仓位投影次序不一致）。统一为 `_load_event` 单一序列，`__init__`、`replay()` 共用。
5. **高水位无法恢复**：新增 `books_state`（按账户、锚定最后事件 seq），`equity_snapshot` 的净值/高水位观察**先落盘再改内存**；重开与 `replay()` 按同一覆盖规则恢复（合同见第 4 节）。
6. **冲正未同步仓位投影**：`reverse()` 运行时即施加逆投影，重放走同一函数；可冲正范围收窄（见第 2 节）。

过程中追加修复的三个存量等价缺口：`MARGIN_LOCK`/`MARGIN_RELEASE`/逐仓 `FUNDING` 重放不递增仓位 `version`；持仓期间入金运行时用标记价净值而重放用推导净值导致份额不同（现转账事件记录 `nav_at_issue`，重放按记录值发行）；重复投递转账在幂等/冲突判定之前做估值快照导致被拒事件改变内存净值观察（判定已前置）。

## 2. `reverse()` 行为收窄与原因码

| 拒绝情形 | reason_code |
|---|---|
| 目标不存在 | `REVERSAL_TARGET_MISSING` |
| 冲正一个冲正 | `REVERSAL_OF_REVERSAL` |
| 目标不是资金流事件（成交类、出入金） | `REVERSAL_TARGET_NOT_FLOW` |
| 目标无资金分录 | `REVERSAL_TARGET_NO_LINES` |
| 逆投影后逐仓保证金为负 | `REVERSAL_MARGIN_UNDERFLOW` |

可冲正类型：`MARGIN_LOCK`、`MARGIN_RELEASE`、`FEE`、`FUNDING`。产品代码中 `reverse()` 无调用方。所有拒绝在持久化之前发生，拒绝前后流水与内存不变（有测试钉住）。

## 3. 旧事件恢复规则：按分录事实，缺证据要明示

- 逐仓手续费：不依赖 metadata，按分录中 `ISOLATED_MARGIN` 贷方金额恢复扣减；无逐仓分录则不动仓位。
- 成交盈亏入账账户：按分录中出现的目标桶（`ISOLATED_MARGIN` 或 `WALLET`）判定，新旧事件同规则。
- 成交 `realized ≠ 0` 而无分录、或分录无法判定唯一目标桶：重放抛 `EVIDENCE_INSUFFICIENT`（`LEDGER_IMBALANCE`），不取默认值掩盖。
- 旧事件原文与旧哈希不改动；新元数据（`from_isolated`、`credit_isolated`、`nav_at_issue`）只出现在新事件上。
- 旧出入金事件无 `nav_at_issue` 时按事件推导净值发行份额（旧语义），不回写。

## 4. `books_state` 合同

- 行结构：`(account_id, anchor_seq, last_unit_nav, high_water_mark)`，`anchor_seq` 为写入时该账户最后事件 seq（按账户隔离）。
- 只提供净值与高水位，永不提供份额、余额或仓位；份额与余额一律由事件推导。
- 覆盖规则：锚点等于当前最大 seq 时，净值取行内观察值；锚点小于时净值按事件推导，高水位取两者最大；**锚点大于当前范围时整行忽略**。
- 行损坏或数值非法：`BOOKS_STATE_CORRUPT`（`SNAPSHOT_INCONSISTENT`），拒绝打开覆盖路径，不静默回退。

## 5. 拒绝不变性原子性合同

- **事件原子性**：任何被拒绝或写盘失败的事件，不改变余额、仓位、份额与事件数（含 `books_state` 之外的持久层）。
- **观察原子性**：估值快照（净值/高水位 + `books_state` 行）随观察即时提交，独立于后续事件成败；行与内存始终一致，重开与 `replay()` 复现内存值。
- 余额不足的出金在快照之前检查：被拒时不产生新观察。
- 写盘失败的持仓转账：事件状态不变，观察按上述合同提交；重试成功后三路径仍等价。

## 6. 验收测试映射（`tests/test_ledger_recovery.py`）

- 三路径等价与继续操作：`test_three_path_equivalence_with_continued_operations`、`test_same_deposit_issues_same_shares_on_every_path`、`test_deposit_while_holding_issues_same_shares_on_every_path`。
- 六项复现：`test_isolated_fee_survives_replay_and_reopen`、`test_fill_credit_to_wallet_survives_replay_and_reopen`、`test_zero_nav_transfer_rejected_before_persistence_and_reopen_still_opens`、`test_observed_hwm_survives_reopen_replay_and_flat_close`、`test_reversal_syncs_position_margin_on_every_path`、`test_reversal_of_fee_and_funding_restores_position_and_counters`。
- 旧事件事实恢复与证据不足：`test_legacy_isolated_fee_recovers_margin_from_lines`、`test_legacy_fill_pnl_target_inferred_from_lines`、`test_legacy_fill_realized_without_lines_is_evidence_insufficient`。
- `books_state`：`test_books_state_stale_anchor_keeps_event_derived_nav`、`test_books_state_supplies_only_nav_and_hwm_never_shares`、`test_books_state_out_of_range_anchor_is_ignored_entirely`、`test_books_state_corrupt_row_fails_closed`。
- 原子性合同：`test_transfer_out_rejected_by_balance_takes_no_snapshot`、`test_transfer_disk_failure_commits_observation_but_not_event`、`test_rejected_events_change_neither_journal_nor_memory`、`test_unreversable_or_insufficient_reversal_changes_nothing`。
