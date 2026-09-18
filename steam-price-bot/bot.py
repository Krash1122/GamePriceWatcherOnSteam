"""
A Discord bot that watches Steam prices and DMs you when one drops.

Run it with `python bot.py`. Everything it needs comes from environment
variables (or a .env file sitting next to this one): DISCORD_TOKEN, optionally
GUILD_ID, POLL_MINUTES and DB_PATH.
"""

import asyncio
import logging
import os

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

import db
import steam

# Read .env before anything looks at os.environ, so the config below sees it.
load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = os.getenv("GUILD_ID")
POLL_MINUTES = int(os.getenv("POLL_MINUTES", "30"))
DB_PATH = os.getenv("DB_PATH", "watches.db")

# Steam's endpoint is undocumented and unmetered, which means the rate limit is
# whatever Valve feels like today. One request per watched game per cycle with a
# pause between them keeps us well under anything they're likely to enforce.
REQUEST_DELAY_SECONDS = 1.5

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("pricebot")


class PriceBot(commands.Bot):
    """
    The bot itself.

    Subclasses commands.Bot only to get a setup_hook, which is the one place
    discord.py guarantees is run inside the event loop before the connection
    goes live. The database connection, the HTTP session and the polling loop
    all get started there.
    """

    def __init__(self):
        """
        Build the bot with the narrowest intents that work.

        Takes nothing. Slash commands and DMs don't need message content or
        member intents, so asking for them would just be a privileged-intent
        prompt in the developer portal for no reason.
        """
        super().__init__(command_prefix="!", intents=discord.Intents.default())
        self.db = None
        self.http_session = None

    async def setup_hook(self):
        """
        Open resources and register commands, before the gateway connects.

        Takes nothing, returns nothing. Called automatically by discord.py.
        """
        self.db = db.connect(DB_PATH)

        # A single session reused for every request: aiohttp pools connections
        # per session, so making a new one per request would throw away the
        # TCP and TLS handshake every time. The timeout is here rather than on
        # each call so a hung request can't stall the polling loop forever.
        self.http_session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=15),
            headers={"User-Agent": "steam-price-watcher/1.0"},
        )

        # Global commands can take up to an hour to appear. Copying them to a
        # single guild makes them show up immediately, which matters a lot while
        # you're still renaming things.
        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            log.info("Commands synced to guild %s", GUILD_ID)
        else:
            await self.tree.sync()
            log.info("Commands synced globally (may take up to an hour to appear)")

        poll_prices.start()

    async def close(self):
        """
        Shut down cleanly on Ctrl+C.

        Takes nothing, returns nothing. Closes the HTTP session and the database
        before handing off to discord.py's own shutdown, so aiohttp doesn't print
        an "unclosed session" warning on the way out.
        """
        if self.http_session:
            await self.http_session.close()
        if self.db:
            self.db.close()
        await super().close()


bot = PriceBot()


@bot.event
async def on_ready():
    """
    Log that the connection is up.

    Takes nothing, returns nothing. Fires after the gateway handshake, and can
    fire more than once if the connection drops and resumes — so it's for
    logging, not for setup. Setup lives in setup_hook.
    """
    log.info("Logged in as %s, polling every %s minutes", bot.user, POLL_MINUTES)


async def send_dm(user_id, text):
    """
    DM a user, without letting a failure take down the polling loop.

    Takes a Discord user id and the message body. Returns True if the message
    was delivered, False otherwise.

    Delivery genuinely fails in normal use: the user has DMs from server members
    turned off, or they left the server the bot shares with them. Returning False
    instead of raising lets the caller leave the notification flag alone so the
    alert is retried next cycle rather than being silently lost.
    """
    # get_user only works if the user is already in the bot's cache; fetch_user
    # hits the API. Trying the cache first avoids a request on the common path.
    user = bot.get_user(user_id)
    if user is None:
        try:
            user = await bot.fetch_user(user_id)
        except discord.HTTPException:
            log.warning("Could not look up user %s", user_id)
            return False

    try:
        await user.send(text)
        return True
    except discord.Forbidden:
        log.warning("User %s has DMs closed", user_id)
        return False
    except discord.HTTPException as error:
        log.warning("Failed to DM user %s: %s", user_id, error)
        return False


@tasks.loop(minutes=POLL_MINUTES)
async def poll_prices():
    """
    One polling cycle: fetch every watched game's price and send what's due.

    Takes nothing, returns nothing. Scheduled by discord.ext.tasks, which
    re-runs it on an interval and, importantly, keeps running it if one cycle
    raises — without that, a single bad response would silently end the loop.
    """
    watches = db.all_watches(bot.db)
    if not watches:
        return

    # Several people can watch the same game, so collect the distinct appids
    # first and fetch each one once. Sorted so the log output is stable.
    appids = sorted({watch["appid"] for watch in watches})
    prices = {}
    for appid in appids:
        price = await steam.fetch_price(bot.http_session, appid)
        if price is not None:
            prices[appid] = price
        await asyncio.sleep(REQUEST_DELAY_SECONDS)

    log.info("Polled %s games for %s watches", len(prices), len(watches))

    for watch in watches:
        price = prices.get(watch["appid"])
        # No price this cycle means the request failed or the game stopped
        # being purchasable. Skipping leaves the flags untouched, so nothing is
        # wrongly reset and the watch just picks up again next cycle.
        if price is None:
            continue
        await check_watch(watch, price)


async def check_watch(watch, price):
    """
    Decide whether one watch owes its owner a DM, and send it.

    Takes a watch row and the price dict steam.fetch_price returned. Returns
    nothing; updates the row's notification flags as a side effect.
    """
    on_sale = price["discount_percent"] > 0
    at_target = price["final"] <= watch["target_cents"]

    current = steam.format_price(price["final"])
    target = steam.format_price(watch["target_cents"])

    messages = []

    # Two independent alerts. A game can go on sale without reaching your
    # target, and it can reach your target without being on sale (Steam
    # sometimes just lowers a base price), so neither condition implies the
    # other and each gets its own flag.
    if on_sale and not watch["notified_sale"]:
        was = steam.format_price(price["initial"])
        messages.append(
            "**{}** is on sale: {}% off, {} (was {}). Your target is {}.".format(
                watch["name"], price["discount_percent"], current, was, target
            )
        )

    if at_target and not watch["notified_target"]:
        messages.append(
            "**{}** hit your target: {} (target {}).\n"
            "https://store.steampowered.com/app/{}/".format(
                watch["name"], current, target, watch["appid"]
            )
        )

    for message in messages:
        delivered = await send_dm(watch["user_id"], message)
        # If the DM didn't land, leave the flags as they were so the alert is
        # retried next cycle instead of being marked as sent.
        if not delivered:
            return

    # The new flag value is just "is the condition true right now". When a sale
    # ends, on_sale goes False, the flag clears, and the next sale gets a fresh
    # notification. That's the whole once-per-price-drop mechanism.
    db.set_flags(bot.db, watch["id"], on_sale, at_target)


@poll_prices.before_loop
async def before_poll():
    """
    Hold the polling loop until the bot is connected.

    Takes nothing, returns nothing. Without this the first cycle can run before
    the gateway handshake finishes, and fetch_user would fail on a bot that
    isn't logged in yet.
    """
    await bot.wait_until_ready()


@bot.tree.command(name="watch", description="Watch a Steam game and get a DM when it drops to your price")
@app_commands.describe(
    game="Steam store URL or bare appid",
    target="Price in CAD you want to be notified at",
)
async def watch_command(interaction, game: str, target: float):
    """
    /watch — start watching a game.

    Takes the interaction, the game as a store URL or appid, and the target
    price in dollars. Returns nothing; replies to the user either way.
    """
    appid = steam.parse_appid(game)
    if appid is None:
        await interaction.response.send_message(
            "I couldn't find an appid in that. Paste the store page URL, or just the number from it.",
            ephemeral=True,
        )
        return

    if target <= 0:
        await interaction.response.send_message(
            "Target price has to be more than zero.", ephemeral=True
        )
        return

    # Discord kills an interaction that isn't answered within three seconds, and
    # the two Steam requests below can easily take longer than that. Deferring
    # buys 15 minutes and shows a "thinking" state in the meantime.
    await interaction.response.defer(ephemeral=True)

    name = await steam.fetch_name(bot.http_session, appid)
    if name is None:
        await interaction.followup.send(
            "Steam doesn't recognise appid {}. Double-check the link?".format(appid)
        )
        return

    # Check there's actually a price before saving the watch, so free games, DLC
    # and region-locked titles fail loudly here instead of sitting in the
    # database being silently skipped by every polling cycle.
    price = await steam.fetch_price(bot.http_session, appid)
    if price is None:
        await interaction.followup.send(
            "**{}** has no price I can track. It's probably free-to-play, DLC, "
            "a bundle, or not sold in Canada.".format(name)
        )
        return

    # Work in cents from here on. round() rather than int() because float maths
    # turns 19.99 into 19.989999..., and int() would truncate that to 1998.
    target_cents = round(target * 100)
    is_new = db.add_watch(bot.db, interaction.user.id, appid, name, target_cents)

    verb = "Watching" if is_new else "Updated"
    await interaction.followup.send(
        "{} **{}** — currently {}, I'll DM you at {} or below.".format(
            verb, name, steam.format_price(price["final"]), steam.format_price(target_cents)
        )
    )


@bot.tree.command(name="unwatch", description="Stop watching a game")
@app_commands.describe(appid="The appid shown in /list")
async def unwatch_command(interaction, appid: int):
    """
    /unwatch — delete one of your watches.

    Takes the interaction and the appid. Returns nothing; replies to the user.
    """
    removed = db.remove_watch(bot.db, interaction.user.id, appid)
    if removed:
        await interaction.response.send_message(
            "Stopped watching appid {}.".format(appid), ephemeral=True
        )
    else:
        await interaction.response.send_message(
            "You weren't watching appid {}.".format(appid), ephemeral=True
        )


@bot.tree.command(name="list", description="Show everything you're watching")
async def list_command(interaction):
    """
    /list — show the caller's watches with current and target prices.

    Takes the interaction. Returns nothing; replies to the user.
    """
    watches = db.list_watches(bot.db, interaction.user.id)
    if not watches:
        await interaction.response.send_message(
            "You're not watching anything yet. Try /watch.", ephemeral=True
        )
        return

    # One live request per watched game, so this can take a while with a long
    # list — defer for the same reason /watch does.
    await interaction.response.defer(ephemeral=True)

    lines = []
    for watch in watches:
        price = await steam.fetch_price(bot.http_session, watch["appid"])
        if price is None:
            current = "price unavailable"
        else:
            current = steam.format_price(price["final"])
            if price["discount_percent"] > 0:
                current += " ({}% off)".format(price["discount_percent"])

        lines.append(
            "**{}** (`{}`) — now {}, target {}".format(
                watch["name"], watch["appid"], current, steam.format_price(watch["target_cents"])
            )
        )
        await asyncio.sleep(REQUEST_DELAY_SECONDS)

    await interaction.followup.send("\n".join(lines))


if __name__ == "__main__":
    # Failing here with a clear message beats discord.py raising LoginFailure
    # fifty lines into a traceback because the token was an empty string.
    if not TOKEN:
        raise SystemExit(
            "DISCORD_TOKEN is not set. Put it in a .env file next to bot.py, "
            "or export it in your shell."
        )
    bot.run(TOKEN)
