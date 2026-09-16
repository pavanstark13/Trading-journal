"""The part the trader writes: why they took it, and what they learned."""
from __future__ import annotations

import uuid
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_principal
from app.core.db import get_session
from app.core.security import UserPrincipal
from app.models import Account, DailyNote, JournalEntry, Setup, Tag, Trade, TradeTag

router = APIRouter(tags=["journal"])

EMOTIONS = ["calm", "confident", "hesitant", "fomo", "revenge", "bored", "tilted"]
COMMON_MISTAKES = [
    "no setup", "moved stop", "oversized", "chased entry", "early exit",
    "no stop loss", "revenge trade", "ignored plan", "overtraded",
]


class JournalIn(BaseModel):
    thesis: str | None = Field(default=None, max_length=8000)
    execution_notes: str | None = Field(default=None, max_length=8000)
    lesson: str | None = Field(default=None, max_length=4000)
    emotion: str | None = Field(default=None, max_length=20)
    confidence: int | None = Field(default=None, ge=1, le=5)
    followed_plan: bool | None = None
    mistakes: list[str] | None = None
    #: Grades the PROCESS, not the money. A losing A-grade trade is still a good trade.
    grade: str | None = Field(default=None, pattern="^[A-F][+-]?$")
    setup_id: uuid.UUID | None = None


async def _trade_for(db: AsyncSession, principal: UserPrincipal, trade_id: uuid.UUID) -> Trade:
    trade = await db.get(Trade, trade_id)
    if trade is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Trade not found")
    account = await db.get(Account, trade.account_id)
    if account is None or (not principal.is_admin and account.user_id != principal.user_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Trade not found")
    return trade


@router.put("/trades/{trade_id}/journal")
async def upsert_journal(
    trade_id: uuid.UUID,
    payload: JournalIn,
    principal: UserPrincipal = Depends(current_principal),
    db: AsyncSession = Depends(get_session),
) -> dict:
    """Save the note.

    Keyed by (account, trade_key) rather than trade id, so rebuilding every trade from
    the raw broker deals cannot orphan what the trader wrote.
    """
    trade = await _trade_for(db, principal, trade_id)
    values = payload.model_dump(exclude_unset=True)

    entry = (
        await db.execute(
            select(JournalEntry).where(
                JournalEntry.account_id == trade.account_id,
                JournalEntry.trade_key == trade.trade_key,
            )
        )
    ).scalar_one_or_none()

    if entry is None:
        entry = JournalEntry(
            account_id=trade.account_id,
            trade_key=trade.trade_key,
            user_id=principal.user_id,
        )
        db.add(entry)

    for field, value in values.items():
        setattr(entry, field, value)

    await db.commit()
    return {"status": "saved", "trade_key": trade.trade_key}


@router.delete("/trades/{trade_id}/journal", status_code=status.HTTP_204_NO_CONTENT)
async def delete_journal(
    trade_id: uuid.UUID,
    principal: UserPrincipal = Depends(current_principal),
    db: AsyncSession = Depends(get_session),
) -> None:
    trade = await _trade_for(db, principal, trade_id)
    await db.execute(
        delete(JournalEntry).where(
            JournalEntry.account_id == trade.account_id,
            JournalEntry.trade_key == trade.trade_key,
        )
    )
    await db.commit()


# ── tags ────────────────────────────────────────────────────────────────────────
class TagIn(BaseModel):
    name: str = Field(max_length=48)
    color: str | None = Field(default=None, max_length=16)


@router.get("/tags")
async def list_tags(
    principal: UserPrincipal = Depends(current_principal),
    db: AsyncSession = Depends(get_session),
) -> list[dict]:
    tags = (
        (await db.execute(select(Tag).where(Tag.user_id == principal.user_id).order_by(Tag.name)))
        .scalars()
        .all()
    )
    return [{"id": str(t.id), "name": t.name, "color": t.color} for t in tags]


@router.post("/tags", status_code=status.HTTP_201_CREATED)
async def create_tag(
    payload: TagIn,
    principal: UserPrincipal = Depends(current_principal),
    db: AsyncSession = Depends(get_session),
) -> dict:
    tag = Tag(user_id=principal.user_id, name=payload.name.strip(), color=payload.color)
    db.add(tag)
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        existing = (
            await db.execute(
                select(Tag).where(
                Tag.user_id == principal.user_id, Tag.name == payload.name.strip()
            )
            )
        ).scalar_one()
        return {"id": str(existing.id), "name": existing.name, "color": existing.color}
    return {"id": str(tag.id), "name": tag.name, "color": tag.color}


@router.put("/trades/{trade_id}/tags")
async def set_trade_tags(
    trade_id: uuid.UUID,
    tag_ids: list[uuid.UUID],
    principal: UserPrincipal = Depends(current_principal),
    db: AsyncSession = Depends(get_session),
) -> dict:
    trade = await _trade_for(db, principal, trade_id)
    await db.execute(
        delete(TradeTag).where(
            TradeTag.account_id == trade.account_id, TradeTag.trade_key == trade.trade_key
        )
    )
    for tag_id in tag_ids:
        await db.execute(
            pg_insert(TradeTag)
            .values(account_id=trade.account_id, trade_key=trade.trade_key, tag_id=tag_id)
            .on_conflict_do_nothing(constraint="uq_trade_tag")
        )
    await db.commit()
    return {"status": "saved", "count": len(tag_ids)}


# ── setups (the playbook) ───────────────────────────────────────────────────────
class SetupIn(BaseModel):
    name: str = Field(max_length=80)
    description: str | None = Field(default=None, max_length=4000)
    checklist: list[str] | None = None
    color: str | None = Field(default=None, max_length=16)


@router.get("/setups")
async def list_setups(
    principal: UserPrincipal = Depends(current_principal),
    db: AsyncSession = Depends(get_session),
) -> list[dict]:
    setups = (
        (
            await db.execute(
                select(Setup)
                .where(Setup.user_id == principal.user_id, Setup.is_archived.is_(False))
                .order_by(Setup.name)
            )
        )
        .scalars()
        .all()
    )
    return [
        {
            "id": str(s.id), "name": s.name, "description": s.description,
            "checklist": s.checklist or [], "color": s.color,
        }
        for s in setups
    ]


@router.post("/setups", status_code=status.HTTP_201_CREATED)
async def create_setup(
    payload: SetupIn,
    principal: UserPrincipal = Depends(current_principal),
    db: AsyncSession = Depends(get_session),
) -> dict:
    setup = Setup(
        user_id=principal.user_id,
        name=payload.name.strip(),
        description=payload.description,
        checklist=payload.checklist,
        color=payload.color,
    )
    db.add(setup)
    await db.commit()
    return {"id": str(setup.id), "name": setup.name}


@router.patch("/setups/{setup_id}")
async def update_setup(
    setup_id: uuid.UUID,
    payload: SetupIn,
    principal: UserPrincipal = Depends(current_principal),
    db: AsyncSession = Depends(get_session),
) -> dict:
    setup = await db.get(Setup, setup_id)
    if setup is None or setup.user_id != principal.user_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Setup not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(setup, field, value)
    await db.commit()
    return {"id": str(setup.id), "name": setup.name}


# ── daily notes ─────────────────────────────────────────────────────────────────
class DailyNoteIn(BaseModel):
    pre_market: str | None = Field(default=None, max_length=8000)
    post_market: str | None = Field(default=None, max_length=8000)
    mood: str | None = Field(default=None, max_length=20)


@router.get("/daily-notes/{note_date}")
async def get_daily_note(
    note_date: date,
    principal: UserPrincipal = Depends(current_principal),
    db: AsyncSession = Depends(get_session),
) -> dict:
    note = (
        await db.execute(
            select(DailyNote).where(
                DailyNote.user_id == principal.user_id, DailyNote.note_date == note_date
            )
        )
    ).scalar_one_or_none()
    if note is None:
        return {"note_date": note_date, "pre_market": None, "post_market": None, "mood": None}
    return {
        "note_date": note.note_date, "pre_market": note.pre_market,
        "post_market": note.post_market, "mood": note.mood,
    }


@router.put("/daily-notes/{note_date}")
async def upsert_daily_note(
    note_date: date,
    payload: DailyNoteIn,
    principal: UserPrincipal = Depends(current_principal),
    db: AsyncSession = Depends(get_session),
) -> dict:
    note = (
        await db.execute(
            select(DailyNote).where(
                DailyNote.user_id == principal.user_id, DailyNote.note_date == note_date
            )
        )
    ).scalar_one_or_none()
    if note is None:
        note = DailyNote(user_id=principal.user_id, note_date=note_date)
        db.add(note)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(note, field, value)
    await db.commit()
    return {"status": "saved", "note_date": note_date}


@router.get("/journal/vocabulary")
async def vocabulary() -> dict:
    """Suggested emotions and mistakes, so tagging stays consistent enough to count."""
    return {"emotions": EMOTIONS, "mistakes": COMMON_MISTAKES}
