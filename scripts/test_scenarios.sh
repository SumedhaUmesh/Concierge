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

_next() {
  echo ""
  echo -e "${DIM}  Press Enter to continue, Ctrl+C to quit...${NC}"
  read -r
}

# ── compute meeting times ────────────────────────────────────────────────────
_add_minutes() {
  local h=$(date +%H) m=$(date +%M)
  local total=$(( 10#$h * 60 + 10#$m + $1 ))
  printf "%02d:%02d" $(( total / 60 % 24 )) $(( total % 60 ))
}

MTG_NEAR=$(_add_minutes 4)    # 4 min from now — triggers quiet mode
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
_title "1 / 10  —  FUEL WARNING"
_reset
_state '{"fuel_percent": 12, "range_km": 45, "speed_kmh": 110}'
_expect "Fuel gauge turns red  •  Urgency-4 card fires in ~5s"
_expect "Card headline: fuel stop with real station name + distance"
_expect "Map: fuel station marker pinned, Navigate button on card"
_expect "TTS reads the headline aloud"
_action "Click 'Navigate to …' to open Google Maps, or dismiss"
_next

# ── 2. Rain + open windows ───────────────────────────────────────────────────
_title "2 / 10  —  RAIN + OPEN WINDOWS"
_reset
_state '{"rain_in_minutes": 8, "windows_open": true, "sunroof_open": true}'
_expect "Rain row appears in sidebar  •  Cabin card fires in ~5s"
_expect "Card headline: 'Rain in ~8 min — close windows and sunroof'"
_expect "Action button: 'Close windows & start AC'"
_action "Accept → windows/sunroof close, AC turns on (check sidebar)"
_next

# ── 3. Skipped meal ──────────────────────────────────────────────────────────
_title "3 / 10  —  SKIPPED MEAL"
_reset
_state '{"hours_since_meal": 6.5, "current_time": "13:00", "speed_kmh": 90}'
_expect "Meal card fires in ~5s  •  Nearest restaurant pinned on map"
_expect "Action button: Navigate to restaurant"
_action "Click action or say 'yes' to accept, or dismiss"
_next

# ── 4. Meeting + traffic ─────────────────────────────────────────────────────
_title "4 / 10  —  MEETING + TRAFFIC DELAY"
_reset
_state "{\"next_meeting_title\": \"Team standup\", \"next_meeting_time\": \"$MTG_FAR\", \"traffic_delay_minutes\": 12, \"speed_kmh\": 80}"
_expect "Meeting card appears in sidebar with time + traffic warning"
_expect "Schedule suggestion fires: leave now to make it on time"
_note "Meeting set for $MTG_FAR (15 min from now)"
_next

# ── 5. Driver fatigue → rest stop ────────────────────────────────────────────
_title "5 / 10  —  FATIGUE + REST STOP"
_reset
_state '{"minutes_driving_continuously": 130, "current_time": "02:00", "speed_kmh": 100, "next_rest_stop_lat": 34.052, "next_rest_stop_lng": -118.452, "next_rest_stop_km": 8.5}'
_expect "Fatigue bar spikes HIGH (red)  •  Risk shows HIGH"
_expect "Rest stop suggestion fires  •  Rest stop marker on map"
_action "Accept to navigate to rest stop"
_next

# ── 6. Cognitive Driver Model ────────────────────────────────────────────────
_title "6 / 10  —  COGNITIVE DRIVER MODEL (all bars)"
_reset
_state '{"minutes_driving_continuously": 100, "speed_kmh": 130, "traffic_delay_minutes": 20, "fuel_percent": 15, "current_time": "23:30", "rain_in_minutes": 5}'
_expect "FATIGUE bar: high (long drive + late night)"
_expect "LOAD bar: high (speed + traffic + rain)"
_expect "STRESS bar: high (low fuel + traffic + speed)"
_expect "Risk level: MODERATE or HIGH"
_note "All three signals combined — watch all three bars animate"
_next

# ── 7. Voice — emotional fast-path ───────────────────────────────────────────
_title "7 / 10  —  VOICE + EMOTIONAL FAST-PATH"
_reset
_state '{"speed_kmh": 80}'
echo ""
echo -e "  Tap ${BOLD}🎙${NC} and say each phrase — watch music + cabin update instantly:"
echo ""
echo -e "  ${YELLOW}Say:${NC} \"I'm tired\"        → cool cabin (19°C) + calm music + alerts suppressed"
echo -e "  ${YELLOW}Say:${NC} \"I'm stressed\"      → calm music (energy 2) + 21°C"
echo -e "  ${YELLOW}Say:${NC} \"I'm energetic\"     → upbeat music (energy 8) + 20°C"
echo -e "  ${YELLOW}Say:${NC} \"It's too hot\"      → AC on + cabin drops to 18°C"
echo -e "  ${YELLOW}Say:${NC} \"I'm bored\"         → upbeat groovy music"
echo -e "  ${YELLOW}Say:${NC} \"Find a scenic route\" → calm music + alerts suppressed 20 min"
echo ""
_note "No LLM call — these fire instantly via keyword matching"
_next

# ── 8. Two-turn meal flow ────────────────────────────────────────────────────
_title "8 / 10  —  VOICE + TWO-TURN MEAL FLOW"
_reset
_state '{"hours_since_meal": 5.0, "speed_kmh": 70}'
echo ""
echo -e "  ${YELLOW}Step 1:${NC} Say \"I'm hungry\""
echo -e "  ${GREEN}Concierge asks:${NC} what cuisine? lists nearby options"
echo -e "  ${YELLOW}Step 2:${NC} Say a cuisine (e.g. \"Mexican\") or restaurant name"
echo -e "  ${GREEN}Concierge:${NC} \"Great, heading to [Name]\" — navigation card appears"
echo ""
_next

# ── 9. Conference call / quiet mode ──────────────────────────────────────────
_title "9 / 10  —  CONFERENCE CALL MODE"
_reset
_state "{\"next_meeting_title\": \"Demo call\", \"next_meeting_time\": \"$MTG_NEAR\", \"speed_kmh\": 60}"
echo ""
echo -e "  Meeting set for ${BOLD}$MTG_NEAR${NC} (4 minutes from now)"
echo -e "  ${DIM}Watch loop checks every 30s — wait up to 30s for it to fire${NC}"
echo ""
_expect "TTS: 'Entering quiet mode — meeting in 4 minutes'"
_expect "Purple 🔇 QUIET MODE badge appears in sidebar"
_expect "Windows/sunroof close, AC turns on"
_expect "Agent goes silent (alerts suppressed 60 min)"
echo ""
echo -e "  ${YELLOW}To exit quiet mode:${NC}"
echo -e "  ${DIM}curl -X POST $BASE/sim/state -H 'Content-Type: application/json' -d '{\"next_meeting_time\": null, \"next_meeting_title\": null}'${NC}"
_expect "TTS: 'Quiet mode ended. Welcome back.' Badge disappears."
_next

# ── 10. Music concierge ──────────────────────────────────────────────────────
_title "10 / 10  —  MUSIC CONCIERGE"
_reset
_state '{"speed_kmh": 90}'
echo ""
echo -e "  ${YELLOW}Say:${NC} \"play something calm\"  or  \"I want upbeat music\""
echo -e "  ${YELLOW}Say:${NC} \"play Blinding Lights by The Weeknd\"  (iTunes direct search)"
echo ""
_expect "Music section appears with 5 tracks"
_expect "Click a track → synth plays immediately"
_expect "After ~2s iTunes preview replaces synth (track name updates)"
_expect "Pause ⏸ and Stop ■ buttons work"
echo ""

# ── Done ─────────────────────────────────────────────────────────────────────
_reset
echo -e "${BOLD}${GREEN}  All scenarios complete. State has been reset.${NC}"
echo ""
echo -e "  Useful endpoints:"
echo -e "  ${DIM}curl $BASE/privacy | python3 -m json.tool${NC}"
echo -e "  ${DIM}curl $BASE/memory/stats | python3 -m json.tool${NC}"
echo -e "  ${DIM}curl $BASE/driver/state | python3 -m json.tool${NC}"
echo ""
