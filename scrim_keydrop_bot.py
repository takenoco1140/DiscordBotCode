# -*- coding: utf-8 -*-
"""
Scrim Key Drop Bot - OR40-style HTML/CSS (White BG) - Full Replacement

✅ Fixes:
- OR40系HTML/CSSを「そのまま」使う（枠線/角丸/カード構成が出る）
- CSS内の { } が .format() で壊れないよう、or40_key_bot.py と同じ brace-safe 方式でHTML生成
- Playwrightの重い処理前に defer して interaction timeout を防止
- discord.File には bytes を直接渡さず io.BytesIO で包む

TOKEN: environment variable SCRIMKEY_TOKEN
"""

from __future__ import annotations

import os
import re
import json
import asyncio
import secrets
import datetime
import tempfile
import io
import base64
from dataclasses import dataclass, asdict
from typing import Optional, Dict, Any, Set, List

import discord
from discord import app_commands
from discord.ext import commands

# =====================
# Constants / Paths
# =====================

JST_OFFSET_MINUTES = 9 * 60

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
CONFIG_PATH = os.path.join(DATA_DIR, "scrim_config.json")
STATE_PATH = os.path.join(DATA_DIR, "scrim_state.json")

RESET_HOUR_JST = 5
RESET_MINUTE_JST = 0

TEAM_LIMITS: Dict[str, int] = {"solo": 100, "duo": 50, "trio": 33, "squad": 25}

SIZE_CHOICES = [
    app_commands.Choice(name="ソロ", value="solo"),
    app_commands.Choice(name="デュオ", value="duo"),
    app_commands.Choice(name="トリオ", value="trio"),
    app_commands.Choice(name="スクワッド", value="squad"),
]

TYPE_CHOICES = [
    app_commands.Choice(name="通常", value="normal"),
    app_commands.Choice(name="トーナメントセッティング", value="tournament"),
    app_commands.Choice(name="リロード", value="reload"),
]

_KEY_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ0123456789"  # avoid I,O only

# OR40っぽい青（必要なら後で変更）
ACCENT_COLOR = "#0B3A96"

ASSETS_DIR = r"D:\DiscordBot\assets"
KEY_BG_PATH = os.path.join(ASSETS_DIR, "カスタムキー台紙.png")

def _bg_data_url() -> str:
    try:
        with open(KEY_BG_PATH, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("ascii")
        return "data:image/png;base64," + b64
    except Exception:
        return ""


# =====================
# Helpers
# =====================

def ensure_data_dir() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)


def utc_now() -> discord.utils.datetime.datetime:
    return discord.utils.utcnow()


def to_jst(dt_utc: discord.utils.datetime.datetime) -> discord.utils.datetime.datetime:
    return dt_utc + datetime.timedelta(minutes=JST_OFFSET_MINUTES)


def jst_date_str(dt_utc: discord.utils.datetime.datetime) -> str:
    return to_jst(dt_utc).strftime("%Y-%m-%d")


def fmt_hhmm_jst(dt_utc: discord.utils.datetime.datetime) -> str:
    return to_jst(dt_utc).strftime("%H:%M")


def load_json(path: str, default: Any) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path: str, obj: Any) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def from_iso(s: Optional[str]) -> Optional[discord.utils.datetime.datetime]:
    if not s:
        return None
    try:
        return discord.utils.datetime.datetime.fromisoformat(s)
    except Exception:
        return None


def to_iso(dt: discord.utils.datetime.datetime) -> str:
    return dt.isoformat()


def generate_custom_key() -> str:
    return "".join(secrets.choice(_KEY_ALPHABET) for _ in range(6))


async def _safe_defer(interaction: discord.Interaction, ephemeral: bool = True) -> None:
    try:
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=ephemeral, thinking=True)
    except Exception:
        pass


async def _ephemeral_reply(interaction: discord.Interaction, content: str) -> None:
    try:
        if interaction.response.is_done():
            await interaction.followup.send(content, ephemeral=True)
        else:
            await interaction.response.defer()
    except Exception:
        pass


# =====================
# HTML/CSS Image Rendering (OR40-style / White BG)
# =====================

def _html_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
         .replace("<", "&lt;")
         .replace(">", "&gt;")
         .replace('"', "&quot;")
    )


def _strip_bg_from_template(tpl: str) -> str:
    # Remove any background-image rules and enforce white background.
    out = tpl
    out = re.sub(r"\s*background-image:\s*url\([^\)]*\);\s*\n", "", out)
    out = re.sub(r'\s*background-image:\s*url\("[^"]*"\);\s*\n', "", out)
    out = re.sub(r"\s*background-image:\s*url\('[^']*'\);\s*\n", "", out)
    # Ensure body has background white
    if re.search(r"body\s*\{[\s\S]*?background\s*:", out) is None:
        out = re.sub(r"(body\s*\{)", r"\1\n  background: #ffffff;\n", out, count=1)
    return out


RAW_HTML_TEMPLATE = """<!DOCTYPE html>
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
  background: #ffffff;
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

  background: rgba(255,255,255,0.98);
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

/* ===== 時刻行（予定/確定） ===== */
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
      <div class="two-line">
        <div class="main">{key_value}</div>
        <div class="sub"> </div>
      </div>
    </div>

    <div class="line-card">
      <span class="line-title">🚎{time_title}</span>
      <div class="time-row">
        <span class="time-row-label">{time_label}</span>
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


def _build_html(template: str, **kwargs: str) -> str:
    """
    Brace-safe formatter:
    - Protect placeholders {key}
    - Escape all remaining braces in template (CSS braces)
    - Restore placeholders
    - Apply .format(**kwargs)
    """
    protected = template
    for k in kwargs.keys():
        protected = protected.replace("{" + k + "}", f"@@__{k}__@@")
    protected = protected.replace("{", "{{").replace("}", "}}")
    for k in kwargs.keys():
        protected = protected.replace(f"@@__{k}__@@", "{" + k + "}")
    return protected.format(**kwargs)


def render_html(match_no: int, key_value: str, time_title: str, time_label: str, time_value: str, note_text: str) -> str:
    html = _build_html(
        RAW_HTML_TEMPLATE,
        accent_color=ACCENT_COLOR,
        match_no=str(match_no),
        key_value=_html_escape(key_value),
        time_title=_html_escape(time_title),
        time_label=_html_escape(time_label),
        time_value=_html_escape(time_value),
        note_text=_html_escape(note_text).replace("\n", "<br/>"),
    )
    return _strip_bg_from_template(html)


async def try_render_png_from_html(html: str) -> Optional[bytes]:
    try:
        from playwright.async_api import async_playwright
    except Exception:
        return None


async def _try_render_png_from_html_key(html: str) -> Optional[bytes]:
    try:
        from playwright.async_api import async_playwright
    except Exception:
        return None

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            page = await browser.new_page(viewport={"width": 800, "height": 267})
            with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False, encoding="utf-8") as f:
                f.write(html)
                html_path = f.name
            await page.goto("file://" + html_path)
            try:
                await page.wait_for_timeout(150)
            except Exception:
                pass
            png = await page.screenshot(type="png", omit_background=True)
            await browser.close()
        try:
            os.remove(html_path)
        except Exception:
            pass
        return png
    except Exception as e:
        print(f"[WARN] key image render failed: {e}")
        return None


# =====================
# Key Image Rendering (Replaced HTML)
# =====================

RAW_KEY_IMAGE_HTML = """<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<style>
html, body {
  margin: 0;
  padding: 0;
  width: 800px;
  height: 267px;
  background: transparent;
  font-family: "Noto Sans JP", "Segoe UI", sans-serif;
}

.panel {
  position: relative;
  width: 100%;
  height: 100%;
  background: url('{bg_data_url}') no-repeat center center;
  background-size: contain;
}

.text {
  position: absolute;
  color: #ffffff;
  text-shadow: 0 2px 6px rgba(0,0,0,0.6);
  white-space: nowrap;
}

/* ○試合目 */
.match {
  top: 4%;
  left: 50%;
  transform: translateX(-50%);
  font-size: 32px;
  font-weight: 700;
}

/* カスタムキー */
.key {
  top: 58%;
  left: 31%;
  transform: translateX(-50%);
  font-size: 44px;
  font-weight: 800;
  letter-spacing: 0.05em;
}

/* 開始時間 */
.time {
  top: 60%;
  right: 31%;
  transform: translateX(50%);
  font-size: 38px;
  font-weight: 800;
  letter-spacing: 0.05em;
}
</style>
</head>
<body>

<div class="panel">
  <div class="text match">{match}</div>
  <div class="text key">{key}</div>
  <div class="text time">{time}</div>
</div>

</body>
</html>
"""

def build_key_image_html(match: str, key: str, time: str) -> str:
    return _build_html(
        RAW_KEY_IMAGE_HTML,
        bg_data_url=_bg_data_url(),
        match=_html_escape(match),
        key=_html_escape(key),
        time=_html_escape(time),
    )

async def img_host_planned(match_no: int, key_value: str, planned_hhmm: str):
    html = build_key_image_html(f"{match_no}試合目", key_value, planned_hhmm)
    return await _try_render_png_from_html_key(html)

async def img_host_confirmed(match_no: int, key_value: str, confirmed_hhmm: str):
    html = build_key_image_html(f"{match_no}試合目", key_value, confirmed_hhmm)
    return await _try_render_png_from_html_key(html)

async def img_key_ephemeral(match_no: int, key_value: str):
    html = build_key_image_html(f"{match_no}試合目", key_value, "")
    return await _try_render_png_from_html_key(html)

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            page = await browser.new_page(viewport={"width": 1280, "height": 720})
            with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False, encoding="utf-8") as f:
                f.write(html)
                html_path = f.name
            await page.goto("file://" + html_path)
            png = await page.screenshot(type="png")
            await browser.close()
        try:
            os.remove(html_path)
        except Exception:
            pass
        return png
    except Exception:
        return None



# =====================
# Models
# =====================

@dataclass
class GuildConfig:
    guild_id: int
    global_channel_id: Optional[int] = None
    keyhost_role_id: Optional[int] = None
    image_enabled: bool = True

    # 管理パネル用
    scrim: Dict[str, Any] = None
    admin_panel_message_id: Optional[int] = None
    admin_panel_channel_id: Optional[int] = None

    # 告知（参加予定）: { message_id(str): [user_id, ...] }
    participations: Dict[str, List[int]] = None

    def __post_init__(self):
        if self.scrim is None:
            self.scrim = {}
        if self.participations is None:
            self.participations = {}




@dataclass
class MatchState:
    match_no: int
    size_mode: str
    match_type: str

    custom_key: Optional[str] = None
    host_user_id: Optional[int] = None

    host_recruit_message_id: Optional[int] = None
    key_view_message_id: Optional[int] = None

    host_thread_id: Optional[int] = None
    host_message_id: Optional[int] = None

    host_selected_at: Optional[str] = None
    planned_time_utc: Optional[str] = None

    confirmed: bool = False
    confirmed_at: Optional[str] = None
    confirmed_time_utc: Optional[str] = None

    thread_delete_at: Optional[str] = None  # ISO

    counted_vc_ids: List[int] = None
    pressed_user_ids: List[int] = None

    def __post_init__(self):
        if self.counted_vc_ids is None:
            self.counted_vc_ids = []
        if self.pressed_user_ids is None:
            self.pressed_user_ids = []


@dataclass
class GuildState:
    active_match: Optional[MatchState] = None
    created_thread_ids: List[int] = None
    last_reset_jst: Optional[str] = None

    def __post_init__(self):
        if self.created_thread_ids is None:
            self.created_thread_ids = []


# =====================
# Views (persistent)
# =====================

class HostRecruitView(discord.ui.View):
    def __init__(self, bot: "ScrimBot"):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(label="キーホストします", style=discord.ButtonStyle.secondary, custom_id="scrim:host_recruit")
    async def host_recruit(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.bot.handle_host_recruit(interaction)


class WaitlistCompleteView(discord.ui.View):
    def __init__(self, bot: "ScrimBot"):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(label="待機列完成", style=discord.ButtonStyle.secondary, custom_id="scrim:waitlist_complete")
    async def waitlist_complete(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.bot.handle_waitlist_complete(interaction)


class KeyViewPanelView(discord.ui.View):
    def __init__(self, bot: "ScrimBot"):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(label="キー閲覧", style=discord.ButtonStyle.secondary, custom_id="scrim:key_view")
    async def key_view(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.bot.handle_key_view(interaction)



# =====================
# Announcement Participation View (persistent)
# =====================

class JoinButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="参加する", style=discord.ButtonStyle.primary, custom_id="scrim:join")

    async def callback(self, interaction: discord.Interaction):
        if not interaction.guild or not interaction.message:
            await interaction.response.defer()
            return
        bot: ScrimBot = interaction.client  # type: ignore

        cfg = bot.cfg(interaction.guild.id)
        mid = str(interaction.message.id)
        members = cfg.participations.setdefault(mid, [])
        if interaction.user.id not in members:
            members.append(interaction.user.id)

        await bot._save_all()

        embed = _announce_embed(interaction.guild, cfg.scrim, members)
        await interaction.response.edit_message(embed=embed, view=AnnounceView())


class CancelButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="キャンセル", style=discord.ButtonStyle.secondary, custom_id="scrim:cancel")

    async def callback(self, interaction: discord.Interaction):
        if not interaction.guild or not interaction.message:
            await interaction.response.defer()
            return
        bot: ScrimBot = interaction.client  # type: ignore

        cfg = bot.cfg(interaction.guild.id)
        mid = str(interaction.message.id)
        members = cfg.participations.setdefault(mid, [])
        if interaction.user.id in members:
            members.remove(interaction.user.id)

        await bot._save_all()

        embed = _announce_embed(interaction.guild, cfg.scrim, members)
        await interaction.response.edit_message(embed=embed, view=AnnounceView())


class AnnounceView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(JoinButton())
        self.add_item(CancelButton())




# =====================
# Admin Panel (Scrim Settings Embed + Toggles)
# =====================

def _scrim_cfg(bot: "ScrimBot", guild_id: int) -> Dict[str, Any]:
    cfg = bot.cfg(guild_id)
    if cfg.scrim is None:
        cfg.scrim = {}
    return cfg.scrim


def _announce_embed(guild: discord.Guild, scrim: Dict[str, Any], members: list[int]) -> discord.Embed:
    org = scrim.get("org") or "未設定"
    start = scrim.get("start_at_jst") or "未設定"

    team = scrim.get("team_mode")
    game = scrim.get("game_mode")
    team_label = {"solo": "ソロ", "duo": "デュオ", "trio": "トリオ", "squad": "スクワッド"}.get(team, "未設定")
    game_label = {"tournament": "トーナメントセッティング", "reload": "リロード"}.get(game, "")
    mode = f"{team_label}（{game_label}）" if game_label else team_label

    system = {"rotation": "回転型", "traditional": "従来型"}.get(scrim.get("system"), "未設定")

    lines: List[str] = []
    for uid in members:
        m = guild.get_member(uid)
        if m:
            lines.append(f"・{m.display_name}")
    member_text = "
".join(lines) if lines else "（なし）"

    team_count = len(members)

    notes = (
        "※チームで1人のみが押してください
"
        "※参加するを押したチームには優先してキーを告知します
"
        "※参加予定申請のみで実際に参加していない場合、
"
        "　累計3回でBANさせていただきます。
"
        "※開始30分前に締め切りますが、
"
        "　参加するを押していなくても参加は可能です。"
    )

    e = discord.Embed(title="⚔本日開催のスクリム", color=discord.Color.orange())
    e.add_field(name="開催団体：", value=org, inline=False)
    e.add_field(name="開始時間：", value=start, inline=False)
    e.add_field(name="モード：", value=mode, inline=False)
    e.add_field(name="開催方式：", value=system, inline=False)
    e.add_field(name="ーーーーーーーーーーーーー", value=f"参加予定チーム数：{team_count}", inline=False)
    e.add_field(name="参加予定チーム：", value=member_text, inline=False)
    e.add_field(name="注意事項", value=notes, inline=False)
    return e


def _scrim_embed(guild: discord.Guild, scrim: Dict[str, Any]) -> discord.Embed:
    org = scrim.get("org") or "未設定"

    start_raw = scrim.get("start_at_jst")
    if start_raw:
        try:
            y, mo, d = start_raw.split(" ")[0].split("-")
            hhmm = start_raw.split(" ")[1]
            import datetime as _dt
            wd = ["月","火","水","木","金","土","日"][_dt.date(int(y),int(mo),int(d)).weekday()]
            start = f"{int(y)}年{int(mo)}月{int(d)}日({wd})　{hhmm}～"
        except Exception:
            start = start_raw
    else:
        start = "未設定"

    team = scrim.get("team_mode")
    game = scrim.get("game_mode")
    team_label = {"solo":"ソロ","duo":"デュオ","trio":"トリオ","squad":"スクワッド"}.get(team, "未設定")
    game_label = {"tournament":"トーナメントセッティング","reload":"リロード"}.get(game, "")
    mode = f"{team_label}（{game_label}）" if game_label else team_label

    system = scrim.get("system")
    system_label = {"rotation":"回転型","traditional":"従来型"}.get(system, "未設定")

    mc = scrim.get("match_count")
    mc_txt = str(mc) if mc is not None else "ー"

    e = discord.Embed(title="🔧スクリム設定", color=discord.Color.blue())
    e.add_field(name="開催団体：", value=org, inline=False)
    e.add_field(name="開催日時：", value=start, inline=False)
    e.add_field(name="モード：", value=mode, inline=False)
    e.add_field(name="開催方式：", value=system_label, inline=False)
    e.add_field(name="試合数：", value=mc_txt, inline=False)
    return e

def _is_selected(label: str, selected: bool) -> str:
    return f"✅{label}" if selected else label

def _team_label(v: str) -> str:
    return {"solo":"ソロ","duo":"デュオ","trio":"トリオ","squad":"スクワッド"}.get(v, v)

def _game_label(v: str) -> str:
    return {"tournament":"トーナメントセッティング","reload":"リロード"}.get(v, v)

def _system_label(v: str) -> str:
    return {"rotation":"回転式","traditional":"従来式"}.get(v, v)

class OrgSelect(discord.ui.Select):
    def __init__(self, bot: "ScrimBot", guild_id: int):
        self.bot = bot
        self.guild_id = guild_id
        options = [
            discord.SelectOption(label="OR40", value="OR40"),
            discord.SelectOption(label="OR50", value="OR50"),
            discord.SelectOption(label="SCRIM", value="SCRIM"),
            discord.SelectOption(label="PRACTICE", value="PRACTICE"),
            discord.SelectOption(label="その他（入力）", value="__OTHER__"),
        ]
        super().__init__(placeholder="開催団体", options=options, min_values=1, max_values=1, custom_id=f"scrimadmin:org:{guild_id}")
        self.row = 0

    async def callback(self, interaction: discord.Interaction):
        v = self.values[0]
        if v == "__OTHER__":
            class OrgModal(discord.ui.Modal, title="開催団体（入力）"):
                def __init__(self, parent_view):
                    super().__init__(timeout=None)
                    self.parent_view = parent_view
                    self.name = discord.ui.TextInput(label="開催団体名", required=True, max_length=60)
                    self.add_item(self.name)

                async def on_submit(self, modal_interaction: discord.Interaction):
                    view = self.parent_view
                    scrim = _scrim_cfg(view.bot, view.guild_id)
                    scrim["org"] = str(self.name).strip()
                    await view.bot._save_all()
                    await view.refresh(modal_interaction)
                    await modal_interaction.response.defer()

            await interaction.response.send_modal(OrgModal(self.view))
            return

        scrim = _scrim_cfg(self.bot, self.guild_id)
        scrim["org"] = v
        await self.bot._save_all()
        await self.view.refresh(interaction, use_edit_message=True)  # type: ignore
        await interaction.response.defer()

class ScrimAdminPanelView(discord.ui.View):
    def __init__(self, bot: "ScrimBot", guild_id: int):
        super().__init__(timeout=None)
        self.bot = bot
        self.guild_id = guild_id
        self.add_item(OrgSelect(bot, guild_id))
        self.refresh_buttons(initial=True)

    def scrim(self) -> Dict[str, Any]:
        return _scrim_cfg(self.bot, self.guild_id)

    def refresh_buttons(self, initial: bool = False):
        if not initial:
            keep = [it for it in self.children if isinstance(it, discord.ui.Select)]
            self.clear_items()
            for it in keep:
                self.add_item(it)

        s = self.scrim()
        team = s.get("team_mode")
        game = s.get("game_mode")
        system = s.get("system")

        self.add_item(SetStartButton())

        self.add_item(TeamToggleButton("solo", _is_selected("ソロ", team == "solo")))
        self.add_item(TeamToggleButton("duo", _is_selected("デュオ", team == "duo")))
        self.add_item(TeamToggleButton("trio", _is_selected("トリオ", team == "trio")))
        self.add_item(TeamToggleButton("squad", _is_selected("スクワッド", team == "squad")))

        self.add_item(GameToggleButton("tournament", _is_selected("トーナメントセッティング", game == "tournament")))
        self.add_item(GameToggleButton("reload", _is_selected("リロード", game == "reload")))

        self.add_item(SystemToggleButton("rotation", _is_selected("回転式", system == "rotation")))
        self.add_item(SystemToggleButton("traditional", _is_selected("従来式", system == "traditional")))
        self.add_item(SetMatchCountButton(enabled=(system == "traditional")))

        self.add_item(AnnounceButton())
        self.add_item(ResetScrimButton())

    async def refresh(self, interaction: discord.Interaction, *, use_edit_message: bool = False):
        self.refresh_buttons(initial=False)
        if not interaction.guild:
            return
        embed = _scrim_embed(interaction.guild, self.scrim())

        # まずは「このインタラクション元メッセージ」を直接更新（エフェメラル不要）
        if use_edit_message:
            try:
                await interaction.response.edit_message(embed=embed, view=self)
                return
            except Exception:
                # fall through
                pass

        cfg = self.bot.cfg(self.guild_id)
        msg = None
        if cfg.admin_panel_message_id:
            try:
                msg = await interaction.channel.fetch_message(cfg.admin_panel_message_id)
            except Exception:
                msg = None
        if msg:
            try:
                await msg.edit(embed=embed, view=self)
            except Exception:
                pass


class SetStartButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="開催日時", style=discord.ButtonStyle.secondary, row=1, custom_id="scrimadmin:start")

    async def callback(self, interaction: discord.Interaction):
        class StartModal(discord.ui.Modal, title="開催日時（JST）"):
            def __init__(self, parent_view):
                super().__init__(timeout=None)
                self.parent_view = parent_view
                self.value = discord.ui.TextInput(label="YYYY/MM/DD HH:MM", required=True, placeholder="2026/2/5 22:00")
                self.add_item(self.value)

            async def on_submit(self, modal_interaction: discord.Interaction):
                text = str(self.value).strip()
                m = re.match(r"^(\d{4})/(\d{1,2})/(\d{1,2})\s+(\d{1,2}):(\d{1,2})$", text)
                if not m:
                    await modal_interaction.response.defer()
                    return
                y, mo, d, hh, mm = map(int, m.groups())
                try:
                    dt = datetime.datetime(y, mo, d, hh, mm)
                except Exception:
                    await modal_interaction.response.defer()
                    return

                view = self.parent_view
                view.scrim()["start_at_jst"] = f"{dt.year:04d}-{dt.month:02d}-{dt.day:02d} {dt.hour:02d}:{dt.minute:02d}"
                await view.bot._save_all()
                await view.refresh(modal_interaction)
                await modal_interaction.response.defer()

        await interaction.response.send_modal(StartModal(self.view))

class TeamToggleButton(discord.ui.Button):
    def __init__(self, value: str, label: str):
        super().__init__(label=label, style=discord.ButtonStyle.secondary, row=2, custom_id=f"scrimadmin:team:{value}")
        self.value = value

    async def callback(self, interaction: discord.Interaction):
        view: ScrimAdminPanelView = self.view  # type: ignore
        view.scrim()["team_mode"] = self.value
        await view.bot._save_all()
        await view.refresh(interaction, use_edit_message=True)

class GameToggleButton(discord.ui.Button):
    def __init__(self, value: str, label: str):
        super().__init__(label=label, style=discord.ButtonStyle.secondary, row=3, custom_id=f"scrimadmin:game:{value}")
        self.value = value

    async def callback(self, interaction: discord.Interaction):
        view: ScrimAdminPanelView = self.view  # type: ignore
        view.scrim()["game_mode"] = self.value
        await view.bot._save_all()
        await view.refresh(interaction, use_edit_message=True)

class SystemToggleButton(discord.ui.Button):
    def __init__(self, value: str, label: str):
        super().__init__(label=label, style=discord.ButtonStyle.secondary, row=1, custom_id=f"scrimadmin:system:{value}")
        self.value = value

    async def callback(self, interaction: discord.Interaction):
        view: ScrimAdminPanelView = self.view  # type: ignore
        view.scrim()["system"] = self.value
        if self.value == "rotation":
            view.scrim().pop("match_count", None)
        await view.bot._save_all()
        await view.refresh(interaction, use_edit_message=True)

class SetMatchCountButton(discord.ui.Button):
    def __init__(self, enabled: bool):
        style = discord.ButtonStyle.secondary if enabled else discord.ButtonStyle.gray
        super().__init__(label="試合数", style=style, row=1, disabled=(not enabled), custom_id="scrimadmin:matchcount")

    async def callback(self, interaction: discord.Interaction):
        view: ScrimAdminPanelView = self.view  # type: ignore
        if view.scrim().get("system") != "traditional":
            await interaction.response.defer()
            return

        class CountModal(discord.ui.Modal, title="試合数（従来型）"):
            def __init__(self, parent_view):
                super().__init__(timeout=None)
                self.parent_view = parent_view
                self.value = discord.ui.TextInput(label="試合数（1〜50）", required=True, placeholder="6")
                self.add_item(self.value)

            async def on_submit(self, modal_interaction: discord.Interaction):
                txt = str(self.value).strip()
                try:
                    n = int(txt)
                    if not (1 <= n <= 50):
                        raise ValueError("range")
                except Exception:
                    await modal_interaction.response.defer()
                    return

                view = self.parent_view
                view.scrim()["match_count"] = n
                await view.bot._save_all()
                await view.refresh(modal_interaction)
                await modal_interaction.response.defer()

        await interaction.response.send_modal(CountModal(self.view))

class AnnounceButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="告知投稿", style=discord.ButtonStyle.primary, row=4, custom_id="scrimadmin:announce")

    async def callback(self, interaction: discord.Interaction):
        view: ScrimAdminPanelView = self.view  # type: ignore
        if not interaction.guild:
            await interaction.response.defer()
            return

        # 告知先：global_channel があればそこ、なければ管理パネルのチャンネル
        ch = interaction.channel
        gch = await view.bot.get_global_channel(interaction.guild)
        if gch:
            ch = gch

        cfg = view.bot.cfg(interaction.guild.id)
        embed = _announce_embed(interaction.guild, cfg.scrim, [])

        msg = await ch.send(embed=embed, view=AnnounceView())
        cfg.participations[str(msg.id)] = []
        await view.bot._save_all()

        # 管理パネル自体の更新だけ（エフェメラル不要）
        await interaction.response.defer()



class ResetScrimButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="リセット", style=discord.ButtonStyle.danger, row=4, custom_id="scrimadmin:reset")

    async def callback(self, interaction: discord.Interaction):
        view: ScrimAdminPanelView = self.view  # type: ignore
        view.bot.cfg(view.guild_id).scrim = {}
        await view.bot._save_all()
        await view.refresh(interaction)
        await interaction.response.defer()

# =====================
# Bot
# =====================

class ScrimBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.none()
        intents.guilds = True
        intents.members = True
        intents.voice_states = True
        super().__init__(command_prefix="!", intents=intents)

        ensure_data_dir()
        self.configs: Dict[int, GuildConfig] = {}
        self.guild_states: Dict[int, GuildState] = {}
        self._lock = asyncio.Lock()
        self._scheduler_task: Optional[asyncio.Task] = None
        self._load_all()

    # ---------- persistence ----------
    def _load_all(self):
        cfg = load_json(CONFIG_PATH, {})
        for gid_str, v in cfg.items():
            gid = int(gid_str)
            self.configs[gid] = GuildConfig(
                guild_id=gid,
                global_channel_id=v.get("global_channel_id"),
                keyhost_role_id=v.get("keyhost_role_id"),
                image_enabled=bool(v.get("image_enabled", True)),
                scrim=v.get("scrim") or {},
                admin_panel_message_id=v.get("admin_panel_message_id"),
                admin_panel_channel_id=v.get("admin_panel_channel_id"),
                participations=v.get("participations") or {},
            )

        st = load_json(STATE_PATH, {})
        for gid_str, v in st.get("guilds", {}).items():
            gid = int(gid_str)
            gs = GuildState(
                active_match=MatchState(**v["active_match"]) if v.get("active_match") else None,
                created_thread_ids=v.get("created_thread_ids") or [],
                last_reset_jst=v.get("last_reset_jst"),
            )
            self.guild_states[gid] = gs

    async def _save_all(self):
        async with self._lock:
            save_json(CONFIG_PATH, {str(gid): asdict(cfg) for gid, cfg in self.configs.items()})
            out = {"guilds": {}}
            for gid, gs in self.guild_states.items():
                out["guilds"][str(gid)] = {
                    "active_match": asdict(gs.active_match) if gs.active_match else None,
                    "created_thread_ids": gs.created_thread_ids,
                    "last_reset_jst": gs.last_reset_jst,
                }
            save_json(STATE_PATH, out)

    # ---------- helpers ----------
    def cfg(self, guild_id: int) -> GuildConfig:
        if guild_id not in self.configs:
            self.configs[guild_id] = GuildConfig(guild_id=guild_id)
        return self.configs[guild_id]

    def gs(self, guild_id: int) -> GuildState:
        if guild_id not in self.guild_states:
            self.guild_states[guild_id] = GuildState()
        return self.guild_states[guild_id]

    def active_match(self, guild_id: int) -> Optional[MatchState]:
        return self.gs(guild_id).active_match

    async def get_global_channel(self, guild: discord.Guild) -> Optional[discord.TextChannel]:
        cid = self.cfg(guild.id).global_channel_id
        if not cid:
            return None
        ch = guild.get_channel(cid)
        return ch if isinstance(ch, discord.TextChannel) else None

    def viewer_limit(self, size_mode: str) -> int:
        cap = TEAM_LIMITS.get(size_mode, 100) - 1
        return max(1, cap)

    # ---------- lifecycle ----------
    async def setup_hook(self):
        self.add_view(HostRecruitView(self))
        self.add_view(WaitlistCompleteView(self))
        self.add_view(KeyViewPanelView(self))
        self.add_view(AnnounceView())
        self._register_commands()
        # 管理パネル（再起動後もボタンが死なないように persistent view を登録）
        try:
            for gid in list(self.configs.keys()):
                self.add_view(ScrimAdminPanelView(self, gid))
        except Exception:
            pass


    async def _restore_admin_panels(self):
        # 保存済みの管理パネルメッセージを再起動後に復元（View再接続）
        for guild in list(self.guilds):
            cfg = self.cfg(guild.id)
            if not cfg.admin_panel_message_id or not cfg.admin_panel_channel_id:
                continue
            ch = guild.get_channel(cfg.admin_panel_channel_id)
            if not isinstance(ch, discord.TextChannel):
                continue
            try:
                msg = await ch.fetch_message(cfg.admin_panel_message_id)
            except Exception:
                continue
            try:
                view = ScrimAdminPanelView(self, guild.id)
                embed = _scrim_embed(guild, cfg.scrim or {})
                await msg.edit(embed=embed, view=view)
            except Exception:
                pass

    async def on_ready(self):
        if self._scheduler_task is None or self._scheduler_task.done():
            self._scheduler_task = asyncio.create_task(self._scheduler_loop())
        try:
            await self.tree.sync()
        except Exception:
            pass

        # 管理パネル復元
        try:
            await self._restore_admin_panels()
        except Exception:
            pass
        print(f"[BOOT] Logged in as {self.user}")

    # ---------- scheduler ----------
    async def _scheduler_loop(self):
        while not self.is_closed():
            try:
                await self._daily_reset_if_due()
                await self._apply_due_thread_deletes()
            except Exception as e:
                print(f"[SCHED] {e}")
            await asyncio.sleep(15)

    async def _daily_reset_if_due(self):
        now = utc_now()
        today_jst = jst_date_str(now)
        for guild in list(self.guilds):
            gs = self.gs(guild.id)
            if gs.last_reset_jst == today_jst:
                continue
            now_jst = to_jst(now)
            if (now_jst.hour, now_jst.minute) < (RESET_HOUR_JST, RESET_MINUTE_JST):
                continue
            await self._full_reset_guild(guild)
            gs.last_reset_jst = today_jst
            await self._save_all()

    async def _apply_due_thread_deletes(self):
        now = utc_now()
        for guild in list(self.guilds):
            m = self.active_match(guild.id)
            if not m or not m.thread_delete_at or not m.host_thread_id:
                continue
            due = from_iso(m.thread_delete_at)
            if not due or now < due:
                continue
            thread = self.get_channel(m.host_thread_id)
            if isinstance(thread, discord.Thread):
                try:
                    await thread.delete(reason="Scrim: host thread auto-delete")
                except Exception:
                    pass
            m.host_thread_id = None
            m.host_message_id = None
            m.thread_delete_at = None
            await self._save_all()

    # ---------- full reset ----------
    async def _full_reset_guild(self, guild: discord.Guild):
        gs = self.gs(guild.id)
        cfg = self.cfg(guild.id)
        gch = None
        if cfg.global_channel_id:
            ch = guild.get_channel(cfg.global_channel_id)
            if isinstance(ch, discord.TextChannel):
                gch = ch
        if gch and gs.active_match:
            for mid in [gs.active_match.host_recruit_message_id, gs.active_match.key_view_message_id]:
                if not mid:
                    continue
                try:
                    msg = await gch.fetch_message(mid)
                    await msg.edit(view=None)
                except Exception:
                    pass
        for tid in list(gs.created_thread_ids):
            th = self.get_channel(tid)
            if isinstance(th, discord.Thread):
                try:
                    await th.delete(reason="Scrim: daily reset")
                except Exception:
                    pass
        gs.active_match = None
        gs.created_thread_ids = []

    # =====================
    # Commands
    # =====================

    def _register_commands(self):
        @self.tree.command(name="scrim_set_channel", description="全体チャンネルを設定")
        async def scrim_set_channel(interaction: discord.Interaction, channel: discord.TextChannel):
            if not interaction.guild:
                await interaction.response.defer()
                return
            self.cfg(interaction.guild.id).global_channel_id = channel.id
            await self._save_all()
            await interaction.response.defer()

        @self.tree.command(name="scrim_prepare", description="準備確定→1試合目募集")
        @app_commands.choices(size_mode=SIZE_CHOICES, match_type=TYPE_CHOICES)
        async def scrim_prepare(interaction: discord.Interaction, size_mode: app_commands.Choice[str], match_type: app_commands.Choice[str]):
            if not interaction.guild:
                await interaction.response.defer()
                return
            gch = await self.get_global_channel(interaction.guild)
            if not gch:
                await interaction.response.defer()
                return
            self.gs(interaction.guild.id).active_match = MatchState(match_no=1, size_mode=size_mode.value, match_type=match_type.value)
            await self._save_all()
            await interaction.response.defer()
            await self._post_host_recruit_panel(interaction.guild, gch)

        @self.tree.command(name="scrim_reset_now", description="全リセット")
        async def scrim_reset_now(interaction: discord.Interaction):
            if not interaction.guild:
                await interaction.response.defer()
                return
            await self._full_reset_guild(interaction.guild)
            self.gs(interaction.guild.id).last_reset_jst = jst_date_str(utc_now())
            await self._save_all()
            await interaction.response.defer()

        @self.tree.command(name="scrim_admin", description="運営用スクリム管理パネルを投稿/更新")
        async def scrim_admin(interaction: discord.Interaction):
            if not interaction.guild:
                await interaction.response.defer()
                return
            if interaction.channel is None:
                await interaction.response.defer()
                return

            # まず応答（遅延対策）。この応答は可能なら後で消す。
            try:
                await interaction.response.defer(thinking=False)
            except Exception:
                pass

            cfg = self.cfg(interaction.guild.id)
            scrim = cfg.scrim if cfg.scrim is not None else {}

            view = ScrimAdminPanelView(self, interaction.guild.id)
            embed = _scrim_embed(interaction.guild, scrim)

            # 既存パネルがあれば更新、なければ新規投稿
            msg = None
            if cfg.admin_panel_message_id:
                try:
                    msg = await interaction.channel.fetch_message(cfg.admin_panel_message_id)  # type: ignore
                except Exception:
                    msg = None

            try:
                cfg.admin_panel_channel_id = interaction.channel.id  # type: ignore
                if msg:
                    await msg.edit(embed=embed, view=view)
                else:
                    posted = await interaction.channel.send(embed=embed, view=view)  # type: ignore
                    cfg.admin_panel_message_id = posted.id
                await self._save_all()
            except Exception as e:
                try:
                    await interaction.followup.send(f"投稿に失敗：{e}", ephemeral=True)
                except Exception:
                    pass
                return

            # 可能なら最初の応答を消す（見た目ノイズ削減）
            try:
                await interaction.delete_original_response()
            except Exception:
                pass


    # =====================
    # Posting
    # =====================

    async def _post_host_recruit_panel(self, guild: discord.Guild, channel: discord.TextChannel):
        m = self.active_match(guild.id)
        if not m:
            return
        if m.host_recruit_message_id:
            try:
                old = await channel.fetch_message(m.host_recruit_message_id)
                await old.edit(view=None)
            except Exception:
                pass
        msg = await channel.send(content=f"{m.match_no}試合目のキーホストを募集します", view=HostRecruitView(self))
        m.host_recruit_message_id = msg.id
        await self._save_all()

    async def _post_key_view_panel(self, guild: discord.Guild, channel: discord.TextChannel):
        m = self.active_match(guild.id)
        if not m:
            return
        if m.key_view_message_id:
            try:
                old = await channel.fetch_message(m.key_view_message_id)
                await old.edit(view=None)
            except Exception:
                pass
        msg = await channel.send(
            content=f"{m.match_no}試合目のキーをご確認ください\n※閲覧数上限で次のマッチの準備になります",
            view=KeyViewPanelView(self),
        )
        m.key_view_message_id = msg.id
        await self._save_all()

    # =====================
    # Handlers
    # =====================

    async def handle_host_recruit(self, interaction: discord.Interaction):
        if not interaction.guild:
            await _ephemeral_reply(interaction, "サーバー内で実行してください。")
            return

        await _safe_defer(interaction, ephemeral=True)

        gch = await self.get_global_channel(interaction.guild)
        m = self.active_match(interaction.guild.id)
        if not gch or not m:
            await _ephemeral_reply(interaction, "未準備。/scrim_prepare から開始してください。")
            return
        if m.host_selected_at:
            await _ephemeral_reply(interaction, "既に確定しています。")
            return

        m.host_selected_at = to_iso(utc_now())
        m.host_user_id = interaction.user.id
        planned_dt = utc_now() + datetime.timedelta(minutes=3)
        m.planned_time_utc = to_iso(planned_dt)
        m.custom_key = generate_custom_key()
        await self._save_all()

        # disable recruit panel
        try:
            if interaction.message:
                await interaction.message.edit(view=None)
        except Exception:
            pass

        # create host thread
        thread = await gch.create_thread(
            name=f"{m.match_no}試合目-キーホスト",
            type=discord.ChannelType.private_thread,
            invitable=True,
            reason="Scrim: keyhost thread",
        )
        try:
            await thread.add_user(interaction.user)
        except Exception:
            pass

        m.host_thread_id = thread.id
        self.gs(interaction.guild.id).created_thread_ids.append(thread.id)
        await self._save_all()

        planned_hhmm = fmt_hhmm_jst(from_iso(m.planned_time_utc) or planned_dt)

        embed = discord.Embed(description=m.custom_key)

        # まずは必ず本文＋ボタンを出す（画像生成が遅い/失敗しても止めない）
        try:
            msg = await thread.send(
                content=f"{interaction.user.mention}\nキーを確認し、待機列を作ってください。\n\n🔒カスタムキー＜コピペ用＞",
                embed=embed,
                view=WaitlistCompleteView(self),
            )
            m.host_message_id = msg.id
            await self._save_all()
        except Exception as e:
            print(f"[ERR] host thread initial send failed: {e}")
            await _ephemeral_reply(interaction, f"スレッドに送信できません: {e}")
            return

        # 画像は後追いで生成して送る（失敗しても無視して継続）
        if self.cfg(interaction.guild.id).image_enabled:
            try:
                png = await img_host_planned(m.match_no, m.custom_key, planned_hhmm)
                if png:
                    await thread.send(files=[discord.File(fp=io.BytesIO(png), filename="host.png")])
            except Exception as e:
                print(f"[WARN] host image render/send failed: {e}")

        await _ephemeral_reply(interaction, "キーホストを確定しました。")

    async def handle_waitlist_complete(self, interaction: discord.Interaction):
        if not interaction.guild:
            await _ephemeral_reply(interaction, "サーバー内で実行してください。")
            return

        await _safe_defer(interaction, ephemeral=True)

        m = self.active_match(interaction.guild.id)
        if not m or not m.host_thread_id:
            await _ephemeral_reply(interaction, "対象スレッドが見つかりません。")
            return
        if not interaction.channel or interaction.channel.id != m.host_thread_id:
            await _ephemeral_reply(interaction, "このボタンはキーホスト用スレッド内で押してください。")
            return
        if m.confirmed:
            await _ephemeral_reply(interaction, "既に確定済みです。")
            return

        confirmed_at = utc_now()
        confirmed_time = confirmed_at + datetime.timedelta(minutes=2)

        planned_dt = from_iso(m.planned_time_utc) or (confirmed_at + datetime.timedelta(minutes=3))
        if planned_dt > confirmed_time:
            planned_dt = confirmed_time
            m.planned_time_utc = to_iso(planned_dt)

        m.confirmed = True
        m.confirmed_at = to_iso(confirmed_at)
        m.confirmed_time_utc = to_iso(confirmed_time)
        m.thread_delete_at = to_iso(confirmed_time + datetime.timedelta(minutes=2))
        await self._save_all()

        confirmed_hhmm = fmt_hhmm_jst(confirmed_time)

        # disable pressed button
        try:
            if interaction.message:
                await interaction.message.edit(view=None)
        except Exception:
            pass

        # post confirmed card
        if self.cfg(interaction.guild.id).image_enabled:
            png = await img_host_confirmed(m.match_no, m.custom_key or "", confirmed_hhmm)
            if png:
                await interaction.channel.send(files=[discord.File(fp=io.BytesIO(png), filename="confirmed.png")])
            else:
                await interaction.channel.send(
                    "ーーーーーーーーーーーー\n"
                    f"📢開始時間は【{confirmed_hhmm}】で確定しました\n"
                    "時間になりましたらマッチ開始してください\n"
                    "ーーーーーーーーーーーー"
                )
        else:
            await interaction.channel.send(
                "ーーーーーーーーーーーー\n"
                f"📢開始時間は【{confirmed_hhmm}】で確定しました\n"
                "時間になりましたらマッチ開始してください\n"
                "ーーーーーーーーーーーー"
            )

        # post key view panel
        gch = await self.get_global_channel(interaction.guild)
        if gch:
            await self._post_key_view_panel(interaction.guild, gch)

        await _ephemeral_reply(interaction, "キー閲覧を開始しました。")

    async def handle_key_view(self, interaction: discord.Interaction):
        if not interaction.guild:
            await _ephemeral_reply(interaction, "サーバー内で実行してください。")
            return

        await _safe_defer(interaction, ephemeral=True)

        m = self.active_match(interaction.guild.id)
        if not m or not interaction.message or interaction.message.id != m.key_view_message_id:
            await _ephemeral_reply(interaction, "無効。最新のパネルから操作してください。")
            return

        member = interaction.guild.get_member(interaction.user.id)
        if not member or not member.voice or not member.voice.channel:
            await _ephemeral_reply(interaction, "VCに接続してから押してください。")
            return

        # "原則1度押し" -> count once per user but allow retry to show key
        if interaction.user.id not in set(m.pressed_user_ids):
            m.pressed_user_ids.append(interaction.user.id)

        vc_id = member.voice.channel.id
        counted: Set[int] = set(m.counted_vc_ids)
        before = len(counted)
        counted.add(vc_id)
        after = len(counted)
        if after != before:
            m.counted_vc_ids = list(counted)
            await self._save_all()

        # send ephemeral key
        files = []
        if self.cfg(interaction.guild.id).image_enabled:
            png = await img_key_ephemeral(m.match_no, m.custom_key or "")
            if png:
                files.append(discord.File(fp=io.BytesIO(png), filename="key.png"))

        embed = discord.Embed(description=m.custom_key)
        if files:
            await interaction.followup.send(content="🔒カスタムキー＜コピペ用＞", embed=embed, files=files, ephemeral=True)
        else:
            await interaction.followup.send(content="🔒カスタムキー＜コピペ用＞", embed=embed, ephemeral=True)

        # cap check (by vc id)
        if len(counted) >= self.viewer_limit(m.size_mode):
            # disable panel
            try:
                if interaction.message:
                    await interaction.message.edit(view=None)
            except Exception:
                pass

            # next match
            next_match = MatchState(match_no=m.match_no + 1, size_mode=m.size_mode, match_type=m.match_type)
            self.gs(interaction.guild.id).active_match = next_match
            await self._save_all()

            gch = await self.get_global_channel(interaction.guild)
            if gch:
                await self._post_host_recruit_panel(interaction.guild, gch)


def main():
    token = os.getenv("SCRIMKEY_TOKEN")
    if not token:
        raise RuntimeError("Environment variable SCRIMKEY_TOKEN is not set.")
    bot = ScrimBot()
    bot.run(token)


if __name__ == "__main__":
    main()

