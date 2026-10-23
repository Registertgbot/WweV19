"""Coins: earning (collecting, match wins, daily), spending (VIP card skins) and the equipped skin."""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from database import transaction
from models import (EconomyState, Match, MatchReward, MatchStatus, TagMatch, UserSkin, Wallet, utcnow)
from services.cards import VIP_STYLES

log = logging.getLogger(__name__)
IST = timedelta(hours=5, minutes=30)

COLLECT_REWARD = {"common": 50, "rare": 120, "epic": 250, "legendary": 600}
WIN_REWARD, PLAY_REWARD, TITLE_REWARD = 100, 25, 300
DAILY_BASE, DAILY_STEP, DAILY_MAX_STEPS = 100, 25, 6          # day 1 = 100 ... day 7+ = 250
POLL_SECONDS = 10


def today_ist():
    return (utcnow() + IST).date()


def skin_name(key: str) -> str:
    return VIP_STYLES[key][4].title()


def skin_price(key: str) -> int:
    return VIP_STYLES[key][5]


async def wallet(session: AsyncSession, user_id: int) -> Wallet:
    row = await session.get(Wallet, user_id)
    if row is None:
        row = Wallet(user_id=user_id, coins=0, daily_streak=0)
        session.add(row)
        await session.flush()
    return row


async def add_coins(session: AsyncSession, user_id: int, amount: int) -> int:
    row = await wallet(session, user_id)
    row.coins += amount
    return row.coins


async def spend(session: AsyncSession, user_id: int, amount: int) -> bool:
    await wallet(session, user_id)
    res = await session.execute(update(Wallet).where(Wallet.user_id == user_id, Wallet.coins >= amount)
                                .values(coins=Wallet.coins - amount))
    return res.rowcount == 1


async def claim_daily(session: AsyncSession, user_id: int) -> tuple[int, int, int] | None:
    """-> (reward, streak, balance) or None when today's reward was already taken."""
    row = await wallet(session, user_id)
    today = today_ist()
    if row.daily_on == today:
        return None
    row.daily_streak = row.daily_streak + 1 if row.daily_on == today - timedelta(days=1) else 1
    row.daily_on = today
    reward = DAILY_BASE + DAILY_STEP * min(row.daily_streak - 1, DAILY_MAX_STEPS)
    row.coins += reward
    return reward, row.daily_streak, row.coins


async def owned_skins(session: AsyncSession, user_id: int) -> set[str]:
    return set((await session.execute(select(UserSkin.skin).where(UserSkin.user_id == user_id))).scalars().all())


async def buy_skin(session: AsyncSession, user_id: int, key: str) -> str:
    """-> ok | owned | poor | unknown. A bought skin is equipped at once."""
    if key not in VIP_STYLES:
        return "unknown"
    if key in await owned_skins(session, user_id):
        return "owned"
    if not await spend(session, user_id, skin_price(key)):
        return "poor"
    session.add(UserSkin(user_id=user_id, skin=key))
    (await wallet(session, user_id)).skin = key
    return "ok"


async def equip(session: AsyncSession, user_id: int, key: str | None) -> bool:
    if key is not None and key not in await owned_skins(session, user_id):
        return False
    (await wallet(session, user_id)).skin = key
    return True


async def equipped_skin(session: AsyncSession, user_id: int) -> str | None:
    row = await session.get(Wallet, user_id)
    if row is None or not row.skin or row.skin not in VIP_STYLES:
        return None
    return row.skin if row.skin in await owned_skins(session, user_id) else None


# ------------------------------------------------------------------------------ match winnings

async def _baseline(session: AsyncSession, key: str, model) -> int:
    row = await session.get(EconomyState, key)
    if row is None:                                            # matches that exist at the first start are not paid
        top = (await session.execute(select(model.id).order_by(model.id.desc()).limit(1))).scalar()
        row = EconomyState(key=key, value=top or 0)
        session.add(row)
        await session.flush()
    return row.value


async def reward_matches() -> None:
    async with transaction() as s:
        base_m, base_t = await _baseline(s, "match_base", Match), await _baseline(s, "tag_base", TagMatch)
        paid = select(MatchReward.match_id).where(MatchReward.kind == "m")
        for m in (await s.execute(select(Match).where(Match.id > base_m, Match.status == MatchStatus.COMPLETED,
                                                      Match.id.not_in(paid)))).scalars().all():
            s.add(MatchReward(kind="m", match_id=m.id))
            if m.winner_id:
                await add_coins(s, m.winner_id, WIN_REWARD)
                for uid in (m.challenger_id, m.opponent_id):
                    if uid and uid != m.winner_id:
                        await add_coins(s, uid, PLAY_REWARD)
        paid_t = select(MatchReward.match_id).where(MatchReward.kind == "t")
        for t in (await s.execute(select(TagMatch).where(TagMatch.id > base_t, TagMatch.status == "completed",
                                                         TagMatch.id.not_in(paid_t)))).scalars().all():
            s.add(MatchReward(kind="t", match_id=t.id))
            for p in t.players:
                await add_coins(s, p.user_id, WIN_REWARD if p.team == t.winner_team else PLAY_REWARD)


async def deadline_loop(bot) -> None:
    while True:
        try:
            await reward_matches()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Economy loop error")
        await asyncio.sleep(POLL_SECONDS)
