"""模拟执行适配器：出件箱、保护覆盖、租约与本地故障注入。"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Optional
from uuid import uuid4

from cts import LIVE_TRADING_AUTHORIZATION
from cts.checksum import sha256_hex
from cts.errors import CtsError, ExitRejectedError, NewRiskBlockedError, UnitValidationError
from cts.execution.liq import assess_liquidation, model_liq_price
from cts.execution.store import ExecutionStore
from cts.execution.venue import SimVenue
from cts.ledger.journal import Ledger
from cts.ledger.models import LedgerEventType, Side
from cts.money import D
from cts.risk.engine import RiskEngine
from cts.risk.models import ReserveRequest, RuntimeMode, SystemState
from cts.risk.parameters import admit_parameters


class SimClock:
    def __init__(self, boot_id: str = "boot-1", elapsed_ms: int = 0) -> None:
        self.boot_id = boot_id
        self.elapsed_ms = elapsed_ms

    def advance(self, ms: int) -> int:
        if ms < 0:
            raise UnitValidationError("单调时钟不能回退")
        self.elapsed_ms += ms
        return self.elapsed_ms


class Executor:
    def __init__(
        self,
        *,
        store: ExecutionStore,
        token: str,
        leased: bool,
        account_id: str,
        risk: RiskEngine,
        ledger: Ledger,
        venue: SimVenue,
        clock: SimClock,
        now: datetime,
        buffer_b: Optional[Decimal],
        mode: RuntimeMode,
    ) -> None:
        self.store = store
        self.token = token
        self.leased = leased
        self.account_id = account_id
        self.risk = risk
        self.ledger = ledger
        self.venue = venue
        self.clock = clock
        self.now = now
        self.buffer_b = buffer_b
        self.mode = mode
        self.mmr = D("0.005")
        self.fee_rate = D("0.0005")
        self.adverse_exit = D("0.0005")
        self.leverage = D("2")
        self.multiplier = D("1")
        self.lot_size = D("0.001")
        self.state = store.load(account_id) or _empty_state(clock.boot_id)
        self._on_open()

    @classmethod
    def open(
        cls,
        path: str,
        *,
        account_id: str,
        risk: RiskEngine,
        ledger: Ledger,
        venue: SimVenue,
        clock: SimClock,
        now: datetime,
        token: Optional[str] = None,
        buffer_b: Optional[Decimal] = None,
        mode: RuntimeMode = RuntimeMode.SIMULATION,
        bundle_hash: Optional[str] = None,
        downtime_bound_ms: Optional[int] = None,
        bundle: Optional["ParameterBundle"] = None,
    ) -> "Executor":
        if mode == RuntimeMode.PRODUCTION:
            if bundle is None or bundle_hash is None:
                raise CtsError("生产入口缺少已登记参数合同", reason_code="PARAM_NOT_APPROVED")
            admit_parameters(mode, bundle, bundle_hash)
            if LIVE_TRADING_AUTHORIZATION == "HOLD":
                raise CtsError("实盘下单未授权", reason_code="LIVE_TRADING_HOLD")
        store = ExecutionStore(path)
        offered = token or uuid4().hex
        held = store.claim(account_id, offered, clock.boot_id)
        executor = cls(
            store=store,
            token=offered,
            leased=held == offered,
            account_id=account_id,
            risk=risk,
            ledger=ledger,
            venue=venue,
            clock=clock,
            now=now,
            buffer_b=D(buffer_b) if buffer_b is not None else None,
            mode=mode,
        )
        if executor.leased and executor.state.get("boot_id") not in (None, "", clock.boot_id):
            if downtime_bound_ms is None:
                executor._block("CLOCK_UNANCHORED", SystemState.FAULT, "跨启动缺少可信停机上界")
                executor.state["clock_fault"] = True
            executor._save()
        elif executor.leased and executor.state.get("boot_id") == clock.boot_id:
            stored = int(executor.state.get("last_elapsed_ms") or 0)
            if clock.elapsed_ms < stored:
                clock.elapsed_ms = stored
        return executor

    def close(self) -> None:
        self.store.close()

    def poll(self) -> None:
        self._require_lease()
        elapsed = self.clock.elapsed_ms
        self._check_config_age(on_submit=False)
        self._check_funding_age()
        self._check_unknowns()
        self._check_protection()
        if self.venue.script.rate_limited:
            self._block("MARKET_HALTED", SystemState.PAUSED, "行情断流或限流，停止新开仓")
            self._alert("MARKET_HALTED", "停止新开仓，有限退避")
            self.state["backoff_ms"] = "1000"
        self.state["last_elapsed_ms"] = elapsed
        self._save()

    def observe_config(self) -> None:
        self._require_lease()
        body = self.venue.account_info(self.clock.elapsed_ms)
        self.state["config_observed_ms"] = self.clock.elapsed_ms
        self.state["config_boot"] = self.clock.boot_id
        self.state["margin_mode_actual"] = body["marginMode"]
        self.state["amr_actual"] = body["autoAddMargin"]
        detected = self.clock.elapsed_ms
        if body["marginMode"] != "ISOLATED":
            self._assertion("A01", "ACCOUNT_MODE_DRIFT", detected, detected, body)
            self._block("ACCOUNT_MODE_DRIFT", SystemState.PAUSED, f"expected=ISOLATED actual={body['marginMode']}")
            self._action("BLOCK", confirmed=True)
        elif body["autoAddMargin"] != "false":
            self._assertion("A01", "AMR_DRIFT", detected, detected, body)
            self._block("AMR_DRIFT", SystemState.PAUSED, f"auto_add_margin_actual={body['autoAddMargin']}")
            self._action("BLOCK", confirmed=True)
        self._save()

    def observe_funding_interval(self, minutes: Decimal) -> None:
        self._require_lease()
        minutes = D(minutes)
        previous = self.state.get("funding_interval")
        self.state["funding_interval"] = format(minutes, "f")
        self.state["funding_observed_ms"] = self.clock.elapsed_ms
        if previous is not None and D(previous) != minutes:
            self.state["funding_state"] = "CHANGING"
            self.state["funding_change_ms"] = self.clock.elapsed_ms
            self.state["funding_recomputed_ms"] = self.clock.elapsed_ms
            self._block("FUNDING_INTERVAL_CHANGED", SystemState.PAUSED, f"{previous}->{format(minutes, 'f')}")
            self._assertion("A03", "FUNDING_INTERVAL_CHANGED", self.clock.elapsed_ms, self.clock.elapsed_ms, {})
        elif previous is None:
            self.state["funding_state"] = "VERIFIED"
        self._save()

    def observe_tier(self, mmr: Decimal) -> None:
        self._require_lease()
        self.mmr = D(mmr)
        self.state["mmr"] = format(self.mmr, "f")
        for symbol, protection in self.state["protections"].items():
            if D(protection["required_qty"]) <= 0:
                continue
            self._recheck_symbol(symbol, D(protection["avg_price"]), D(protection["stop"]), Side(protection["side"]))
        self._save()

    def note_mark_gap(self, *, overlaps_exposure: bool) -> str:
        self.state["mark_gap_overlaps"] = overlaps_exposure
        grade = "SIGNAL_RESEARCH_ONLY"
        if overlaps_exposure:
            self._block("MARK_GAP_OVERLAPS_EXPOSURE", SystemState.PAUSED, "持仓期间标记价缺失，不能验收强平回放")
        self.state["data_grade"] = grade
        self._save()
        return grade

    def submit_entry(
        self,
        *,
        symbol: str,
        side: Side,
        entry_price: Decimal,
        stop_price: Decimal,
        signal_key: str,
        mark: Decimal,
        q_bound: Decimal = Decimal("0"),
    ) -> dict:
        self._require_lease()
        if self.state.get("restored_pending"):
            raise CtsError("备份恢复后对账前禁止新订单", reason_code="RECONCILIATION_REQUIRED")
        if self.state.get("clock_fault"):
            raise CtsError("时钟跨启动未锚定", reason_code="CLOCK_UNANCHORED")
        existing = self.state["intents"].get(signal_key)
        if existing is not None:
            return existing
        self.poll()
        self._check_config_age(on_submit=True)
        view = self.risk.occupancy()
        if view.system_state in (SystemState.PAUSED, SystemState.FAULT, SystemState.RECONCILING):
            intent = self._intent(signal_key, symbol, side, "RISK_REJECTED", reason=view.system_state.value)
            self._save()
            return intent
        try:
            reserved = self.risk.reserve(
                [
                    ReserveRequest(
                        symbol=symbol,
                        side=side,
                        signal_key=signal_key,
                        entry_price=D(entry_price),
                        stop_price=D(stop_price),
                        multiplier=self.multiplier,
                        lot_size=self.lot_size,
                        fee_rate_one_way=self.fee_rate,
                        stress_slippage=self.adverse_exit,
                    )
                ],
                now=self.now,
            )[0]
        except NewRiskBlockedError as exc:
            intent = self._intent(signal_key, symbol, side, "RISK_REJECTED", reason=exc.reason_code)
            self._save()
            return intent
        isolated = reserved.notional / self.leverage
        model = model_liq_price(
            side=side,
            avg_price=D(entry_price),
            qty=reserved.qty,
            multiplier=self.multiplier,
            isolated_margin=isolated,
            mmr=self.mmr,
            fee_rate=self.fee_rate,
        )
        quote = self.venue.liq_quote(symbol, model)
        assessment = assess_liquidation(
            side=side,
            mark=D(mark),
            stop=D(stop_price),
            model_price=model,
            venue_raw=quote,
            buffer_b=self.buffer_b,
            q_bound=D(q_bound),
        )
        if assessment.reason_code:
            self.risk.request_cancel(reserved.reservation_id, now=self.now)
            self.risk.confirm_cancel(reserved.reservation_id, Decimal("0"), now=self.now)
            self._block(assessment.reason_code, SystemState.PAUSED, assessment.status)
            intent = self._intent(signal_key, symbol, side, "RISK_REJECTED", reason=assessment.reason_code)
            intent["liq_status"] = assessment.status
            self._save()
            return intent
        client_id = sha256_hex({"account": self.account_id, "signal": signal_key, "kind": "ENTRY"})[:24]
        response = self.venue.submit(
            {
                "client_order_id": client_id,
                "kind": "ENTRY",
                "side": side.value,
                "qty": format(reserved.qty, "f"),
                "price": format(D(entry_price), "f"),
                "symbol": symbol,
                "reduce_only": "false",
            },
            self.clock.elapsed_ms,
        )
        intent = self._intent(signal_key, symbol, side, "SUBMITTING", reason=None)
        intent.update(
            {
                "client_order_id": client_id,
                "reservation_id": reserved.reservation_id,
                "qty": format(reserved.qty, "f"),
                "price": format(D(entry_price), "f"),
                "stop": format(D(stop_price), "f"),
                "mark": format(D(mark), "f"),
                "isolated": format(isolated, "f"),
            }
        )
        if response["status"] == "TIMEOUT":
            intent["state"] = "UNKNOWN"
            intent["unknown_since_ms"] = self.clock.elapsed_ms
            intent["unknown_boot_id"] = self.clock.boot_id
            self.risk.mark_unknown(reserved.reservation_id, now=self.now)
            self._alert("UNKNOWN_ORDER", "提交结果不确定，已阻断新风险")
        elif response["status"] == "REJECTED":
            intent["state"] = "REJECTED"
            self.risk.request_cancel(reserved.reservation_id, now=self.now)
            self.risk.confirm_cancel(reserved.reservation_id, Decimal("0"), now=self.now)
        elif response["status"] == "RATE_LIMIT":
            intent["state"] = "RISK_REJECTED"
            intent["reason"] = "MARKET_HALTED"
            self._block("MARKET_HALTED", SystemState.PAUSED, "限流")
        else:
            self._apply_entry_fill(intent, D(response.get("filled") or "0"))
        self.state["last_elapsed_ms"] = self.clock.elapsed_ms
        self._save()
        return intent

    def reconcile_open_orders(self) -> None:
        self._require_lease()
        for intent in self.state["intents"].values():
            cid = intent.get("client_order_id")
            if not cid:
                continue
            body = self.venue.query(cid, self.clock.elapsed_ms)
            if body.get("status") == "NOT_FOUND":
                continue
            filled = D(body.get("filled") or "0")
            if intent.get("state") == "UNKNOWN" and filled > 0:
                self._apply_entry_fill(intent, filled)
                intent["state"] = "RESOLVED"
            elif intent.get("kind", "ENTRY") == "ENTRY" and intent.get("applied_qty"):
                applied = D(intent["applied_qty"])
                if filled > applied and intent.get("reservation_id"):
                    self.risk.confirm_cancel(intent["reservation_id"], filled, now=self.now)
                    self._book_additional_fill(intent, filled - applied)
                    intent["applied_qty"] = format(filled, "f")
                    self._sync_protection(intent["symbol"], filled, reset_clock=False)
        self._check_protection()
        self._save()

    def cancel_entry(self, signal_key: str) -> dict:
        self._require_lease()
        intent = self.state["intents"][signal_key]
        if intent.get("state") == "UNKNOWN" or intent.get("unknown_since_ms") is not None:
            self.risk.request_cancel(intent["reservation_id"], now=self.now)
            intent["cancel"] = "UNKNOWN"
            self._save()
            return intent
        body = self.venue.cancel(intent["client_order_id"], self.clock.elapsed_ms, kind="ENTRY")
        filled = D(body.get("filled") or intent.get("applied_qty") or "0")
        self.risk.request_cancel(intent["reservation_id"], now=self.now)
        self.risk.confirm_cancel(intent["reservation_id"], filled, now=self.now)
        if filled > D(intent.get("applied_qty") or "0"):
            self._book_additional_fill(intent, filled - D(intent.get("applied_qty") or "0"))
        intent["applied_qty"] = format(filled, "f")
        intent["state"] = "CANCELED" if filled < D(intent["qty"]) else "FILLED"
        self._sync_protection(intent["symbol"], filled, reset_clock=False)
        self._save()
        return intent

    def pause_new_entries(self) -> None:
        self._require_lease()
        self._block("MANUAL_PAUSE", SystemState.PAUSED, "暂停新开仓")
        self._action("CANCEL_ENTRY", confirmed=False)
        for intent in self.state["intents"].values():
            cid = intent.get("client_order_id")
            if not cid or intent.get("state") in ("FILLED", "CANCELED", "RISK_REJECTED", "REJECTED", "RESOLVED"):
                continue
            if intent.get("state") == "UNKNOWN":
                continue
            self.venue.cancel(cid, self.clock.elapsed_ms, kind="ENTRY")
            self._action("CANCEL_ENTRY", confirmed=True)
        self.state["pause_cancels_protection"] = False
        self._save()

    def request_flatten(self, symbol: str, qty: Decimal, *, position_version: Optional[int] = None) -> dict:
        self._require_lease()
        self.state["flatten_requested"] = True
        auth = self.risk.authorize_exit(
            symbol=symbol,
            qty=D(qty),
            expected_position_version=position_version,
            liq_validation_failed=True,
            now=self.now,
        )
        inflight = D(self.state["exit_inflight"].get(symbol, "0"))
        position = self._position_qty(symbol)
        if inflight + auth.qty > position:
            raise ExitRejectedError("退出数量超过剩余真实持仓，禁止翻仓", reason_code="EXIT_WOULD_FLIP")
        self.state["exit_inflight"][symbol] = format(inflight + auth.qty, "f")
        trigger = D(self.state["protections"].get(symbol, {}).get("mark") or "0")
        if trigger <= 0:
            trigger = D(self.state["intents_by_symbol"].get(symbol, {}).get("mark") or "1")
        side = Side(self.state["protections"][symbol]["side"]) if symbol in self.state["protections"] else Side.LONG
        fill_price = trigger * (Decimal("1") - self.adverse_exit) if side == Side.LONG else trigger * (Decimal("1") + self.adverse_exit)
        client_id = sha256_hex({"account": self.account_id, "symbol": symbol, "kind": "EXIT", "qty": format(auth.qty, "f"), "version": str(auth.position_version)})[:24]
        response = self.venue.submit(
            {
                "client_order_id": client_id,
                "kind": "EXIT",
                "side": Side.SHORT.value if side == Side.LONG else Side.LONG.value,
                "qty": format(auth.qty, "f"),
                "price": format(fill_price, "f"),
                "trigger_price": format(trigger, "f"),
                "symbol": symbol,
                "reduce_only": "true",
            },
            self.clock.elapsed_ms,
        )
        if response["status"] in ("TIMEOUT", "RATE_LIMIT", "NOT_FOUND"):
            self._action("REDUCE", confirmed=False)
            self._save()
            return {"confirmed": False, "reason_code": "EXIT_UNCONFIRMED", "trigger_price": format(trigger, "f")}
        self.ledger.apply_fill(
            symbol=symbol,
            side=Side.SHORT if side == Side.LONG else Side.LONG,
            qty=auth.qty,
            price=fill_price,
            multiplier=self.multiplier,
            event_time=self.now,
            received_at=self.now,
            external_id=f"exit:{client_id}",
            venue="SIM",
            venue_trade_id=client_id,
            reduce_only=True,
            price_source="SIM_BOOK",
            quote_grade="LOCAL_INJECTION",
        )
        remaining = position - auth.qty
        self.risk.sync_position(
            symbol=symbol,
            side=side if remaining > 0 else None,
            qty=remaining,
            notional=Decimal("0"),
            risk_amount=Decimal("0"),
            isolated_margin=self.ledger.position(symbol).isolated_margin,
            now=self.now,
        )
        self.state["exit_inflight"][symbol] = "0"
        self._sync_protection(symbol, remaining, reset_clock=False)
        self._action("REDUCE", confirmed=True)
        self._save()
        return {
            "confirmed": True,
            "fill_price": format(fill_price, "f"),
            "trigger_price": format(trigger, "f"),
            "client_order_id": client_id,
        }

    def pull_external(self) -> int:
        self._require_lease()
        applied = 0
        for event in self.venue.external:
            if event["external_id"] in self.state["processed_external"]:
                continue
            self.state["processed_external"].append(event["external_id"])
            applied += 1
            symbol = event["symbol"]
            self._block("EXTERNAL_REDUCTION", SystemState.FAULT, event["kind"])
            if event["qty"] == "":
                self._block("POSITION_QTY_UNKNOWN", SystemState.FAULT, "数量未知，不凭旧数量重建退出单")
                self._action("RECONCILE", confirmed=False)
                continue
            qty = D(event["qty"])
            held = self._position_qty(symbol)
            if qty > held:
                self._block("POSITION_QTY_UNKNOWN", SystemState.FAULT, "外部减仓超过可确认仓位")
                continue
            side = Side(self.state["protections"][symbol]["side"])
            event_type = LedgerEventType.LIQUIDATION if event["kind"] == "LIQUIDATION" else LedgerEventType.ADL
            self.ledger.apply_fill(
                symbol=symbol,
                side=Side.SHORT if side == Side.LONG else Side.LONG,
                qty=qty,
                price=D(event["price"]),
                multiplier=self.multiplier,
                event_time=self.now,
                received_at=self.now,
                external_id=f"ext:{event['external_id']}",
                venue="SIM",
                venue_trade_id=event["external_id"],
                reduce_only=True,
                event_type=event_type,
            )
            remaining = held - qty
            self.risk.sync_position(
                symbol=symbol,
                side=side if remaining > 0 else None,
                qty=remaining,
                notional=Decimal("0"),
                risk_amount=Decimal("0"),
                isolated_margin=self.ledger.position(symbol).isolated_margin,
                now=self.now,
            )
            self._sync_protection(symbol, remaining, reset_clock=False)
            if remaining == 0:
                self._cancel_protection(symbol)
            self._action("RECONCILE", confirmed=True)
        self._save()
        return applied

    def acknowledge_restore(self) -> None:
        self._require_lease()
        self.state["restored_pending"] = False
        self._save()

    def mark_restored(self) -> None:
        self.state["restored_pending"] = True
        self._save()

    def _on_open(self) -> None:
        if not self.leased:
            return
        if not self.state.get("boot_id"):
            self.state["boot_id"] = self.clock.boot_id
        self.state["last_elapsed_ms"] = max(int(self.state.get("last_elapsed_ms") or 0), self.clock.elapsed_ms)
        self._save()

    def _apply_entry_fill(self, intent: dict, filled: Decimal) -> None:
        filled = D(filled)
        if filled <= 0:
            intent["state"] = "OPEN"
            return
        already = D(intent.get("applied_qty") or "0")
        if filled <= already:
            intent["state"] = "RESOLVED" if intent.get("state") == "UNKNOWN" else intent.get("state", "FILLED")
            return
        delta = filled - already
        price = D(intent["price"])
        self.ledger.apply_fill(
            symbol=intent["symbol"],
            side=Side(intent["side"]),
            qty=delta,
            price=price,
            multiplier=self.multiplier,
            event_time=self.now,
            received_at=self.now,
            external_id=f"entry:{intent['client_order_id']}:{format(filled, 'f')}",
            venue="SIM",
            venue_trade_id=f"{intent['client_order_id']}-{format(filled, 'f')}",
        )
        if already == 0:
            self.ledger.lock_margin(
                amount=D(intent["isolated"]) * (delta / D(intent["qty"])),
                symbol=intent["symbol"],
                event_time=self.now,
                received_at=self.now,
                external_id=f"im:{intent['client_order_id']}",
                venue="SIM",
            )
            self.ledger.post_fee(
                amount=price * delta * self.multiplier * self.fee_rate,
                event_time=self.now,
                received_at=self.now,
                external_id=f"fee:{intent['client_order_id']}",
                venue="SIM",
                symbol=intent["symbol"],
            )
        self.risk.apply_fill(intent["reservation_id"], delta, fill_id=f"{intent['client_order_id']}:{format(filled, 'f')}", now=self.now)
        intent["applied_qty"] = format(filled, "f")
        intent["state"] = "FILLED" if filled == D(intent["qty"]) else "PARTIAL"
        self.state["intents_by_symbol"][intent["symbol"]] = {
            "mark": intent["mark"],
            "stop": intent["stop"],
            "side": intent["side"],
            "avg_price": intent["price"],
        }
        self._sync_protection(intent["symbol"], self._position_qty(intent["symbol"]), reset_clock=already == 0 and filled > 0)
        self._place_protection(intent["symbol"])

    def _book_additional_fill(self, intent: dict, delta: Decimal) -> None:
        if delta <= 0:
            return
        price = D(intent["price"])
        self.ledger.apply_fill(
            symbol=intent["symbol"],
            side=Side(intent["side"]),
            qty=delta,
            price=price,
            multiplier=self.multiplier,
            event_time=self.now,
            received_at=self.now,
            external_id=f"entry-add:{intent['client_order_id']}:{format(D(intent.get('applied_qty') or '0') + delta, 'f')}",
            venue="SIM",
            venue_trade_id=f"add-{intent['client_order_id']}-{format(delta, 'f')}",
            allow_add=True,
        )

    def _place_protection(self, symbol: str) -> None:
        protection = self.state["protections"][symbol]
        if protection.get("client_order_id") and protection.get("coverage") == "PENDING":
            return
        qty = D(protection["required_qty"])
        if qty <= 0:
            return
        client_id = sha256_hex({"symbol": symbol, "qty": format(qty, "f"), "kind": "PROTECTION", "boot": self.clock.boot_id})[:24]
        if client_id in self.venue.orders:
            protection["client_order_id"] = client_id
            return
        side = Side(protection["side"])
        self.venue.submit(
            {
                "client_order_id": client_id,
                "kind": "PROTECTION",
                "side": Side.SHORT.value if side == Side.LONG else Side.LONG.value,
                "qty": format(qty, "f"),
                "price": protection["stop"],
                "symbol": symbol,
                "reduce_only": "true",
            },
            self.clock.elapsed_ms,
        )
        protection["client_order_id"] = client_id
        protection["binding_mode_declared"] = "FULL"
        self._action("PROTECT", confirmed=False)

    def _sync_protection(self, symbol: str, required: Decimal, *, reset_clock: bool) -> None:
        current = self.state["protections"].get(symbol)
        meta = self.state["intents_by_symbol"].get(symbol, {})
        if current is None:
            current = {
                "required_qty": "0",
                "confirmed_qty": "0",
                "coverage": "UNKNOWN",
                "uncovered_since_ms": None,
                "side": meta.get("side", Side.LONG.value),
                "stop": meta.get("stop", "0"),
                "mark": meta.get("mark", "0"),
                "avg_price": meta.get("avg_price", "0"),
                "client_order_id": "",
                "binding_mode_declared": "FULL",
                "binding_mode_observed": "",
            }
            self.state["protections"][symbol] = current
        current["required_qty"] = format(D(required), "f")
        if reset_clock and current.get("uncovered_since_ms") is None and D(required) > D(current["confirmed_qty"]):
            current["uncovered_since_ms"] = self.clock.elapsed_ms
        self._refresh_coverage(symbol)

    def _refresh_coverage(self, symbol: str) -> None:
        protection = self.state["protections"][symbol]
        cid = protection.get("client_order_id") or ""
        if cid:
            body = self.venue.query(cid, self.clock.elapsed_ms)
            if body.get("status") == "CONFIRMED":
                protection["confirmed_qty"] = body["filled"]
                protection["binding_mode_observed"] = "FULL"
                self._action("PROTECT", confirmed=True)
        required = D(protection["required_qty"])
        confirmed = D(protection["confirmed_qty"])
        if required <= 0:
            protection["coverage"] = "FLAT"
        elif confirmed >= required and protection.get("binding_mode_observed") == "FULL":
            protection["coverage"] = "COVERED"
        elif confirmed > 0:
            protection["coverage"] = "SHORT"
            if protection.get("uncovered_since_ms") is None:
                protection["uncovered_since_ms"] = self.clock.elapsed_ms
        else:
            protection["coverage"] = "UNKNOWN"
            if protection.get("uncovered_since_ms") is None and required > 0:
                protection["uncovered_since_ms"] = self.clock.elapsed_ms

    def _check_protection(self) -> None:
        for symbol in list(self.state["protections"]):
            self._refresh_coverage(symbol)
            protection = self.state["protections"][symbol]
            if protection["coverage"] in ("COVERED", "FLAT"):
                continue
            since = protection.get("uncovered_since_ms")
            if since is None:
                continue
            if self.clock.elapsed_ms - int(since) >= 5000:
                self._block("PROTECTION_OVERDUE", SystemState.FAULT, symbol)
                self._assertion("A06", "PROTECTION_OVERDUE", int(since), self.clock.elapsed_ms, {"symbol": symbol})
                self._action("BLOCK", confirmed=True)

    def _check_unknowns(self) -> None:
        for intent in self.state["intents"].values():
            since = intent.get("unknown_since_ms")
            if since is None or intent.get("state") not in ("UNKNOWN", "RESOLVED"):
                if since is None:
                    continue
            if intent.get("state") == "RESOLVED":
                continue
            age = self.clock.elapsed_ms - int(since)
            if age >= 30000 and not intent.get("warned"):
                intent["warned"] = True
                self._alert("ORDER_CONFIRMATION_OVERDUE", "订单超过 30 秒仍未确认")
                self._assertion("A05", "ORDER_CONFIRMATION_OVERDUE", int(since), self.clock.elapsed_ms, {})
            if age >= 120000:
                self._block("ORDER_CONFIRMATION_OVERDUE", SystemState.FAULT, intent["signal_key"])

    def _check_config_age(self, *, on_submit: bool) -> None:
        observed = self.state.get("config_observed_ms")
        if observed is None:
            if on_submit:
                raise CtsError("入场前没有账户配置快照", reason_code="CONFIG_UNKNOWN")
            return
        if self.state.get("config_boot") != self.clock.boot_id:
            self._block("CONFIG_STALE", SystemState.PAUSED, "重启后旧配置快照不能继续准入")
            if on_submit:
                raise CtsError("配置快照属于其他启动周期", reason_code="STALE_DATA")
            return
        age = self.clock.elapsed_ms - int(observed)
        if age > 60000:
            self._block("CONFIG_STALE", SystemState.PAUSED, "配置超过 60 秒没有新鲜核验")
            self._assertion("A01", "CONFIG_STALE", int(observed), self.clock.elapsed_ms, {})
        if on_submit and age > 5000:
            raise CtsError("发单前配置快照超过 5 秒", reason_code="STALE_DATA")

    def _check_funding_age(self) -> None:
        observed = self.state.get("funding_observed_ms")
        if observed is None:
            return
        if self.clock.elapsed_ms - int(observed) > 60000:
            self.state["funding_state"] = "STALE"
            self._block("FUNDING_STALE", SystemState.PAUSED, "资金费元数据超过 60 秒未刷新")

    def _recheck_symbol(self, symbol: str, avg: Decimal, stop: Decimal, side: Side) -> None:
        protection = self.state["protections"][symbol]
        qty = D(protection["required_qty"])
        isolated = self.ledger.position(symbol).isolated_margin
        model = model_liq_price(
            side=side,
            avg_price=avg,
            qty=qty,
            multiplier=self.multiplier,
            isolated_margin=isolated if isolated > 0 else D("1"),
            mmr=self.mmr,
            fee_rate=self.fee_rate,
        )
        quote = self.venue.liq_quote(symbol, model)
        mark = D(protection.get("mark") or avg)
        assessment = assess_liquidation(
            side=side,
            mark=mark,
            stop=stop,
            model_price=model,
            venue_raw=quote,
            buffer_b=self.buffer_b,
        )
        if assessment.reason_code:
            severity = SystemState.FAULT if self._position_qty(symbol) > 0 else SystemState.PAUSED
            self._block(assessment.reason_code, severity, symbol)

    def _cancel_protection(self, symbol: str) -> None:
        protection = self.state["protections"].get(symbol)
        if not protection or not protection.get("client_order_id"):
            return
        self.venue.cancel(protection["client_order_id"], self.clock.elapsed_ms, kind="PROTECTION")
        protection["required_qty"] = "0"
        protection["coverage"] = "FLAT"

    def _position_qty(self, symbol: str) -> Decimal:
        return self.ledger.position(symbol).qty

    def _intent(self, signal_key: str, symbol: str, side: Side, state: str, reason: Optional[str]) -> dict:
        intent = {
            "signal_key": signal_key,
            "symbol": symbol,
            "side": side.value,
            "state": state,
            "reason": reason or "",
            "kind": "ENTRY",
        }
        self.state["intents"][signal_key] = intent
        return intent

    def _block(self, reason: str, severity: SystemState, details: str) -> None:
        self.risk.add_blocker(reason, severity, details, now=self.now)
        self.state["blockers"][reason] = {"severity": severity.value, "details": details, "elapsed_ms": self.clock.elapsed_ms}

    def _alert(self, code: str, message: str) -> None:
        self.state["alerts"].append({"code": code, "message": message, "elapsed_ms": self.clock.elapsed_ms})

    def _assertion(self, assertion_id: str, reason: str, detected: int, blocked: int, body: dict) -> None:
        self.state["assertions"].append(
            {
                "assertion_id": assertion_id,
                "reason_code": reason,
                "detected_at_ms": detected,
                "blocked_at_ms": blocked,
                "evidence_layer": "LOCAL_INJECTION",
                "body": body,
            }
        )

    def _action(self, action_type: str, *, confirmed: bool) -> None:
        self.state["actions"].append(
            {
                "action_type": action_type,
                "attempted_at_ms": self.clock.elapsed_ms,
                "confirmed_at_ms": self.clock.elapsed_ms if confirmed else None,
                "result": "CONFIRMED" if confirmed else "UNCONFIRMED",
            }
        )

    def _require_lease(self) -> None:
        if not self.leased:
            raise CtsError("执行租约已失效，不能发单", reason_code="LEASE_LOST")

    def _save(self) -> None:
        if self.leased:
            self.state["last_elapsed_ms"] = max(int(self.state.get("last_elapsed_ms") or 0), self.clock.elapsed_ms)
            self.store.save(self.account_id, self.state)


def _empty_state(boot_id: str) -> dict:
    return {
        "boot_id": boot_id,
        "last_elapsed_ms": 0,
        "intents": {},
        "intents_by_symbol": {},
        "protections": {},
        "alerts": [],
        "assertions": [],
        "actions": [],
        "processed_external": [],
        "blockers": {},
        "exit_inflight": {},
        "restored_pending": False,
        "flatten_requested": False,
        "pause_cancels_protection": False,
        "funding_state": "UNKNOWN",
        "data_grade": "SIGNAL_RESEARCH_ONLY",
        "mark_gap_overlaps": False,
        "clock_fault": False,
    }
