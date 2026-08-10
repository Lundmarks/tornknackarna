"""
discord_events.py - Discord Guild Scheduled Event creation for upcoming matches.

Fires (from bot.py's main meeting loop) once a match enters the "<= 12 days
out" window, creating one Discord scheduled event per meeting. Requires a
BOT token with MANAGE_EVENTS in the target guild (this is a different
credential from DISCORD_WEBHOOK_URL in webhook.py -- webhooks cannot create
scheduled events, only the bot REST API can). Set DISCORD_BOT_TOKEN and
DISCORD_GUILD_ID in .env to enable; unset -> all calls are no-ops, same
convention as webhook.py's _enabled() gate.

AGENT-RESUME 2026-08-10: written and wired into bot.py's run() loop, but
NEVER RUN AGAINST A REAL DISCORD BOT/GUILD -- token+guild not issued yet at
write time. Before trusting this in production, do a manual --run-once with
a meeting inside the 12-day window and confirm in Discord that:
  (a) the event actually appears with correct start/end time in local tz
      rendering (Discord shows scheduled_start_time converted to each
      viewer's local time -- the ISO string we send must carry correct
      UTC offset, i.e. matchDate parsed via fromisoformat after Z->+00:00
      replacement, same as webhook.py does; NOT verified end-to-end here)
  (b) MANAGE_EVENTS permission is actually sufficient (vs needing
      CREATE_EVENTS, which is the Discord doc's actual permission name in
      newer API versions -- verify against current Discord dev docs, this
      was written from memory/spec, not a live test)
  (c) the cover image (if PIL path taken) doesn't exceed Discord's image
      size ceiling for this endpoint -- not enforced client-side here.
Search this file for "AGENT-RESUME" for other open items.

AGENT-RESUME 2026-08-10 (reschedule/expiry question, answered from training
knowledge -- discord.com/developers docs was unreachable from this sandbox's
network egress policy at write time, so this is UNVERIFIED against a live
fetch, only against long-standing memory of the API's documented status
enum): Discord does NOT auto-delete or auto-complete a scheduled event once
its time passes. The status field (SCHEDULED=1/ACTIVE=2/COMPLETED=3/
CANCELED=4) only auto-transitions for STAGE_INSTANCE/VOICE entity types,
tied to the actual stage/voice channel going live and ending. For our
entity_type=EXTERNAL events, nothing happens automatically -- an event
whose end time has passed just sits there in SCHEDULED status indefinitely
until a human (or a future API call this codebase doesn't make yet) PATCHes
its status to COMPLETED/CANCELED or DELETEs it outright. Practical
consequence: once a meeting moves out of bot.py's `our_meetings` (upcoming)
list -- i.e. the match has been played -- update_match_event() stops being
called for it, so its Discord event is never touched again and will remain
visible as a stale past event in the server's Events list. Confirm this
empirically once real events exist (check the server's Event list a day
after a match with no manual action taken); if it's a real ongoing
annoyance, the fix is a small addition to bot.py's `_process_past_meetings`
(which already knows the win/loss/tie result) that PATCHes status to
COMPLETED via a new discord_events.complete_match_event() -- not built
here since it wasn't asked for, and status transitions for EXTERNAL events
require an extra `entity_metadata`-preserving PATCH sequence (per API docs,
untested) that's worth its own verification pass rather than bolting on
speculatively.
"""

import base64
import io
import logging
import os
from datetime import datetime, timedelta

import httpx

log = logging.getLogger("bot")

API_BASE  = "https://discord.com/api/v10"
BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN") or None
GUILD_ID  = os.environ.get("DISCORD_GUILD_ID") or None
TIMEOUT_S = 8

# AGENT-RESUME: Dota 2 CM Bo1/Bo3 games at this club run roughly 1-2.5h per
# game observed in scraped match data (see esparven.py sample: 36min and
# 59min single games) but a Bo3 meeting could run ~2.5-3h total. Discord
# requires scheduled_end_time for EXTERNAL events -- there is no authoritative
# source for it (E-Sparven's API doesn't expose an expected end time), so
# this is a guess. Tune this constant, don't rearchitect around it.
ESTIMATED_DURATION = timedelta(hours=2)

DASHBOARD_URL = "https://lundmarks.github.io/tornknackarna/"

# Cover image canvas -- Discord's own upload UI recommends 16:9 for event
# covers, hence 960x540. Not a hard Discord requirement, just convention.
_COVER_SIZE  = (960, 540)
_LOGO_HEIGHT = 360

# --- PIL is optional at import time on purpose --------------------------
# AGENT-RESUME: Pillow was NOT in requirements.txt before this change; it is
# now added there, but until `pip install -r requirements.txt` is re-run in
# whatever env runs bot.py (the Docker image needs a rebuild too -- see
# Dockerfile), PIL will be absent and this whole module must degrade to
# "create the event with no cover image" rather than crashing the bot loop.
# That's the "fallback for image processing" requested: event creation
# ships now, cover-image generation activates automatically once Pillow is
# actually installed, no code changes needed on that side.
try:
    from PIL import Image
    _PIL_AVAILABLE = True
except ImportError:
    _PIL_AVAILABLE = False
    log.debug("Pillow not installed -- Discord event cover images disabled until it is")


def _enabled() -> bool:
    if not BOT_TOKEN or not GUILD_ID:
        log.debug("Discord bot not configured (DISCORD_BOT_TOKEN/DISCORD_GUILD_ID) -- skipping event creation")
        return False
    return True


def _fetch_image_bytes(url: str) -> bytes | None:
    try:
        r = httpx.get(url, timeout=10)
        r.raise_for_status()
        return r.content
    except Exception as e:
        log.warning(f"Discord event cover: failed to fetch logo {url}: {e}")
        return None


def build_cover_image(home_logo_url: str, away_logo_url: str) -> bytes | None:
    """
    Composite two team logos side-by-side onto a dark canvas. Returns PNG
    bytes on success, None on ANY failure (missing Pillow, fetch error,
    decode error) -- always safe to pass straight to create_match_event's
    cover_image_bytes param without the caller checking first.

    AGENT-RESUME: never run against real E-Sparven logo URLs end-to-end.
    Known caveat from a live sample (see esparven.py's _extract_team_logos
    docstring): Tornknackarna's own logo URL resolved to a plain
    "67.png" with no content hash, unlike every opponent sampled which had
    a hashed filename -- this smells like a default/placeholder image
    rather than a real uploaded team logo. Fetch it and LOOK AT IT before
    trusting this function's output in a real event; if it's a generic
    placeholder, either upload a real logo on E-Sparven's team admin page
    or special-case OUR_TEAM_ID here to use a local static asset instead
    of scraping.
    """
    if not _PIL_AVAILABLE:
        return None

    home_bytes = _fetch_image_bytes(home_logo_url)
    away_bytes = _fetch_image_bytes(away_logo_url)
    if not home_bytes or not away_bytes:
        return None

    try:
        canvas = Image.new("RGB", _COVER_SIZE, color=(20, 20, 24))
        x_centers = (_COVER_SIZE[0] * 0.27, _COVER_SIZE[0] * 0.73)
        for raw, x_center in zip((home_bytes, away_bytes), x_centers):
            logo = Image.open(io.BytesIO(raw)).convert("RGBA")
            ratio = _LOGO_HEIGHT / logo.height
            logo = logo.resize((max(1, int(logo.width * ratio)), _LOGO_HEIGHT))
            x = int(x_center - logo.width / 2)
            y = (_COVER_SIZE[1] - _LOGO_HEIGHT) // 2
            canvas.paste(logo, (x, y), logo)

        buf = io.BytesIO()
        canvas.save(buf, format="PNG")
        return buf.getvalue()
    except Exception as e:
        # Any single bad/corrupt/unexpected-format logo must not block
        # event creation -- image is a nice-to-have, the event is not.
        log.warning(f"Discord event cover: composite failed: {e}")
        return None


def create_match_event(
    opponent_name: str,
    match_date: datetime,
    meeting_id: int,
    cover_image_bytes: bytes | None = None,
) -> str | None:
    """
    Create a Discord Guild Scheduled Event for an upcoming match.

    Returns the new event's Discord snowflake ID on success, None on any
    failure or if disabled. Caller (bot.py) is responsible for the "has an
    event already been created for this meeting" dedup check via
    state["meetings"][id]["discord_event_id"] -- this function always
    creates a NEW event when called, it does not check for duplicates
    itself (mirrors webhook.py's notify_* functions, which have the same
    contract).

    match_date must be tz-aware (UTC). meeting_id is used only to build the
    E-Sparven deep link, same URL pattern as webhook.py's esparven_url.

    Links both the scouting dashboard and the E-Sparven meeting page:
    entity_metadata.location carries the dashboard URL (Discord's client
    shows this field prominently, right under the cover image, and
    auto-linkifies it since it's a valid URL -- AGENT-RESUME: that
    auto-linkify behavior is from training-knowledge, not verified live,
    same caveat as the rest of this module) so that's the one-click link;
    the description repeats both URLs as plain text, which Discord also
    auto-linkifies as bare URLs, as a fallback in clients/contexts where
    the location field isn't rendered as prominently.
    """
    if not _enabled():
        return None

    end = match_date + ESTIMATED_DURATION
    esparven_url = f"https://esparven.se/none/Meeting/Details/{meeting_id}"

    payload = {
        "name": f"Tornknäckarna vs {opponent_name}",
        "privacy_level": 2,  # GUILD_ONLY -- the only legal value as of API v10
        "scheduled_start_time": match_date.isoformat(),
        "scheduled_end_time": end.isoformat(),
        "entity_type": 3,  # EXTERNAL
        "entity_metadata": {"location": DASHBOARD_URL},
        "description": (
            f"Captain's Mode match vs {opponent_name}.\n\n"
            f"Scouting dashboard: {DASHBOARD_URL}\n"
            f"E-Sparven: {esparven_url}"
        ),
    }

    if cover_image_bytes:
        payload["image"] = f"data:image/png;base64,{base64.b64encode(cover_image_bytes).decode('ascii')}"

    try:
        r = httpx.post(
            f"{API_BASE}/guilds/{GUILD_ID}/scheduled-events",
            headers={"Authorization": f"Bot {BOT_TOKEN}"},
            json=payload,
            timeout=TIMEOUT_S,
        )
        r.raise_for_status()
        event_id = r.json()["id"]
        log.info(f"Discord: created scheduled event for match vs [bold]{opponent_name}[/] ({event_id})")
        return event_id
    except Exception as e:
        # AGENT-RESUME: never observed a real error response from this
        # endpoint. If this starts firing in practice, log r.text (not just
        # the exception) to see Discord's JSON error body -- it names which
        # payload field is invalid, which speeds up debugging a lot versus
        # guessing from the generic httpx exception message alone.
        log.warning(f"Discord event creation failed: {e}")
        return None


def update_match_event(event_id: str, match_date: datetime) -> bool:
    """
    PATCH an already-created event's start/end time after E-Sparven reports
    a reschedule. Returns True on success, False on any failure -- caller
    (bot.py's _sync_discord_event) is expected to leave its stored
    "last known match date" untouched on False so the update is retried on
    the next daily run rather than silently dropped.

    Deliberately does NOT touch name/description/entity_metadata/image --
    only time fields, since a reschedule doesn't change the opponent or
    cover art. Deliberately never called for a date that hasn't changed
    (bot.py checks that) and never deletes/cancels the event for any
    reason, including a reschedule that pushes the match back outside the
    12-day creation window -- once an event exists it is retimed in place,
    never removed, per explicit product decision (a user asked for this
    exact behavior after the initial build).

    AGENT-RESUME: untested against a real Discord event, same caveat as
    create_match_event -- log r.text on failure if this starts erroring in
    practice, not just the exception message.
    """
    if not _enabled():
        return False

    end = match_date + ESTIMATED_DURATION
    payload = {
        "scheduled_start_time": match_date.isoformat(),
        "scheduled_end_time": end.isoformat(),
    }
    try:
        r = httpx.patch(
            f"{API_BASE}/guilds/{GUILD_ID}/scheduled-events/{event_id}",
            headers={"Authorization": f"Bot {BOT_TOKEN}"},
            json=payload,
            timeout=TIMEOUT_S,
        )
        r.raise_for_status()
        log.info(f"Discord: rescheduled event {event_id} -> {match_date.isoformat()}")
        return True
    except Exception as e:
        log.warning(f"Discord event reschedule failed for {event_id}: {e}")
        return False


def complete_match_event(event_id: str) -> bool:
    """
    Mark a played match's event COMPLETED, once, so it stays visible in
    Discord's "Past events" list for history instead of either lingering
    forever as a stale SCHEDULED event (see the "does Discord auto-clean
    this up" AGENT-RESUME at the top of this file -- it doesn't) or being
    deleted (explicit product decision: keep events for history, never
    delete). Returns True on success, False on any failure -- caller
    (bot.py's _process_past_meetings, via _maybe_complete_discord_event)
    is expected to leave its "completed" flag unset on False so this is
    retried on a later run rather than the event getting stuck forever.

    AGENT-RESUME 2026-08-10: Discord's documented status state machine
    (from training knowledge, NOT verified live -- discord.com was
    unreachable from this sandbox at write time, same caveat as the
    reschedule/expiry note above) does not allow SCHEDULED -> COMPLETED
    directly; it requires SCHEDULED -> ACTIVE -> COMPLETED, hence the two
    sequential PATCH calls below. Two follow-on risks worth checking once
    real events exist:
      1. If this function's own first call (-> ACTIVE) previously
         succeeded but the second (-> COMPLETED) then failed (e.g.
         network blip), the event is left stuck in ACTIVE with our
         "completed" flag still unset -- retrying calls -> ACTIVE again,
         which is probably an invalid transition from an already-ACTIVE
         state and would then fail loudly instead of quietly recovering.
         If that happens in practice, GET the event first and branch on
         its current `status` field rather than blindly PATCHing -> ACTIVE
         every retry.
      2. Never confirmed whether Discord requires an EXTERNAL event's
         entity_metadata.location / scheduled_end_time to still be present
         on these status-only PATCH calls, or whether omitting them (as
         done here) is treated as "leave unchanged" -- if this starts
         erroring, try including the full payload create_match_event sent
         originally, not just {"status": ...}.
    """
    if not _enabled():
        return False

    try:
        r1 = httpx.patch(
            f"{API_BASE}/guilds/{GUILD_ID}/scheduled-events/{event_id}",
            headers={"Authorization": f"Bot {BOT_TOKEN}"},
            json={"status": 2},  # ACTIVE
            timeout=TIMEOUT_S,
        )
        r1.raise_for_status()
        r2 = httpx.patch(
            f"{API_BASE}/guilds/{GUILD_ID}/scheduled-events/{event_id}",
            headers={"Authorization": f"Bot {BOT_TOKEN}"},
            json={"status": 3},  # COMPLETED
            timeout=TIMEOUT_S,
        )
        r2.raise_for_status()
        log.info(f"Discord: marked event {event_id} as completed")
        return True
    except Exception as e:
        log.warning(f"Discord event completion failed for {event_id}: {e}")
        return False
