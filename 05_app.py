#!/usr/bin/env python3
"""
CLI-прототип платёжного сервиса — кейс 1.

Использование:
    python 04_прототип.py payment.json

TODO для команды:
    1. Дописать validate() — правила V-3…V-9 из 02_правила.md.
    2. Дописать emulate_gateway() — 4 сценария (SUCCESS / INSUFFICIENT_FUNDS /
       CARD_DECLINED / TIMEOUT).
    3. Добавить чтение recipients.csv для проверки recipient.
    4. Обработать таймаут: exit code 3.
    5. Записать результат в payments_out.csv (append).
"""
import csv
import hashlib
import json
import os
import re
import sys
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RECIPIENTS_CSV = os.path.join(BASE_DIR, "recipients.csv")
OUT_CSV = os.path.join(BASE_DIR, "payments_out.csv")
ORDER_ID_RE = re.compile(r'^[A-Za-z0-9_\-]{1,64}$')
PAN_RE = re.compile(r'\d{13,19}')


def load_active_recipients():
    """Прочитать recipients.csv, вернуть set() активных ID."""
    active = set()
    try:
        with open(RECIPIENTS_CSV, encoding="cp1251") as f:
            for row in csv.DictReader(f):
                if row.get("status") == "ACTIVE":
                    active.add(row["recipient_id"])
    except FileNotFoundError:
        print(f"WARN: {RECIPIENTS_CSV} не найден", file=sys.stderr)
    return active

def load_existing_payments(path):
    """
    Читает payments_out.csv (или payments.csv), возвращает:
      existing_orders — set() пар (order_id, recipient) для V-8
      seen_idem       — dict() idempotency_key -> строка CSV для V-9
    """
    existing_orders = set()
    seen_idem = {}
    try:
        with open(path, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                existing_orders.add((row.get("order_id"), row.get("recipient")))
                key = row.get("idempotency_key")
                if key:
                    seen_idem[key] = row
    except FileNotFoundError:
        pass  # файла ещё нет — первый запуск
    return existing_orders, seen_idem

def validate(payload, active_recipients, existing_orders=None, seen_idempotency=None):
    """
    V-1…V-9 из 02_правила.md.
    Возвращает [(field, code), ...].
    """
    errors = []

    # ---------- V-1: обязательные поля ----------
    for field in ["order_id", "recipient", "purpose", "amount", "currency"]:
        if payload.get(field) in (None, "", []):
            errors.append((field, "VALIDATION_ERROR"))
    if errors:
        # без обязательных полей дальше проверять нечего
        return errors

    # ---------- V-2: формат order_id ----------
    if not ORDER_ID_RE.match(str(payload["order_id"])):
        errors.append(("order_id", "INVALID_FORMAT"))

    # ---------- V-3: диапазон суммы ----------
    amt = None
    try:
        amt = float(payload["amount"])
    except (TypeError, ValueError):
        errors.append(("amount", "AMOUNT_OUT_OF_RANGE"))
    if amt is not None and (amt <= 0 or amt > 1_000_000):
        errors.append(("amount", "AMOUNT_OUT_OF_RANGE"))

    # ---------- V-4: валюта ----------
    if payload["currency"] not in ("RUB",):
        errors.append(("currency", "UNSUPPORTED_CURRENCY"))

    # ---------- V-5: получатель активен ----------
    if payload["recipient"] not in active_recipients:
        errors.append(("recipient", "RECIPIENT_NOT_FOUND"))

    # ---------- V-6: purpose (длина + PAN) ----------
    purpose = str(payload["purpose"])
    if len(purpose) > 200 or PAN_RE.search(purpose):
        errors.append(("purpose", "INVALID_PURPOSE"))

    # ---------- V-7: точность суммы (≤ 2 знака) ----------
    raw_amount = str(payload["amount"])
    if "." in raw_amount:
        decimals = raw_amount.split(".", 1)[1]
        if len(decimals) > 2:
            errors.append(("amount", "INVALID_AMOUNT_PRECISION"))

    # ---------- V-8: дубль order_id + recipient ----------
    if existing_orders and (payload["order_id"], payload["recipient"]) in existing_orders:
        errors.append(("order_id", "DUPLICATE_ORDER"))

    # ---------- V-9: повтор Idempotency-Key ----------
    key = payload.get("idempotency_key")
    if seen_idempotency is not None and key and key in seen_idempotency:
        errors.append(("idempotency_key", "DUPLICATE_REQUEST"))

    return errors

def emulate_gateway(order_id):
    """
    4 сценария. Возвращает (status, error_code).
    hash(order_id) mod 10:
      0..6 — SUCCESS
      7    — DECLINED / INSUFFICIENT_FUNDS
      8    — DECLINED / CARD_DECLINED
      9    — PENDING  / TIMEOUT
    """
    h = int(hashlib.md5(str(order_id).encode()).hexdigest(), 16) % 10
    if h <= 6:
        return "SUCCESS", None
    if h == 7:
        return "DECLINED", "INSUFFICIENT_FUNDS"
    if h == 8:
        return "DECLINED", "CARD_DECLINED"
    return "PENDING", "TIMEOUT"

def append_result(payload, payment_id, status, error_code):
    """Дописать результат в payments_out.csv (append, с заголовком при первом запуске)."""
    now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    row = {
        "payment_id": payment_id,
        "order_id": payload["order_id"],
        "recipient": payload["recipient"],
        "purpose": payload["purpose"],
        "amount": f'{float(payload["amount"]):.2f}',
        "currency": payload["currency"],
        "status": status,
        "error_code": error_code or "",
        "idempotency_key": payload.get("idempotency_key", ""),
        "created_at": now,
        "confirmed_at": now if status == "SUCCESS" else "",
        "correlation_id": "corr_" + payment_id[-4:],
    }
    file_exists = os.path.exists(OUT_CSV)
    with open(OUT_CSV, "a", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(row.keys()))
        if not file_exists:
            writer.writeheader()
        writer.writerow(row)

def main():
    if len(sys.argv) < 2:
        print("Использование: python 05_app.py payment.json")
        sys.exit(1)

    # 1) читаем входной JSON
    with open(sys.argv[1], encoding="utf-8") as f:
        payload = json.load(f)

    # 2) подготавливаем контекст
    active = load_active_recipients()
    existing_orders, seen_idem = load_existing_payments(OUT_CSV)

    # 3) валидация V-1…V-9
    errors = validate(payload, active, existing_orders, seen_idem)

    # 4) V-9 special: повтор Idempotency-Key — вернуть кэш, не ошибку
    if any(code == "DUPLICATE_REQUEST" for _, code in errors):
        saved = seen_idem[payload["idempotency_key"]]
        print(json.dumps({
            "payment_id": saved["payment_id"],
            "status": saved["status"],
            "error_code": saved["error_code"] or None,
            "idempotent_replay": True,
        }, ensure_ascii=False, indent=2))
        sys.exit(0)

    # 5) остальные ошибки — вывод и exit 2
    if errors:
        print(json.dumps({
            "status": "ERROR",
            "error_code": "VALIDATION_ERROR",
            "errors": [{"field": f, "code": c} for f, c in errors],
        }, ensure_ascii=False, indent=2))
        sys.exit(2)

    # 6) эмуляция шлюза
    status, error_code = emulate_gateway(payload["order_id"])

    # 7) payment_id: pay_demo_ + последние 4 символа order_id
    payment_id = "pay_demo_" + str(payload["order_id"])[-4:]

    # 8) запись в payments_out.csv
    append_result(payload, payment_id, status, error_code)

    # 9) вывод результата
    print(json.dumps({
        "payment_id": payment_id,
        "status": status,
        "error_code": error_code,
    }, ensure_ascii=False, indent=2))

    # 10) exit codes: 0 SUCCESS, 1 DECLINED, 3 TIMEOUT(PENDING)
    if status == "SUCCESS":
        sys.exit(0)
    if status == "PENDING":
        sys.exit(3)
    sys.exit(1)

if __name__ == "__main__":
    main()