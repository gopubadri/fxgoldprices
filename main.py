"""Fetch a live gold price and post it to a Telegram chat.

The USD quote is the spot price per troy ounce. One gram in INR is that
price divided by 31.1034768 grams and multiplied by the USD/INR rate.
The INR change is the difference from the last successful message, stored
in state/last_price.json.

Environment variables:
    GOLD_API_URL         Gold price endpoint. Defaults to the XAU spot API.
    GOLD_API_KEY         Optional key sent as the X-API-Key header.
    TELEGRAM_BOT_TOKEN   Bot token from BotFather.
    TELEGRAM_CHAT_ID     Target channel, group, or user chat id.
    STATE_PATH           File that remembers the previous 1 gram INR price.
"""

import html
import json
import os
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests

DEFAULT_GOLD_API_URL = "https://api.gold-api.com/price/XAU"
DEFAULT_STATE_PATH = "state/last_price.json"
FX_URLS = (
    "https://api.frankfurter.app/latest?from=USD&to=INR",
    "https://open.er-api.com/v6/latest/USD",
)
TROY_OUNCE_GRAMS = 31.1034768
REQUEST_TIMEOUT_SECONDS = 30
IST = ZoneInfo("Asia/Kolkata")


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def require_env(name: str) -> str:
    value = env(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def first_present(payload: dict, *keys: str):
    """Return the first matching value, including one level of nesting."""
    for key in keys:
        if key in payload and payload[key] not in (None, ""):
            return payload[key]
    for value in payload.values():
        if isinstance(value, dict):
            found = first_present(value, *keys)
            if found not in (None, ""):
                return found
    return None


def as_number(value):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "").replace("%", "").replace("₹", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def money(amount: float, symbol: str) -> str:
    return f"{symbol}{amount:,.2f}"


def inr_change(current: float, previous) -> str:
    if previous is None:
        return "N/A"
    delta = current - previous
    return f"{'+' if delta >= 0 else '-'}₹{abs(delta):,.2f}"


def quote_time_ist(value) -> datetime:
    if isinstance(value, str) and value.strip():
        text = value.strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            parsed = None
        if parsed is not None:
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(IST)
    return datetime.now(IST)


def fetch_json(url: str, api_key: str = "") -> dict:
    headers = {"Accept": "application/json", "User-Agent": "fxgoldprices"}
    if api_key:
        headers["X-API-Key"] = api_key

    response = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS)
    if response.status_code != 200:
        detail = response.text.strip().replace("\n", " ")[:300]
        raise RuntimeError(
            f"HTTP {response.status_code} from {url}"
            + (f": {detail}" if detail else "")
        )
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError(f"Response from {url} was not a JSON object")
    return payload


def fetch_gold_quote(url: str, api_key: str) -> tuple[float, datetime]:
    payload = fetch_json(url, api_key)
    price = as_number(
        first_present(payload, "price", "gold_price", "rate", "ask", "bid", "value", "last")
    )
    if price is None or price <= 0:
        raise RuntimeError("Gold API response did not include a USD price")
    when = quote_time_ist(
        first_present(payload, "updatedAt", "updated_at", "timestamp", "time", "date")
    )
    return price, when


def usd_inr_from_payload(payload: dict):
    rates = payload.get("rates")
    if isinstance(rates, dict):
        return as_number(rates.get("INR"))
    return as_number(first_present(payload, "INR", "inr"))


def fetch_usd_inr() -> float:
    errors = []
    for url in FX_URLS:
        try:
            rate = usd_inr_from_payload(fetch_json(url))
        except (requests.RequestException, ValueError, RuntimeError) as exc:
            errors.append(str(exc))
            continue
        if rate is not None and rate > 0:
            return rate
        errors.append(f"No INR rate in {url}")
    detail = "; ".join(errors) or "no FX response"
    raise RuntimeError(f"Could not fetch USD/INR rate: {detail}")


def load_previous_inr(path: str):
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    value = as_number(payload.get("inr_per_gram"))
    if value is None or value <= 0:
        return None
    return value


def save_state(path: str, usd_per_ounce: float, inr_per_gram: float, usd_inr: float, when: datetime) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    payload = {
        "usd_per_ounce": round(usd_per_ounce, 4),
        "inr_per_gram": round(inr_per_gram, 4),
        "usd_inr": round(usd_inr, 4),
        "sent_at_ist": when.isoformat(),
    }
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
        handle.write("\n")


def build_price_message(usd_per_ounce: float, inr_per_gram: float, previous_inr, when: datetime) -> str:
    rows = (
        ("Gold price (USD)", f"{money(usd_per_ounce, '$')} / oz"),
        ("1 gram (INR)", money(inr_per_gram, "₹")),
        ("Change (INR)", inr_change(inr_per_gram, previous_inr)),
        ("Date", when.strftime("%d %b %Y")),
        ("Time", when.strftime("%I:%M %p IST")),
    )
    lines = ["🥇 <b>Gold Price Update</b>", ""]
    lines.extend(f"<b>{label}:</b> {html.escape(value)}" for label, value in rows)
    return "\n".join(lines)


def build_error_message(reason: str) -> str:
    when = datetime.now(IST)
    return "\n".join(
        [
            "⚠️ <b>Error fetching gold data</b>",
            "",
            f"<b>Details:</b> {html.escape(reason[:500])}",
            f"<b>Date:</b> {html.escape(when.strftime('%d %b %Y'))}",
            f"<b>Time:</b> {html.escape(when.strftime('%I:%M %p IST'))}",
        ]
    )


def send_telegram(token: str, chat_id: str, text: str) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    response = requests.post(
        url,
        json={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    if response.status_code != 200:
        detail = response.text.strip().replace("\n", " ")[:300]
        raise RuntimeError(
            f"Telegram returned HTTP {response.status_code}"
            + (f": {detail}" if detail else "")
        )


def main() -> int:
    token = require_env("TELEGRAM_BOT_TOKEN")
    chat_id = require_env("TELEGRAM_CHAT_ID")
    gold_url = env("GOLD_API_URL", DEFAULT_GOLD_API_URL) or DEFAULT_GOLD_API_URL
    gold_key = env("GOLD_API_KEY")
    state_path = env("STATE_PATH", DEFAULT_STATE_PATH) or DEFAULT_STATE_PATH

    try:
        usd_per_ounce, when = fetch_gold_quote(gold_url, gold_key)
        usd_inr = fetch_usd_inr()
        inr_per_gram = (usd_per_ounce / TROY_OUNCE_GRAMS) * usd_inr
        previous_inr = load_previous_inr(state_path)
        message = build_price_message(usd_per_ounce, inr_per_gram, previous_inr, when)
    except (requests.RequestException, ValueError, RuntimeError) as exc:
        message = build_error_message(str(exc) or exc.__class__.__name__)
        try:
            send_telegram(token, chat_id, message)
        except (requests.RequestException, RuntimeError) as notify_exc:
            print(f"Gold fetch failed: {exc}", file=sys.stderr)
            print(f"Telegram alert failed: {notify_exc}", file=sys.stderr)
            return 1
        print(f"Gold fetch failed: {exc}", file=sys.stderr)
        return 1

    send_telegram(token, chat_id, message)
    save_state(state_path, usd_per_ounce, inr_per_gram, usd_inr, when)
    print(
        f"Sent gold update: USD {usd_per_ounce:,.2f}/oz, "
        f"INR {inr_per_gram:,.2f}/g, USDINR {usd_inr:,.2f}"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        raise SystemExit(1) from exc
