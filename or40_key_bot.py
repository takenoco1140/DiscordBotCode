from __future__ import annotations
automation_loop_task = None
_last_ops_header_refresh_minute = None  # 'YYYY-MM-DD HH:MM'
# -*- coding: utf-8 -*-
"""
OR40 Key Drop BOT（中核：キー配布〜進行）
========================================
前提:
- Bot Token は環境変数 KEY_TOKEN
- /keydrop_panel で運営パネルを設置して、以後はボタン進行
- 画像生成（Playwright/Chromium）導入済み前提（失敗時のみ最終フォールバックでテキスト）

中核フロー（確定）:
- キー生成 → キーホストだけに連絡（画像A：出発予定＋注記）
- キーホストが「待機列完成」 → 一般に連絡（画像B：出発確定＋注記）
  - 同時に、キーホスト表示を「画像A'（確定版：出発確定＋注記）」へ切替（一般Bと完全一致させない）
- 出発時間の1分後に、一般チャンネルの投稿を削除（リセット）
- 緊急停止: その試合は「キーホストへの通知からやり直し」（match_no維持）
"""

import os
import re
import json
import random
import asyncio
from dataclasses import dataclass, asdict, fields, field
from datetime import datetime, date, timezone, timedelta
from typing import Optional, Set, List

import discord
from discord import app_commands
from discord.ext import commands
from pathlib import Path

def _find_project_root(start: Path) -> Path:
    """Find project root by walking up until a 'bots' directory is found."""
    start = start.resolve()
    for p in [start] + list(start.parents):
        if p.name.lower() == "bots":
            return p.parent
    # Fallback: assume .../bots/<bot>/...
    return start.parents[2]

BOT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = _find_project_root(BOT_DIR)
SECRETS_DIR = PROJECT_ROOT / "secrets"
DATA_DIR = BOT_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

SERVICE_ACCOUNT_JSON = SECRETS_DIR / "service_account.json"
KEYDROP_STATE_JSON = str(DATA_DIR / "keydrop_state.json")
DEFAULT_MODE = "reload"          # reload | tournament
DEFAULT_MATCH_COUNT = 4
DEFAULT_MATCH1_START = "22:15"   # 予定（目安）
CHECKIN_STATUS_CHANNEL_ID = 1467202863515046119  # 通知用（運営）
REPLAY_OPS_CHANNEL_ID = 1442840269257969780      # リプレイ提出 完了通知（運営）

LEGACY_STATE_PATH = str(BOT_DIR / "keydrop_state.json")
STATE_PATH = str(DATA_DIR / "keydrop_state.json")

def _migrate_legacy_state_file() -> None:
    """Migrate legacy state JSON stored next to .py into data/ directory.

    If data/keydrop_state.json does not exist but legacy keydrop_state.json exists,
    we copy it into data/.
    """
    try:
        if (not os.path.exists(STATE_PATH)) and os.path.exists(LEGACY_STATE_PATH):
            # copy instead of move to be safe
            with open(LEGACY_STATE_PATH, 'rb') as src, open(STATE_PATH, 'wb') as dst:
                dst.write(src.read())
    except Exception:
        pass

_migrate_legacy_state_file()


def _strip_bg_from_template(tpl: str) -> str:
    # 背景画像が読めない場合の白背景フォールバック
    out = tpl
    out = re.sub(r"\s*background-image:\s*url\([^\)]*\);\s*\n", "", out)
    # body に background が無ければ白を追加
    m = re.search(r"body\s*\{([\s\S]*?)\}\s*\n", out)
    if m and ("background:" not in m.group(1)):
        body_block = m.group(0)
        body_inner = m.group(1) + "\n  background: #ffffff;\n"
        out = out.replace(body_block, "body{\n" + body_inner + "}\n", 1)
    return out


def _inject_bg_uri(tpl: str, bg_uri: str) -> str:
    # CSS内の background-image url(...) を絶対URIに差し替える
    out = tpl
    out = re.sub(r'background-image:\s*url\("[^"]*"\);', f'background-image: url("{bg_uri}");', out)
    out = re.sub(r"background-image:\s*url\('[^']*'\);", f'background-image: url("{bg_uri}");', out)
    out = re.sub(r"background-image:\s*url\([^\)]*\);", f'background-image: url("{bg_uri}");', out)
    return out

ORANGE = 0xFF8A00

# 固定デフォルト（運用値）
DEFAULT_KEY_CHANNEL_ID = 1442840272730853492
DEFAULT_COMMENTARY_CHANNEL_ID = 1442840271539929195

# fixed channel ids
KEY_CHANNEL_FIXED_ID = 1442840272730853492  # キー配布チャンネル（本番デフォルト）
CASTER_CHANNEL_ID = 1442840271539929195       # 実況解説用チャンネル
ASSETS_DIR = str(PROJECT_ROOT / "assets")
BOARD_IMAGE_PATH = os.path.join(ASSETS_DIR, "OR40SOLOリロード台紙.jpg")


def hhmm(dt: datetime) -> str:
    return dt.strftime("%H:%M")

JST = timezone(timedelta(hours=9))

def now_jst() -> datetime:
    return datetime.now(tz=JST)

def parse_hhmm_dt(hhmm_str: str, base: Optional[datetime] = None) -> datetime:
    base = base or now_jst()
    h, m = hhmm_str.split(":")
    return base.replace(hour=int(h), minute=int(m), second=0, microsecond=0)

def is_in_pause_window(now: datetime) -> bool:
    if not STATE.key_pause_from or not STATE.key_pause_to:
        return False
    try:
        start = parse_hhmm_dt(STATE.key_pause_from, now)
        end = parse_hhmm_dt(STATE.key_pause_to, now)
        return start <= now < end
    except Exception:
        return False

def apply_map_remaining_minutes(now: datetime, remaining_min: int) -> None:
    # 残り分から切替時刻とキー配布停止時間帯を算出してSTATEへ反映
    rem = max(0, int(remaining_min))
    switch_dt = now + timedelta(minutes=rem)
    STATE.map_remaining_min = rem
    hhmm_val = switch_dt.strftime("%H:%M")
    # HTMLレンダリング等の互換用
    STATE.map_switch_hhmm = hhmm_val
    # パネル表示などの現行フィールド
    STATE.map_switch_time = hhmm_val

    # 停止帯：1試合目は切替前7分未満〜切替、2試合目以降は切替前4分未満〜切替
    lead = 7 if int(getattr(STATE, "match_no", 1) or 1) == 1 else 4
    pause_from = switch_dt - timedelta(minutes=lead)
    pause_to = switch_dt
    STATE.key_pause_from = pause_from.strftime("%H:%M")
    STATE.key_pause_to = pause_to.strftime("%H:%M")
    save_state(STATE)


def recompute_pause_window_from_state(now: Optional[datetime] = None) -> None:
    """Recompute key pause window from STATE.map_switch_time / STATE.map_remaining_min and STATE.match_no.

    Used when match_no changes after remaining minutes were entered.
    """
    now = now or now_jst()
    try:
        match_no = int(getattr(STATE, "match_no", 1) or 1)
    except Exception:
        match_no = 1

    switch_dt = None

    # Prefer explicit switch time
    sw = (getattr(STATE, "map_switch_time", None) or "").strip()
    if sw:
        try:
            switch_dt = parse_hhmm(sw, now)
        except Exception:
            switch_dt = None

    # Fallback: remaining minutes (should normally also set map_switch_time)
    if switch_dt is None:
        rem = getattr(STATE, "map_remaining_min", None)
        if rem is not None:
            try:
                rem = max(0, int(rem))
                switch_dt = now + timedelta(minutes=rem)
                hhmm_val = switch_dt.strftime("%H:%M")
                STATE.map_switch_hhmm = hhmm_val
                STATE.map_switch_time = hhmm_val
            except Exception:
                switch_dt = None

    if switch_dt is None:
        return

    lead = 7 if match_no == 1 else 4
    pause_from = switch_dt - timedelta(minutes=lead)
    pause_to = switch_dt
    STATE.key_pause_from = pause_from.strftime("%H:%M")
    STATE.key_pause_to = pause_to.strftime("%H:%M")
    save_state(STATE)





def load_entry_panel_state() -> dict:
    # Try to load entry-bot panel_state.json (event_date/start_time) from common locations.
    # Returns {} if not found/invalid.
    candidates = []

    base = Path(__file__).resolve().parent
    candidates.append(base / "data" / "panel_state.json")
    candidates.append(base.parent / "or40_entry_bot" / "data" / "panel_state.json")
    candidates.append(base.parent / "or40_entry_bot" / "panel_state.json")
    candidates.append(base / "panel_state.json")

    for p in candidates:
        try:
            if p.exists():
                return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
    return {}


def _parse_event_date_to_date(s: str) -> Optional[date]:
    # Accepts 'YYYY/M/D', 'YYYY-MM-DD', 'YYYY/MM/DD'
    if not s:
        return None
    s = str(s).strip()
    try:
        if "/" in s:
            y, m, d = s.split("/")
            s2 = f"{int(y):04d}/{int(m):02d}/{int(d):02d}"
            return datetime.strptime(s2, "%Y/%m/%d").date()
        if "-" in s:
            y, m, d = s.split("-")
            s2 = f"{int(y):04d}-{int(m):02d}-{int(d):02d}"
            return datetime.strptime(s2, "%Y-%m-%d").date()
    except Exception:
        return None
    return None


def is_event_day(now: Optional[datetime] = None) -> bool:
    # True only when today's date (JST) matches the configured event day.
    #
    # Priority:
    # 1) If STATE.display_date_override is set (YYYY-MM-DD), treat that as the event day (test run).
    # 2) Otherwise, use entry-bot panel_state.json event_date/date.
    #
    # If nothing is configured or parsing fails, returns False (safe).
    now = now or now_jst()

    # 1) Test override (display_date_override) — also governs automation start day.
    try:
        ov = (getattr(STATE, "display_date_override", None) or "").strip()
    except Exception:
        ov = ""
    if ov:
        try:
            d = _parse_event_date_to_date(ov)
            return bool(d and now.date() == d)
        except Exception:
            return False

    # 2) Entry-bot configured event day
    cfg = load_entry_panel_state()
    ev = cfg.get("event_date") or cfg.get("date") or ""
    evd = _parse_event_date_to_date(ev)
    if not evd:
        return False
    return now.date() == evd

def _extract_roster_numbers(guild: discord.Guild) -> List[str]:
    # チャンネル名の先頭3桁（例: "001-xxx"）を参加番号として扱う
    nums = set()
    pat = re.compile(r"^(\d{3})")
    for ch in getattr(guild, "text_channels", []):
        m = pat.match(ch.name)
        if m:
            nums.add(m.group(1))
    # ついでにボイスも拾いたい場合はここで追加できる
    for ch in getattr(guild, "voice_channels", []):
        m = pat.match(ch.name)
        if m:
            nums.add(m.group(1))
    return sorted(nums)

def get_event_date() -> Optional[date]:
    """EntryBot の panel_state.json の event_date/date を date にして返す。"""
    try:
        cfg = load_entry_panel_state()
        ev = cfg.get("event_date") or cfg.get("date") or ""
        return _parse_event_date_to_date(str(ev))
    except Exception:
        return None


def get_tournament_start_dt() -> Optional[datetime]:
    """大会日 + 大会開始時間(22:00想定) の datetime(JST) を返す。"""
    d = get_event_date()
    if not d:
        return None
    hhmm = load_entry_tournament_start_time()
    try:
        h, m = [int(x) for x in str(hhmm).split(":")]
        return datetime(d.year, d.month, d.day, h, m, tzinfo=JST)
    except Exception:
        return None


def get_match1_start_dt() -> Optional[datetime]:
    """大会日 + 第1試合開始時間(22:15想定) の datetime(JST) を返す。"""
    d = get_event_date()
    if not d:
        return None
    hhmm = load_entry_match1_start_time()
    try:
        h, m = [int(x) for x in str(hhmm).split(":")]
        return datetime(d.year, d.month, d.day, h, m, tzinfo=JST)
    except Exception:
        return None




def _calc_unchecked_numbers(guild: discord.Guild) -> str:
    roster = _extract_roster_numbers(guild)
    checked = set(getattr(STATE, "checked_in_numbers", []) or [])
    declined = set(getattr(STATE, "declined_numbers", []) or [])
    forfeited = set(getattr(STATE, "forfeit_numbers", []) or [])
    operated = checked | declined | forfeited
    unchecked = [n for n in roster if n not in operated]
    return ",".join(unchecked)



def find_channel_by_number(guild: discord.Guild, number: str) -> Optional[discord.abc.GuildChannel]:
    n = str(number).strip()
    if not re.fullmatch(r"\d{3}", n):
        return None
    pat = re.compile(rf"^{re.escape(n)}")
    for ch in guild.text_channels:
        if pat.match(ch.name):
            return ch
    try:
        for th in guild.threads:
            if pat.match(th.name):
                return th
    except Exception:
        pass
    return None



async def automation_tick_once(force: bool = False):
    """Run one automation decision tick. If force=True, ignores event-day check."""
    now = now_jst()
    if not force:
        if not is_event_day(now):
            return

    # Single-tick automation logic (minimal safe fallback)
    if STATE.auto_enabled and not STATE.keyhost_notified_once and not STATE.emergency_stop:
        if STATE.planned_departure:
            try:
                # Compare HH:MM strings safely
                if now.strftime("%H:%M") >= STATE.planned_departure:
                    if "keyhost_notify_once" in globals():
                        await keyhost_notify_once(None, reason="debug_auto")
            except Exception:
                pass






def generate_key(used: Set[str]) -> str:
    for _ in range(20000):
        k = f"OR40{random.randint(0, 9999):04d}"
        if k not in used:
            used.add(k)
            return k
    return f"OR40{random.randint(0, 9999):04d}"


def parse_hhmm_str(s: str) -> Optional[str]:
    s = (s or "").strip()
    if len(s) == 5 and s[2] == ":" and s[:2].isdigit() and s[3:].isdigit():
        hh = int(s[:2]); mm = int(s[3:])
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            return f"{hh:02d}:{mm:02d}"
    return None


@dataclass
class BotState:
    # channels
    key_channel_id: Optional[int] = None          # 一般参加者が見るキー配布チャンネル
    keyhost_channel_id: Optional[int] = None      # キーホストが見るチャンネル
    commentary_channel_id: Optional[int] = None    # 実況解説チャンネル

    # (互換/将来用) state に残っていても落ちないよう保持
    replay_channel_id: Optional[int] = None

    # tournament config
    mode: str = DEFAULT_MODE
    match_count: int = DEFAULT_MATCH_COUNT
    match1_start: str = DEFAULT_MATCH1_START


    # display (test override)
    display_date_override: Optional[str] = None   # YYYY-MM-DD (表示用テスト)
    # progress
    match_no: int = 1
    phase: str = "INIT"  # INIT | PREP | KEYHOST_SENT | DEPART_CONFIRMED | IN_MATCH | WAIT_REPLAY | ENDED
    emergency_stop: bool = False


    # map switch / key pause
    map_switch_time: Optional[str] = None          # HH:MM
    # 互換用：旧実装で参照していた属性名（HTMLレンダリング等で使用）
    map_switch_hhmm: Optional[str] = None          # HH:MM
    map_remaining_min: Optional[int] = None        # 残り分（表示/再計算用）
    key_pause_from: Optional[str] = None           # HH:MM
    key_pause_to: Optional[str] = None             # HH:MM

    # checkin / automation
    auto_enabled: bool = False
    checkin_closed: bool = False
    keyhost_notified_once: bool = False           # 第1試合のキー通知済み（表示切替用）
    uncheckin_numbers: Optional[str] = None       # "001,009" など（表示用）
    uncheckin_calc_date: Optional[str] = None    # YYYY-MM-DD（同日二重計算防止）
    checked_in_numbers: List[str] = field(default_factory=list)  # ["001", ...]

    declined_numbers: List[str] = field(default_factory=list)  # ["010", ...]
    forfeit_numbers: List[str] = field(default_factory=list)   # ["003", ...]

    # checkin automation flags
    checkin_phase1_sent_date: Optional[str] = None  # YYYY-MM-DD
    checkin_phase2_sent_date: Optional[str] = None  # YYYY-MM-DD
    checkin_phase3_sent_date: Optional[str] = None  # YYYY-MM-DD
    checkin_phase4_sent_date: Optional[str] = None  # YYYY-MM-DD

    # status message in notification channel
    checkin_status_message_id: Optional[int] = None
    checkin_status_last_min: Optional[str] = None   # YYYY-MM-DD HH:MM

    # ops panel header update throttle
    ops_header_last_min: Optional[str] = None       # YYYY-MM-DD HH:MM
    checkin_button_sent_date: Optional[str] = None  # YYYY-MM-DD
    checkin_button_message_ids: dict[str, int] = field(default_factory=dict)  # number -> message_id
    checkin_button_channel_ids: dict[str, int] = field(default_factory=dict)  # number -> channel_id
    checkin_cleanup_date: Optional[str] = None  # YYYY-MM-DD


    # replay request messages (per number)
    replay_request_message_ids: dict[str, int] = field(default_factory=dict)  # number -> message_id
    replay_request_channel_ids: dict[str, int] = field(default_factory=dict)  # number -> channel_id

    # replay escalation (rank contacts) for replay-forgotten
    replay_rank_match_no: Optional[int] = None
    replay_rank1: Optional[str] = None
    replay_rank2: Optional[str] = None
    replay_rank3: Optional[str] = None
    replay_rank_stage: int = 0  # 0->rank1, 1->rank2, 2->rank3, 3=done


    # per match
    custom_key: Optional[str] = None
    planned_departure: Optional[str] = None
    departure_time: Optional[str] = None

    # message ids (general)
    last_key_image_msg_id: Optional[int] = None
    last_key_embed_msg_id: Optional[int] = None  # 互換用（一般にテキストキーは出さない）
    delete_at_iso: Optional[str] = None

    # message ids (keyhost)
    last_keyhost_image_msg_id: Optional[int] = None
    last_keyhost_key_msg_id: Optional[int] = None

    # ops panel
    ops_panel_channel_id: Optional[int] = None
    ops_panel_message_id: Optional[int] = None

    # key history
    used_keys: Optional[list[str]] = None


def _state_field_names() -> Set[str]:
    return {f.name for f in fields(BotState)}


def load_state() -> BotState:
    """
    旧stateに未知キーが混ざっていても落ちない（互換フィルタ）。
    """
    if not os.path.exists(STATE_PATH):
        s = BotState(used_keys=[])
        # 固定デフォルト
        s.key_channel_id = DEFAULT_KEY_CHANNEL_ID
        s.commentary_channel_id = DEFAULT_COMMENTARY_CHANNEL_ID
        save_state(s)
        return s

    with open(STATE_PATH, "r", encoding="utf-8") as f:
        d = json.load(f) or {}

    if d.get("used_keys") is None:
        d["used_keys"] = []

    allowed = _state_field_names()
    filtered = {k: v for k, v in d.items() if k in allowed}
    s = BotState(**filtered)
    if s.key_channel_id is None:
        s.key_channel_id = DEFAULT_KEY_CHANNEL_ID
    if getattr(s, "commentary_channel_id", None) is None:
        s.commentary_channel_id = DEFAULT_COMMENTARY_CHANNEL_ID
    return s


def save_state(s: BotState) -> None:
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(asdict(s), f, ensure_ascii=False, indent=2)



def load_entry_tournament_start_time() -> str:
    """EntryBot の data/panel_state.json から大会開始時間(HH:MM)を読む。
    読めなければ "22:00" を返す。
    """
    try:
        p = PROJECT_ROOT / "bots" / "or40_entry_bot" / "data" / "panel_state.json"
        if not p.exists():
            return "22:00"
        data = json.loads(p.read_text(encoding="utf-8")) or {}
        v = (
            data.get("tournament_start_time")
            or data.get("tournament_start")
            or data.get("tournament_start_hhmm")
            or data.get("tournament_start_time_hhmm")
            or ""
        )
        v = parse_hhmm_str(str(v))
        return v or "22:00"
    except Exception:
        return "22:00"


def load_entry_match1_start_time() -> str:
    """第1試合開始時間(HH:MM)を返す。
    仕様: 第1試合開始 = 大会開始 + 15分（panel_state.json の start_time は参照しない）
    """
    try:
        t0 = load_entry_tournament_start_time()
        # HH:MM -> minutes
        if not t0 or ":" not in str(t0):
            raise ValueError("invalid tournament start")
        hh, mm = [int(x) for x in str(t0).split(":")]
        total = (hh * 60 + mm + 15) % (24 * 60)
        hh2 = total // 60
        mm2 = total % 60
        return f"{hh2:02d}:{mm2:02d}"
    except Exception:
        return DEFAULT_MATCH1_START


def load_entry_start_time() -> str:
    """互換用（旧名）。第1試合開始時間を返す。"""
    return load_entry_match1_start_time()

def reset_to_before_match1() -> None:
    """全リセット：1試合目開始前に戻す（送信先設定は保持）。"""
    # keep configured ids
    keep = {
        "key_channel_id": STATE.key_channel_id,
        "keyhost_channel_id": STATE.keyhost_channel_id,
        "commentary_channel_id": STATE.commentary_channel_id,
        "ops_panel_channel_id": STATE.ops_panel_channel_id,
        "ops_panel_message_id": STATE.ops_panel_message_id,
        "mode": STATE.mode,
        "match_count": STATE.match_count,
        "match1_start": STATE.match1_start,
    }

    # reset core
    STATE.match_no = 1
    STATE.phase = "INIT"


    # display date override reset
    STATE.display_date_override = None
    STATE.custom_key = None
    # ★大会開始時間を内部初期値として入れる
    STATE.planned_departure = load_entry_tournament_start_time()  # 1試合目のキー配布予定（大会開始）
    STATE.departure_time = None

    # map switch / pause
    STATE.map_switch_time = None
    STATE.map_switch_hhmm = None
    STATE.map_remaining_min = None
    STATE.key_pause_from = None
    STATE.key_pause_to = None

    # checkin / automation
    STATE.auto_enabled = False
    STATE.checkin_closed = False
    STATE.keyhost_notified_once = False
    STATE.uncheckin_numbers = None

    # stops
    STATE.emergency_stop = False

    # message ids
    STATE.last_key_image_msg_id = None
    STATE.last_key_embed_msg_id = None
    STATE.last_keyhost_image_msg_id = None
    STATE.last_keyhost_key_msg_id = None
    try:
        STATE.replay_request_message_ids.clear()
        STATE.replay_request_channel_ids.clear()
    except Exception:
        STATE.replay_request_message_ids = {}
        STATE.replay_request_channel_ids = {}
    STATE.delete_at_iso = None

    # restore kept
    for k, v in keep.items():
        setattr(STATE, k, v)

    save_state(STATE)

STATE = load_state()



# default fixed key channel (can be overridden via /set_key_target for testing)
if STATE.key_channel_id is None:
    STATE.key_channel_id = KEY_CHANNEL_FIXED_ID
    save_state(STATE)
def used_set() -> Set[str]:
    return set(STATE.used_keys or [])


def persist_used(used: Set[str]) -> None:
    STATE.used_keys = sorted(list(used))[-20000:]


RAW_HTML_TEMPLATE_BASE = """<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8" />

<style>
* { box-sizing: border-box; }

html, body{
  margin: 0;
  padding: 0;
  width: 1280px;
  height: 720px;
}

body{
  font-family:
    "Noto Sans JP",
    "Hiragino Sans",
    "Yu Gothic",
    "Meiryo",
    system-ui,
    -apple-system,
    "Segoe UI",
    sans-serif;
  color: #222;

  background-image: url("OR40SOLOリロード台紙.jpg");
  background-size: cover;
  background-position: center;
  background-repeat: no-repeat;
}

/* ===== 全体 ===== */
.app{
  position: absolute;
  top: 140px;
  left: 60px;
  width: 1000px;
  display: flex;
  flex-direction: column;
  gap: 34px;
}

/* ===== 試合目 ===== */
.match-box p{
  position: relative;
  display: inline-block;
  padding: 10px 1.2em;
  margin: 0;

  font-size: 26px;
  font-weight: 800;
  letter-spacing: 0.05em;
  color: #111;
}

.match-box p::before,
.match-box p::after{
  content: "";
  position: absolute;
  width: 22px;
  height: 28px;
}

.match-box p::before{
  top: 0;
  left: 0;
  border-left: 5px solid {accent_color};
  border-top: 5px solid {accent_color};
}

.match-box p::after{
  bottom: 0;
  right: 0;
  border-right: 5px solid {accent_color};
  border-bottom: 5px solid {accent_color};
}

/* ===== 共通カード ===== */
.line-card{
  position: relative;
  width: 100%;
  min-height: 180px;
  border: 5px solid {accent_color};
  border-radius: 28px;

  background: rgba(255,255,255,0.95);
  box-shadow: 0 10px 26px rgba(0,0,0,0.12);

  padding: 32px 34px 26px;
  display: flex;
  flex-direction: column;
  justify-content: center;
}

/* タイトル（下だけ白ベタ） */
.line-title{
  position: absolute;
  top: -22px;
  left: 30px;
  padding: 0 12px;

  font-size: 20px;
  font-weight: 900;
  color: {accent_color};
  background: transparent;

  z-index: 2;
}

.line-title::after{
  content: "";
  position: absolute;
  left: 0;
  right: 0;
  bottom: 0;
  height: 55%;
  background: #fff;
  z-index: -1;
  border-radius: 6px;
}

/* ===== 2行ブロック共通 ===== */
.two-line{
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  line-height: 1;
}

.two-line .main{
  font-size: 72px;
  font-weight: 900;
  letter-spacing: 0.03em;
  line-height: 1;
  color: #111;
}

.two-line .sub{
  margin-top: 8px;
  font-size: 16px;
  font-weight: 800;
  letter-spacing: 0.12em;
  color: #444;
  min-height: 1em;
}

/* ===== カスタムキー ===== */
.key-value{
  display: flex;
  align-items: flex-end;
  gap: 6px;   /* ← ここだけ変更（10px → 6px） */
}

/* 固定側（OR40） */
.key-fixed .main{ letter-spacing: 0.02em; }
.key-fixed .sub { letter-spacing: 0.12em; }

/* 可変側（1234） */
.key-dynamic .main{ letter-spacing: 0.03em; }
.key-dynamic .sub { letter-spacing: 0.12em; }

/* ===== 出発予定 ===== */
.time-block{
  align-items: flex-start;
}

.time-block .main{
  font-size: 64px;
  font-weight: 900;
  letter-spacing: 0.03em;
  line-height: 1;
}

/* ===== 注釈 ===== */
.note-out{
  margin-top: -18px;
  padding-left: 20px;
  font-size: 22px;
  font-weight: 900;
  line-height: 1.4;
  color: #111;
  text-shadow:
    -2px 0 #fff,
     2px 0 #fff,
     0 -2px #fff,
     0  2px #fff,
    -2px -2px #fff,
     2px -2px #fff,
    -2px  2px #fff,
     2px  2px #fff;
}
</style>
</head>

<body>
  <div class="app">

    <div class="match-box">
      <p>⚔　{match_no}試合目　⚔</p>
    </div>

    <div class="line-card">
      <span class="line-title">🔒カスタムキー</span>

      <div class="key-value">
        <div class="two-line key-fixed">
          <div class="main">OR40</div>
          <div class="sub">オー・アール</div>
        </div>

        <div class="two-line key-dynamic">
          <div class="main">{key_dynamic}</div>
          <div class="sub"> </div>
        </div>
      </div>
    </div>

    <div class="line-card">
      <span class="line-title">🚎{time_title}</span>

      <div class="two-line time-block">
        <div class="main">{time_value}</div>
        <div class="sub"> </div>
      </div>
    </div>

    <div class="note-out">
      {note_text}
    </div>

  </div>
</body>
</html>
"""

RAW_HTML_TEMPLATE_KEYHOST_PLANNED = """<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8" />

<style>
* { box-sizing: border-box; }

html, body{
  margin: 0;
  padding: 0;
  width: 1280px;
  height: 720px;
}

body{
  font-family:
    "Noto Sans JP",
    "Hiragino Sans",
    "Yu Gothic",
    "Meiryo",
    system-ui,
    -apple-system,
    "Segoe UI",
    sans-serif;
  color: #222;

  background-image: url("OR40SOLOリロード台紙.jpg");
  background-size: cover;
  background-position: center;
  background-repeat: no-repeat;
}

/* ===== 全体 ===== */
.app{
  position: absolute;
  top: 140px;
  left: 60px;
  width: 1000px;
  display: flex;
  flex-direction: column;
  gap: 34px;
}

/* ===== 試合目 ===== */
.match-box p{
  position: relative;
  display: inline-block;
  padding: 10px 1.2em;
  margin: 0;

  font-size: 26px;
  font-weight: 800;
  letter-spacing: 0.05em;
  color: #111;
}

.match-box p::before,
.match-box p::after{
  content: "";
  position: absolute;
  width: 22px;
  height: 28px;
}

.match-box p::before{
  top: 0;
  left: 0;
  border-left: 5px solid {accent_color};
  border-top: 5px solid {accent_color};
}

.match-box p::after{
  bottom: 0;
  right: 0;
  border-right: 5px solid {accent_color};
  border-bottom: 5px solid {accent_color};
}

/* ===== 共通カード ===== */
.line-card{
  position: relative;
  width: 100%;
  min-height: 180px;
  border: 5px solid {accent_color};
  border-radius: 28px;

  background: rgba(255,255,255,0.95);
  box-shadow: 0 10px 26px rgba(0,0,0,0.12);

  padding: 32px 34px 26px;
  display: flex;
  flex-direction: column;
  justify-content: center;
}

/* タイトル（下だけ白ベタ） */
.line-title{
  position: absolute;
  top: -22px;
  left: 30px;
  padding: 0 12px;

  font-size: 20px;
  font-weight: 900;
  color: {accent_color};
  background: transparent;

  z-index: 2;
}

.line-title::after{
  content: "";
  position: absolute;
  left: 0;
  right: 0;
  bottom: 0;
  height: 55%;
  background: #fff;
  z-index: -1;
  border-radius: 6px;
}

/* ===== 2行ブロック共通 ===== */
.two-line{
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  line-height: 1;
}

.two-line .main{
  font-size: 72px;
  font-weight: 900;
  letter-spacing: 0.03em;
  line-height: 1;
  color: #111;
}

.two-line .sub{
  margin-top: 8px;
  font-size: 16px;
  font-weight: 800;
  letter-spacing: 0.12em;
  color: #444;
  min-height: 1em;
}

/* ===== カスタムキー ===== */
.key-value{
  display: flex;
  align-items: flex-end;
  gap: 6px;   /* ← ここだけ変更（10px → 6px） */
}

/* 固定側（OR40） */
.key-fixed .main{ letter-spacing: 0.02em; }
.key-fixed .sub { letter-spacing: 0.12em; }

/* 可変側（1234） */
.key-dynamic .main{ letter-spacing: 0.03em; }
.key-dynamic .sub { letter-spacing: 0.12em; }

/* ===== 出発時間（予定） ===== */
.time-row{
  display: flex;
  align-items: center;
  gap: 18px;
  width: 100%;
  line-height: 1;
}
.time-row-label{
  font-size: 22px;
  font-weight: 900;
  letter-spacing: 0.06em;
  color: #fff;
  background: #111;
  padding: 10px 18px;
  border-radius: 16px;
  box-shadow: 0 6px 14px rgba(0,0,0,0.18);
  white-space: nowrap;
}
.time-row-value{
  font-size: 64px;
  font-weight: 900;
  letter-spacing: 0.03em;
  line-height: 1;
  color: #111;
}

/* ===== 出発時間（通常） ===== */
.time-block .main{
  letter-spacing: 0.03em;
}

/* ===== 注釈 ===== */
.note-out{
  margin-top: -18px;
  padding-left: 20px;
  font-size: 22px;
  font-weight: 900;
  line-height: 1.4;
  color: #111;
}
</style>
</head>

<body>
  <div class="app">

    <div class="match-box">
      <p>⚔　{match_no}試合目　⚔</p>
    </div>

    <div class="line-card">
      <span class="line-title">🔒カスタムキー</span>

      <div class="key-value">
        <div class="two-line key-fixed">
          <div class="main">OR40</div>
          <div class="sub">オー・アール</div>
        </div>

        <div class="two-line key-dynamic">
          <div class="main">{key_dynamic}</div>
          <div class="sub"> </div>
        </div>
      </div>
    </div>

    <div class="line-card">
      <span class="line-title">🚎{time_title}</span>

      <div class="time-row">
        <span class="time-row-label">予定</span>
        <span class="time-row-value">{time_value}</span>
      </div>
    </div>

    <div class="note-out">
      {note_text}
    </div>

  </div>
</body>
</html>
"""

RAW_HTML_TEMPLATE_KEYHOST_CONFIRMED = """<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8" />

<style>
* { box-sizing: border-box; }

html, body{
  margin: 0;
  padding: 0;
  width: 1280px;
  height: 720px;
}

body{
  font-family:
    "Noto Sans JP",
    "Hiragino Sans",
    "Yu Gothic",
    "Meiryo",
    system-ui,
    -apple-system,
    "Segoe UI",
    sans-serif;
  color: #222;

  background-image: url("OR40SOLOリロード台紙.jpg");
  background-size: cover;
  background-position: center;
  background-repeat: no-repeat;
}

/* ===== 全体 ===== */
.app{
  position: absolute;
  top: 140px;
  left: 60px;
  width: 1000px;
  display: flex;
  flex-direction: column;
  gap: 34px;
}

/* ===== 試合目 ===== */
.match-box p{
  position: relative;
  display: inline-block;
  padding: 10px 1.2em;
  margin: 0;

  font-size: 26px;
  font-weight: 800;
  letter-spacing: 0.05em;
  color: #111;
}

.match-box p::before,
.match-box p::after{
  content: "";
  position: absolute;
  width: 22px;
  height: 28px;
}

.match-box p::before{
  top: 0;
  left: 0;
  border-left: 5px solid {accent_color};
  border-top: 5px solid {accent_color};
}

.match-box p::after{
  bottom: 0;
  right: 0;
  border-right: 5px solid {accent_color};
  border-bottom: 5px solid {accent_color};
}

/* ===== 共通カード ===== */
.line-card{
  position: relative;
  width: 100%;
  min-height: 180px;
  border: 5px solid {accent_color};
  border-radius: 28px;

  background: rgba(255,255,255,0.95);
  box-shadow: 0 10px 26px rgba(0,0,0,0.12);

  padding: 32px 34px 26px;
  display: flex;
  flex-direction: column;
  justify-content: center;
}

/* タイトル（下だけ白ベタ） */
.line-title{
  position: absolute;
  top: -22px;
  left: 30px;
  padding: 0 12px;

  font-size: 20px;
  font-weight: 900;
  color: {accent_color};
  background: transparent;

  z-index: 2;
}

.line-title::after{
  content: "";
  position: absolute;
  left: 0;
  right: 0;
  bottom: 0;
  height: 55%;
  background: #fff;
  z-index: -1;
  border-radius: 6px;
}

/* ===== 2行ブロック共通 ===== */
.two-line{
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  line-height: 1;
}

.two-line .main{
  font-size: 64px;
  font-weight: 900;
  letter-spacing: 0.03em;
  line-height: 1;
  color: #111;
}

.two-line .sub{
  margin-top: 8px;
  font-size: 16px;
  font-weight: 800;
  letter-spacing: 0.12em;
  color: #444;
  min-height: 1em;
}

/* ===== カスタムキー ===== */
.key-value{
  display: flex;
  align-items: flex-end;
  gap: 6px;   /* ← ここだけ変更（10px → 6px） */
}

/* 固定側（OR40） */
.key-fixed .main{ letter-spacing: 0.02em; }
.key-fixed .sub { letter-spacing: 0.12em; }

/* 可変側（1234） */
.key-dynamic .main{ letter-spacing: 0.03em; }
.key-dynamic .sub { letter-spacing: 0.12em; }

/* ===== 出発予定 ===== */
.time-block .main{
  letter-spacing: 0.03em;
}

/* ===== 出発時間確定（横並び） ===== */
.time-transition{
  display: flex;
  justify-content: flex-start;
  align-items: center;
  gap: 22px;
  line-height: 1;
}
.time-label{
  font-size: 22px;
  font-weight: 900;
  letter-spacing: 0.06em;
  color: #fff;
  background: #111;
  padding: 10px 18px;
  border-radius: 16px;
  box-shadow: 0 6px 14px rgba(0,0,0,0.18);
}
.time-confirm-tag{
  font-size: 22px;
  font-weight: 900;
  letter-spacing: 0.06em;
  color: #fff;
  background: #7a1f2b; /* ボルドー系 */
  padding: 10px 18px;
  border-radius: 16px;
  box-shadow: 0 6px 14px rgba(0,0,0,0.18);
  margin-top: 4px;
}
.time-planned{
  font-size: 64px;
  font-weight: 900;
  letter-spacing: 0.03em;
  line-height: 1;
  color: #111;
  text-decoration-line: line-through;
  text-decoration-thickness: 8px;
  text-decoration-color: #111;
}
.time-arrow{
  font-size: 48px;
  font-weight: 900;
  letter-spacing: 0.02em;
  color: #111;
}
.time-confirmed{
  font-size: 64px;
  font-weight: 900;
  letter-spacing: 0.03em;
  line-height: 1;
  color: #111;
}

/* ===== 注釈 ===== */
.note-out{
  margin-top: -18px;
  padding-left: 20px;
  font-size: 22px;
  font-weight: 900;
  line-height: 1.4;
  color: #111;
}
</style>
</head>

<body>
  <div class="app">

    <div class="match-box">
      <p>⚔　{match_no}試合目　⚔</p>
    </div>

    <div class="line-card">
      <span class="line-title">🔒カスタムキー</span>

      <div class="key-value">
        <div class="two-line key-fixed">
          <div class="main">OR40</div>
          <div class="sub">オー・アール</div>
        </div>

        <div class="two-line key-dynamic">
          <div class="main">{key_dynamic}</div>
          <div class="sub"> </div>
        </div>
      </div>
    </div>

    <div class="line-card">
      <span class="line-title">🚎{time_title}</span>

      <div class="time-transition">
        <span class="time-label">予定</span>
        <span class="time-planned">{planned_time}</span>
        <span class="time-arrow">▶</span>
        <span class="time-confirm-tag">確定</span>
        <span class="time-confirmed">{time_value}</span>
      </div>
    </div>

    <div class="note-out">
      {note_text}
    </div>

  </div>
</body>
</html>
"""

def _make_key_embed(custom_key: str) -> "discord.Embed":
    return discord.Embed(description=custom_key)

def _make_time_embed(time_value: str) -> "discord.Embed":
    return discord.Embed(description=time_value)

def _build_html(template: str, **kwargs) -> str:
    """完成デザインを保持したまま、プレースホルダだけ差し替える。"""
    t = template
    keys = list(kwargs.keys())
    for k in keys:
        t = t.replace("{" + k + "}", f"@@__{k}__@@")
    t = t.replace("{", "{{").replace("}", "}}")
    for k in keys:
        t = t.replace(f"@@__{k}__@@", "{" + k + "}")
    return t.format(**kwargs, map_switch=(STATE.map_switch_hhmm or '未設定'), pause_from=(STATE.key_pause_from or '00:00'), pause_to=(STATE.key_pause_to or '00:00'))

async def try_render_png(
    match_no: int,
    custom_key: str,
    time_title: str,
    time_value: str,
    note: Optional[str],
    *,
    variant: str = "general",
    planned_time: Optional[str] = None
) -> Optional[str]:
    # 背景画像が無い/読めない場合でも「白背景で画像生成」を必ず試す
    try:
        from pathlib import Path as _Path
        from playwright.async_api import async_playwright
    except Exception:
        return None

    # テンプレ選択（先に決める）
    if variant == "keyhost_planned":
        accent_color = "#0b3d91"  # 濃い青（運用）
        template = RAW_HTML_TEMPLATE_KEYHOST_PLANNED
    elif variant == "keyhost_confirmed":
        accent_color = "#0b3d91"  # 濃い青（運用）
        template = RAW_HTML_TEMPLATE_KEYHOST_CONFIRMED
    else:
        accent_color = "#ff8a00"
        template = RAW_HTML_TEMPLATE_BASE

    # 背景画像（存在すれば絶対URIに差し替え、無ければ白背景へ）
    board_path = _Path(BOARD_IMAGE_PATH)
    if board_path.exists():
        try:
            template = _inject_bg_uri(template, board_path.resolve().as_uri())
        except Exception:
            template = _strip_bg_from_template(template)
    else:
        template = _strip_bg_from_template(template)

    # キー末尾4桁
    if custom_key and custom_key.startswith("OR40") and len(custom_key) >= 8:
        key_dynamic = custom_key[4:8]
    else:
        key_dynamic = (custom_key or "")[-4:] if custom_key else ""

    safe_note = (note or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    note_text = safe_note.replace("\\n", "<br>") if safe_note else " "

    html = _build_html(
        template,
        accent_color=accent_color,
        match_no=match_no,
        key_dynamic=key_dynamic,
        time_title=time_title,
        time_value=time_value,
        planned_time=(planned_time or " "),
        note_text=note_text)

    out_dir = _Path(os.path.dirname(__file__)) / "render_out"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"keydrop_m{match_no}_{int(datetime.now().timestamp())}.png"

    # HTMLは bot フォルダ直下に書く（存在保証）
    tmp_dir = _Path(__file__).resolve().parent
    temp_html_path = tmp_dir / f"__keydrop_render_{int(datetime.now().timestamp())}.html"
    try:
        temp_html_path.write_text(html, encoding="utf-8")
    except Exception:
        return None

    async def _shot() -> None:
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            page = await browser.new_page(viewport={"width": 1280, "height": 720})
            await page.goto(temp_html_path.absolute().as_uri(), wait_until="load")
            await page.screenshot(path=str(out_path))
            await browser.close()

    try:
        await _shot()
    except Exception:
        # ここで落ちるなら、背景に関係なく失敗。最後に白背景テンプレをもう一段強制して再試行
        try:
            template2 = _strip_bg_from_template(template)
            html2 = _build_html(
                template2,
                accent_color=accent_color,
                match_no=match_no,
                key_dynamic=key_dynamic,
                time_title=time_title,
                time_value=time_value,
                planned_time=(planned_time or " "),
                note_text=note_text)
            temp_html_path.write_text(html2, encoding="utf-8")
            await _shot()
        except Exception:
            try:
                temp_html_path.unlink(missing_ok=True)
            except Exception:
                pass
            return None

    try:
        temp_html_path.unlink(missing_ok=True)
    except Exception:
        pass

    return str(out_path)



intents = discord.Intents.default()
intents.guilds = True
intents.messages = True
bot = commands.Bot(command_prefix="!", intents=intents)


def _fmt_numbers_slash(nums: List[str]) -> str:
    nums = [str(x).zfill(3) for x in (nums or []) if re.fullmatch(r"\d{3}", str(x).zfill(3))]
    return " / ".join(sorted(set(nums)))


def build_ops_embed() -> discord.Embed:
    s = STATE

    phase_map = {
        "INIT": "待機",
        "PREP": "準備中",
        "KEYHOST_SENT": "キー通知済",
        "DEPART_CONFIRMED": "出発確定",
        "IN_MATCH": "進行中",
        "WAIT_REPLAY": "リプレイ待ち",
        "ENDED": "終了",
    }
    status = phase_map.get(s.phase, s.phase)

    stop_now = False
    try:
        stop_now = bool(getattr(s, "emergency_stop", False)) or is_in_pause_window(now_jst())
    except Exception:
        stop_now = bool(getattr(s, "emergency_stop", False))

    if stop_now and "停止中" not in status:
        status = f"{status} / 停止中"

    # 未操作（ヘッダー）
    unop = []
    try:
        if getattr(s, "uncheckin_numbers", None):
            unop = [x.strip() for x in str(s.uncheckin_numbers).split(",") if x.strip()]
    except Exception:
        unop = []
    unop_txt = _fmt_numbers_slash(unop)
    if not unop_txt:
        unop_txt = "なし"

    # 値整形
    key_val = s.custom_key or "未設定"

    if getattr(s, "departure_time", None):
        dep_val = f"{s.departure_time}"
    else:
        base = (s.planned_departure or "").strip()
        dep_val = base if base else "未設定"

    # マップ切替（指定：切替時間残り｜）
    sw = (getattr(s, "map_switch_time", None) or "").strip()
    if sw:
        switch_remaining = sw
    else:
        switch_remaining = "未設定"

    pf = getattr(s, "key_pause_from", None) or "00:00"
    pt = getattr(s, "key_pause_to", None) or "00:00"

    mode_label = "ソロ（リロード）" if getattr(s, "mode", "") == "reload" else "ソロ"
    match1 = load_entry_match1_start_time()

    # 設定日（EntryBotの event_date を "2月15日(日)" 形式に）
    # 設定日（表示用）：基本は大会日。テスト時は display_date_override を優先。
    setting_date = "未設定"
    try:
        d_base = get_event_date()
        d_show = d_base
        is_test = False
        ov = (getattr(STATE, "display_date_override", None) or "").strip()
        if ov:
            try:
                d_show = _parse_event_date_to_date(ov)
                is_test = True
            except Exception:
                # 不正なら無視して大会日へ
                d_show = d_base
                is_test = False

        if d_show:
            _w = ["月","火","水","木","金","土","日"][d_show.weekday()]
            setting_date = f"{d_show.month}月{d_show.day}日({_w})"
            if is_test:
                setting_date += " ※テスト"
    except Exception:
        setting_date = "未設定"

    e = discord.Embed(title="🍀進捗確認＆緊急用パネル", color=ORANGE)

    # description を「パネル本体」として固定フォーマット化
    e.description = (
        "ーーーーーーーーーーーーーーーーー\n"
        "⌛現在の状況\n"
        f"第{s.match_no}試合 / {status}\n\n"
        "🔒キー＆時間\n"
        f"キー｜{key_val}\n"
        f"出発時間｜{dep_val}\n\n"
        "🌍マップ切替\n"
        f"切替時間残り｜{switch_remaining}\n"
        f"🕙キー停止⏸️{pf}～{pt}\n"
        "ーーーーーーーーーーーーーーーーーー\n"
        f"⏳未操作：{unop_txt}\n\n"
        "🔫大会情報\n"
        f"設定日｜{setting_date}\n"
        f"モード｜{mode_label}\n"
        f"試合数｜{s.match_count}\n"
        f"第1試合開始時間｜{match1}（予定）"
    )

    return e

async def delete_general_channel_posts(guild: discord.Guild) -> None:
    if not STATE.key_channel_id:
        return
    ch = guild.get_channel(STATE.key_channel_id)
    if ch is None or not isinstance(ch, (discord.TextChannel, discord.Thread)):
        return

    for mid_attr in ("last_key_image_msg_id", "last_key_embed_msg_id"):
        mid = getattr(STATE, mid_attr)
        if not mid:
            continue
        try:
            msg = await ch.fetch_message(mid)
            await msg.delete()
        except Exception:
            pass
        setattr(STATE, mid_attr, None)




async def delete_keyhost_channel_posts(guild: discord.Guild) -> None:
    if not STATE.keyhost_channel_id:
        return
    ch = guild.get_channel(STATE.keyhost_channel_id)
    if ch is None or not isinstance(ch, (discord.TextChannel, discord.Thread)):
        return

    for mid_attr in ("last_keyhost_image_msg_id", "last_keyhost_key_msg_id"):
        mid = getattr(STATE, mid_attr)
        if not mid:
            continue
        try:
            msg = await ch.fetch_message(mid)
            await msg.delete()
        except Exception:
            pass
        setattr(STATE, mid_attr, None)


async def delete_replay_request_posts(guild: discord.Guild) -> None:
    # number -> (channel_id, message_id)
    ids = getattr(STATE, "replay_request_message_ids", None) or {}
    ch_ids = getattr(STATE, "replay_request_channel_ids", None) or {}
    if not ids:
        return

    for n, mid in list(ids.items()):
        cid = ch_ids.get(n)
        if not cid:
            continue
        ch = guild.get_channel(cid)
        if ch is None or not isinstance(ch, (discord.TextChannel, discord.Thread)):
            continue
        try:
            msg = await ch.fetch_message(mid)
            await msg.delete()
        except Exception:
            pass

    # clear
    try:
        STATE.replay_request_message_ids.clear()
        STATE.replay_request_channel_ids.clear()
    except Exception:
        STATE.replay_request_message_ids = {}
        STATE.replay_request_channel_ids = {}

async def schedule_delete_after_departure() -> None:
    if not STATE.departure_time:
        return

    now = datetime.now()
    hh = int(STATE.departure_time[:2])
    mm = int(STATE.departure_time[3:])
    dep = now.replace(hour=hh, minute=mm, second=0, microsecond=0)

    if dep < now - timedelta(minutes=1):
        dep = dep + timedelta(days=1)

    delete_at = dep + timedelta(minutes=1)
    STATE.delete_at_iso = delete_at.isoformat()
    save_state(STATE)




async def silent_ack(interaction: discord.Interaction, *, thinking: bool = False) -> None:
    """Acknowledge an interaction without sending any message."""
    try:
        if interaction.response.is_done():
            return
        await interaction.response.defer(thinking=thinking)
    except Exception:
        pass

class OpsPanelView(discord.ui.View):
    """
    /keydrop_panel で設置する運営パネル（確定UI）
    Row0: 自動｜手動
    Row1: 1試合目｜2試合目｜3試合目｜4試合目（✅は移動）
    Row2: リロード用マップ残り時間｜キー配布｜リプレイデータ提出依頼
    Row3: ♻️全リセット｜🚫緊急停止中🚫（通常時は⏯️緊急停止）
    """
    def __init__(self):
        super().__init__(timeout=None)

        # 状態
        is_auto = bool(getattr(STATE, "auto_enabled", False)) and not bool(getattr(STATE, "emergency_stop", False))
        is_manual = not is_auto
        is_stop = bool(getattr(STATE, "emergency_stop", False))

        # 再起動後も死なないよう、custom_idで見た目/無効化を制御
        for item in self.children:
            cid = getattr(item, "custom_id", None)

            # mode buttons
            if cid == "mode_auto":
                item.label = "✅自動" if is_auto else "自動"
            elif cid == "mode_manual":
                item.label = "✅手動" if is_manual else "手動"

            # match buttons (✅表示を移動)
            if cid and cid.startswith("match_"):
                try:
                    n = int(cid.split("_", 1)[1])
                except Exception:
                    n = None
                if n:
                    item.label = f"✅{n}試合目" if int(getattr(STATE, "match_no", 1) or 1) == n else f"{n}試合目"

            # key distribution: auto時は無効（緊急停止中は手動扱いで有効）
            if cid == "key_drop":
                item.disabled = is_auto

            # emergency stop button: 押したら「🚫緊急停止中🚫」に切替＋押下不可
            if cid == "stop_on":
                if is_stop:
                    item.label = "🚫緊急停止中🚫"
                    item.emoji = "🚫"
                    item.disabled = True
                else:
                    item.label = "緊急停止"
                    item.emoji = "⏯️"
                    item.disabled = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return bool(interaction.user.guild_permissions.administrator)

    # --------------------------
    # Row0: mode
    # --------------------------
    @discord.ui.button(label="自動", style=discord.ButtonStyle.secondary, row=0, custom_id="mode_auto")
    async def mode_auto_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        # 自動へ：緊急停止解除＋自動再開（即時でOK）
        STATE.auto_enabled = True
        STATE.emergency_stop = False
        save_state(STATE)

        # ここで「即再開」を実現：ループの次tickを待たずにパネル更新だけ先に反映
        await update_ops_panel_guild(interaction.guild)
        await silent_ack(interaction)

    @discord.ui.button(label="手動", style=discord.ButtonStyle.secondary, row=0, custom_id="mode_manual")
    async def mode_manual_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        # 手動へ：自動を止める（緊急停止とは別）
        STATE.auto_enabled = False
        save_state(STATE)
        await update_ops_panel_guild(interaction.guild)
        await silent_ack(interaction)

    # --------------------------
    # Row1: match select (✅移動)
    # --------------------------
    @discord.ui.button(label="1試合目", style=discord.ButtonStyle.secondary, row=1, custom_id="match_1")
    async def match_1(self, interaction: discord.Interaction, button: discord.ui.Button):
        STATE.match_no = 1
        recompute_pause_window_from_state(now_jst())
        save_state(STATE)
        await update_ops_panel_guild(interaction.guild)
        await silent_ack(interaction)

    @discord.ui.button(label="2試合目", style=discord.ButtonStyle.secondary, row=1, custom_id="match_2")
    async def match_2(self, interaction: discord.Interaction, button: discord.ui.Button):
        STATE.match_no = 2
        recompute_pause_window_from_state(now_jst())
        save_state(STATE)
        await update_ops_panel_guild(interaction.guild)
        await silent_ack(interaction)

    @discord.ui.button(label="3試合目", style=discord.ButtonStyle.secondary, row=1, custom_id="match_3")
    async def match_3(self, interaction: discord.Interaction, button: discord.ui.Button):
        STATE.match_no = 3
        recompute_pause_window_from_state(now_jst())
        save_state(STATE)
        await update_ops_panel_guild(interaction.guild)
        await silent_ack(interaction)

    @discord.ui.button(label="4試合目", style=discord.ButtonStyle.secondary, row=1, custom_id="match_4")
    async def match_4(self, interaction: discord.Interaction, button: discord.ui.Button):
        STATE.match_no = 4
        recompute_pause_window_from_state(now_jst())
        save_state(STATE)
        await update_ops_panel_guild(interaction.guild)
        await silent_ack(interaction)

    # --------------------------
    # Row2: actions
    # --------------------------
    @discord.ui.button(label="リロード用マップ残り時間", style=discord.ButtonStyle.primary, row=2, custom_id="map_remaining")
    async def map_remaining(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(MapRemainingModal())

    @discord.ui.button(label="キー配布", style=discord.ButtonStyle.primary, row=2, custom_id="key_drop")
    async def key_drop(self, interaction: discord.Interaction, button: discord.ui.Button):
        # 自動中は __init__ で disabled。ここでは最終防衛だけ。
        if bool(getattr(STATE, "auto_enabled", False)) and not bool(getattr(STATE, "emergency_stop", False)):
            await silent_ack(interaction)
            return

        # 画像レンダリング等で3秒を超えることがあるため、先にdefer（Unknown interaction回避）
        try:
            await interaction.response.defer(thinking=True)
        except Exception:
            pass

        ok = False
        try:
            ok = await keyhost_notify_once(interaction.guild, reason="manual_panel")
        except Exception:
            ok = False

        await update_ops_panel_guild(interaction.guild)
        msg = "OK：キーを配布しました（キーホスト宛）。" if ok else "送信に失敗しました（送信先/権限を確認）"
        try:
            await interaction.followup.send(msg, ephemeral=True)
        except Exception:
            pass

    @discord.ui.button(label="リプレイデータ提出依頼", style=discord.ButtonStyle.primary, row=2, custom_id="replay_request")
    async def replay_request(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ReplayRequestNumbersModal(interaction.guild, STATE.match_no))


    # --------------------------
    # Row3: reset / emergency stop
    # --------------------------
    @discord.ui.button(label="全リセット", style=discord.ButtonStyle.secondary, row=3, custom_id="reset_to_start_btn", emoji="♻️")
    async def full_reset(self, interaction: discord.Interaction, button: discord.ui.Button):
        reset_to_before_match1()
        await update_ops_panel_guild(interaction.guild)
        await silent_ack(interaction)

    @discord.ui.button(label="緊急停止", style=discord.ButtonStyle.danger, row=3, custom_id="stop_on", emoji="⏯️")
    async def emergency_stop(self, interaction: discord.Interaction, button: discord.ui.Button):
        # 押したら「緊急停止中」表示へ（押下不可化は __init__ で反映される）
        STATE.emergency_stop = True
        # 緊急停止時は手動へ強制トグル
        STATE.auto_enabled = False
        save_state(STATE)
        await update_ops_panel_guild(interaction.guild)
        await silent_ack(interaction)


    # --------------------------
    # Row4: 設定日（テスト表示） override
    # --------------------------
    @discord.ui.button(label="🧪 設定日(テスト)", style=discord.ButtonStyle.secondary, row=4, custom_id="display_date_set")
    async def display_date_set(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await interaction.response.send_modal(DisplayDateSetModal())
        except Exception:
            try:
                await interaction.response.send_message("エラー：モーダルを開けませんでした。", ephemeral=True)
            except Exception:
                pass

    @discord.ui.button(label="🔄 設定日リセット", style=discord.ButtonStyle.secondary, row=4, custom_id="display_date_reset")
    async def display_date_reset(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            STATE.display_date_override = None
            save_state(STATE)
            await update_ops_panel_guild(interaction.guild)
            await interaction.response.send_message("OK：設定日を大会日に戻しました。", ephemeral=True)
        except Exception:
            try:
                await interaction.response.send_message("エラー：設定日リセットに失敗しました。", ephemeral=True)
            except Exception:
                pass



class DisplayDateSetModal(discord.ui.Modal, title="テスト設定日（表示用）"):
    date_str = discord.ui.TextInput(
        label="テストしたい日付（YYYY-MM-DD）",
        placeholder="例：2026-02-10",
        required=True,
        max_length=10,
    )

    async def on_submit(self, interaction: discord.Interaction):
        raw = str(self.date_str.value or "").strip()
        # 厳密：YYYY-MM-DD
        ok = False
        try:
            d = _parse_event_date_to_date(raw)  # 既存のパーサを流用
            ok = d is not None
        except Exception:
            ok = False

        if not ok:
            await interaction.response.send_message("エラー：YYYY-MM-DD 形式で入力してください。", ephemeral=True)
            return

        STATE.display_date_override = raw
        save_state(STATE)
        await update_ops_panel_guild(interaction.guild)
        await interaction.response.send_message(f"OK：設定日を {raw}（※テスト）に切り替えました。", ephemeral=True)


class ReplayRequestNumbersModal(discord.ui.Modal, title="提出対象番号（3桁）"):
    numbers = discord.ui.TextInput(
        label="番号（例：005,012）",
        style=discord.TextStyle.short,
        required=True,
        max_length=200,
        placeholder="005,012"
    )

    rank1 = discord.ui.TextInput(
        label="運営からの連絡：1位の番号（3桁・空欄OK）",
        style=discord.TextStyle.short,
        required=False,
        max_length=3,
        placeholder="例：005"
    )
    rank2 = discord.ui.TextInput(
        label="運営からの連絡：2位の番号（3桁・空欄OK）",
        style=discord.TextStyle.short,
        required=False,
        max_length=3,
        placeholder="例：012"
    )
    rank3 = discord.ui.TextInput(
        label="運営からの連絡：3位の番号（3桁・空欄OK）",
        style=discord.TextStyle.short,
        required=False,
        max_length=3,
        placeholder="例：027"
    )


    def __init__(self, guild: discord.Guild, match_no: int):
        super().__init__()
        self.guild = guild
        self.match_no = int(match_no or 1)

    async def on_submit(self, interaction: discord.Interaction):
        raw = str(self.numbers.value).strip()
        nums = [x.strip() for x in raw.split(",") if x.strip()]
        fixed: List[str] = []
        for x in nums:
            if re.fullmatch(r"\d{1,3}", x):
                fixed.append(f"{int(x):03d}")
        # uniq keep order
        seen = set()
        target = []
        for n in fixed:
            if n not in seen:
                seen.add(n)
                target.append(n)

        # acknowledge silently first (modal submit must respond)
        await silent_ack(interaction)

        # store rank contacts for replay-forgot escalation (blank OK)
        def _norm_rank(v: str) -> Optional[str]:
            v = str(v or "").strip()
            if not v:
                return None
            if re.fullmatch(r"\d{1,3}", v):
                return f"{int(v):03d}"
            return None

        STATE.replay_rank_match_no = self.match_no
        STATE.replay_rank1 = _norm_rank(getattr(self, "rank1", None).value if hasattr(self, "rank1") else "")
        STATE.replay_rank2 = _norm_rank(getattr(self, "rank2", None).value if hasattr(self, "rank2") else "")
        STATE.replay_rank3 = _norm_rank(getattr(self, "rank3", None).value if hasattr(self, "rank3") else "")
        STATE.replay_rank_stage = 0
        save_state(STATE)

        if not target:
            return

        sent = 0
        for n in target:
            ch = find_channel_by_number(self.guild, n)
            if isinstance(ch, discord.TextChannel):
                try:
                    msg = await ch.send(
                        f"先ほどの試合のリプレイデータをこのチャンネルに提出してください。\n"
                        f"［提出完了］［サイズ超過］［リプレイ取り忘れ］",
                        view=ReplaySubmitView(match_no=self.match_no, number=n)
                    )
                    STATE.replay_request_message_ids[n] = msg.id
                    STATE.replay_request_channel_ids[n] = ch.id
                    save_state(STATE)
                    sent += 1
                except Exception:
                    continue

        # 試合終了宣言（＝リプレイ提出依頼送信）
        try:
            nxt = min(int(STATE.match_no) + 1, int(getattr(STATE, "match_count", 4) or 4))
        except Exception:
            nxt = int(STATE.match_no) + 1
        STATE.phase = "WAIT_REPLAY_DONE"
        STATE.pending_next_match_no = nxt
        STATE.pending_keyhost_send = False
        STATE.pending_keyhost_send_at = None
        STATE.keyhost_notified_once = False
        save_state(STATE)

        await update_ops_panel_guild(self.guild)

class ReplayNumbersModal(discord.ui.Modal, title="提出対象番号（3桁）"):
    numbers = discord.ui.TextInput(
        label="番号（例：005,012）",
        style=discord.TextStyle.short,
        required=True,
        max_length=200,
        placeholder="005,012"
    )

    def __init__(self, parent_view: "ReplayRequestConfirmView"):
        super().__init__()
        self.parent_view = parent_view

    async def on_submit(self, interaction: discord.Interaction):
        raw = str(self.numbers.value).strip()
        nums = [x.strip() for x in raw.split(",") if x.strip()]
        fixed = []
        for x in nums:
            if re.fullmatch(r"\d{1,3}", x):
                fixed.append(f"{int(x):03d}")
        fixed = sorted(dict.fromkeys(fixed))
        self.parent_view.target_numbers = fixed
        await self.parent_view.refresh(interaction)


class ReplayRequestConfirmView(discord.ui.View):
    def __init__(self, guild: discord.Guild, match_no: int):
        super().__init__(timeout=180)
        self.guild = guild
        self.match_no = match_no
        self.target_numbers: List[str] = []

    def _build_embed(self) -> discord.Embed:
        e = discord.Embed(title="リプレイデータ提出依頼", color=0x2f3136)
        if not self.target_numbers:
            dest = "未指定"
            missing = ""
        else:
            chs = []
            missing_nums = []
            for n in self.target_numbers:
                ch = find_channel_by_number(self.guild, n)
                if isinstance(ch, (discord.TextChannel, discord.Thread)):
                    chs.append(ch.mention)
                else:
                    missing_nums.append(n)
            dest = " ".join(chs) if chs else "（なし）"
            missing = f"見つからない：{','.join(missing_nums)}" if missing_nums else ""

        e.description = f"送信先：{dest}"
        e.add_field(name="対象番号", value=",".join(self.target_numbers) if self.target_numbers else "未指定", inline=False)
        if missing:
            e.add_field(name="注意", value=missing, inline=False)
        e.set_footer(text=f"第{self.match_no}試合")
        return e

    async def refresh(self, interaction: discord.Interaction):
        self.send_btn.disabled = (len(self.target_numbers) == 0)
        await interaction.response.edit_message(embed=self._build_embed(), view=self)

    @discord.ui.button(label="番号入力", style=discord.ButtonStyle.primary, row=0)
    async def input_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ReplayNumbersModal(self))

    @discord.ui.button(label="送信", style=discord.ButtonStyle.success, row=0)
    async def send_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.target_numbers:
            await interaction.response.send_message("番号が未指定です。")
            return

        sent = 0
        for n in self.target_numbers:
            ch = find_channel_by_number(self.guild, n)
            if isinstance(ch, discord.TextChannel):
                try:
                    msg = await ch.send(
                        f"先ほどの試合のリプレイデータをこのチャンネルに提出してください。\n"
                        f"［提出完了］［サイズ超過］［リプレイ取り忘れ］",
                        view=ReplaySubmitView(match_no=self.match_no, number=n)
                    )
                    # 出発時間後に削除するため保持
                    STATE.replay_request_message_ids[n] = msg.id
                    STATE.replay_request_channel_ids[n] = ch.id
                    save_state(STATE)
                    sent += 1
                except Exception:
                    continue
        # 試合終了宣言（＝リプレイ提出依頼送信）
        try:
            nxt = min(int(STATE.match_no) + 1, int(getattr(STATE, "match_count", 4) or 4))
        except Exception:
            nxt = int(STATE.match_no) + 1
        STATE.phase = "WAIT_REPLAY_DONE"
        STATE.pending_next_match_no = nxt
        STATE.pending_keyhost_send = False
        STATE.pending_keyhost_send_at = None
        STATE.keyhost_notified_once = False
        save_state(STATE)

        await interaction.response.edit_message(embed=self._build_embed(), view=None)
        await interaction.followup.send(f"OK：送信しました（{sent}件）。")

    @discord.ui.button(label="キャンセル", style=discord.ButtonStyle.secondary, row=0)
    async def cancel_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="キャンセルしました。", embed=None, view=None)



def _get_role_mention_by_name(guild: discord.Guild, role_name: str) -> str:
    """Return a role mention like <@&id> if role exists, else a plain '@name'."""
    try:
        for r in getattr(guild, "roles", []) or []:
            if getattr(r, "name", None) == role_name:
                return r.mention
    except Exception:
        pass
    return f"@{role_name}"


def _is_ops_user(member: discord.Member) -> bool:
    try:
        if member.guild_permissions.administrator:
            return True
    except Exception:
        pass
    try:
        for r in getattr(member, "roles", []) or []:
            if getattr(r, "name", None) == "運営":
                return True
    except Exception:
        pass
    return False


async def _send_ops_notify(guild: discord.Guild, content: str) -> None:
    """Send a message to the fixed ops channel for replay notifications."""
    try:
        ch = guild.get_channel(REPLAY_OPS_CHANNEL_ID)
        if ch is None:
            ch = await bot.fetch_channel(REPLAY_OPS_CHANNEL_ID)
        if isinstance(ch, (discord.TextChannel, discord.Thread)):
            await ch.send(content)
    except Exception:
        pass


async def _send_ops_notify_view(guild: discord.Guild, content: str, *, embed: Optional[discord.Embed] = None, view: Optional[discord.ui.View] = None) -> None:
    """Send a message with view to the fixed ops channel."""
    try:
        ch = guild.get_channel(REPLAY_OPS_CHANNEL_ID)
        if ch is None:
            ch = await bot.fetch_channel(REPLAY_OPS_CHANNEL_ID)
        if isinstance(ch, (discord.TextChannel, discord.Thread)):
            await ch.send(content, embed=embed, view=view)
    except Exception:
        pass


async def _send_message_to_number_channel(guild: discord.Guild, number: str, content: str) -> bool:
    ch = find_channel_by_number(guild, number)
    if isinstance(ch, discord.TextChannel):
        try:
            await ch.send(content)
            return True
        except Exception:
            return False
    return False


class ReplayPlacementsModal(discord.ui.Modal, title="運営からの連絡（順位入力）"):
    first = discord.ui.TextInput(
        label="1位の番号（3桁）",
        style=discord.TextStyle.short,
        required=False,
        max_length=3,
        placeholder="例：005（空欄OK）",
    )
    second = discord.ui.TextInput(
        label="2位の番号（3桁）",
        style=discord.TextStyle.short,
        required=False,
        max_length=3,
        placeholder="例：012（空欄OK）",
    )
    third = discord.ui.TextInput(
        label="3位の番号（3桁）",
        style=discord.TextStyle.short,
        required=False,
        max_length=3,
        placeholder="例：033（空欄OK）",
    )

    def __init__(self, parent_view: "ReplayForgotOpsView"):
        super().__init__()
        self.parent_view = parent_view

    @staticmethod
    def _norm(v: str) -> Optional[str]:
        v = (v or "").strip()
        if not v:
            return None
        if re.fullmatch(r"\d{1,3}", v):
            return f"{int(v):03d}"
        return None

    async def on_submit(self, interaction: discord.Interaction):
        # ops only
        if not isinstance(interaction.user, discord.Member) or not _is_ops_user(interaction.user):
            await silent_ack(interaction)
            return

        self.parent_view.first_no = self._norm(str(self.first.value))
        self.parent_view.second_no = self._norm(str(self.second.value))
        self.parent_view.third_no = self._norm(str(self.third.value))

        await self.parent_view.refresh(interaction)


class ReplayForgotOpsView(discord.ui.View):
    """Ops-side controller for replay-forgot escalation: 1st -> 2nd -> 3rd."""

    def __init__(self, guild: discord.Guild, match_no: int, reporter_number: str):
        super().__init__(timeout=1800)
        self.guild = guild
        self.match_no = int(match_no or 1)
        self.reporter_number = reporter_number
        self.first_no: Optional[str] = None
        self.second_no: Optional[str] = None
        self.third_no: Optional[str] = None

        # initial state
        self.send_2nd.disabled = True
        self.send_3rd.disabled = True

    def _build_embed(self) -> discord.Embed:
        e = discord.Embed(title="リプレイ取り忘れ対応（順位入力）", color=0x7a1f2b)
        e.description = (
            f"報告：{self.reporter_number}\n"
            f"対象：第{self.match_no}試合\n\n"
            "順位（空欄OK）\n"
            f"1位：{self.first_no or '未入力'}\n"
            f"2位：{self.second_no or '未入力'}\n"
            f"3位：{self.third_no or '未入力'}\n"
        )
        return e

    async def refresh(self, interaction: discord.Interaction):
        # enable escalation buttons only if number exists
        self.send_2nd.disabled = (self.second_no is None)
        self.send_3rd.disabled = (self.third_no is None)
        await interaction.response.edit_message(embed=self._build_embed(), view=self)

        # Send to 1st immediately after placements are entered (if present)
        if self.first_no:
            ops_mention = _get_role_mention_by_name(self.guild, "運営")
            await _send_message_to_number_channel(
                self.guild,
                self.first_no,
                f"{ops_mention}\n運営からの連絡：第{self.match_no}試合のリプレイデータを提出してください。"
            )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return bool(isinstance(interaction.user, discord.Member) and _is_ops_user(interaction.user))

    @discord.ui.button(label="順位入力", style=discord.ButtonStyle.primary, row=0)
    async def input_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ReplayPlacementsModal(self))

    @discord.ui.button(label="2位へ送る", style=discord.ButtonStyle.secondary, row=0)
    async def send_2nd(self, interaction: discord.Interaction, button: discord.ui.Button):
        await silent_ack(interaction)
        if not self.second_no:
            return
        ops_mention = _get_role_mention_by_name(self.guild, "運営")
        ok = await _send_message_to_number_channel(
            self.guild,
            self.second_no,
            f"{ops_mention}\n運営からの連絡：第{self.match_no}試合のリプレイデータを提出してください。"
        )
        # disable after sending to prevent spam
        self.send_2nd.disabled = True
        try:
            await interaction.message.edit(embed=self._build_embed(), view=self)
        except Exception:
            pass

    @discord.ui.button(label="3位へ送る", style=discord.ButtonStyle.secondary, row=0)
    async def send_3rd(self, interaction: discord.Interaction, button: discord.ui.Button):
        await silent_ack(interaction)
        if not self.third_no:
            return
        ops_mention = _get_role_mention_by_name(self.guild, "運営")
        ok = await _send_message_to_number_channel(
            self.guild,
            self.third_no,
            f"{ops_mention}\n運営からの連絡：第{self.match_no}試合のリプレイデータを提出してください。"
        )
        self.send_3rd.disabled = True
        try:
            await interaction.message.edit(embed=self._build_embed(), view=self)
        except Exception:
            pass


class ReplaySubmitView(discord.ui.View):
    def __init__(self, match_no: int, number: str):
        super().__init__(timeout=None)
        self.match_no = match_no
        self.number = number

    async def _notify_ops(self, guild: discord.Guild, text: str) -> None:
        ops_mention = _get_role_mention_by_name(guild, "運営")
        await _send_ops_notify(guild, f"{ops_mention}\n{text}")

    async def _after_submit_common(self, interaction: discord.Interaction):
        """Existing next-match trigger logic (kept as-is from previous implementation)."""
        # Next match keyhost distribution trigger
        nxt = getattr(STATE, "pending_next_match_no", None)
        if nxt is not None:
            now = now_jst()
            if is_in_pause_window(now):
                STATE.pending_keyhost_send = True
                STATE.pending_keyhost_send_at = STATE.key_pause_to
                save_state(STATE)
                if STATE.key_pause_to:
                    await send_to_key_channel(
                        interaction.guild,
                        f"マップ切替時間と重なるため、キー配布時間を調整中です（{STATE.key_pause_to}予定）"
                    )
            else:
                try:
                    STATE.match_no = int(nxt)
                except Exception:
                    STATE.match_no = nxt
                save_state(STATE)
                try:
                    await keyhost_notify_once(interaction.guild, reason="replay_done_trigger")
                except Exception:
                    pass

    @discord.ui.button(label="提出完了", style=discord.ButtonStyle.primary, custom_id="replay_submit_done")
    async def done(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            if not interaction.response.is_done():
                await interaction.response.defer()
        except Exception:
            pass

        await self._notify_ops(interaction.guild, f"第{self.match_no}試合 {self.number} 提出完了")
        # match2 special: give 5 min break then deliver match3 key to keyhost
        if int(self.match_no) == 2:
            try:
                await schedule_match3_break_after_match2_replay(interaction.guild)
            except Exception:
                pass
            return




        await self._after_submit_common(interaction)

    @discord.ui.button(label="サイズ超過", style=discord.ButtonStyle.secondary, custom_id="replay_submit_size_over")
    async def size_over(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            if not interaction.response.is_done():
                await interaction.response.defer()
        except Exception:
            pass

        await self._notify_ops(interaction.guild, f"第{self.match_no}試合 {self.number} サイズ超過（提出不可）")

    @discord.ui.button(label="リプレイ取り忘れ", style=discord.ButtonStyle.danger, custom_id="replay_submit_forgot")
    async def replay_forgot(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            if not interaction.response.is_done():
                await interaction.response.defer()
        except Exception:
            pass

        # Escalation flow: each press contacts next rank (1st -> 2nd -> 3rd), only when configured.
        # Ranks are set when ops sends replay-request modal (blank OK).
        if getattr(STATE, "replay_rank_match_no", None) != int(self.match_no):
            # safety: reset stage when match differs
            STATE.replay_rank_match_no = int(self.match_no)
            STATE.replay_rank_stage = 0
            save_state(STATE)

        stage = int(getattr(STATE, "replay_rank_stage", 0) or 0)
        r1 = getattr(STATE, "replay_rank1", None)
        r2 = getattr(STATE, "replay_rank2", None)
        r3 = getattr(STATE, "replay_rank3", None)

        target_rank = None
        target_number = None
        if stage <= 0:
            target_rank, target_number = "1位", r1
        elif stage == 1:
            target_rank, target_number = "2位", r2
        elif stage == 2:
            target_rank, target_number = "3位", r3
        else:
            target_rank, target_number = "完了", None

        ops_mention = _get_role_mention_by_name(interaction.guild, "運営")

        if target_number:
            ch = find_channel_by_number(interaction.guild, target_number)
            if isinstance(ch, discord.TextChannel):
                try:
                    await ch.send(
                        "運営からの連絡"
                        f"第{self.match_no}試合のリプレイデータ提出のご協力をお願いします。"
                        f"（対象：{target_rank} {target_number}）"
                    )
                    # advance stage only when we actually sent
                    STATE.replay_rank_stage = min(stage + 1, 3)
                    save_state(STATE)
                    await _send_ops_notify(
                        interaction.guild,
                        f"{ops_mention}\n第{self.match_no}試合 {self.number}：リプレイ取り忘れ → {target_rank}（{target_number}）へ連絡しました。"
                    )
                    return
                except Exception:
                    pass

            # channel not found / send failed
            await _send_ops_notify(
                interaction.guild,
                f"{ops_mention}\n第{self.match_no}試合 {self.number}：リプレイ取り忘れ → {target_rank}（{target_number}）へ連絡できません（チャンネル未検出/送信失敗）。"
            )
            return

        # not configured for this stage (blank)
        await _send_ops_notify(
            interaction.guild,
            f"{ops_mention}\n第{self.match_no}試合 {self.number}：リプレイ取り忘れ → {target_rank} の番号が未設定です（空欄）。"
        )


class MapRemainingModal(discord.ui.Modal, title="マップ切替 残り時間（分）"):
    remaining = discord.ui.TextInput(
        label="残り時間（分）",
        style=discord.TextStyle.short,
        required=True,
        max_length=4,
        placeholder="例：12"
    )

    async def on_submit(self, interaction: discord.Interaction):
        # モーダル送信は3秒制限が厳しいので、先にACKしてから処理する（KEY DROP が考え中...対策）
        await silent_ack(interaction)

        raw = str(self.remaining.value).strip()
        try:
            m = int(raw)
        except Exception:
            return

        apply_map_remaining_minutes(now_jst(), m)

        # パネル更新（guild 優先）
        try:
            if interaction.guild is not None:
                await update_ops_panel_guild(interaction.guild)
            else:
                await update_ops_panel_guild(interaction.guild)
        except Exception:
            pass



class Match1StartModal(discord.ui.Modal, title="第1試合開始時間（HH:MM）"):
    hhmm = discord.ui.TextInput(
        label="第1試合開始時間（例：22:15）",
        style=discord.TextStyle.short,
        required=True,
        max_length=5,
        placeholder="22:15"
    )

    async def on_submit(self, interaction: discord.Interaction):
        v = parse_hhmm_str(str(self.hhmm.value))
        if not v:
            await silent_ack(interaction)
            return
        STATE.match1_start = v
        save_state(STATE)
        await update_ops_panel_guild(interaction.guild)
        await silent_ack(interaction)



class CheckinView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    def _resolve_number(self, interaction: discord.Interaction) -> Optional[str]:
        m = re.match(r"^(\d{3})", interaction.channel.name if interaction.channel else "")
        return m.group(1) if m else None

    def _mark_only(self, num: str, kind: str) -> None:
        # kind: checkin | decline | forfeit
        num = str(num).zfill(3)
        # remove from all
        try:
            STATE.checked_in_numbers = [x for x in (STATE.checked_in_numbers or []) if x != num]
            STATE.declined_numbers = [x for x in (STATE.declined_numbers or []) if x != num]
            STATE.forfeit_numbers = [x for x in (STATE.forfeit_numbers or []) if x != num]
        except Exception:
            pass

        if kind == "checkin":
            STATE.checked_in_numbers.append(num)
            STATE.checked_in_numbers = sorted(set(STATE.checked_in_numbers))
        elif kind == "decline":
            STATE.declined_numbers.append(num)
            STATE.declined_numbers = sorted(set(STATE.declined_numbers))
        elif kind == "forfeit":
            STATE.forfeit_numbers.append(num)
            STATE.forfeit_numbers = sorted(set(STATE.forfeit_numbers))

        save_state(STATE)

    def _apply_button_state(self, pressed: discord.ui.Button, all_buttons: List[discord.ui.Button], mode: str) -> None:
        # mode:
        # - lock_all: 押したボタン以外を無効化
        # - lock_self: 押したボタンのみ無効化
        for b in all_buttons:
            if b is pressed:
                if not (b.label or "").startswith("✅"):
                    b.label = f"✅{b.label}"
                if mode in ("lock_all", "lock_self"):
                    b.disabled = True if mode == "lock_self" else False  # lock_all は押したボタンは無効化しない
            else:
                if mode == "lock_all":
                    b.disabled = True

    @discord.ui.button(label="チェックイン", style=discord.ButtonStyle.success, custom_id="checkin:checkin")
    async def btn_checkin(self, interaction: discord.Interaction, button: discord.ui.Button):
        num = self._resolve_number(interaction)
        if not num:
            await interaction.response.send_message("このチャンネルでは操作できません。", ephemeral=True)
            return
        self._mark_only(num, "checkin")

        # ✅付与 + 他ボタン無効化 + 自分は無効化しない
        buttons = [c for c in self.children if isinstance(c, discord.ui.Button)]
        self._apply_button_state(button, buttons, mode="lock_all")
        try:
            await interaction.response.edit_message(view=self)
        except Exception:
            try:
                await interaction.response.send_message("チェックインしました。", ephemeral=True)
            except Exception:
                pass

    @discord.ui.button(label="参加辞退", style=discord.ButtonStyle.danger, custom_id="checkin:decline")
    async def btn_decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        num = self._resolve_number(interaction)
        if not num:
            await interaction.response.send_message("このチャンネルでは操作できません。", ephemeral=True)
            return
        self._mark_only(num, "decline")

        # ✅付与 + 他ボタン無効化 + 自分は無効化しない
        buttons = [c for c in self.children if isinstance(c, discord.ui.Button)]
        self._apply_button_state(button, buttons, mode="lock_all")
        try:
            await interaction.response.edit_message(view=self)
        except Exception:
            try:
                await interaction.response.send_message("参加辞退にしました。", ephemeral=True)
            except Exception:
                pass

    @discord.ui.button(label="第1試合棄権", style=discord.ButtonStyle.secondary, custom_id="checkin:forfeit")
    async def btn_forfeit(self, interaction: discord.Interaction, button: discord.ui.Button):
        num = self._resolve_number(interaction)
        if not num:
            await interaction.response.send_message("このチャンネルでは操作できません。", ephemeral=True)
            return
        self._mark_only(num, "forfeit")

        # ✅付与 + 当該ボタンのみ無効化（他は無効化しない）
        buttons = [c for c in self.children if isinstance(c, discord.ui.Button)]
        self._apply_button_state(button, buttons, mode="lock_self")
        try:
            await interaction.response.edit_message(view=self)
        except Exception:
            try:
                await interaction.response.send_message("第1試合棄権にしました。", ephemeral=True)
            except Exception:
                pass

class DebugCheckinView(discord.ui.View):
    """Debug-only view. Does NOT modify STATE."""
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="チェックイン", style=discord.ButtonStyle.success, custom_id="debug:checkin")
    async def checkin_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("OK：チェックイン（確認用）を押しました。")


class DebugReplayDoneView(discord.ui.View):
    """Debug-only view. Does NOT modify STATE."""
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="提出完了", style=discord.ButtonStyle.primary, custom_id="debug:replay_done")
    async def done(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("OK：提出完了（確認用）を押しました。")


class KeyhostView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="待機列完成", style=discord.ButtonStyle.success, custom_id="queue_ready")
    async def queue_ready(self, interaction: discord.Interaction, button: discord.ui.Button):
        if STATE.emergency_stop:
            await interaction.response.send_message("🚨 緊急停止中です。解除されるまで操作できません。")
            return
        if not STATE.custom_key:
            await interaction.response.send_message("まだキーが生成されていません。")
            return
        if not STATE.key_channel_id:
            await interaction.response.send_message("先に /set_key_target を設定してね（一般通知先）。")
            return

        # 先にACK（アプリの「考え中...」を残さない）
        try:
            await interaction.response.defer(thinking=False)
        except Exception:
            pass

        dep_candidate = now_jst() + timedelta(minutes=2)
        # 仕様：確定が早い場合は予定を採用（前倒ししない）
        dep = dep_candidate
        if STATE.planned_departure:
            try:
                planned_dt = parse_hhmm(str(STATE.planned_departure), dep_candidate)
                if planned_dt > dep_candidate:
                    dep = planned_dt
            except Exception:
                pass

        STATE.departure_time = hhmm(dep)
        STATE.phase = "DEPART_CONFIRMED"
        save_state(STATE)

        # ボタン連打/再操作防止：押されたメッセージのボタンを外す
        try:
            await interaction.message.edit(view=None)
        except Exception:
            pass

        await schedule_delete_after_departure()

        guild = interaction.guild
        assert guild is not None

        # ---------- 一般向け（キー配布チャンネル）：確定画像 or フォールバック ----------
        key_ch = guild.get_channel(STATE.key_channel_id)
        if not isinstance(key_ch, (discord.TextChannel, discord.Thread)):
            try:
                await interaction.followup.send("キー配布チャンネルIDが不正です。/set_key_target をやり直して。", ephemeral=True)
            except Exception:
                pass
            return

        imgB = None
        errB = None
        try:
            # 画像生成がハングした場合の保険（Playwright起動など）
            imgB = await asyncio.wait_for(
                try_render_png(
                    STATE.match_no,
                    STATE.custom_key,
                    "出発時間",
                    STATE.departure_time,
                    None,
                    variant="general",
                    planned_time=STATE.planned_departure,
                ),
                timeout=25,
            )
        except Exception as e:
            errB = e
            imgB = None

        if imgB:
            try:
                msg_general = await key_ch.send(file=discord.File(str(imgB)))
            except Exception as e:
                errB = e
                msg_general = await key_ch.send(f"【画像送信失敗】出発時間: {STATE.departure_time}")
        else:
            msg_general = await key_ch.send(f"【画像生成失敗】出発時間: {STATE.departure_time}")

        STATE.last_key_image_msg_id = getattr(msg_general, "id", None)
        save_state(STATE)

        if errB and "_send_ops_notify" in globals():
            try:
                await _send_ops_notify(guild, f"⚠ 画像生成/送信に失敗しました（一般向け）: {type(errB).__name__}: {errB}")
            except Exception:
                pass

        # ---------- キーホスト向け：確定画像 or embed フォールバック ----------
        kh_ch = None
        if STATE.keyhost_channel_id:
            kh_ch = guild.get_channel(STATE.keyhost_channel_id)

        imgA = None
        errA = None
        if isinstance(kh_ch, (discord.TextChannel, discord.Thread)):
            try:
                imgA = await asyncio.wait_for(
                    try_render_png(
                        STATE.match_no,
                        STATE.custom_key,
                        "出発時間",
                        STATE.departure_time,
                        None,
                        variant="keyhost_confirmed",
                        planned_time=STATE.planned_departure,
                    ),
                    timeout=25,
                )
            except Exception as e:
                errA = e
                imgA = None

            if imgA:
                try:
                    edited = False
                    # 既存の「キーホスト向け画像（予定）」メッセージを差し替える（これが要件）
                    if STATE.last_keyhost_image_msg_id:
                        try:
                            target_msg = await kh_ch.fetch_message(int(STATE.last_keyhost_image_msg_id))
                            try:
                                # discord.py のバージョン差分対策（files / file）
                                await target_msg.edit(attachments=[], files=[discord.File(str(imgA))])
                            except TypeError:
                                await target_msg.edit(attachments=[], file=discord.File(str(imgA)))
                            edited = True
                        except Exception:
                            edited = False

                    # 取れなかった/編集できなかった場合は新規送信にフォールバック
                    if not edited:
                        msg = await kh_ch.send(file=discord.File(str(imgA)))
                        STATE.last_keyhost_image_msg_id = getattr(msg, "id", None)
                    save_state(STATE)
                except Exception as e:
                    errA = e
                    try:
                        await kh_ch.send(embed=_make_time_embed(STATE.departure_time))
                    except Exception:
                        pass
            else:
                try:
                    await kh_ch.send(embed=_make_time_embed(STATE.departure_time))
                except Exception:
                    pass

            if errA and "_send_ops_notify" in globals():
                try:
                    await _send_ops_notify(guild, f"⚠ 画像生成/送信に失敗しました（キーホスト）: {type(errA).__name__}: {errA}")
                except Exception:
                    pass

        # ---------- パネル更新 ----------
        try:
            await update_ops_panel_guild(guild)
        except Exception:
            pass

        # ---------- 最後にinteractionを完了（チャンネルには出さない） ----------
        try:
            await interaction.followup.send("OK", ephemeral=True)
        except Exception:
            pass
async def post_ops_panel(interaction: discord.Interaction) -> None:
    """/keydrop_panel の設置（新規投稿を最小化して、'使用しました' を出さない運用用）。
    - 既存パネルがあれば edit
    - 無ければ、そのチャンネルに bot が通常送信で1回だけ作成
    """
    guild = interaction.guild
    ch = interaction.channel
    if guild is None or ch is None or not isinstance(ch, (discord.TextChannel, discord.Thread)):
        return

    # 既存パネルがあれば上書き
    if STATE.ops_panel_channel_id and STATE.ops_panel_message_id:
        try:
            ch2 = guild.get_channel(STATE.ops_panel_channel_id) or await bot.fetch_channel(STATE.ops_panel_channel_id)
        except Exception:
            ch2 = None
        if isinstance(ch2, (discord.TextChannel, discord.Thread)):
            try:
                msg = await ch2.fetch_message(STATE.ops_panel_message_id)
                await msg.edit(embed=build_ops_embed(), view=OpsPanelView())
                return
            except Exception:
                pass

    # 無ければ新規作成（interaction.response は使わない）
    try:
        msg = await ch.send(embed=build_ops_embed(), view=OpsPanelView())
        STATE.ops_panel_channel_id = msg.channel.id
        STATE.ops_panel_message_id = msg.id
        save_state(STATE)
    except Exception:
        pass


async def update_ops_panel(interaction: discord.Interaction) -> None:
    if not STATE.ops_panel_channel_id or not STATE.ops_panel_message_id:
        return
    guild = interaction.guild
    if guild is None:
        return
    ch = guild.get_channel(STATE.ops_panel_channel_id)
    if ch is None or not isinstance(ch, (discord.TextChannel, discord.Thread)):
        return
    try:
        msg = await ch.fetch_message(STATE.ops_panel_message_id)
        await msg.edit(embed=build_ops_embed(), view=OpsPanelView())
    except Exception:
        pass


# @bot.tree.command(name="debug_auto_once", description="【デバッグ】自動化判定を1回だけ即時実行（大会日チェック無視）")
async def debug_auto_once(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("権限がありません。")
        return
    await interaction.response.defer()
    try:
        await automation_tick_once(force=True)
        await interaction.followup.send("OK：自動化判定を1回実行しました。")
    except Exception as e:
        await interaction.followup.send(f"失敗しました: {e}")


# @bot.tree.command(name="debug_keyhost_send", description="【デバッグ】キーホスト通知を即時実行")
async def debug_keyhost_send(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("権限がありません。")
        return
    await interaction.response.defer()
    try:
        if "keyhost_notify_once" in globals():
            await keyhost_notify_once(interaction.guild, reason="debug")
            await interaction.followup.send("OK：キーホスト通知を送信しました。")
        else:
            await interaction.followup.send("keyhost_notify_once が未定義です。")
    except Exception as e:
        await interaction.followup.send(f"失敗しました: {e}")

@bot.tree.command(name="keydrop_panel", description="OR40 運営パネルを設置します（中核）")
@app_commands.checks.has_permissions(administrator=True)
async def keydrop_panel(interaction: discord.Interaction):
    # コマンド使用ログ（「◯◯が /keydrop_panel を使用しました」）を出さないため、まずephemeralでdefer。
    try:
        await interaction.response.defer(ephemeral=True)
    except Exception:
        try:
            await interaction.response.defer()
        except Exception:
            pass

    try:
        await post_ops_panel(interaction)
        # 「考え中…」を残さないため、必ず応答を返す
        try:
            await interaction.followup.send("OK：運営パネルを設置しました。", ephemeral=True)
        except Exception:
            pass
    except Exception:
        try:
            await interaction.followup.send("エラー：運営パネルの設置に失敗しました。", ephemeral=True)
        except Exception:
            pass



# @bot.tree.command(name="set_key_channel", description="キー配布チャンネルを設定（一般参加者が見る）")
# @app_commands.checks.has_permissions(administrator=True)
async def set_key_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    STATE.key_channel_id = channel.id
    save_state(STATE)
    await interaction.response.send_message(f"OK：キー配布チャンネルを {channel.mention} に設定しました。")


# @bot.tree.command(name="set_keyhost_channel", description="キーホスト用チャンネルを設定")
# @app_commands.checks.has_permissions(administrator=True)
async def set_keyhost_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    STATE.keyhost_channel_id = channel.id
    save_state(STATE)
    await interaction.response.send_message(f"OK：キーホスト用チャンネルを {channel.mention} に設定しました。")





@bot.tree.command(name="set_key_target", description="【設定】キー配布（一般参加者）送信先を『この場所』に設定")
@app_commands.checks.has_permissions(administrator=True)
async def set_key_target(interaction: discord.Interaction):
    if interaction.channel_id is None:
        await interaction.response.send_message("この場所を取得できませんでした。")
        return
    STATE.key_channel_id = int(interaction.channel_id)
    save_state(STATE)
    await interaction.response.send_message("OK：キー配布先を『ここ』に設定しました。")
@bot.tree.command(name="set_keyhost_target", description="キーホストへの送信先を『この場所』に設定（チャンネル/スレッド両対応）")
@app_commands.checks.has_permissions(administrator=True)
async def set_keyhost_target(interaction: discord.Interaction):
    if interaction.channel_id is None:
        await interaction.response.send_message("この場所を取得できませんでした。")
        return
    STATE.keyhost_channel_id = interaction.channel_id
    save_state(STATE)
    await interaction.response.send_message("OK：キーホスト送信先を『ここ』に設定しました。")


# @bot.tree.command(name="set_commentary_channel", description="実況解説の送信先チャンネルを設定（チャンネル指定）")
# @app_commands.checks.has_permissions(administrator=True)
async def set_commentary_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    STATE.commentary_channel_id = channel.id
    save_state(STATE)
    await interaction.response.send_message(f"OK：実況解説の送信先を {channel.mention} に設定しました。")


@bot.tree.command(name="set_commentary_target", description="実況解説の送信先を『この場所』に設定（チャンネル/スレッド両対応）")
@app_commands.checks.has_permissions(administrator=True)
async def set_commentary_target(interaction: discord.Interaction):
    if interaction.channel_id is None:
        await interaction.response.send_message("この場所を取得できませんでした。")
        return
    STATE.commentary_channel_id = interaction.channel_id
    save_state(STATE)
    await interaction.response.send_message("OK：実況解説の送信先を『ここ』に設定しました。")
# @bot.tree.command(name="debug_state", description="【動作確認】現在のSTATEを表示")
# @app_commands.checks.has_permissions(administrator=True)
async def debug_state(interaction: discord.Interaction):
    items = []
    for k, v in STATE.__dict__.items():
        items.append(f"{k}: {v}")
    text = "\n".join(items) if items else "(empty)"
    await interaction.response.send_message(f"```\n{text}\n```")


async def prep_and_send(interaction: discord.Interaction) -> None:
    """旧デバッグコマンド互換：キーホスト通知を強制送信する。"""
    if interaction.guild is None:
        return
    if "keyhost_notify_once" in globals():
        await keyhost_notify_once(interaction.guild, reason="debug_send_keyhost")
    else:
        raise NameError("keyhost_notify_once is not defined")


# @bot.tree.command(name="debug_render", description="【デバッグ】画像レンダリングをこの場でテスト")
# @app_commands.checks.has_permissions(administrator=True)
async def debug_render(interaction: discord.Interaction, variant: str = "general"):
    await interaction.response.defer()

    match_no = int(getattr(STATE, "match_no", 1) or 1)
    key = str(getattr(STATE, "custom_key", "") or "OR400000")
    planned = str(getattr(STATE, "planned_departure", "") or "00:00")
    confirmed = str(getattr(STATE, "departure_time", "") or planned or "00:00")

    try:
        if variant in ("keyhost_planned", "keyhost_confirmed"):
            time_value = planned if variant == "keyhost_planned" else confirmed
            p = await try_render_png(
                match_no,
                key,
                "出発時間",
                time_value,
                "debug",
                variant=variant,
                planned_time=planned)
        else:
            p = await try_render_png(
                match_no,
                key,
                "出発時間",
                confirmed,
                "debug",
                variant="general",
                planned_time=planned)
    except Exception as e:
        await interaction.followup.send(f"失敗: {e}")
        return

    if p:
        await interaction.followup.send("OK：render 成功", file=discord.File(p))
    else:
        err = globals().get("LAST_RENDER_ERR")
        await interaction.followup.send(f"render 失敗（画像が作れません）\nLAST_RENDER_ERR={err}")


# @bot.tree.command(name="debug_send_keyhost", description="【動作確認】キーホストへキー通知を強制送信")
# @app_commands.checks.has_permissions(administrator=True)
async def debug_send_keyhost(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    ok = False
    try:
        ok = await keyhost_notify_once(interaction.guild, reason="manual_debug")
    except Exception:
        ok = False
    await interaction.followup.send("OK：キーホストへ送信しました。" if ok else "失敗：送信できませんでした（送信先/権限/状態を確認）")


# @bot.tree.command(name="debug_caster", description="【動作確認】実況解説チャンネルへテスト通知")
# @app_commands.checks.has_permissions(administrator=True)
async def debug_caster(interaction: discord.Interaction, text: str = "テスト通知"):
    await interaction.response.defer()
    target_id = getattr(STATE, "commentary_channel_id", None)
    if not target_id:
        await interaction.followup.send("実況解説送信先が未設定です。")
        return
    ch = interaction.guild.get_channel(target_id)
    if ch is None:
        try:
            ch = await interaction.client.fetch_channel(target_id)
        except Exception:
            ch = None
    if ch:
        await ch.send(text)
        await interaction.followup.send("OK：実況解説に送信しました。")
    else:
        await interaction.followup.send("送信先が見つかりません。")


# @bot.tree.command(name="reset_to_start", description="【運営】全リセット（1試合目開始前に戻す）")
# @app_commands.checks.has_permissions(administrator=True)
async def reset_to_start(interaction: discord.Interaction):
    await interaction.response.defer()
    reset_to_before_match1()
    # update panel if possible
    if interaction.guild:
        await update_ops_panel_guild(interaction.guild)
    await interaction.followup.send("OK：1試合目開始前に戻しました。")

# @bot.tree.command(name="set_tournament", description="大会設定を上書き（例外対応）")
# @app_commands.checks.has_permissions(administrator=True)
async def set_tournament(
    interaction: discord.Interaction,
    mode: Optional[str] = None,
    match_count: Optional[int] = None,
    match1_start: Optional[str] = None
):
    if mode:
        m = mode.strip().lower()
        if m not in ("reload", "tournament"):
            await interaction.response.send_message("mode は reload / tournament のどちらか。")
            return
        STATE.mode = m

    if match_count is not None:
        if match_count < 1 or match_count > 20:
            await interaction.response.send_message("match_count は 1〜20 にして。")
            return
        STATE.match_count = int(match_count)
        if STATE.match_no > STATE.match_count:
            STATE.match_no = STATE.match_count

    if match1_start:
        v = parse_hhmm_str(match1_start)
        if not v:
            await interaction.response.send_message("match1_start は HH:MM（例 22:15）で。")
            return
        STATE.match1_start = v

    save_state(STATE)
    await interaction.response.send_message("OK：大会設定を更新しました。")
    await update_ops_panel_guild(interaction.guild)


# @bot.tree.command(name="mark_checkin", description="【運営】チェックイン済みに番号を追加（デバッグ/補正用）")
# @app_commands.checks.has_permissions(administrator=True)
async def mark_checkin(interaction: discord.Interaction, number: str):
    n = str(number).strip()
    if not re.fullmatch(r"\d{3}", n):
        await interaction.response.send_message("番号は3桁（例: 001）")
        return
    if n not in STATE.checked_in_numbers:
        STATE.checked_in_numbers.append(n)
        STATE.checked_in_numbers = sorted(set(STATE.checked_in_numbers))
        save_state(STATE)
    await update_ops_panel_guild(interaction.guild)
    await interaction.response.send_message(f"OK：{n} をチェックイン済みに追加しました。")

# @bot.tree.command(name="set_map_remaining", description="【運営】リロード用マップ切替の残り時間（分）を入力して停止時間帯を算出")
# @app_commands.checks.has_permissions(administrator=True)
async def set_map_remaining(interaction: discord.Interaction, minutes: int):
    apply_map_remaining_minutes(now_jst(), int(minutes))
    await update_ops_panel_guild(interaction.guild)
    await interaction.response.send_message("OK：残り時間を反映しました。")

# @bot.tree.command(name="reset_tournament_defaults", description="大会設定をデフォルトに戻す（例外解除）")
# @app_commands.checks.has_permissions(administrator=True)
async def reset_tournament_defaults(interaction: discord.Interaction):
    STATE.mode = DEFAULT_MODE
    STATE.match_count = DEFAULT_MATCH_COUNT
    STATE.match1_start = DEFAULT_MATCH1_START
    save_state(STATE)
    await interaction.response.send_message("OK：大会設定をデフォルトに戻しました。")
    await update_ops_panel_guild(interaction.guild)


CHECKIN_PRE_HHMM = "21:55"
CHECKIN_CLOSE_HHMM = "21:58"
AUTO_KEYHOST_HHMM = "22:00"

automation_loop_task: Optional[asyncio.Task] = None
match2_break_task: Optional[asyncio.Task] = None  # match2 replay submitted -> schedule match3 keyhost

async def send_to_key_channel(guild: discord.Guild, content: str) -> None:
    if not STATE.key_channel_id:
        return
    ch = guild.get_channel(STATE.key_channel_id)
    if ch is None:
        try:
            ch = await bot.fetch_channel(STATE.key_channel_id)
        except Exception:
            ch = None
    if isinstance(ch, (discord.TextChannel, discord.Thread)):
        try:
            await ch.send(content)
        except Exception:
            pass

async def schedule_match3_break_after_match2_replay(guild: discord.Guild) -> None:
    """When match2 replay is submitted, announce in fixed key channel and deliver match3 keyhost in 5 minutes."""
    global match2_break_task

    now = now_jst()
    notify_time = now + timedelta(minutes=5)
    notify_hhmm = notify_time.strftime("%H:%M")

    # 1) announce to fixed key channel (as requested)
    try:
        ch = guild.get_channel(KEY_CHANNEL_FIXED_ID)
        if ch is None:
            ch = await bot.fetch_channel(KEY_CHANNEL_FIXED_ID)
        if isinstance(ch, (discord.TextChannel, discord.Thread)):
            await ch.send(
                "3試合目のキー配布開始予定時刻を\n"
                f"{notify_hhmm}といたします。\n"
                "この間にお手洗い等をお済ませになってください。"
            )
    except Exception:
        pass

    # 2) update state for match3 planned departure (do not trigger immediate distribution)
    try:
        STATE.match_no = 3
    except Exception:
        pass
    STATE.planned_departure = notify_hhmm
    STATE.departure_time = None
    STATE.phase = "PREP"
    # clear any pending immediate trigger to avoid double-send
    try:
        setattr(STATE, "pending_next_match_no", None)
        setattr(STATE, "pending_keyhost_send", False)
        setattr(STATE, "pending_keyhost_send_at", None)
    except Exception:
        pass
    save_state(STATE)

    # 3) schedule keyhost distribution at notify_time
    if match2_break_task and not match2_break_task.done():
        try:
            match2_break_task.cancel()
        except Exception:
            pass

    async def _deliver():
        # sleep precise until notify_time
        try:
            delay = max(0.0, (notify_time - now_jst()).total_seconds())
        except Exception:
            delay = 300.0
        try:
            await asyncio.sleep(delay)
        except Exception:
            return
        # safety checks
        if bool(getattr(STATE, "emergency_stop", False)):
            return
        try:
            if int(getattr(STATE, "match_no", 0) or 0) != 3:
                return
        except Exception:
            return
        try:
            await keyhost_notify_once(guild, reason="auto_break_m3")
        except Exception:
            pass

    match2_break_task = asyncio.create_task(_deliver())

async def keyhost_notify_once(guild: discord.Guild, *, reason: str = "auto") -> bool:
    # キーホスト通知（緊急停止時の手動配布を含む）
    if not STATE.keyhost_channel_id:
        return False

    ok = False
    is_test = ("test" in str(reason).lower())

    # 通知先（チャンネル/スレッド）を取得
    kh_ch = guild.get_channel(STATE.keyhost_channel_id)
    if kh_ch is None:
        try:
            kh_ch = await bot.fetch_channel(STATE.keyhost_channel_id)
        except Exception:
            kh_ch = None

    if not isinstance(kh_ch, (discord.TextChannel, discord.Thread)):
        return False

    # スレッドの場合、未参加だと送れないことがあるので join を試す
    if isinstance(kh_ch, discord.Thread):
        try:
            await kh_ch.join()
        except Exception:
            pass

    # 直近の送信（画像/コピペ用キー）を掃除
    for attr in ("last_keyhost_image_msg_id", "last_keyhost_key_msg_id"):
        mid = getattr(STATE, attr, None)
        if mid:
            try:
                old = await kh_ch.fetch_message(mid)
                await old.delete()
            except Exception:
                pass
            setattr(STATE, attr, None)

    # 状態（PREPへ）
    STATE.phase = "PREP"
    STATE.custom_key = None
    # planned_departure は消さない（消すと 00:00 固定になる）
    STATE.departure_time = None
    STATE.delete_at_iso = None
    save_state(STATE)

    try:
        used = used_set()
        k = generate_key(used)
        STATE.custom_key = k
        # 予定の出発時間
        planned = (STATE.planned_departure or "").strip()
        # 手動配布（緊急停止中の"キーホストに配布"など）は、"押した時刻+3分"で必ず上書きする
        if str(reason).startswith("manual") or "replay_done" in str(reason):
            planned = (now_jst() + timedelta(minutes=3)).strftime("%H:%M")
        if planned in ("", "00:00"):
            # 予定が無い場合は「今+3分」を暫定予定にする
            planned = (now_jst() + timedelta(minutes=3)).strftime("%H:%M")
        # 仕様: 1試合目のキー配布予定は大会開始時間。
        # ただし、その時刻がキー配布停止時間帯に入る場合は「停止終了時刻」に繰り下げる。
        try:
            if int(getattr(STATE, "match_no", 1) or 1) == 1:
                t0 = load_entry_tournament_start_time()
                if planned == t0 and STATE.key_pause_from and STATE.key_pause_to:
                    pdt = parse_hhmm(planned, now_jst())
                    sdt = parse_hhmm(str(STATE.key_pause_from), pdt)
                    edt = parse_hhmm(str(STATE.key_pause_to), pdt)
                    if sdt <= pdt < edt:
                        planned = str(STATE.key_pause_to)
        except Exception:
            pass

        STATE.planned_departure = planned

        note_keyhost = "待機列ができたら、Discordで【️待機列完成】ボタンを押してお知らせください"

        img_path = None
        try:
            img_path = await try_render_png(
                STATE.match_no,
                k,
                "出発時間",
                planned,
                note_keyhost,
                variant="keyhost_planned",
                planned_time=planned)
        except Exception:
            img_path = None

        # 画像が出せるなら「画像」と「コピペ用キー（埋め込み）」を分けて送る（これが正）
        if img_path:
            try:
                msg_img = await kh_ch.send(content=("【⚠テスト送信】" if is_test else None), file=discord.File(str(img_path)))
                STATE.last_keyhost_image_msg_id = msg_img.id
            except Exception:
                img_path = None

        # コピペ用キー（埋め込み）は必ず出す
        embed_key = discord.Embed(description=str(k))
        embed_key.color = 0x2f3136
        # 「待機列完成」ボタンはキーホスト向けメッセージに付ける
        msg_key = await kh_ch.send(content=("【⚠テスト送信】\n" if is_test else "") + "🔒カスタムキー＜コピペ用＞", embed=embed_key, view=KeyhostView())
        STATE.last_keyhost_key_msg_id = msg_key.id

        # 画像が無理ならテキストで補助（最低限）
        if not img_path:
            await kh_ch.send(("【⚠テスト送信】\n" if is_test else "") + f"⚔{STATE.match_no}試合目\n出発予定時間　{planned}")

        save_state(STATE)
        ok = True
    except Exception:
        ok = False

    # 実況解説チャンネルにはキーは出さず、出発予定/確定など時間だけ（既存仕様）
    try:
        caster_target_id = getattr(STATE, "commentary_channel_id", None) or CASTER_CHANNEL_ID
        caster_ch = guild.get_channel(caster_target_id)
        if caster_ch is None:
            try:
                caster_ch = await bot.fetch_channel(caster_target_id)
            except Exception:
                caster_ch = None
        if isinstance(caster_ch, (discord.TextChannel, discord.Thread)):
            if isinstance(caster_ch, discord.Thread):
                try:
                    await caster_ch.join()
                except Exception:
                    pass
            # キーホスト通知と同時：予定時間のみ通知
            if STATE.planned_departure:
                await caster_ch.send(f"⚔{STATE.match_no}試合目\n出発予定時間　{STATE.planned_departure}")
    except Exception:
        pass
    # 成功時は『キー通知済み』を確定して二重送信を防止
    if ok:
        STATE.keyhost_notified_once = True
        save_state(STATE)


    return ok


async def send_checkin_phase1(guild: discord.Guild, force: bool = False) -> None:
    """① 大会開始30分前：チェックイン開始（全員に送る）"""
    today = now_jst().date().isoformat()
    if not force and STATE.checkin_phase1_sent_date == today:
        return

    nums = _extract_roster_numbers(guild)
    sent_any = False
    for n in nums:
        ch = find_channel_by_number(guild, n)
        if isinstance(ch, (discord.TextChannel, discord.Thread)):
            try:
                view = CheckinView()
                await ch.send(
                    "ーーーーーーーーーーーーーーー\n"
                    "🔔準備が整いましたらチェックインを行ってください\n"
                    "ーーーーーーーーーーーーーーー",
                    view=view,
                )
                sent_any = True
            except Exception:
                continue

    if sent_any:
        STATE.checkin_phase1_sent_date = today
        save_state(STATE)


async def send_checkin_phase2(guild: discord.Guild, force: bool = False) -> None:
    """② 大会開始10分前：集合時間アナウンス（未操作のみ）"""
    today = now_jst().date().isoformat()
    if not force and STATE.checkin_phase2_sent_date == today:
        return

    roster = _extract_roster_numbers(guild)
    checked = set(getattr(STATE, "checked_in_numbers", []) or [])
    declined = set(getattr(STATE, "declined_numbers", []) or [])
    forfeited = set(getattr(STATE, "forfeit_numbers", []) or [])
    operated = checked | declined | forfeited
    targets = [n for n in roster if n not in operated]

    sent_any = False
    for n in targets:
        ch = find_channel_by_number(guild, n)
        if isinstance(ch, (discord.TextChannel, discord.Thread)):
            try:
                view = CheckinView()
                await ch.send(
                    "ーーーーーーーーーーーーーーー\n"
                    "🔷集合時間になりました\n"
                    "ーーーーーーーーーーーーーーー",
                    view=view,
                )
                sent_any = True
            except Exception:
                continue

    if sent_any:
        STATE.checkin_phase2_sent_date = today
        save_state(STATE)


def _calc_checkin_lists(guild: discord.Guild) -> dict:
    roster = _extract_roster_numbers(guild)
    checked = sorted(set(getattr(STATE, "checked_in_numbers", []) or []))
    forfeited = sorted(set(getattr(STATE, "forfeit_numbers", []) or []))
    declined = sorted(set(getattr(STATE, "declined_numbers", []) or []))
    operated = set(checked) | set(forfeited) | set(declined)
    unop = [n for n in roster if n not in operated]
    return {
        "checked": checked,
        "forfeit": forfeited,
        "declined": declined,
        "unoperated": unop,
    }



def _format_checkin_status_text(guild: discord.Guild) -> str:
    d = _calc_checkin_lists(guild)
    return (
        "📝 チェックイン状況（第1試合）\n\n"
        "✅ チェックイン済\n"
        f"{_fmt_numbers_slash(d['checked']) if d['checked'] else 'なし'}\n\n"
        "⚠️ 第1試合棄権\n"
        f"{_fmt_numbers_slash(d['forfeit']) if d['forfeit'] else 'なし'}\n\n"
        "❌ 参加辞退\n"
        f"{_fmt_numbers_slash(d['declined']) if d['declined'] else 'なし'}\n\n"
        "⏳ 未操作\n"
        f"{_fmt_numbers_slash(d['unoperated']) if d['unoperated'] else 'なし'}"
    )


async def update_checkin_status_channel(guild: discord.Guild, force: bool = False) -> None:
    """通知用チャンネルにチェックイン状況を1分ごとに反映（編集更新）。"""
    now = now_jst()
    minute_key = now.strftime("%Y-%m-%d %H:%M")
    if not force and STATE.checkin_status_last_min == minute_key:
        return

    ch_id = CHECKIN_STATUS_CHANNEL_ID
    ch = guild.get_channel(ch_id)
    if ch is None:
        try:
            ch = await bot.fetch_channel(ch_id)
        except Exception:
            ch = None
    if not isinstance(ch, (discord.TextChannel, discord.Thread)):
        return

    text = _format_checkin_status_text(guild)

    # edit existing message if possible
    if STATE.checkin_status_message_id:
        try:
            msg = await ch.fetch_message(STATE.checkin_status_message_id)
            await msg.edit(content=text)
            STATE.checkin_status_last_min = minute_key
            save_state(STATE)
            return
        except Exception:
            STATE.checkin_status_message_id = None
            save_state(STATE)

    try:
        msg = await ch.send(text)
        STATE.checkin_status_message_id = msg.id
        STATE.checkin_status_last_min = minute_key
        save_state(STATE)
    except Exception:
        pass


async def refresh_unoperated_cache(guild: discord.Guild, force: bool = False) -> None:
    """ops panel のヘッダー用：未操作だけを1分ごとに計算して STATE に保存。"""
    now = now_jst()
    minute_key = now.strftime("%Y-%m-%d %H:%M")
    if not force and getattr(STATE, "ops_header_last_min", None) == minute_key:
        return

    try:
        d = _calc_checkin_lists(guild)
        STATE.uncheckin_numbers = ",".join(d["unoperated"])
        STATE.ops_header_last_min = minute_key
        save_state(STATE)
    except Exception:
        pass



async def send_checkin_phase4_golive(guild: discord.Guild, force: bool = False) -> None:
    """④ 大会開始2分前：GoLive案内（全員向けキー配布チャンネル）"""
    today = now_jst().date().isoformat()
    if not force and STATE.checkin_phase4_sent_date == today:
        return

    try:
        await send_to_key_channel(
            guild,
            "ーーーーーーーーーーーーーーー\n"
            "🎥GoLive配信を開始してください\n"
            "ーーーーーーーーーーーーーーー\n"
            "GoLive配信が始まらない方、GoLive配信の画面がブラックアウトしている方は、\n"
            "運営がお声掛けに回ることがあります"
        )
        STATE.checkin_phase4_sent_date = today
        save_state(STATE)
    except Exception:
        pass


async def cleanup_checkin_buttons(guild: discord.Guild) -> None:
    today = now_jst().date().isoformat()
    if STATE.checkin_cleanup_date == today:
        return
    # delete button messages
    for n, mid in list((STATE.checkin_button_message_ids or {}).items()):
        cid = (STATE.checkin_button_channel_ids or {}).get(n)
        if not cid:
            continue
        ch = guild.get_channel(cid)
        if ch is None:
            try:
                ch = await bot.fetch_channel(cid)
            except Exception:
                ch = None
        if isinstance(ch, (discord.TextChannel, discord.Thread)):
            try:
                msg = await ch.fetch_message(mid)
                await msg.delete()
            except Exception:
                pass
    STATE.checkin_cleanup_date = today
    save_state(STATE)

    try:
        STATE.last_keyhost_image_msg_id = msg_kh.id
        save_state(STATE)
    except Exception:
        pass

    caster_target_id = getattr(STATE, "commentary_channel_id", None) or CASTER_CHANNEL_ID
    caster_ch = guild.get_channel(caster_target_id)
    if caster_ch is None:
        try:
            caster_ch = await bot.fetch_channel(caster_target_id)
        except Exception:
            caster_ch = None
    if isinstance(caster_ch, (discord.TextChannel, discord.Thread)):
        try:
            await caster_ch.send(f"⚔{STATE.match_no}試合目\n出発予定時間　{STATE.planned_departure}")
        except Exception:
            pass

async def update_ops_panel_guild(guild: discord.Guild) -> None:
    if not STATE.ops_panel_channel_id or not STATE.ops_panel_message_id:
        return
    ch = guild.get_channel(STATE.ops_panel_channel_id)
    if ch is None:
        try:
            ch = await bot.fetch_channel(STATE.ops_panel_channel_id)
        except Exception:
            return
    if not isinstance(ch, (discord.TextChannel, discord.Thread)):
        return
    try:
        msg = await ch.fetch_message(STATE.ops_panel_message_id)
    except Exception:
        return
    try:
        await msg.edit(embed=build_ops_embed(), view=OpsPanelView())
    except Exception:
        pass

async def automation_loop():
    await bot.wait_until_ready()
    if not bot.guilds:
        return
    guild = bot.guilds[0]
    while not bot.is_closed():
        try:
            now = now_jst()


            # チェックイン自動運用（起点：大会開始 22:00）
            t0 = get_tournament_start_dt()
            if t0 is not None:
                t30 = t0 - timedelta(minutes=30)
                t10 = t0 - timedelta(minutes=10)
                t5 = t0 - timedelta(minutes=5)
                t2 = t0 - timedelta(minutes=2)

                if now >= t30:
                    await send_checkin_phase1(guild)
                if now >= t10:
                    await send_checkin_phase2(guild)

                # パネル（ヘッダー：未操作）を1分ごと更新：大会当日 21:55〜22:00 のみ
                if is_event_day(now) and (t5 <= now <= t0):
                    global _last_ops_header_refresh_minute
                    minute_key = now.strftime("%Y-%m-%d %H:%M")
                    if _last_ops_header_refresh_minute != minute_key:
                        _last_ops_header_refresh_minute = minute_key
                        await refresh_unoperated_cache(guild)
                        await update_ops_panel_guild(guild)

                if now >= t5:
                    # ③：運営確認用（未操作含む状況を最新化）※当日1回だけ
                    today = now.date().isoformat()
                    if STATE.checkin_phase3_sent_date != today:
                        await refresh_unoperated_cache(guild)
                        await update_checkin_status_channel(guild, force=True)
                        STATE.checkin_phase3_sent_date = today
                        save_state(STATE)

                if now >= t2:
                    await send_checkin_phase4_golive(guild)

            # 停止時間帯明けの予約配布            # 停止時間帯明けの予約配布
            if getattr(STATE, "pending_keyhost_send", False) and getattr(STATE, "pending_keyhost_send_at", None):
                try:
                    at = parse_hhmm(STATE.pending_keyhost_send_at, now)
                    if now >= at and not is_in_pause_window(now):
                        nxt = getattr(STATE, "pending_next_match_no", None)
                        if nxt is not None:
                            try:
                                STATE.match_no = int(nxt)
                            except Exception:
                                pass
                        STATE.pending_next_match_no = None
                        STATE.pending_keyhost_send = False
                        STATE.pending_keyhost_send_at = None
                        STATE.keyhost_notified_once = False
                        save_state(STATE)
                        await keyhost_notify_once(guild, reason="pause_release")
                except Exception:
                    pass

            # 大会日以外は自動化しない
            if not is_event_day(now):
                await asyncio.sleep(10)
                continue
            if hhmm(now) >= CHECKIN_CLOSE_HHMM and not STATE.checkin_closed:
                STATE.checkin_closed = True
                STATE.auto_enabled = True
                save_state(STATE)
                await update_ops_panel_guild(guild)

            if STATE.auto_enabled and STATE.match_no == 1 and not STATE.keyhost_notified_once:
                if hhmm(now) >= AUTO_KEYHOST_HHMM and not (is_in_pause_window(now) or STATE.emergency_stop):
                    await keyhost_notify_once(guild, reason="auto_2200")
                    await update_ops_panel_guild(guild)
        except Exception:
            pass
        await asyncio.sleep(10)



@bot.event
async def on_ready():

    bot.add_view(CheckinView())
    bot.add_view(OpsPanelView())
    bot.add_view(KeyhostView())
    try:
        await bot.tree.sync()
    except Exception:
        pass
    print(f"[READY] Logged in as {bot.user} / state={STATE_PATH}")
    bot.loop.create_task(deleter_loop())
    # start automation loop
    global automation_loop_task
    if automation_loop_task is None or automation_loop_task.done():
        automation_loop_task = bot.loop.create_task(automation_loop())


async def deleter_loop():
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            if STATE.delete_at_iso:
                try:
                    target = datetime.fromisoformat(STATE.delete_at_iso)
                except Exception:
                    target = None

                if target and datetime.now() >= target:
                    for g in bot.guilds:
                        await delete_general_channel_posts(g)
                        await delete_keyhost_channel_posts(g)
                        await delete_replay_request_posts(g)
                    STATE.delete_at_iso = None
                    save_state(STATE)

            await asyncio.sleep(3)
        except Exception:
            await asyncio.sleep(3)


def main():
    token = os.getenv("KEY_TOKEN")
    if not token:
        raise RuntimeError("環境変数 KEY_TOKEN が未設定です。setx KEY_TOKEN \"...\" してから起動して。")
    bot.run(token)



# -------------------------
# Debug / confirmation commands (safe)
# -------------------------

@bot.tree.command(name="debug_checkin_message", description="【確認用】チェックインメッセージを送信（この場所）")
@app_commands.checks.has_permissions(administrator=True)
async def debug_checkin_message(interaction: discord.Interaction):
    await interaction.channel.send(
        "チェックインを行ってください。\n下の【チェックイン】ボタンを押してください。",
        view=DebugCheckinView()
    )
    await interaction.response.send_message("OK：送信しました。")


@bot.tree.command(name="debug_golive_message", description="【確認用】GoLive配信開始メッセージを送信（この場所）")
@app_commands.checks.has_permissions(administrator=True)
async def debug_golive_message(interaction: discord.Interaction):
    await interaction.channel.send("🎥 GoLive配信を開始してください")
    await interaction.response.send_message("OK：送信しました。")


@bot.tree.command(name="debug_replay_request_message", description="【確認用】リプレイデータ提出依頼メッセージを送信（この場所）")
@app_commands.checks.has_permissions(administrator=True)
async def debug_replay_request_message(interaction: discord.Interaction):
    await interaction.channel.send(
        "第○試合目のリプレイデータを提出してください。\n提出後、下の【提出完了】ボタンを押してください。",
        view=DebugReplayDoneView()
    )
    await interaction.response.send_message("OK：送信しました。")


# -------------------------
# Check-in ops commands
# -------------------------

@bot.tree.command(name="checkin_tick", description="【運営】現在時刻に基づいてチェックイン周りを1回だけ実行（不足分があれば送る）")
@app_commands.checks.has_permissions(administrator=True)
async def checkin_tick(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True, thinking=False)
    guild = interaction.guild
    if guild is None:
        return
    now = now_jst()
    t0 = get_tournament_start_dt()
    if t0 is None:
        await interaction.followup.send("NG：大会日時が取得できません。")
        return

    if now >= t0 - timedelta(minutes=30):
        await send_checkin_phase1(guild, force=False)
    if now >= t0 - timedelta(minutes=10):
        await send_checkin_phase2(guild, force=False)
    await refresh_unoperated_cache(guild, force=True)
    await update_ops_panel_guild(guild)
    await update_checkin_status_channel(guild, force=True)
    if now >= t0 - timedelta(minutes=2):
        await send_checkin_phase4_golive(guild, force=False)

    await interaction.followup.send("OK：実行しました。")


@app_commands.choices(
    kind=[
        app_commands.Choice(name="①チェックイン開始（全員）", value="phase1"),
        app_commands.Choice(name="②集合アナウンス（未操作のみ）", value="phase2"),
        app_commands.Choice(name="④GoLive案内（全員）", value="phase4"),
        app_commands.Choice(name="状況更新（通知チャンネル）", value="status"),
        app_commands.Choice(name="全部", value="all"),
    ]
)
@bot.tree.command(name="checkin_emergency_send", description="【運営｜緊急】未送信/送信失敗に備えて強制送信する")
@app_commands.checks.has_permissions(administrator=True)
async def checkin_emergency_send(interaction: discord.Interaction, kind: app_commands.Choice[str]):
    await interaction.response.defer(ephemeral=True, thinking=False)
    guild = interaction.guild
    if guild is None:
        return

    v = kind.value
    if v in ("phase1", "all"):
        await send_checkin_phase1(guild, force=True)
    if v in ("phase2", "all"):
        await send_checkin_phase2(guild, force=True)
    if v in ("phase4", "all"):
        await send_checkin_phase4_golive(guild, force=True)
    if v in ("status", "all"):
        await refresh_unoperated_cache(guild, force=True)
        await update_ops_panel_guild(guild)
        await update_checkin_status_channel(guild, force=True)

    await interaction.followup.send("OK：送信しました。")


@bot.tree.command(name="checkin_status", description="【運営】チェックイン状況を通知チャンネルへ反映（編集更新）")
@app_commands.checks.has_permissions(administrator=True)
async def checkin_status(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True, thinking=False)
    guild = interaction.guild
    if guild is None:
        return
    await refresh_unoperated_cache(guild, force=True)
    await update_ops_panel_guild(guild)
    await update_checkin_status_channel(guild, force=True)
    await interaction.followup.send("OK：更新しました。")


# -------------------------
# Test command (progresses to next phase)
# -------------------------

@bot.tree.command(name="test_replay_request", description="【テスト用｜進行あり】リプレイ提出依頼（提出完了で次フェーズへ／キーホスト送信まで確認）")
@app_commands.checks.has_permissions(administrator=True)
async def test_replay_request(interaction: discord.Interaction):
    header = "【⚠ テスト用｜進行あり】\n"
    body = (
        f"第{STATE.match_no}試合目のリプレイデータを提出してください。\n"
        "提出後、下の【提出完了】ボタンを押してください。"
    )

    # 次試合番号を設定（本番と同様）
    try:
        nxt = min(int(STATE.match_no) + 1, int(getattr(STATE, "match_count", 4) or 4))
    except Exception:
        nxt = int(STATE.match_no) + 1
    STATE.phase = "WAIT_REPLAY_DONE"
    STATE.pending_next_match_no = nxt
    STATE.pending_keyhost_send = False
    STATE.pending_keyhost_send_at = None
    STATE.keyhost_notified_once = False
    save_state(STATE)

    await interaction.response.send_message("OK：テスト用メッセージを送信しました。")
    await interaction.channel.send(
        header + body,
        view=ReplaySubmitView(match_no=STATE.match_no, number="TEST")
    )

if __name__ == "__main__":
    main()