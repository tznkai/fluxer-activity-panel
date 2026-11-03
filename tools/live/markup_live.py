# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Hand-run markup check: post test messages to LIVE_CHANNEL_ID and ask the operator what renders on web and mobile.

Run from the project root: .venv/bin/python tools/live/markup_live.py (needs .env; not run by CI).
Needs LIVE_VOICE_CHANNEL_ID (a voice channel in the test guild), LIVE_OTHER_GUILD_ID (a community the bot is not in)
and FLUXER_INSTANCE_URL for the channel links. Every test message is deleted at the end.
"""
from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import aiohttp

from _live import Live, LiveRefused, Redactor, Results, endpoint_secrets, live, load_env, main, out, wait_for_enter
from fluxer_client.ids import is_snowflake
from fluxer_client.rest import FluxerHTTPError, FluxerREST
from fluxer_panel import render

NO_MENTIONS: dict[str, list[str]] = {"parse": []}
EDIT_BURST = 20
EXAMPLE_LINK = "https://example.com"   # a documentation domain on the hygiene allowlist
ESCAPE_SAMPLE = "Join: mc-server (v1.2) #1 <3 a_b *x* ~y~ |z|"


def check_markup_env(env: Mapping[str, str]) -> None:
    """Refuses by naming the setting, never a value. The other community must not be the test guild."""
    for name in ("LIVE_VOICE_CHANNEL_ID", "LIVE_OTHER_GUILD_ID"):
        if not is_snowflake(env.get(name)):
            raise LiveRefused(f"{name} is missing or not a snowflake id")
    if env["LIVE_OTHER_GUILD_ID"] == env.get("LIVE_GUILD_ID"):
        raise LiveRefused("LIVE_OTHER_GUILD_ID must be a different community from LIVE_GUILD_ID")
    if not env.get("FLUXER_INSTANCE_URL"):
        raise LiveRefused("FLUXER_INSTANCE_URL is needed for the channel link check")


def markup_secrets(env: Mapping[str, str]) -> tuple[str, ...]:
    """The instance URL's forms (see endpoint_secrets). The operator's typed NOTE can echo the host, and an error
    from the instance can name it, so every printed line hides it."""
    return endpoint_secrets(env["FLUXER_INSTANCE_URL"])


def parse_verdict(line: str) -> tuple[str, str]:
    """('pass' | 'fail' | 'note', text). y passes and n fails; anything else is kept as a note."""
    answer = line.strip()
    if answer.lower() == "y":
        return "pass", ""
    if answer.lower() == "n":
        return "fail", ""
    return "note", answer


def burst_line(outcomes: Sequence[int | None], seconds: float) -> str:
    """Summary of an edit burst. None is a success; any other value is the HTTP status of a failed edit."""
    ok = sum(1 for outcome in outcomes if outcome is None)
    limited = sum(1 for outcome in outcomes if outcome == 429)
    other = len(outcomes) - ok - limited
    return (f"edits={len(outcomes)} ok={ok} rate_limited_429={limited} other_errors={other} "
            f"seconds={seconds:.2f}")


@dataclass
class Ctx:
    lv: Live
    res: Results
    instance: str
    guild: str
    channel: str
    user: str
    voice: str
    other: str
    sent: list[str] = field(default_factory=list)


async def post(ctx: Ctx, *, content: str | None = None, embeds: list[dict[str, Any]] | None = None) -> str:
    reply = await ctx.lv.rest.send_message(ctx.channel, content=content, embeds=embeds,
                                           allowed_mentions=NO_MENTIONS)
    message_id = str(reply["id"])
    ctx.lv.redactor.label(message_id, "message")
    ctx.sent.append(message_id)
    return message_id


async def ask(ctx: Ctx, name: str, look_for: str) -> None:
    redactor = ctx.lv.redactor
    out(redactor, f"CHECK {name}")
    out(redactor, "  Look for:", look_for)
    out(redactor, "  Type y if it works, n if it does not, or a short note (not y or n), then press Enter.")
    verdict, note = parse_verdict(await wait_for_enter())
    if verdict == "pass":
        ctx.res.ok(name)
    elif verdict == "fail":
        ctx.res.fail(name, "operator reported that it does not work")
    else:
        out(redactor, f"NOTE {name}: {note}")


async def check_timestamps(ctx: Ctx) -> None:
    now = int(time.time())
    relative, full = f"<t:{now + 3600}:R>", f"<t:{now + 90000}:f>"
    await post(ctx, content=f"Timestamp in content: {relative} {full}",
               embeds=[{"title": "Timestamp in an embed", "fields": [{"name": "Time", "value": f"{relative} {full}"}]}])
    await ask(ctx, "timestamp markup",
              "both times in the content and in the embed field show as local times, not as raw <t:...> text. "
              "The R one should read as relative (for example 'in 1 hour').")


async def check_channel_links(ctx: Ctx) -> None:
    """The three link forms point at the voice channel, the one the card's Voice field links to. The message is
    posted to the text channel, and a link to the channel you are already in does nothing when clicked."""
    link = render.channel_link(ctx.instance, ctx.guild, ctx.voice)
    await post(ctx, content=f"Mention <#{ctx.voice}> and bare link {link}",
               embeds=[{"description": f"Masked link [voice]({link})",
                        "fields": [{"name": "Mention", "value": f"<#{ctx.voice}>"}]}])
    await ask(ctx, "channel links",
              "which of these open the voice channel, on web and on mobile: the bare link in the content, the masked "
              "'voice' link, the <#...> mention (all three point at the voice channel). Then copy a link to the voice "
              "channel from the client and type its shape as a note, with the host written as H, the guild id as G "
              "and the channel id as C.")


async def check_masked_links(ctx: Ctx) -> None:
    await post(ctx, embeds=[{"description": f"Description link [text]({EXAMPLE_LINK})",
                             "fields": [{"name": "Field", "value": f"[text]({EXAMPLE_LINK})"}]}])
    await ask(ctx, "masked links in embeds",
              "'text' is a clickable link in the field and in the description, on web and on mobile.")


async def check_mention(ctx: Ctx) -> None:
    await post(ctx, embeds=[{"fields": [{"name": "Mention", "value": f"<@{ctx.user}>"}]}])
    await ask(ctx, "mention in an embed",
              "the embed field renders the user as a mention and no notification reached the test user. Empty "
              "allowed_mentions was sent, so the mention renders without a ping. Type y only if it renders as a "
              "mention and no notification arrived.")


async def check_edit_burst(ctx: Ctx) -> None:
    rest = ctx.lv.rest
    message_id = await post(ctx, content="edit burst target")
    # max_retries=0, so every 429 is counted rather than retried away.
    raw = FluxerREST(ctx.lv.session, rest.api_base, ctx.lv.env["FLUXER_TOKEN"], max_retries=0)
    outcomes: list[int | None] = []
    started = time.monotonic()
    for number in range(EDIT_BURST):
        try:
            await raw.edit_message(ctx.channel, message_id, embeds=[{"description": f"burst edit {number}"}],
                                   allowed_mentions=NO_MENTIONS)
        except FluxerHTTPError as exc:
            outcomes.append(exc.status)
        else:
            outcomes.append(None)
    out(ctx.lv.redactor, burst_line(outcomes, time.monotonic() - started))


async def check_escaping(ctx: Ctx) -> None:
    escaped = render.escape(ESCAPE_SAMPLE)
    await post(ctx, embeds=[{"description": escaped, "fields": [{"name": "Escaped", "value": escaped}]}])
    await ask(ctx, "escaping",
              "the sample text shows with no visible backslashes, on web and on mobile. If any backslash shows, "
              "type the characters that show one, as a note.")


async def report_voice_and_guild(ctx: Ctx) -> None:
    redactor, rest = ctx.lv.redactor, ctx.lv.rest
    try:
        channel = await rest.get_channel(ctx.voice)
    except FluxerHTTPError as exc:
        out(redactor, f"voice channel: HTTP {exc.status}")
    else:
        out(redactor, "voice channel type:", channel.get("type"))
    try:
        await rest.get_guild(ctx.other)
    except FluxerHTTPError as exc:
        out(redactor, f"GET /guilds for a community the bot is not in: HTTP {exc.status}")
    else:
        out(redactor, "GET /guilds for a community the bot is not in: HTTP 200 (the bot can read it)")


async def report_text_channel(ctx: Ctx) -> None:
    """The type of the posting channel. It confirms TEXT_CHANNEL_TYPE on a live instance: a text channel should be 0."""
    redactor, rest = ctx.lv.redactor, ctx.lv.rest
    try:
        channel = await rest.get_channel(ctx.channel)
    except FluxerHTTPError as exc:
        out(redactor, f"text channel: HTTP {exc.status}")
    else:
        out(redactor, "text channel type:", channel.get("type"))


async def delete_sent(rest: FluxerREST, channel: str, message_ids: Sequence[str], redactor: Redactor) -> None:
    """Deletes each test message. A failed delete is reported by its label alone, and the rest are still tried."""
    for message_id in message_ids:
        try:
            await rest.delete_message(channel, message_id)
        except (FluxerHTTPError, aiohttp.ClientError, TimeoutError):
            out(redactor, f"delete failed: {redactor.labels.get(message_id, '<message>')}")


async def run() -> Results:
    env = load_env()
    check_markup_env(env)
    async with live(gateway=False, extra_secrets=markup_secrets(env)) as lv:
        ctx = Ctx(lv=lv, res=Results(lv.redactor), instance=env["FLUXER_INSTANCE_URL"],
                  guild=env["LIVE_GUILD_ID"], channel=env["LIVE_CHANNEL_ID"], user=env["LIVE_USER_ID"],
                  voice=env["LIVE_VOICE_CHANNEL_ID"], other=env["LIVE_OTHER_GUILD_ID"])
        try:
            await check_timestamps(ctx)
            await check_channel_links(ctx)
            await check_masked_links(ctx)
            await check_mention(ctx)
            await check_edit_burst(ctx)
            await check_escaping(ctx)
            await report_voice_and_guild(ctx)
            await report_text_channel(ctx)
        finally:
            await delete_sent(lv.rest, ctx.channel, ctx.sent, lv.redactor)
        return ctx.res


if __name__ == "__main__":
    main(run)
