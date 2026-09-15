"""Operational CLI.  python -m app.cli <command>"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import sys

from sqlalchemy import select

from app.core import crypto
from app.core.config import settings
from app.core.db import SessionLocal
from app.core.security import Role
from app.models import EaInstallation, MasterAccount, SystemSettings, User


async def create_superadmin(email: str) -> int:
    password = getpass.getpass("Password (min 12 chars): ")
    if len(password) < 12:
        print("Password must be at least 12 characters.", file=sys.stderr)
        return 1
    if password != getpass.getpass("Confirm: "):
        print("Passwords do not match.", file=sys.stderr)
        return 1

    async with SessionLocal() as db:
        existing = (
            await db.execute(select(User).where(User.email == email.lower()))
        ).scalar_one_or_none()
        if existing:
            print(f"{email} already exists; promoting to SUPER_ADMIN.")
            existing.role = Role.SUPER_ADMIN
            existing.password_hash = crypto.hash_password(password)
        else:
            db.add(
                User(
                    email=email.lower(),
                    password_hash=crypto.hash_password(password),
                    role=Role.SUPER_ADMIN,
                    full_name="Super Admin",
                )
            )
        if await db.get(SystemSettings, 1) is None:
            # New installs start in PAPER. Going LIVE is a deliberate, audited action.
            db.add(SystemSettings(id=1, mode="PAPER", copying_paused=False))
        await db.commit()
    print(f"Super admin ready: {email}")
    return 0


async def create_master(label: str, mt5_login: int, server: str) -> int:
    async with SessionLocal() as db:
        master = MasterAccount(
            label=label, mt5_login=mt5_login, broker_server=server, currency="USD"
        )
        db.add(master)
        await db.flush()

        code = crypto.generate_install_code()
        db.add(
            EaInstallation(
                kind="MASTER",
                master_account_id=master.id,
                install_code=code,
                api_key_id="ea_" + crypto.generate_secret(12),
                api_secret_hash="",
                status="PENDING",
            )
        )
        await db.commit()
    print(f"Master account created: {master.id}")
    print(f"Install code (paste into MasterTradeBridge.mq5): {code}")
    return 0


async def show_status() -> int:
    async with SessionLocal() as db:
        system = await db.get(SystemSettings, 1)
        masters = (await db.execute(select(MasterAccount))).scalars().all()
        print(f"mode              : {system.mode if system else 'UNSET'}")
        print(f"copying_paused    : {system.copying_paused if system else '-'}")
        print(f"emergency_stop    : {system.emergency_stop if system else '-'}")
        print(f"master accounts   : {len(masters)}")
        for m in masters:
            print(f"  - {m.label} ({m.mt5_login}@{m.broker_server}) hb={m.last_heartbeat_at}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="app.cli", description="TradeBridge operations")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("create-superadmin", help="Create or promote a super admin")
    p.add_argument("--email", required=True)

    p = sub.add_parser("create-master", help="Register a master account and issue an install code")
    p.add_argument("--label", default="MASTER-001")
    p.add_argument("--mt5-login", type=int, required=True)
    p.add_argument("--server", required=True)

    sub.add_parser("status", help="Show system state")

    args = parser.parse_args()
    settings.assert_production_safe()

    if args.command == "create-superadmin":
        return asyncio.run(create_superadmin(args.email))
    if args.command == "create-master":
        return asyncio.run(create_master(args.label, args.mt5_login, args.server))
    return asyncio.run(show_status())


if __name__ == "__main__":
    raise SystemExit(main())
