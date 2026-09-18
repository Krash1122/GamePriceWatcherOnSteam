# GamePriceWatcherOnSteam


What it is

A Discord bot that watches Steam games for me and DMs me when one drops in price. I add a game with /watch, give it a price I'd be happy to pay, then forget about it. When the game goes on sale, or falls to the number I picked, the bot messages me. Everything's in Canadian dollars.

Why I made it

I like getting games at a good price and I was going about it badly. I'd check the store every so often, hear about a sale from a friend after it ended, or buy something full price and watch it go 60% off two weeks later. Steam's wishlist emails you, but they're noisy and I never read them.

What I actually wanted was a message where I already look. I'm in Discord all day, so a DM is something I'll see in minutes. Once I started building it I realised it wasn't only useful to me. Anyone in the server could use the same bot with their own list and their own prices, so I built it that way instead of hardcoding myself as the only user.

How it works

There are three slash commands. /watch takes a Steam store URL or a bare appid plus a target price, and saves it. /unwatch takes an appid and removes it. /list shows everything I'm watching with the current price next to my target.

In the background a loop wakes up every 30 minutes, asks Steam what each watched game costs, and compares it to what I asked for. If something crossed a line I get a DM. If not, it goes back to sleep.

Prices come from Steam's storefront endpoint, store.steampowered.com/api/appdetails. It's undocumented so I don't trust its shape. The response is keyed by appid as a string and has a success flag that comes back false for DLC and bundles. Free-to-play games return data as an empty list instead of an object, which will crash anything written for the happy path. So my parsing checks types rather than just truthiness, and anything unexpected means "skip this game this cycle" instead of "the price is zero". I also only make one request per game per cycle with a pause between them, since hammering an endpoint Valve never promised me is a good way to get rate limited.

The part I thought about most was not annoying myself. My first instinct was a single "already told him" boolean per game, but that breaks. A game can go on sale without hitting my target, and it can hit my target without being on sale. So each watch carries two flags, one per alert type. Each one gets set when its alert fires and cleared when its condition stops being true. A sale that runs a week gives me one DM, and when it ends the flags reset so the next sale counts as new.

How I built it

Connecting the bot. I made an application in Discord's developer portal, which gives you a bot user and a token. That token is basically the bot's password, so it lives in a .env file that's in .gitignore and never in the code. That's the whole reason I can push this to GitHub safely. Then I invited the bot to a server with an OAuth2 URL asking for two scopes: bot, and applications.commands so it's allowed to register slash commands. I asked for no server permissions at all, since everything it sends goes to DMs. It does still have to share a server with you, because Discord won't let a bot message someone it has no connection to.

Getting the commands to show up. Slash commands are registered with Discord rather than parsed out of messages. Registering globally works but can take up to an hour to propagate, which is painful while you're still renaming things. So the bot reads a GUILD_ID from the environment and syncs to that one server instead. Those appear instantly.

How a command actually runs. Discord validates the arguments before my code ever sees them. target is declared as a float, so someone typing "twenty bucks" gets rejected on Discord's end. Then Discord starts a three second timer, and if the bot hasn't answered it shows an error. /watch makes two requests to Steam, which can easily take longer than that, so it defers first. That answers the "are you alive" ping right away and buys fifteen minutes for the real reply.

Storage. One SQLite table, one row per person and game. A dict would've worked until the process stopped, and the process always stops eventually. Every watch would vanish on restart, and worse, the notification flags would reset and re-notify me about a sale I'd already been told about. Prices are stored as integer cents rather than dollars, because comparing floats for money is a bug waiting to happen.

What actually broke. The bot wouldn't connect at first and threw an SSL certificate verification error. It wasn't my code or my token. The Python I'd installed from python.org doesn't hook into macOS's certificate store, and you have to run a script it ships with to point it at its own bundle. Ran that, and it connected first try.
