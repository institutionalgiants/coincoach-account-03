from __future__ import annotations

import asyncio
import json
import math
import os
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import AccountConfig, load_config
from app.cycle import run_cycle
from app.official_web_chart import OfficialBinanceWebChartCapture
from app.social_publishers import SocialSyndicator
from app.square_publisher import SquarePublishError, SquarePublisher
from app.web_capture import WebCaptureError
from app.publish_guard import check_publish_guard, record_publish_attempt, record_successful_publish
from app.account03_ledger_client import Account03LedgerClient, Account03LedgerError




def configure_matrix_account(config) -> str | None:
    """Limit one matrix job to one configured Square API account.

    The workflow passes the selected account id and its key through generic
    variables. Mapping the key back to that account's configured key name keeps
    the rest of the publishing code unchanged while preventing account_01 from
    blocking account_02, account_03, and later accounts.
    """

    account_id = os.getenv("ACTIVE_ACCOUNT_ID", "").strip()
    if not account_id:
        return None

    account = next((item for item in config.accounts if item.id == account_id), None)
    if account is None:
        raise RuntimeError(f"Unknown ACTIVE_ACCOUNT_ID: {account_id}")
    if not account.enabled:
        raise RuntimeError(f"ACTIVE_ACCOUNT_ID is disabled in config: {account_id}")

    active_key = os.getenv("BINANCE_SQUARE_KEY_ACTIVE", "").strip()
    if not active_key:
        raise RuntimeError(f"Missing API key for {account_id}")

    os.environ[account.key_env] = active_key
    config.accounts = [account]
    return account_id

def _truthy(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _safe_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _safe_float(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def validate_account03_live_mode(
    matrix_account_id: str | None,
    publishing_requested: bool,
    account03_live_mode: bool,
    active_account_ids: set[str],
) -> None:
    """Prevent Account 03 live publishing outside its dedicated workflow."""
    if account03_live_mode and matrix_account_id != "account_03":
        raise RuntimeError(
            "ACCOUNT03_LIVE_MODE requires ACTIVE_ACCOUNT_ID=account_03"
        )

    if publishing_requested and "account_03" in active_account_ids:
        if matrix_account_id != "account_03" or not account03_live_mode:
            raise RuntimeError(
                "Live Account 03 requires its dedicated workflow, "
                "ACTIVE_ACCOUNT_ID=account_03 and ACCOUNT03_LIVE_MODE=true"
            )


def apply_account03_runtime_overrides(settings: dict[str, Any]) -> None:
    """Apply Account 03-only scan, schedule, caption and publishing settings."""
    settings["schedule"]["signals_per_account_per_hour"] = 1
    settings["schedule"]["slot_spacing_minutes"] = 60
    settings["market"]["shortlist_size"] = 300
    settings["market"]["deep_scan_limit"] = 180

    captions = settings["captions"]
    captions["guys_probability"] = 0.0
    captions["maximum_recent_phrases"] = 1000
    captions["maximum_length"] = 1000
    captions["enforce_maximum_length"] = True
    captions["include_context_line"] = True
    captions["include_market_analysis"] = True

    settings["publishing"]["per_account_daily_limit"] = 24


def _load_ledger(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"days": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"days": {}}
    return data if isinstance(data, dict) else {"days": {}}


def _save_ledger(path: Path, ledger: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(ledger, indent=2), encoding="utf-8")
    temporary.replace(path)


def _day_key() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def account03_slot_hour_matches(
    authorized_hour_bucket: str,
    now: datetime | None = None,
) -> bool:
    """Return whether the current UTC hour is the one authorized by the Worker."""
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    else:
        current = current.astimezone(timezone.utc)
    return authorized_hour_bucket == current.strftime("%Y-%m-%d-%H")


def _published_today(ledger: dict[str, Any], account_id: str) -> int:
    return int(ledger.get("days", {}).get(_day_key(), {}).get(account_id, 0))


def _account03_signal_payload(signal: Any) -> dict[str, Any]:
    """Build the strict, JSON-safe signal payload required by the D1 API."""
    def value(name: str) -> Any:
        if isinstance(signal, dict):
            return signal.get(name)
        return getattr(signal, name, None)

    side = value("side")
    side = getattr(side, "value", side)

    payload = {
        "signal_id": str(value("signal_id") or ""),
        "symbol": str(value("symbol") or ""),
        "base_asset": str(value("base_asset") or ""),
        "side": str(side or "").upper(),
        "timeframe": str(value("timeframe") or ""),
    }

    for name in (
        "entry_mid", "stop_percent", "tp1_percent", "tp2_percent", "tp3_percent"
    ):
        raw = value(name)
        try:
            payload[name] = float(raw)
        except (TypeError, ValueError) as exc:
            raise Account03LedgerError(
                f"Signal is missing a valid {name} value."
            ) from exc

    if not all(payload[name] for name in (
        "signal_id", "symbol", "base_asset", "side", "timeframe"
    )):
        raise Account03LedgerError("Signal is missing a required identity field.")

    return payload


def _record_publish(ledger: dict[str, Any], account_id: str) -> None:
    days = ledger.setdefault("days", {})
    day = days.setdefault(_day_key(), {})
    day[account_id] = int(day.get(account_id, 0)) + 1
    for old_day in sorted(days)[:-7]:
        days.pop(old_day, None)


def configure_live_capture_safety(settings: dict[str, Any]) -> dict[str, int | float | bool]:
    """Apply the verified non-destructive chart policy to the real one-post pilot."""

    web = settings["capture"]["web"]
    web["allow_ma_overlay_for_capture_proof"] = True
    web["require_ma_overlay_removed"] = False
    web["skip_indicator_cleanup_for_proof"] = True
    web["require_network_timeframe_confirmation"] = False
    web["non_blocking_dialog_recovery_for_proof"] = True

    return {
        "allow_ma_overlay_for_capture_proof": True,
        "require_ma_overlay_removed": False,
        "skip_indicator_cleanup_for_proof": True,
        "require_network_timeframe_confirmation": False,
        "non_blocking_dialog_recovery_for_proof": True,
        "outer_capture_attempts": _safe_int("LIVE_OUTER_CAPTURE_ATTEMPTS", 2, 1, 4),
        "retry_delay_seconds": _safe_float("LIVE_RETRY_DELAY_SECONDS", 2.0, 0.0, 15.0),
    }


def configure_live_candidate_reserve(
    settings: dict[str, Any],
    *,
    target_per_account: int,
    reserve_per_account: int,
) -> dict[str, int]:
    """Request reserve signals but publish only the configured target per account."""

    schedule = settings["schedule"]
    original_max_per_timeframe = max(
        0, int(schedule.get("max_signals_per_timeframe_per_account", 0))
    )
    timeframe_count = max(1, len(settings.get("market", {}).get("timeframes", [])))
    candidate_per_account = target_per_account + max(1, reserve_per_account)
    candidate_max_per_timeframe = max(
        original_max_per_timeframe,
        int(math.ceil(candidate_per_account / timeframe_count)),
    )
    schedule["signals_per_account_per_hour"] = candidate_per_account
    schedule["max_signals_per_timeframe_per_account"] = candidate_max_per_timeframe
    return {
        "target_per_account": target_per_account,
        "candidate_per_account": candidate_per_account,
        "candidate_max_per_timeframe": candidate_max_per_timeframe,
    }


async def capture_with_live_retry(
    capture: OfficialBinanceWebChartCapture,
    symbol: str,
    timeframe: str,
    output_path: Path,
    *,
    outer_attempts: int,
    retry_delay_seconds: float,
) -> int:
    """Retry the whole official page capture before moving to a reserve signal."""

    attempts = max(1, int(outer_attempts))
    last_error: WebCaptureError | None = None
    diagnostic_path = output_path.with_name(output_path.stem + "_diagnostic.png")
    page_text_path = output_path.with_name(output_path.stem + "_page_text.txt")
    report_path = output_path.with_suffix(".capture.json")

    for attempt in range(1, attempts + 1):
        for stale in (output_path, diagnostic_path, page_text_path, report_path):
            stale.unlink(missing_ok=True)
        try:
            await capture.capture(symbol, timeframe, output_path)
            diagnostic_path.unlink(missing_ok=True)
            page_text_path.unlink(missing_ok=True)
            return attempt
        except WebCaptureError as exc:
            last_error = exc
            if attempt < attempts:
                await asyncio.sleep(max(0.0, retry_delay_seconds))

    assert last_error is not None
    raise last_error


def _relative(path: Path, root: Path) -> str:
    return str(path.relative_to(root)).replace("\\", "/")


def rotate_candidates_for_stateless_slot(
    candidates: list[dict[str, Any]],
    account_id: str,
) -> list[dict[str, Any]]:
    """Rotate reserve candidates when the runtime has no persistent disk."""

    if not candidates or not _truthy("STATELESS_SLOT_ROTATION"):
        return list(candidates)
    raw_slot = os.getenv("LIVE_SLOT_INDEX", "").strip()
    try:
        slot_index = int(raw_slot) if raw_slot else int(
            datetime.now(timezone.utc).timestamp() // (15 * 60)
        )
    except ValueError:
        slot_index = int(datetime.now(timezone.utc).timestamp() // (15 * 60))
    account_bias = sum(ord(character) for character in account_id)
    offset = (slot_index + account_bias) % len(candidates)
    return list(candidates[offset:] + candidates[:offset])


async def main() -> None:
    root = Path(__file__).resolve().parents[1]
    config = load_config(root)
    matrix_account_id = configure_matrix_account(config)

    # Explicitly isolate Account 03's live configuration and D1 ledger.
    account03_live_mode = _truthy("ACCOUNT03_LIVE_MODE")
    publishing_requested = bool(config.settings["publishing"]["enabled"])

    active_account_ids = {
        account.id for account in config.active_accounts()
    }
    validate_account03_live_mode(
        matrix_account_id=matrix_account_id,
        publishing_requested=publishing_requested,
        account03_live_mode=account03_live_mode,
        active_account_ids=active_account_ids,
    )

    if account03_live_mode:
        apply_account03_runtime_overrides(config.settings)

    posts_per_account = _safe_int("POSTS_PER_ACCOUNT_PER_RUN", 1, 1, 5)
    reserve_per_account = _safe_int("LIVE_RESERVE_CANDIDATES", 4, 1, 8)
    candidate_plan = configure_live_candidate_reserve(
        config.settings,
        target_per_account=posts_per_account,
        reserve_per_account=reserve_per_account,
    )
    capture_policy = configure_live_capture_safety(config.settings)
    config.settings["signals"]["symbol_cooldown_hours"] = _safe_int(
        "SYMBOL_COOLDOWN_HOURS", 1, 1, 24
    )

    live = bool(config.settings["publishing"]["enabled"])
    if live and not _truthy("LIVE_PUBLISH_APPROVED"):
        raise RuntimeError("PUBLISH_ENABLED=true requires LIVE_PUBLISH_APPROVED=true")

    account03_d1 = live and account03_live_mode and matrix_account_id == "account_03"
    ledger_client = None
    if account03_d1:
        # Account 03 must never fall back to its local JSON ledger in live mode.
        ledger_client = Account03LedgerClient.from_env()
        ledger_client.healthcheck()

    preview = await run_cycle(config)
    candidate_posts = list(preview.get("posts", []))
    account_map: dict[str, AccountConfig] = {
        account.id: account for account in config.active_accounts()
    }
    candidates_by_account: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for post in candidate_posts:
        candidates_by_account[str(post["account_id"])].append(post)

    capture = OfficialBinanceWebChartCapture(root, config.settings)
    publisher = SquarePublisher(config.settings)
    publish_results: list[dict[str, Any]] = []
    candidate_failures: list[dict[str, Any]] = []
    ledger_value = os.getenv("PUBLISH_LEDGER_PATH", "").strip()
    ledger_path = Path(ledger_value) if ledger_value else root / "data" / "publish_ledger.json"
    if not ledger_path.is_absolute():
        ledger_path = root / ledger_path
    ledger = {"days": {}} if account03_d1 else _load_ledger(ledger_path)
    configured_daily_limit = int(
        config.settings["publishing"].get("per_account_daily_limit", 96)
    )
    daily_limit = _safe_int(
        "PER_ACCOUNT_DAILY_LIMIT", configured_daily_limit, 1, 500
    )

    account03_authorized_hour: str | None = None

    for account_id, account in account_map.items():
        successful_slots = 0
        account_candidates = rotate_candidates_for_stateless_slot(
            candidates_by_account.get(account_id, []),
            account_id,
        )
        candidate_index = 0
        terminal_publish_error = False

        while successful_slots < posts_per_account and candidate_index < len(account_candidates):
            if live and not account03_d1 and _published_today(ledger, account_id) >= daily_limit:
                publish_results.append(
                    {
                        "account_id": account_id,
                        "published": False,
                        "status": "blocked_daily_limit",
                        "error": f"Local safety limit reached: {daily_limit} posts per UTC day",
                    }
                )
                terminal_publish_error = True
                break

            post = account_candidates[candidate_index]
            candidate_index += 1
            signal = post["signal"]
            if live and not account03_d1:
                guard_reason = check_publish_guard(
                    ledger, account_id, signal,
                    duplicate_hours=24, max_coin_posts_per_utc_day=2,
                    min_interval_minutes=0,
                )
                if guard_reason:
                    publish_results.append({
                        "account_id": account_id, "symbol": signal.get("symbol"),
                        "published": False, "status": f"blocked_{guard_reason}",
                    })
                    if guard_reason == "hourly_limit":
                        terminal_publish_error = True
                        break
                    continue
            slot_number = successful_slots + 1
            image_path = (
                root
                / "output"
                / "charts"
                / f"{account_id}_{slot_number:02d}_candidate_{candidate_index:02d}_"
                f"{signal['symbol']}_{signal['timeframe']}.png"
            )
            image_relative = _relative(image_path, root)

            try:
                outer_attempt = await capture_with_live_retry(
                    capture,
                    signal["symbol"],
                    signal["timeframe"],
                    image_path,
                    outer_attempts=int(capture_policy["outer_capture_attempts"]),
                    retry_delay_seconds=float(capture_policy["retry_delay_seconds"]),
                )
            except WebCaptureError as exc:
                diagnostic_path = image_path.with_name(image_path.stem + "_diagnostic.png")
                capture_report = image_path.with_suffix(".capture.json")
                failure: dict[str, Any] = {
                    "account_id": account_id,
                    "symbol": signal["symbol"],
                    "timeframe": signal["timeframe"],
                    "candidate_number": candidate_index,
                    "error": str(exc),
                }
                if diagnostic_path.exists():
                    failure["diagnostic_path"] = _relative(diagnostic_path, root)
                if capture_report.exists():
                    failure["capture_report"] = _relative(capture_report, root)
                candidate_failures.append(failure)
                continue

            post["image_path"] = image_relative
            result: dict[str, Any] = {
                "account_id": account_id,
                "symbol": signal["symbol"],
                "timeframe": signal["timeframe"],
                "caption": post["caption"],
                "image_path": image_relative,
                "capture_outer_attempt": outer_attempt,
                "candidate_number": candidate_index,
                "published": False,
            }

            if not live:
                result["status"] = "preview_only"
                publish_results.append(result)
                successful_slots += 1
                continue

            reservation_id: str | None = None

            if account03_d1:
                try:
                    # Consume only after analysis and chart capture are complete.
                    # The Worker rejects expired nonces and authorizations from a
                    # different UTC hour; the local check fails closed as well.
                    if account03_authorized_hour is None:
                        authorization = ledger_client.consume_slot_authorization(
                            os.getenv("ACCOUNT03_SLOT_NONCE", "")
                        )
                        account03_authorized_hour = str(
                            authorization.get("hour_bucket", "")
                        )
                    if not account03_slot_hour_matches(account03_authorized_hour):
                        raise Account03LedgerError(
                            "Account 03 scheduler authorization no longer matches the current UTC hour."
                        )
                    reservation = ledger_client.reserve(_account03_signal_payload(signal))
                except Account03LedgerError as exc:
                    result["status"] = "blocked_ledger_unavailable"
                    result["error"] = str(exc)
                    publish_results.append(result)
                    terminal_publish_error = True
                    break

                if not reservation.get("reserved"):
                    reason = str(reservation.get("reason", "reservation_conflict"))
                    result["status"] = f"blocked_{reason}"
                    publish_results.append(result)
                    if reason in {
                        "hourly_limit", "account_daily_limit", "reservation_conflict"
                    }:
                        terminal_publish_error = True
                        break
                    continue

                reservation_id = str(reservation["reservation_id"])
            else:
                # Existing accounts retain their original local-ledger behavior.
                record_publish_attempt(ledger, account_id, signal, status="publish_pending")
                _save_ledger(ledger_path, ledger)

            # Re-check after reservation and immediately before the external
            # Binance request. If the hour rolled over, leave the reservation
            # pending conservatively and do not make the external publish call.
            if account03_d1 and not account03_slot_hour_matches(
                account03_authorized_hour or ""
            ):
                result["status"] = "blocked_slot_hour_expired"
                result["error"] = (
                    "Account 03 authorization UTC hour expired before Binance publish; "
                    "D1 reservation remains pending conservatively."
                )
                publish_results.append(result)
                terminal_publish_error = True
                break

            try:
                output = publisher.publish_image(account, post["caption"], image_path)
            except Exception as exc:
                if account03_d1:
                    try:
                        ledger_client.finalize(reservation_id, "publish_uncertain")
                    except Account03LedgerError as ledger_exc:
                        result["ledger_finalize_error"] = str(ledger_exc)
                else:
                    # Preserve the existing local-ledger behavior for other accounts.
                    record_publish_attempt(
                        ledger, account_id, signal, status="publish_uncertain"
                    )
                    _save_ledger(ledger_path, ledger)

                result["status"] = "failed_publish"
                result["error"] = str(exc)
                publish_results.append(result)
                terminal_publish_error = True
                break

            result["published"] = True
            result["status"] = "published"
            result["publisher_output"] = output

            if account03_d1:
                try:
                    ledger_client.finalize(reservation_id, "published")
                except Account03LedgerError as exc:
                    # Binance confirmed the post. The pending D1 reservation still
                    # consumes the quota, so stop rather than risking another post.
                    result["status"] = "published_ledger_finalize_failed"
                    result["ledger_finalize_error"] = str(exc)
                    successful_slots += 1
                    publish_results.append(result)
                    terminal_publish_error = True
                    break
            else:
                record_successful_publish(ledger, account_id, signal)
                _save_ledger(ledger_path, ledger)

            successful_slots += 1

            # Social syndication is deliberately downstream of a confirmed Square
            # success. The active account's own social credentials are used when
            # connected; accounts without connected social destinations stay
            # Binance-only. No social error can undo the confirmed Square publish.
            try:
                with SocialSyndicator(root, account_id) as social:
                    result["social_syndication"] = social.publish_after_square(
                        caption=post["caption"],
                        image_path=image_path,
                        signal=signal,
                    )
            except Exception as exc:
                result["social_syndication"] = {
                    "source_account_id": account_id,
                    "status": "failed_isolated",
                    "error": str(exc),
                }

            publish_results.append(result)

        if successful_slots < posts_per_account and not terminal_publish_error:
            publish_results.append(
                {
                    "account_id": account_id,
                    "published": False,
                    "status": "failed_no_capturable_candidate",
                    "error": (
                        f"No capturable official Binance chart remained after "
                        f"{candidate_index} candidate(s)"
                    ),
                    "candidate_failures": sum(
                        1 for item in candidate_failures if item["account_id"] == account_id
                    ),
                }
            )

    output_dir = root / "output" / "previews"
    output_dir.mkdir(parents=True, exist_ok=True)
    target_posts = posts_per_account * len(account_map)
    report = {
        "mode": "live" if live else "preview",
        "image_provider": "official_binance_mobile_web_live_verified",
        "active_accounts": list(account_map),
        "matrix_account_id": matrix_account_id,
        "target_posts": target_posts,
        "candidate_posts_generated": len(candidate_posts),
        "reserve_candidates_per_account": reserve_per_account,
        "published_posts": sum(1 for item in publish_results if item.get("published")),
        "failed_posts": sum(
            1 for item in publish_results if str(item.get("status", "")).startswith("failed")
        ),
        "candidate_capture_failures": len(candidate_failures),
        "per_account_daily_limit": daily_limit,
        "scheduled_posts_per_hour": _safe_int(
            "LIVE_EXPECTED_POSTS_PER_HOUR", 0, 0, 12
        ),
        "stateless_slot_rotation": _truthy("STATELESS_SLOT_ROTATION"),
        "live_slot_index": os.getenv("LIVE_SLOT_INDEX", ""),
        "capture_policy": capture_policy,
        "results": publish_results,
        "candidate_failures": candidate_failures,
    }
    report_path = output_dir / "square_api_run.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    summary_lines = [
        "# CoinCoach Square API run",
        "",
        f"- Mode: {report['mode']}",
        "- Image provider: official Binance mobile-web live chart",
        f"- Active accounts: {len(account_map)}",
        f"- Target posts: {report['target_posts']}",
        f"- Candidate posts generated: {report['candidate_posts_generated']}",
        f"- Reserve chart failures replaced: {report['candidate_capture_failures']}",
        f"- Per-account daily limit: {report['per_account_daily_limit']}",
        f"- Scheduled posts per hour: {report['scheduled_posts_per_hour']}",
        f"- Published posts: {report['published_posts']}",
        f"- Failed posts: {report['failed_posts']}",
        "",
    ]
    for item in publish_results:
        heading = f"## {item['account_id']}"
        if item.get("symbol"):
            heading += f" — {item['symbol']} {item['timeframe']}"
        summary_lines.extend([heading, "", f"- Status: {item['status']}"])
        if item.get("image_path"):
            summary_lines.append(f"- Image: `{item['image_path']}`")
        if item.get("error"):
            summary_lines.append(f"- Error: `{item['error']}`")
        social = item.get("social_syndication")
        if isinstance(social, dict):
            summary_lines.append(f"- Social syndication: {social.get('status', 'unknown')}")
            for delivery in social.get("deliveries", []):
                for platform, platform_result in delivery.get("platforms", {}).items():
                    summary_lines.append(
                        f"  - {platform}: {platform_result.get('status', 'unknown')}"
                    )
        if item.get("caption"):
            summary_lines.extend(["", "```text", item["caption"], "```"])
        summary_lines.append("")

    if candidate_failures:
        summary_lines.extend(["## Replaced chart candidates", ""])
        for item in candidate_failures:
            summary_lines.append(
                f"- {item['account_id']} — {item['symbol']} {item['timeframe']}: `{item['error']}`"
            )
        summary_lines.append("")

    summary_path = output_dir / "square_api_summary.md"
    summary_path.write_text("\n".join(summary_lines), encoding="utf-8")

    github_summary = os.getenv("GITHUB_STEP_SUMMARY")
    if github_summary:
        Path(github_summary).write_text(summary_path.read_text(encoding="utf-8"), encoding="utf-8")

    print(json.dumps(report, indent=2))
    if live and report["published_posts"] == 0:
        raise SystemExit("Live mode completed without publishing a post")


if __name__ == "__main__":
    asyncio.run(main())
