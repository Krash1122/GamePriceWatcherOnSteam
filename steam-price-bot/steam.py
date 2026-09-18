"""
Everything that talks to Steam.

Two jobs: turning whatever the user typed into an appid, and asking Steam's
storefront endpoint what a game currently costs. Nothing in here knows about
Discord or the database, which makes it the easy part of the project to test
on its own.
"""

import asyncio
import re

import aiohttp

APPDETAILS_URL = "https://store.steampowered.com/api/appdetails"

# Steam store URLs always put the appid directly after /app/, e.g.
# https://store.steampowered.com/app/570/Dota_2/ -> 570. Anything after that
# (the slug, query string, trailing slash) is decoration we can ignore.
APP_URL_RE = re.compile(r"/app/(\d+)")


def parse_appid(text):
    """
    Pull a Steam appid out of a store URL or a bare number.

    Takes the raw string the user typed into /watch. Returns the appid as an
    int, or None if there's no appid in there at all.
    """
    text = text.strip()

    # Bare appid is the easy case: the whole string is digits.
    if text.isdigit():
        return int(text)

    # Otherwise look for the /app/<id> segment anywhere in the string. This
    # also catches people pasting a steamcommunity.com link, which uses the
    # same path shape.
    match = APP_URL_RE.search(text)
    if match:
        return int(match.group(1))

    return None


async def fetch_price(session, appid):
    """
    Ask Steam what the game currently costs in Canadian dollars.

    Takes an open aiohttp session and an appid. Returns a dict with keys
    'final', 'initial' (both integer cents) and 'discount_percent', or None if
    the game has no price we can compare against — which covers a bad appid, a
    free-to-play game, DLC and bundles that the endpoint refuses, region-locked
    titles, and the endpoint being down or rate-limiting us.

    None means "skip this one this cycle", never "the price is zero".
    """
    params = {"appids": str(appid), "cc": "ca", "filters": "price_overview"}

    # This endpoint is undocumented, so it can fail in ways a documented API
    # wouldn't: HTML error pages, empty bodies, sudden 429s. Anything that goes
    # wrong at the network or parsing layer just means no price this cycle.
    try:
        async with session.get(APPDETAILS_URL, params=params) as response:
            if response.status != 200:
                return None
            # content_type=None because Steam sometimes labels the body
            # text/html even when it's JSON, and aiohttp refuses to parse it
            # otherwise.
            payload = await response.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        return None

    return _extract_price(payload, appid)


def _extract_price(payload, appid):
    """
    Dig the price out of an appdetails response body.

    Takes the parsed JSON and the appid we asked about. Returns the same dict
    fetch_price returns, or None. Split out from fetch_price so it can be
    tested without doing any networking.
    """
    if not isinstance(payload, dict):
        return None

    # The response is keyed by appid as a *string*, and carries a success flag
    # that is false for bundles, DLC and appids that don't exist.
    entry = payload.get(str(appid))
    if not isinstance(entry, dict) or not entry.get("success"):
        return None

    # For free games Steam returns "data": [] — an empty list, not a dict. That
    # is the single most common way naive code for this endpoint crashes, so
    # check the type rather than the truthiness.
    data = entry.get("data")
    if not isinstance(data, dict):
        return None

    price = data.get("price_overview")
    if not isinstance(price, dict) or "final" not in price:
        return None

    final = price["final"]
    return {
        "final": final,
        # Some responses omit 'initial'; falling back to final just means we
        # read it as "not discounted", which is the safe assumption.
        "initial": price.get("initial", final),
        "discount_percent": price.get("discount_percent", 0),
    }


async def fetch_name(session, appid):
    """
    Look up a game's display name.

    Takes an open aiohttp session and an appid. Returns the name as a string,
    or None if Steam doesn't recognise the appid. Uses filters=basic because
    the price_overview filter strips the name out of the response.

    Only called once, when a watch is created, so the name can be stored
    alongside the row instead of being re-fetched every polling cycle.
    """
    params = {"appids": str(appid), "cc": "ca", "filters": "basic"}

    try:
        async with session.get(APPDETAILS_URL, params=params) as response:
            if response.status != 200:
                return None
            payload = await response.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        return None

    if not isinstance(payload, dict):
        return None

    entry = payload.get(str(appid))
    if not isinstance(entry, dict) or not entry.get("success"):
        return None

    data = entry.get("data")
    if not isinstance(data, dict):
        return None

    return data.get("name")


def format_price(cents):
    """
    Turn integer cents into something readable in a Discord message.

    Takes a price in cents as Steam reports it. Returns a string like '$29.99'.
    """
    return "${:.2f}".format(cents / 100)
