#!/usr/bin/env python3
"""One-time migration: set up multi-user database and create admin account.

Run this once after deploying the multi-user version:
    python migrate.py

It will:
1. Create user tables (users, user_segment_configs, decision_log, user_api_usage, trading_profiles)
2. Create the admin user (you) with your existing credentials from .env
3. Create default segment configs for the admin user
4. Enable all segments for the admin with current settings
5. Migrate existing segment configs to trading profiles
"""

import os
import sys
from dotenv import load_dotenv

load_dotenv()

from models import (
    init_user_db, create_user, update_user_credentials,
    get_user_by_email, update_user_segment_config,
    create_default_segment_configs,
    migrate_segments_to_profiles,
)
from crypto import encrypt


def migrate():
    print("=== QuantOpsAI Migration ===\n")

    # Step 1: Create tables
    print("[1/4] Creating database tables...")
    init_user_db()
    print("  Done.\n")

    # Step 2: Create admin user
    admin_email = os.getenv("NOTIFICATION_EMAIL", "admin@quantopsai.local")
    print(f"[2/4] Creating admin user: {admin_email}")

    existing = get_user_by_email(admin_email)
    if existing:
        print(f"  Admin user already exists (id={existing['id']}). Skipping.\n")
        user_id = existing["id"]
    else:
        # Default password — admin should change this
        user_id = create_user(
            email=admin_email,
            password="quantopsai2026",
            display_name="Admin",
            is_admin=True,
        )
        print(f"  Created admin user (id={user_id})")
        print(f"  Default password: quantopsai2026  <-- CHANGE THIS after first login\n")

    # Step 3: Create default segment configs (legacy, for backward compat)
    print("[3/4] Importing credentials and creating segment configs...")

    # Ensure segment config rows exist
    create_default_segment_configs(user_id)

    smallcap_key = os.getenv("SMALLCAP_ALPACA_KEY") or os.getenv("ALPACA_API_KEY", "")
    smallcap_secret = os.getenv("SMALLCAP_ALPACA_SECRET") or os.getenv("ALPACA_SECRET_KEY", "")
    midcap_key = os.getenv("MIDCAP_ALPACA_KEY", "")
    midcap_secret = os.getenv("MIDCAP_ALPACA_SECRET", "")
    largecap_key = os.getenv("LARGECAP_ALPACA_KEY", "")
    largecap_secret = os.getenv("LARGECAP_ALPACA_SECRET", "")
    crypto_key = os.getenv("CRYPTO_ALPACA_KEY", "")
    crypto_secret = os.getenv("CRYPTO_ALPACA_SECRET", "")
    anthropic_key = os.getenv("ANTHROPIC_API_KEY", "")
    notification_email = os.getenv("NOTIFICATION_EMAIL", "")
    resend_key = os.getenv("RESEND_API_KEY", "")

    # Store default credentials on user record (microsmall cap keys)
    update_user_credentials(
        user_id=user_id,
        alpaca_key=smallcap_key,
        alpaca_secret=smallcap_secret,
        anthropic_key=anthropic_key,
        notification_email=notification_email,
        resend_key=resend_key,
    )
    print("  Stored Anthropic and notification credentials.")

    # Enable and store per-segment Alpaca keys
    for segment, key, secret in [
        ("microsmall", smallcap_key, smallcap_secret),
        ("midcap", midcap_key, midcap_secret),
        ("largecap", largecap_key, largecap_secret),
        ("crypto", crypto_key, crypto_secret),
    ]:
        if key and secret:
            update_user_segment_config(
                user_id, segment,
                enabled=1,
                alpaca_api_key_enc=encrypt(key),
                alpaca_secret_key_enc=encrypt(secret),
            )
            print(f"  Enabled segment: {segment} (with dedicated Alpaca keys)")
        else:
            print(f"  Skipped segment: {segment} (no credentials)")

    # Step 4: Migrate segment configs to trading profiles
    print("\n[4/4] Migrating segment configs to trading profiles...")
    created_ids = migrate_segments_to_profiles(user_id)
    if created_ids:
        print(f"  Created {len(created_ids)} trading profiles: {created_ids}")
    else:
        print("  No new profiles created (already migrated or no segments to migrate).")

    print(f"\n=== Migration Complete ===")
    print(f"Admin login: {admin_email} / quantopsai2026")
    print(f"Web UI: http://localhost:5000 (local) or http://<droplet-ip> (remote)")


if __name__ == "__main__":
    migrate()
