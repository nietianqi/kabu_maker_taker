# AI Troubleshooting Notes

This document records live-trading issues found during the kabu maker/taker work so future AI agents can continue from evidence instead of rediscovering the same branches.

## 2026-05-18: kabu stock Board bid/ask fields are reversed

### Symptom
- Live trade example showed a new margin buy at 844.8 JPY and a repayment sell at 844 JPY for 8136.
- This looked like "buy high, sell low" shortly after startup.

### Evidence
- `kabu_STATION_API.yaml` states that for stock Board responses, `BidPrice=Sell1.Price` and `AskPrice=Buy1.Price`.
- In normal trading terminology, best bid is the executable sell price and best ask is the executable buy price, so kabu stock Board field names are reversed from the usual convention.

### Fix
- Enable `signals.kabu_bidask_reversed=true` in live configs.
- Keep `signals.auto_fix_negative_spread=true` only as a fallback, not as the primary live behavior.
- Added/kept tests so kabu stock Board payloads normalize to normal semantics where `bid < ask`.

### Files
- `config.live.multi.example.json`
- `config.live.shadow.example.json`
- `kabu_maker_taker/models.py`
- `tests/test_models.py`

## 2026-05-18: automatic timeout/flatten exits could realize a loss

### Symptom
- A lollipop timeout or emergency cleanup could submit an exit while current executable price was worse than average entry price.
- User requirement: do not close at a loss, even on timeout.

### Risk
- Blocking loss exits can leave a live position unresolved during emergency cleanup. This is intentional per the requested behavior, but it increases overnight/market risk and must be visible in logs.

### Fix
- Added `risk.prevent_loss_exit`, defaulting to `false` for backward compatibility.
- Live configs enable `prevent_loss_exit=true`.
- Added `RiskManager.can_exit_without_loss()` and route all automatic exit paths through it.
- Long exits are blocked when sell reference/limit price is below average price.
- Short exits are blocked when buy reference/limit price is above average price.
- Missing exit price is treated as unsafe and blocked.
- Blocked exits emit `loss_exit_blocked` with position and intent diagnostics.

### Covered Paths
- Lollipop take-profit intent before submission.
- Timeout force exit.
- Stop-loss induced force exit.
- Flow-flip force exit.
- Deferred force exit after cancel/replace.
- Live REST submit path.
- Shadow submit path.
- Dry-run simulator exit submit path.
- Emergency flatten.

### Files
- `kabu_maker_taker/risk.py`
- `kabu_maker_taker/combined.py`
- `kabu_maker_taker/live_runtime.py`
- `kabu_maker_taker/app.py`
- `tests/test_lollipop.py`
- `tests/test_kill_switch.py`

## 2026-05-18: per-symbol risk cap changed to 400 shares

### Requirement
- Each stock may hold up to 400 shares.
- This is a risk cap only; the strategy must not automatically top up a position to 400 shares.

### Fix
- Set `risk.max_inventory_qty=400`.
- Set `risk.max_notional=450000` so high-priced names such as 8136 are not blocked by a 300,000 JPY notional cap before reaching 400 shares.
- Entry sizing still uses `strategy.trade_qty=100`; scale-in only happens through normal strategy entries and risk gates.

### Files
- `config.live.multi.example.json`
- `config.live.shadow.example.json`
- Local-only `config.live.multi.json`
- `tests/test_launcher.py`

## 2026-05-18: launcher stopped after one worker failed with 401

### Symptom
- Launcher output showed preflight OK for 8136 and 3697, then one worker exited:
- `GET /kabusapi/orders failed with status 401 (code=4001009, message=APIキー不一致)`
- Launcher then removed arm files and stopped the remaining worker.

### Cause
- Worker startup calls `KabuRestExecutor.snapshot()`, which calls `GET /kabusapi/orders`.
- kabu Station can invalidate an older API token when another token is issued.
- In multi-worker startup, one worker can fail with `APIキー不一致`.
- Launcher is designed to stop all workers if any real worker exits non-zero, then cleanup launcher-created arm files.

### Fix
- Launcher obtains one shared token per `kabu.base_url` after preflight/arm validation and passes it to all workers.
- Worker logs auth diagnostics as `shared_token`, `token_source`, and `token_sha256_8`; it never logs the raw token.
- If `snapshot()` receives 401 while using a shared token, the worker discards the shared token, obtains a fresh worker token once, and retries the snapshot.
- If the retry still fails, startup exits with diagnostics.

### Files
- `kabu_maker_taker/launcher.py`
- `kabu_maker_taker/execution/client.py`
- `kabu_maker_taker/execution/executor.py`
- `kabu_maker_taker/app.py`
- `tests/test_launcher.py`
- `tests/test_kabu_rest.py`

## 2026-05-18: SOR order exchange is not PUSH registration exchange

### Symptom
- kabu PUSH registration can fail or board routing can mismatch when using SOR order exchange directly for market-data registration.

### Cause
- Order routing may use `exchange=9` or `27`, but `/kabusapi/register` expects a market-data venue such as TSE `1`.

### Fix
- Added `kabu.register_exchange`.
- Default `register_exchange=0` auto-maps TSE-family trading exchanges `9/27` to registration exchange `1`.
- Live validation rejects invalid register exchange values.
- Preflight/live logs include `trade_exchange` and `register_exchange` for diagnosis.

### Files
- `kabu_maker_taker/config.py`
- `kabu_maker_taker/execution/executor.py`
- `kabu_maker_taker/app.py`
- `kabu_maker_taker/live_runtime.py`
- `tests/test_kabu_rest.py`
- `tests/test_live_websocket.py`

## 2026-05-18: do not commit local live credentials

### Rule
- `config.live.multi.json` is local-only and must not be pushed.
- Example configs must keep `kabu.api_password` empty.
- Real kabu passwords should be entered only in local ignored config files or another local secret mechanism.

### Fix
- Added `config.live.multi.json` to `.gitignore`.
- Kept tracked examples with empty `api_password`.
- `KabuConfig` defaults `api_password` to an empty string.

## 2026-05-18: WebSocket preflight failed on null quote fields

### Symptom
- Launcher stopped during preflight with:
- `live_preflight_failed reason=websocket_bad_message`
- Detail: `float() argument must be a string or a real number, not 'NoneType'`
- Summary showed `received_boards=0`, meaning no valid board was accepted before failure.

### Cause
- kabu PUSH Board messages can contain `null` in price/quantity fields such as `BidPrice`, `AskPrice`, `BidQty`, `AskQty`, or `CurrentPrice`.
- `BoardSnapshot.from_dict()` previously called `float(...)` / `int(...)` directly, so a `null` quote became a parser exception instead of an invalid board.

### Fix
- Added safe numeric parsing in `BoardSnapshot.from_dict()` and `Level.from_any()`.
- `None` or empty numeric fields now become `0`.
- Such snapshots have `snapshot.valid=false` rather than raising.
- Preflight and live WebSocket loops now count invalid quote boards as `ignored_boards` and continue waiting for the next valid board.

### Files
- `kabu_maker_taker/models.py`
- `kabu_maker_taker/live_runtime.py`
- `tests/test_models.py`
- `tests/test_live_websocket.py`

### Expected Behavior
- A null/empty quote board no longer produces `websocket_bad_message`.
- If a later valid board arrives before timeout, preflight succeeds and reports the invalid board in `ignored_boards`.
- If only null/invalid boards arrive, preflight times out with `websocket_preflight_timeout` and a nonzero `ignored_boards` count.

## 2026-05-18: flat workers stopped after WebSocket idle timeout

### Symptom
- Launcher preflight passed and both workers started.
- WebSocket connected, then reconnected once after `Connection timed out`.
- After the configured reconnect attempts were exhausted, the worker emitted:
- `live_halted reason=websocket_reconnect_exhausted cleanup.last_error="Connection timed out"`
- The launcher then stopped every symbol because one worker exited with return code `3`.

### Cause
- Live config used `kabu.websocket_reconnect_attempts=1`.
- In flat state, kabu PUSH can be quiet or repeatedly timeout without meaning the strategy has live exposure.
- The old worker loop treated an exhausted receive timeout the same as a dangerous live disconnect and returned `websocket_reconnect_exhausted`.
- Launcher behavior is intentionally strict: any nonzero live worker exit stops the whole multi-symbol run.

### Fix
- Added `kabu.websocket_reconnect_forever_when_flat`.
- Live example configs enable it so a flat worker keeps unregistering, re-registering, reconnecting, and waiting for the next valid board.
- The forever loop is only allowed when `_has_live_exposure(strategy)==False`.
- It only applies to WebSocket idle/timeout/disconnect style errors. REST/API failures such as register 401 still halt after the configured attempts.
- If a position or active entry/exit order exists, WebSocket failure still emits `websocket_disconnected` and goes through fault cleanup.
- Flat idle reconnect logs `live_websocket_idle_timeout` with `symbol`, `attempt`, `ignored_boards`, `last_target_board_age_ms`, and `has_exposure=false`.

### Files
- `kabu_maker_taker/config.py`
- `kabu_maker_taker/live_runtime.py`
- `config.live.multi.example.json`
- `config.live.shadow.example.json`
- `tests/test_live_websocket.py`
- `tests/test_kabu_rest.py`

### Expected Behavior
- With no position and no active orders, repeated WebSocket receive timeouts no longer stop the worker.
- With any position or active order, WebSocket loss still stops live trading and starts cleanup.
- REST/API failures are not hidden by the flat forever-reconnect setting.

## 2026-05-18: borrowed diagnostics and auth retry from kabu_micro_edge_c

### Symptom
- Python workers had useful JSON stdout, but no compact per-symbol runtime summary file for other agents to inspect after a live run.
- REST 401/403 recovery existed only for the startup shared-token snapshot path.
- Multi-symbol launcher had per-worker risk caps but no startup check for aggregate configured account exposure.

### Comparison
- `D:\kabu_micro_edge_c\include\kabu_micro_edge\diagnostics.hpp` writes compact runtime summaries.
- `D:\kabu_micro_edge_c\include\kabu_micro_edge\app\runtime.hpp` retries 401/403 by refreshing token once.
- `D:\kabu_micro_edge_c\config.example.json` includes diagnostics heartbeat and account-risk fields.
- Python keeps its multi-worker launcher and only borrows these safety/diagnostic pieces.

### Fix
- Added `diagnostics.runtime_summary_jsonl_path` and `diagnostics.heartbeat_interval_s`.
- Added `RuntimeSummaryWriter`; live examples write `runtime_summary.jsonl` under each worker `log_dir`.
- Runtime summary includes `websocket`, `position`, `active_orders`, `metrics`, `risk`, `auth`, and `consistency`.
- `auth` only records safe fields: `shared_token`, `token_source`, `token_sha256_8`, and `token_refresh_count`.
- Added generic one-shot 401/403 token refresh retry for `snapshot/register/unregister/submit/cancel/poll/positions/open_order_snapshots`.
- `poll_order_events()` now uses one full `/orders` query when more than one active order exists, then matches by broker order id locally.
- Added root-level launcher `account_risk.enabled/max_total_inventory_qty/max_total_notional` static validation.

### Files
- `kabu_maker_taker/config.py`
- `kabu_maker_taker/telemetry.py`
- `kabu_maker_taker/execution/executor.py`
- `kabu_maker_taker/launcher.py`
- `config.live.multi.example.json`
- `config.live.shadow.example.json`
- `docs/KABU_MICRO_EDGE_C_COMPARISON.md`
- `tests/test_kabu_rest.py`
- `tests/test_launcher.py`
- `tests/test_telemetry.py`

### Expected Behavior
- A worker can recover once from 401/403 without exposing the raw token.
- Persistent auth failure still returns the original live-safe halt/error path.
- 429/5xx and missing `OrderId` are not retried as auth errors.
- `account_risk` only blocks unsafe startup configuration; it is not realtime cross-worker account risk.

## 2026-05-18: live consistency guard added

### Symptom
- Some dangerous local state contradictions were only visible indirectly through later order or position behavior.

### Fix
- Added `CombinedMakerTakerStrategy.status_snapshot()` and `consistency_issues()`.
- The live board path checks consistency after poll, after strategy decision, and after submit/cancel branches.
- High-severity issues return `consistency_violation` and go through `live_halted`.

### High-Severity Issues
- Active exit order while local position is flat.
- Active exit side does not reduce current position.
- Active exit quantity exceeds current position quantity.
- Order cumulative quantity exceeds intent quantity.
- Position quantity exceeds `risk.max_inventory_qty`.

### Files
- `kabu_maker_taker/combined.py`
- `kabu_maker_taker/live_runtime.py`
- `kabu_maker_taker/telemetry.py`
- `tests/test_live_websocket.py`

## Verification
- `python -m json.tool config.live.multi.json`
- `python -m unittest discover -s tests -p "test_*.py"`
- Latest full result: 413 tests OK.
