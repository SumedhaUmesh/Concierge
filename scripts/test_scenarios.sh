#!/usr/bin/env bash
# Interactive scenario tester — press Enter to advance, Ctrl+C to quit.

BASE="http://localhost:8000"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; DIM='\033[2m'; NC='\033[0m'

_reset() {
  curl -s -X POST "$BASE/sim/reset" > /dev/null
  curl -s -X POST "$BASE/agent/reset" > /dev/null
}

_state() {
  curl -s -X POST "$BASE/sim/state" \
    -H 'Content-Type: application/json' \
    -d "$1" > /dev/null
}

_title() {
  echo ""
  echo -e "${BOLD}${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
  echo -e "${BOLD}${CYAN}  $1${NC}"
  echo -e "${BOLD}${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
}

_expect() {
  echo -e "  ${GREEN}✓ Expect:${NC} $1"
}

_action() {
  echo -e "  ${YELLOW}▶ Do:${NC} $1"
}

_note() {
  echo -e "  ${DIM}  $1${NC}"
}

_bug() {
  echo -e "  ${RED}🐛 Bug fixed:${NC} $1"
}

_next() {
  echo ""
  echo -e "${DIM}  Press Enter to continue, Ctrl+C to quit...${NC}"
  read -r
}

# ── compute meeting times from real clock ────────────────────────────────────
_add_minutes() {
  local h=$(date +%H) m=$(date +%M)
  local total=$(( 10#$h * 60 + 10#$m + $1 ))
  printf "%02d:%02d" $(( total / 60 % 24 )) $(( total % 60 ))
}

MTG_FAR=$(_add_minutes 15)    # 15 min from now — shows in sidebar

# ════════════════════════════════════════════════════════════════════════════
echo ""
echo -e "${BOLD}  Concierge — Scenario Test Suite${NC}"
echo -e "${DIM}  Make sure the server is running: bash scripts/run.sh${NC}"
echo ""
echo -e "${DIM}  Opening dashboard...${NC}"
open "http://localhost:8000" 2>/dev/null || true
_next

# ── 1. Fuel / Range ─────────────────────────────────────────────────────────
_title "1 / 9  —  FUEL WARNING"
_reset
_state '{"fuel_percent": 12, "range_km": 45, "speed_kmh": 110}'
_expect "Fuel gauge turns red  •  Urgency-4 card fires in ~5s"
_expect "Card headline: fuel stop with real station name + distance"
_expect "Map: fuel station marker pinned, Navigate button on card"
_expect "TTS reads the headline aloud"
_action "Click 'Navigate to …' to open Google Maps, or dismiss"
_next

# ── 2. Rain + open windows ───────────────────────────────────────────────────
_title "2 / 9  — RAIN + OPEN WINDOWS"
_reset
_state '{"rain_in_minutes": 8, "windows_open": true, "sunroof_open": true}'
_expect "Rain row appears in sidebar  •  Cabin card fires in ~5s"
_expect "Card headline: 'Rain in ~8 min — close windows and sunroof'"
_expect "Action button: 'Close windows & start AC'"
_action "Accept → windows/sunroof close, AC turns on (check sidebar)"
_next

# ── 3. Meal suggestion — preference first ────────────────────────────────────
_title "3 / 9  — MEAL SUGGESTION (preference first)"
_reset
_state '{"hours_since_meal": 6.5, "current_time": "13:00", "speed_kmh": 90}'
echo ""
_expect "Within ~5s: TTS asks 'What are you in the mood for?' + cuisine list"
_expect "Dashboard shows meal options (not a single restaurant card)"
_expect "System waits for your preference before picking a restaurant"
echo ""
echo -e "  ${YELLOW}Step 1:${NC} Wait for the preference question to appear"
echo -e "  ${YELLOW}Step 2:${NC} Click a cuisine tile or say a preference (e.g. 'Mexican')"
echo -e "  ${GREEN}Expect:${NC}  Specific restaurant card with Navigate button"
echo ""
_note "Improvement: system asks first → feels like an assistant, not a vending machine"
_next

# ── 4. Meeting + traffic — fires only when at risk of being late ─────────────
_title "4 / 9  — MEETING + TRAFFIC (lateness-based trigger)"
_reset
# Fixed clock state: meeting at 14:25, current time 14:00
# travel=20 min + traffic=12 min = needs 32 min, has 25 → 7 min late → fires
_state '{"current_time": "14:00", "next_meeting_title": "Dinner Reservation", "next_meeting_time": "14:25", "normal_travel_minutes": 20, "traffic_delay_minutes": 12, "speed_kmh": 80}'
echo ""
_expect "Schedule card fires: 'may be late for Dinner Reservation'"
_expect "Detail: 25 min left, needs 32 min (20 travel + 12 traffic)"
_expect "Action button: 'Start navigation'"
echo ""
_note "Current time: 14:00  •  Meeting: 14:25  •  Travel: 20 min  •  Traffic: +12 min"
_note "Trigger fires because 25 min left < 32 min needed"
echo ""
echo -e "  ${YELLOW}Contrast — this should NOT trigger (plenty of time):${NC}"
echo -e "  ${DIM}curl -X POST $BASE/sim/state -H 'Content-Type: application/json'${NC}"
echo -e "  ${DIM}     -d '{\"current_time\": \"13:00\", \"next_meeting_time\": \"15:00\",${NC}"
echo -e "  ${DIM}          \"normal_travel_minutes\": 20, \"traffic_delay_minutes\": 5}'${NC}"
_note "Gate should stay silent — 60 min left, only needs 25 min"
_next

# ── 5. Fatigue + rest stop ───────────────────────────────────────────────────
_title "5 / 9  — FATIGUE + REST STOP"
_reset
_state '{"minutes_driving_continuously": 130, "current_time": "02:00", "speed_kmh": 100, "next_rest_stop_lat": 34.052, "next_rest_stop_lng": -118.452, "next_rest_stop_km": 8.5}'
_expect "Fatigue bar HIGH (red)  •  Risk level: HIGH"
_expect "Rest stop suggestion fires  •  Rest stop marker pinned on map"
_expect "Card headline: 'Rest stop 8 km ahead — take a break'"
_expect "Action button: Navigate to rest stop"
_action "Accept to navigate  •  Map should pan to rest stop pin"
_next

# ── 6. Compound query — coffee + meeting time check ──────────────────────────
_title "6 / 9  — COMPOUND QUERY  (coffee shop + meeting awareness)"
_reset
# Meeting at 15:00, current time 14:00, travel=30 min → 30 min margin after detour
_state '{"current_time": "14:00", "next_meeting_title": "Client call", "next_meeting_time": "15:00", "normal_travel_minutes": 30, "traffic_delay_minutes": 0, "speed_kmh": 70, "lat": 34.052, "lng": -118.243}'
echo ""
echo -e "  Tap ${BOLD}🎙${NC} and say:"
echo ""
echo -e "  ${YELLOW}\"Find me a good coffee shop on my route and tell me if I have time before my next meeting\"${NC}"
echo ""
_expect "Coffee shop found and filtered to route"
_expect "Detour time calculated via OSRM"
_expect "Spoken verdict — one of:"
echo -e "  ${GREEN}  → Has time:${NC} 'Blue Bottle is 4 min off your route. Meeting in 60 min. You have enough time for a quick stop.'"
echo -e "  ${GREEN}  → Tight:   ${NC} 'It\\'d be tight — your meeting is in 60 minutes. I\\'d skip it today.'"
_expect "Suggestion card with Navigate button"
echo ""
_note "Multi-intent: meal + schedule triggers compound handler (no LLM call)"
echo ""
echo -e "  ${YELLOW}Try the tight version:${NC}"
echo -e "  ${DIM}curl -X POST $BASE/sim/state -H 'Content-Type: application/json'${NC}"
echo -e "  ${DIM}     -d '{\"next_meeting_time\": \"14:20\", \"current_time\": \"14:00\", \"normal_travel_minutes\": 18}'${NC}"
_note "Only 20 min left + 18 min drive → no time for coffee"
_next

# ── 7. Voice — cabin control (not dismiss) ───────────────────────────────────
_title "7 / 9  — VOICE + CABIN CONTROL"
_reset
_state '{"speed_kmh": 80, "cabin_temp_c": 26}'
echo ""
_bug "Previously: 'It\\'s too hot' was classified as DISMISS (triggered cooldown)"
echo -e "  ${GREEN}Fixed:${NC}    Cabin/comfort keywords bypass classifier entirely"
echo ""
echo -e "  Tap ${BOLD}🎙${NC} and say each phrase:"
echo ""
echo -e "  ${YELLOW}Say:${NC} \"It's too hot\"     → AC on, cabin drops to 18°C  (NOT dismiss)"
echo -e "  ${YELLOW}Say:${NC} \"I'm cold\"          → cabin warms to 24°C"
echo -e "  ${YELLOW}Say:${NC} \"Turn on the AC\"    → AC on + cabin cools"
echo -e "  ${YELLOW}Say:${NC} \"Open the window\"   → windows open in sidebar"
echo -e "  ${YELLOW}Say:${NC} \"Close the sunroof\" → sunroof closes in sidebar"
echo ""
_expect "Each phrase: cabin sidebar reflects the change"
_expect "TTS confirms the action ('I'll cool the cabin down for you')"
_expect "Agent dismiss streak is NOT incremented"
echo ""
echo -e "  ${DIM}Other emotional phrases still work:${NC}"
echo -e "  ${YELLOW}Say:${NC} \"I'm tired\"       → cool cabin (19°C) + calm music + alerts suppressed"
echo -e "  ${YELLOW}Say:${NC} \"I'm stressed\"    → calm music + 21°C"
echo -e "  ${YELLOW}Say:${NC} \"I'm energetic\"   → upbeat music + 20°C"
_next

# ── 9. Meal flow — two paths ─────────────────────────────────────────────────
_title "8 / 9  — VOICE + MEAL FLOW (two paths)"
_reset
_state '{"hours_since_meal": 5.0, "speed_kmh": 70}'
echo ""
echo -e "  ${BOLD}Path A — vague request (two turns):${NC}"
echo -e "  ${YELLOW}Say:${NC} \"I'm hungry\""
echo -e "  ${GREEN}Expect:${NC} Concierge asks 'What are you in the mood for?' + cuisine list"
echo -e "  ${YELLOW}Then say:${NC} a cuisine  (e.g. 'Mexican' / 'burger' / 'Italian' / 'sandwich')"
echo -e "  ${GREEN}Expect:${NC} Restaurant card + map pin  (no second question)"
echo ""
echo -e "  ${BOLD}Path B — specific request (single turn):${NC}"
_bug "Previously: 'I feel like drinking a coffee' asked preference question anyway"
echo -e "  ${GREEN}Fixed:${NC}    Preference detected in original utterance → skips question"
echo ""
echo -e "  ${YELLOW}Say:${NC} \"I feel like drinking a coffee\""
echo -e "  ${GREEN}Expect:${NC} No preference question — goes straight to nearest café + card"
echo -e "  ${YELLOW}Say:${NC} \"Get me a burger\""
echo -e "  ${GREEN}Expect:${NC} No question — straight to nearest burger place"
echo ""
_expect "If no exact match: honest fallback ('closest I found is [Name]')"
_note "Vague = 'I'm hungry' / 'find me food'  →  asks  •  Specific = 'coffee/burger/pizza'  →  skips"
_next

# ── 10. Music concierge + TTS stop ──────────────────────────────────────────
_title "9 / 9  — MUSIC CONCIERGE + TTS STOP"
_reset
_state '{"speed_kmh": 90}'
echo ""
echo -e "  ${YELLOW}Say:${NC} \"play something calm\"      → mellow synth + 5 tracks listed"
echo -e "  ${YELLOW}Say:${NC} \"I want upbeat music\"       → high-energy synth"
echo -e "  ${YELLOW}Say:${NC} \"play Blinding Lights by The Weeknd\"  (iTunes direct search)"
echo ""
_expect "Music section appears with 5 tracks"
_expect "Click a track → synth plays immediately"
_expect "After ~2s iTunes preview replaces synth (track name in now-playing bar)"
_expect "Pause ⏸ and Stop ■ buttons work"
echo ""
echo -e "  ${BOLD}TTS stop — while music is playing:${NC}"
echo -e "  ${YELLOW}Say:${NC} anything  (e.g. \"what's the range?\")  while music plays"
_expect "Music stops completely the instant TTS starts speaking"
_expect "Music resumes after TTS finishes"
_note "Server broadcasts tts_start / tts_end  •  Frontend pauses/resumes audio context + preview"
_next



# ── Done ─────────────────────────────────────────────────────────────────────
_reset
echo ""
echo -e "${BOLD}${GREEN}  All scenarios complete. State has been reset.${NC}"
echo ""
echo -e "  ${BOLD}Useful endpoints:${NC}"
echo -e "  ${DIM}curl $BASE/privacy              | python3 -m json.tool${NC}"
echo -e "  ${DIM}curl $BASE/memory/stats         | python3 -m json.tool${NC}"
echo -e "  ${DIM}curl $BASE/driver/state         | python3 -m json.tool${NC}"
echo -e "  ${DIM}curl $BASE/agent/stats          | python3 -m json.tool${NC}"
echo ""
