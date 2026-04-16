#!/usr/bin/env python3
"""Database migration: create/update tables, create admin user, store credentials.

Safe to re-run at any time — idempotent. Does NOT touch existing profiles or
trading data. Only creates tables, adds missing columns, and ensures the admin
user + credentials exist.

Usage:
    python migrate.py
"""

import os
import sys
from dotenv import load_dotenv

load_dotenv()

from models import (
    init_user_db, create_user, update_user_credentials,
    get_user_by_email, get_user_profiles,
)


def migrate():
    print("=== QuantOpsAI Migration ===\n")

    # Step 1: Create tables + run column migrations
    print("[1/3] Creating/updating database tables...")
    init_user_db()
    print("  Done.\n")

    # Step 2: Create admin user (skip if exists)
    admin_email = os.getenv("NOTIFICATION_EMAIL", "admin@quantopsai.local")
    print(f"[2/3] Admin user: {admin_email}")

    existing = get_user_by_email(admin_email)
    if existing:
        print(f"  Already exists (id={existing['id']}).\n")
        user_id = existing["id"]
    else:
        user_id = create_user(
            email=admin_email,
            password="quantopsai2026",
            display_name="Admin",
            is_admin=True,
        )
        print(f"  Created admin user (id={user_id})")
        print(f"  Default password: quantopsai2026  <-- CHANGE THIS after first login\n")

    # Step 3: Store credentials from .env on the user record
    print("[3/3] Storing credentials from .env...")
    anthropic_key = os.getenv("ANTHROPIC_API_KEY", "")
    notification_email = os.getenv("NOTIFICATION_EMAIL", "")
    resend_key = os.getenv("RESEND_API_KEY", "")
    smallcap_key = os.getenv("SMALLCAP_ALPACA_KEY") or os.getenv("ALPACA_API_KEY", "")
    smallcap_secret = os.getenv("SMALLCAP_ALPACA_SECRET") or os.getenv("ALPACA_SECRET_KEY", "")

    update_user_credentials(
        user_id=user_id,
        alpaca_key=smallcap_key,
        alpaca_secret=smallcap_secret,
        anthropic_key=anthropic_key,
        notification_email=notification_email,
        resend_key=resend_key,
    )
    print("  Stored Anthropic, Resend, and default Alpaca credentials.\n")

    # Report current profiles
    profiles = get_user_profiles(user_id)
    if profiles:
        print(f"Existing trading profiles ({len(profiles)}):")
        for p in profiles:
            print(f"  #{p['id']} {p['name']} ({p['market_type']}, schedule={p.get('schedule_type', '?')})")
    else:
        print("No trading profiles found. Create them via the web UI Settings page.")

    print(f"\n=== Migration Complete ===")
    print(f"Admin login: {admin_email}")
    print(f"Web UI: http://localhost:5000 (local) or http://<droplet-ip> (remote)")


if __name__ == "__main__":
    migrate()
