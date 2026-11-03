# 账本恢复一致性修复 R1 登记簿

状态：**3799af1 复审 CHANGES-REQUIRED**。本提交只对齐观察锚点与净值刷新的 `seq` 口径，不签发 PASS，不登记新冻结点。

| 项 | 值 |
|---|---|
| 修复基线（修复前 HEAD） | `7108eb48682c2a2a51725e2e71ccfc4f11d60a2a` |
| R1 提交 | `e3592049ea41d1e07fdd28ece7d7bb646d719a23`（复审 CHANGES-REQUIRED，不是冻结点） |
| 上一轮边界 | `8069d5257ad9ecbd628e5ef21fe0b8f62ca71a54`（复审 CHANGES-REQUIRED，不是冻结点） |
| 三组收口 | `3799af1d503e0b4f28dbae823ee12405183260f8`（复审 CHANGES-REQUIRED，不是冻结点，保留不改写） |
| 本轮提交 | 本文档此次更新所在提交 |
| 改动 | `src/cts/ledger/journal.py`，`tests/test_ledger_recovery.py`，本文档 |
| 测试 | 全量通过：233 项（3799af1 的 231 项保持，本轮新增 2 项） |
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
| 同一原事件已被经济冲正 | `REVERSAL_ALREADY_APPLIED` |
| 冲正后钱包或逐仓资金为负 | `REVERSAL_BALANCE_UNDERFLOW` |

可冲正类型：`MARGIN_LOCK`、`MARGIN_RELEASE`、`FEE`、`FUNDING`。产品代码中 `reverse()` 无调用方。同一账户对同一原事件的经济冲正在持久化事务里只允许一次（部分唯一索引，陈旧实例写盘时拒绝）。透支检查只覆盖钱包、逐仓资金和相关仓位保证金；资金费等会计桶可以保持正常贷方余额。所有拒绝在持久化之前发生，拒绝前后本实例流水与内存不变。

## 3. 旧事件恢复规则：按分录事实，缺证据要明示

- 逐仓手续费：不依赖 metadata，按分录中 `ISOLATED_MARGIN` 贷方金额恢复扣减；无逐仓分录则不动仓位。
- 成交盈亏入账账户：按分录中出现的目标桶（`ISOLATED_MARGIN` 或 `WALLET`）判定，新旧事件同规则。
- 成交 `realized ≠ 0` 而无分录、或分录无法判定唯一目标桶：重放抛 `EVIDENCE_INSUFFICIENT`（`LEDGER_IMBALANCE`），不取默认值掩盖。
- 旧事件原文与旧哈希不改动；新元数据（`from_isolated`、`credit_isolated`、`nav_at_issue`）只出现在新事件上。
- 旧出入金事件无 `nav_at_issue` 时按事件推导净值发行份额（旧语义），不回写。

## 4. `books_state` 合同

- 行结构：`(account_id, anchor_seq, last_unit_nav, high_water_mark)`，`anchor_seq` 为写入时该账户最后事件 seq（按账户隔离）。
- 只提供净值与高水位，永不提供份额、余额或仓位；份额与余额一律由事件推导。
- 覆盖规则：锚点大于当前最大 seq 时整行忽略。锚点等于最大 seq 时，净值取该观察。锚点更旧时，高水位仍取较大值；若此后没有新的净值刷新，恢复这次观察的净值，锚点保持原时点，不把旧估值改记到后续事件上。若此后已有事件刷新净值（例如平仓），使用刷新后的净值，不用旧观察覆盖。观察锚点与净值刷新时点都用 `ledger_events.seq`，不用本账户事件数；其他账户插入流水后，两者仍然可比。
- 行损坏、负数或非有限值（含 `Infinity` 与 `NaN`）：`BOOKS_STATE_CORRUPT`（`SNAPSHOT_INCONSISTENT`），拒绝打开覆盖路径，不静默回退，也不把 `NaN` 比较抛成裸 `InvalidOperation`。

## 5. 拒绝不变性原子性合同

- **事件原子性**：任何被拒绝或写盘失败的事件，不改变余额、仓位、份额与事件数。
- **显式观察**：`equity_snapshot()` 可以独立提交净值与高水位；该观察不随随后失败的转账回滚。
- **转账内部的隐式估值**：必须与该笔转账在同一事务提交。写盘失败时事件、`books_state` 与内存净值／高水位全部保持原样。不能靠改写本条合同把失败转账的观察留在盘上。
- 余额不足的出金在估值之前检查：被拒时不产生新观察。
- 持仓入金成功后，即时净值、`replay()` 与重开使用同一观察（锚点等于该转账 seq）。
- 冲正：同一外部编号重复投递不重复投影；换编号不能再次经济冲正同一原事件；钱包与逐仓余额不能透支。拒绝不落盘。

## 6. 验收测试映射（`tests/test_ledger_recovery.py`）

- 三路径等价与继续操作：`test_three_path_equivalence_with_continued_operations`、`test_same_deposit_issues_same_shares_on_every_path`、`test_deposit_while_holding_issues_same_shares_on_every_path`。
- 六项复现：`test_isolated_fee_survives_replay_and_reopen`、`test_fill_credit_to_wallet_survives_replay_and_reopen`、`test_zero_nav_transfer_rejected_before_persistence_and_reopen_still_opens`、`test_observed_hwm_survives_reopen_replay_and_flat_close`、`test_reversal_syncs_position_margin_on_every_path`、`test_reversal_of_fee_and_funding_restores_position_and_counters`。
- 旧事件事实恢复与证据不足：`test_legacy_isolated_fee_recovers_margin_from_lines`、`test_legacy_fill_pnl_target_inferred_from_lines`、`test_legacy_fill_realized_without_lines_is_evidence_insufficient`。
- `books_state`：`test_books_state_stale_anchor_keeps_last_observation`、`test_books_state_supplies_only_nav_and_hwm_never_shares`、`test_books_state_out_of_range_anchor_is_ignored_entirely`、`test_books_state_corrupt_row_fails_closed`、`test_fee_after_holding_deposit_keeps_observed_nav_and_its_seq`。
- 原子性合同：`test_transfer_out_rejected_by_balance_takes_no_snapshot`、`test_transfer_disk_failure_rolls_back_implicit_observation`、`test_explicit_snapshot_survives_failed_transfer`、`test_rejected_events_change_neither_journal_nor_memory`、`test_unreversable_or_insufficient_reversal_changes_nothing`。
- 五组复审边界：`test_same_reversal_delivery_does_not_reproject`、`test_second_external_id_cannot_reverse_same_event`、`test_reversal_of_release_after_withdrawal_does_not_overdraw_wallet`、`test_deposit_while_holding_restores_mark_nav_on_every_path`、`test_books_state_rejects_non_finite_values`、`test_transfer_disk_failure_rolls_back_implicit_observation`。
- 三组收口：`test_reversal_uniqueness_is_enforced_for_a_stale_ledger`、`test_funding_credit_balance_can_be_partially_reversed`、`test_books_state_stale_anchor_keeps_last_observation`、`test_fee_after_holding_deposit_keeps_observed_nav_and_its_seq`。
- 多账户 `seq`：`test_prior_account_does_not_let_old_observation_cover_close_nav`、`test_close_nav_survives_when_account_count_equals_global_anchor`。
