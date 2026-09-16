"""Operational CLI.  python -m app.cli <command>"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import sys
import uuid

from sqlalchemy import select

from app.core import crypto
from app.core.config import settings
from app.core.db import SessionLocal
from app.core.security import Role
from app.models import Account, RawDeal, Trade, User
from app.services import rebuild


async def create_user(email: str, admin: bool) -> int:
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
        role = Role.ADMIN if admin else Role.MEMBER
        if existing:
            print(f"{email} already exists; updating password and role to {role}.")
            existing.role = role
            existing.password_hash = crypto.hash_password(password)
        else:
            db.add(
                User(
                    email=email.lower(),
                    password_hash=crypto.hash_password(password),
                    role=role,
                )
            )
        await db.commit()
    print(f"Ready: {email} ({'admin' if admin else 'member'})")
    return 0


async def rebuild_account(account_id: str | None) -> int:
    """Recompute trades from stored deals. Journal notes are never touched."""
    async with SessionLocal() as db:
        stmt = select(Account).where(Account.is_archived.is_(False))
        if account_id:
            stmt = stmt.where(Account.id == uuid.UUID(account_id))
        accounts = (await db.execute(stmt)).scalars().unique().all()

        if not accounts:
            print("No matching accounts.")
            return 1

        for account in accounts:
            before = (
                await db.execute(select(Trade).where(Trade.account_id == account.id))
            ).scalars().all()
            count = await rebuild.rebuild_account(db, account)
            print(f"{account.label}: {len(before)} -> {count} trades")
    return 0


async def show_status() -> int:
    async with SessionLocal() as db:
        users = (await db.execute(select(User))).scalars().all()
        accounts = (await db.execute(select(Account))).scalars().unique().all()
        print(f"users    : {len(users)}")
        print(f"accounts : {len(accounts)}")
        for account in accounts:
            deals = (
                await db.execute(select(RawDeal).where(RawDeal.account_id == account.id))
            ).scalars().all()
            trades = (
                await db.execute(select(Trade).where(Trade.account_id == account.id))
            ).scalars().all()
            print(
                f"  - {account.label} ({account.mt5_login}@{account.broker_server}) "
                f"{account.margin_mode} · {len(deals)} deals · {len(trades)} trades "
                f"· {account.sync_status}"
            )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="app.cli", description="Trading journal admin")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("create-user", help="Create or update a user")
    p.add_argument("--email", required=True)
    p.add_argument("--admin", action="store_true")

    p = sub.add_parser("rebuild", help="Recompute trades from stored deals")
    p.add_argument("--account", default=None, help="Account id; omit for all")

    sub.add_parser("status", help="Show what is connected")

    args = parser.parse_args()
    settings.assert_production_safe()

    if args.command == "create-user":
        return asyncio.run(create_user(args.email, args.admin))
    if args.command == "rebuild":
        return asyncio.run(rebuild_account(args.account))
    return asyncio.run(show_status())


if __name__ == "__main__":
    raise SystemExit(main())
