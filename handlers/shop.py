"""/daily, /balance, /shop (VIP card skins), /skin and the owner's /givecoins."""
from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton as B, InlineKeyboardMarkup as M, Message

from config import get_settings
from database import transaction
from services import economy
from services import mycard
from services.cards import VIP_STYLES

router = Router(name="shop")


def _shop_kb(owned: set[str]) -> M:
    rows = [[B(text=f"{'✅' if k in owned else '🪙'} {economy.skin_name(k)} — {economy.skin_price(k)}", callback_data=f"sh:v:{k}")]
            for k in VIP_STYLES]
    return M(inline_keyboard=rows)


@router.message(Command("daily"))
async def cmd_daily(message: Message) -> None:
    if not message.from_user:
        return
    async with transaction() as s:
        res = await economy.claim_daily(s, message.from_user.id)
    if res is None:
        await message.reply("🎁 You already took today\'s reward. Come back tomorrow!")
        return
    reward, streak, balance = res
    await message.reply(f"🎁 <b>Daily reward: +{reward} coins</b>\n🔥 Streak: {streak} day{'s' if streak != 1 else ''} "
                        f"(a longer streak pays more)\n💰 Balance: <b>{balance}</b>")


@router.message(Command("balance", "coins"))
async def cmd_balance(message: Message) -> None:
    if not message.from_user:
        return
    async with transaction() as s:
        w = await economy.wallet(s, message.from_user.id)
        coins = w.coins
    await message.reply(f"💰 You have <b>{coins}</b> coins.\nEarn them with /daily, by collecting wrestlers and winning matches. Spend them in /shop.")


async def _shop_text(user_id: int) -> tuple[str, M]:
    async with transaction() as s:
        coins = (await economy.wallet(s, user_id)).coins
        owned = await economy.owned_skins(s, user_id)
    return (f"🛒 <b>VIP Card Shop</b>\n💰 Your coins: <b>{coins}</b>\n\nPick a skin to preview it on <b>your own</b> card:",
            _shop_kb(owned))


@router.message(Command("shop"))
async def cmd_shop(message: Message) -> None:
    if message.from_user:
        text, kb = await _shop_text(message.from_user.id)
        await message.answer(text, reply_markup=kb)


@router.callback_query(F.data.startswith("sh:"))
async def cb_shop(cb: CallbackQuery, bot: Bot) -> None:
    parts = (cb.data or "").split(":")
    uid = cb.from_user.id
    if parts[1] == "l":
        await cb.answer()
        text, kb = await _shop_text(uid)
        await cb.message.answer(text, reply_markup=kb)
    elif parts[1] == "v" and len(parts) == 3 and parts[2] in VIP_STYLES:
        await cb.answer("🎨 Drawing your preview…")
        key = parts[2]
        async with transaction() as s:
            photo = await mycard.render(s, bot, uid, cb.from_user.full_name, cb.from_user.username, skin=key)
            owned = key in await economy.owned_skins(s, uid)
        kb = M(inline_keyboard=[[B(text="✅ Equip" if owned else f"🛒 Buy for {economy.skin_price(key)} coins",
                                   callback_data=f"sh:{'e' if owned else 'b'}:{key}")]])
        await bot.send_photo(cb.message.chat.id, BufferedInputFile(photo, "skin.jpg"),
                             caption=f"✨ <b>{economy.skin_name(key)}</b> — {economy.skin_price(key)} coins", reply_markup=kb)
    elif parts[1] == "b" and len(parts) == 3:
        async with transaction() as s:
            result = await economy.buy_skin(s, uid, parts[2])
            coins = (await economy.wallet(s, uid)).coins
        if result == "ok":
            await cb.answer("🎉 Purchased and equipped!", show_alert=True)
            await cb.message.edit_reply_markup(reply_markup=None)
            await cb.message.answer(f"🎉 <b>{economy.skin_name(parts[2])}</b> is yours! Send /mycard to see it.\n💰 Left: {coins}")
        elif result == "owned":
            await cb.answer("You already own this skin. Use /skin to equip it.", show_alert=True)
        elif result == "poor":
            await cb.answer(f"Not enough coins. You have {coins}, it costs {economy.skin_price(parts[2])}.", show_alert=True)
        else:
            await cb.answer()
    elif parts[1] == "e" and len(parts) == 3:
        async with transaction() as s:
            ok = await economy.equip(s, uid, parts[2])
        await cb.answer("✅ Equipped! Send /mycard." if ok else "You don\'t own this skin.", show_alert=True)
    else:
        await cb.answer()


@router.message(Command("skin"))
async def cmd_skin(message: Message) -> None:
    if not message.from_user:
        return
    async with transaction() as s:
        owned = await economy.owned_skins(s, message.from_user.id)
        current = await economy.equipped_skin(s, message.from_user.id)
    rows = [[B(text=f"{'🔘' if k == current else '⚪'} {economy.skin_name(k)}", callback_data=f"sk:{k}")] for k in VIP_STYLES if k in owned]
    rows.append([B(text=f"{'🔘' if current is None else '⚪'} Default (titles theme)", callback_data="sk:none")])
    rows.append([B(text="🛒 Open shop", callback_data="sh:l")])
    await message.answer("🎨 <b>Your card skin</b>", reply_markup=M(inline_keyboard=rows))


@router.callback_query(F.data.startswith("sk:"))
async def cb_skin(cb: CallbackQuery) -> None:
    key = (cb.data or "").split(":")[1]
    async with transaction() as s:
        ok = await economy.equip(s, cb.from_user.id, None if key == "none" else key)
    await cb.answer("✅ Skin equipped — send /mycard" if ok else "You don\'t own this skin.", show_alert=True)


@router.message(Command("givecoins"))
async def cmd_givecoins(message: Message, command: CommandObject) -> None:
    if not message.from_user or message.from_user.id != get_settings().owner_id:
        return
    args = (command.args or "").split()
    if not args or not args[0].lstrip("-").isdigit():
        await message.answer("Use <code>/givecoins 500</code> as a reply, or <code>/givecoins 500 user_id</code>.")
        return
    amount = int(args[0])
    target = int(args[1]) if len(args) > 1 and args[1].isdigit() else (
        message.reply_to_message.from_user.id if message.reply_to_message and message.reply_to_message.from_user else None)
    if target is None:
        await message.answer("Reply to the player or give his user id.")
        return
    async with transaction() as s:
        balance = await economy.add_coins(s, target, amount)
    await message.answer(f"✅ {amount:+d} coins for <code>{target}</code>. Balance: {balance}")
