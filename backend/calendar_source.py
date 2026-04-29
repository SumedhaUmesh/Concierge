"""
macOS Calendar integration via AppleScript.

Reads the next upcoming event within 12 hours from the Calendar app.
No API key or account required — uses the local app directly.
macOS will prompt for Calendar access on first run.
"""

import asyncio
import logging
import subprocess
from typing import Optional

log = logging.getLogger(__name__)

_SCRIPT = """
tell application "Calendar"
    set now to current date
    set cutoff to now + (12 * 60 * 60)
    set skipList to {"Birthdays", "Siri Suggestions", "US Holidays", "Holidays in India"}
    set nextEvent to missing value
    set nextStart to missing value
    repeat with c in (every calendar)
        if name of c is not in skipList then
            try
                repeat with e in (every event of c whose start date > now and start date < cutoff)
                    set eStart to start date of e
                    if nextStart is missing value or eStart < nextStart then
                        set nextStart to eStart
                        set nextEvent to e
                    end if
                end repeat
            end try
        end if
    end repeat
    if nextEvent is missing value then
        return "NONE"
    end if
    set eTitle to summary of nextEvent
    set eStart to start date of nextEvent
    set eLoc to ""
    try
        if location of nextEvent is not missing value then
            set eLoc to location of nextEvent
        end if
    end try
    set h to hours of eStart
    set m to minutes of eStart
    if m < 10 then
        set ms to "0" & (m as string)
    else
        set ms to m as string
    end if
    return eTitle & "|" & h & ":" & ms & "|" & eLoc
end tell
"""


async def get_next_event() -> Optional[dict]:
    """
    Return the next upcoming Calendar event within 12 hours, or None.

    Dict keys: title (str), time (str HH:MM), location (str, may be empty)
    """
    try:
        result = await asyncio.to_thread(
            subprocess.run,
            ["osascript", "-e", _SCRIPT],
            capture_output=True,
            text=True,
            timeout=30,
        )
        output = result.stdout.strip()
        if not output or output == "NONE":
            log.info("Calendar: no upcoming events in next 12 h")
            return None

        parts = output.split("|", 2)
        if len(parts) < 2:
            log.warning("Calendar: unexpected output %r", output)
            return None

        event = {
            "title":    parts[0].strip(),
            "time":     parts[1].strip(),
            "location": parts[2].strip() if len(parts) > 2 else "",
        }
        log.info("Calendar: next event = %s @ %s (%s)",
                 event["title"], event["time"], event["location"] or "no location")
        return event

    except Exception:
        log.exception("Calendar AppleScript failed")
        return None
