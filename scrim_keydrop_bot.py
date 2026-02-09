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
import sqlite3
from dataclasses import dataclass, asdict
from typing import Optional, Dict, Any, Set, List

import discord
from collections import defaultdict
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

AUTOPOST_TODAY_PANEL = os.environ.get("SCRIM_TODAY_AUTOPOST", "1") != "0"
AUTOPOST_HOUR_JST = int(os.environ.get("SCRIM_TODAY_POST_HOUR_JST", "17"))
AUTOPOST_MINUTE_JST = int(os.environ.get("SCRIM_TODAY_POST_MINUTE_JST", "0"))

# 今日パネルを「何件ごとに分割するか」(例: 1なら 1件=1枚)
TODAY_PANEL_MAX_EVENTS_PER_PAGE = int(os.environ.get("SCRIM_TODAY_MAX_EVENTS_PER_PANEL", "1"))
if TODAY_PANEL_MAX_EVENTS_PER_PAGE <= 0:
    TODAY_PANEL_MAX_EVENTS_PER_PAGE = 1


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
GENERATED_KEYS_DIR = os.path.join(ASSETS_DIR, "generated_keys")
KEY_BG_PATH = os.path.join(ASSETS_DIR, "カスタムキー台紙.png")

SCRIM_CALENDAR_DB_PATH = os.environ.get("SCRIM_CALENDAR_DB_PATH", r"D:\DiscordBot\bots\scrim_calendar\scrim.db")

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


def _write_latest_key_images(png_bytes: bytes) -> tuple[str, str | None]:
    """Save png to assets/generated_keys as latest.png, keeping prev.png."""
    os.makedirs(GENERATED_KEYS_DIR, exist_ok=True)
    latest = os.path.join(GENERATED_KEYS_DIR, "latest.png")
    prev = os.path.join(GENERATED_KEYS_DIR, "prev.png")
    tmp = os.path.join(GENERATED_KEYS_DIR, "latest.tmp.png")

    if os.path.exists(latest):
        try:
            os.replace(latest, prev)
        except Exception:
            try:
                import shutil
                shutil.copy2(latest, prev)
            except Exception:
                pass

    with open(tmp, "wb") as f:
        f.write(png_bytes)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, latest)

    return latest, (prev if os.path.exists(prev) else None)


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
  :root {
    --bg:#07080c;
    --panel:#0d1017;
    --card:#0b0e14;
    --line: rgba(255,255,255,.08);
    --text:#eef1ff;
    --muted:#b4bbd8;
    --pill: rgba(255,255,255,.03);
  }
  *{box-sizing:border-box}
  body{
    margin:0;
    background:
      radial-gradient(1200px 700px at 30% -20%, rgba(255,255,255,.05), transparent 60%),
      radial-gradient(900px 600px at 90% 0%, rgba(214,178,108,.10), transparent 55%),
      linear-gradient(180deg, rgba(255,255,255,.03), transparent 35%),
      var(--bg);
    color:var(--text);
    font-family:"Noto Sans JP",system-ui,-apple-system,"Segoe UI",sans-serif;
  }
  .wrap{ width:600px; max-width:600px; margin:0 auto; }
  .panel{ max-width:600px; margin:0 auto;
    width:944px;
    background:
      linear-gradient(180deg, rgba(255,255,255,.05), rgba(255,255,255,.02) 45%, rgba(255,255,255,.01)),
      var(--panel);
    border:1px solid var(--line);
    border-radius:18px;
    box-shadow:0 18px 55px rgba(0,0,0,.60),0 0 0 1px rgba(0,0,0,.25) inset;
    overflow:hidden;
  }
  .head{padding:14px 16px;border-bottom:1px solid rgba(255,255,255,.06);display:flex;align-items:center;justify-content:space-between;gap:10px;}
  .head .h{font-size:18px;font-weight:900;letter-spacing:.04em;}
  .head .d{font-size:12px;color:var(--muted);padding:5px 10px;border:1px solid rgba(255,255,255,.10);border-radius:999px;background:var(--pill);}
  .list{padding:14px 14px 18px;display:flex;flex-direction:column;gap:12px;}
  .card{
    background:
      linear-gradient(180deg, rgba(255,255,255,.04), rgba(255,255,255,.015)),
      var(--card);
    border:1px solid rgba(255,255,255,.06);
    border-radius:16px;
    padding:14px 14px 12px;
    box-shadow:0 10px 26px rgba(0,0,0,.40),0 0 0 1px rgba(255,255,255,.03) inset;
  }
  .row{display:flex;align-items:center;gap:10px;font-weight:900;min-width:0}
  .ico{display:inline-flex;width:22px;height:22px;align-items:center;justify-content:center;font-size:16px;}
  .ico.none{opacity:.55}
  .title{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:760px;}
  .tags{margin-top:10px;display:flex;flex-wrap:wrap;gap:8px;font-size:12px;}
  .tag{background:var(--pill);border:1px solid rgba(255,255,255,.08);border-radius:999px;padding:6px 10px;white-space:nowrap;}
  .tag b{opacity:.85;}
  .note{margin-top:10px;color:var(--muted);font-size:12px;line-height:1.45;}
  .note b{color:var(--text);}

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
# Daily Scrim Panel Rendering (HTML -> PNG)  [NEW]
# =====================

def _scrim_panel_icon(style: str) -> str:
    s = (style or "").strip()
    if s == "回転式":
        return "🟠"
    if s == "従来式":
        return "🔵"
    # 登録しない / 未設定 / その他
    return ""

def _html_esc(s: Any) -> str:
    s = "" if s is None else str(s)
    return (
        s.replace("&", "&amp;")
         .replace("<", "&lt;")
         .replace(">", "&gt;")
         .replace('"', "&quot;")
         .replace("'", "&#39;")
    )


def _read_today_scrim_events_from_db(today_ymd: str) -> List[Dict[str, Any]]:
    """scrim_calendar の scrim.db から、当日(date=YYYY-MM-DD)の予定を読む"""
    db_path = SCRIM_CALENDAR_DB_PATH
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"scrim calendar DB not found: {db_path}")

    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """
        SELECT id, date, title, style, start_time, matches, mode_primary, mode_secondary, composite_json, note
        FROM events
        WHERE date = ?
        ORDER BY start_time, id
        """,
        (today_ymd,),
    ).fetchall()
    con.close()

    out: List[Dict[str, Any]] = []
    for r in rows:
        comp = []
        if r["composite_json"]:
            try:
                comp = json.loads(r["composite_json"])
            except Exception:
                comp = []
        if not isinstance(comp, list):
            comp = []

        out.append(
            {
                "id": r["id"],
                "title": r["title"] or "",
                "style": r["style"] or "登録しない",
                "start_time": r["start_time"] or "",
                "matches": r["matches"],
                "mode_primary": r["mode_primary"] or "",
                "mode_secondary": r["mode_secondary"] or "",
                "composite": comp,
                "note": r["note"] or "",
            }
        )
    return out



def _ensure_scrim_channel_map_table(db_path: str) -> None:
    # scrim名(=events.title) -> channel_id (複数可)
    con = sqlite3.connect(db_path)
    try:
        con.execute(
            "CREATE TABLE IF NOT EXISTS scrim_channel_map ("
            " guild_id INTEGER NOT NULL,"
            " scrim_name TEXT NOT NULL,"
            " channel_id INTEGER NOT NULL,"
            " PRIMARY KEY (guild_id, scrim_name, channel_id)"
            ")"
        )
        con.commit()
    finally:
        con.close()


def _lookup_scrim_channels_from_db(guild_id: int, scrim_name: str) -> List[int]:
    db_path = SCRIM_CALENDAR_DB_PATH
    if not os.path.exists(db_path):
        return []
    try:
        _ensure_scrim_channel_map_table(db_path)
    except Exception:
        return []
    con = sqlite3.connect(db_path)
    try:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT channel_id FROM scrim_channel_map WHERE guild_id = ? AND scrim_name = ? ORDER BY channel_id",
            (int(guild_id), str(scrim_name)),
        ).fetchall()
        out: List[int] = []
        for r in rows:
            try:
                out.append(int(r["channel_id"]))
            except Exception:
                pass
        return out
    except Exception:
        return []
    finally:
        con.close()
def _build_today_panel_html(
    today_ymd: str,
    events: List[Dict[str, Any]],
    page_no: int = 1,
    page_total: int = 1,
) -> str:
    """
    Discord投稿用：Webプレビュー版と同一レイアウト（上品ラグジュアリー / 1カラム）
    - Jinja2は使わずPythonでHTMLを生成する
    - page_no/page_total は互換用（表示しない）
    """
    updated_at = fmt_hhmm_jst(utc_now())

    date_badge = today_ymd.replace("-", "/") if isinstance(today_ymd, str) else str(today_ymd)
    server_name = ""

    if not events:
        cards_html = ('<div class="card"><div class="sub">本日の予定はありません</div></div>')
        # (fixed) empty-state card html
    else:
        parts: List[str] = []
        for e in events:
            icon = _scrim_panel_icon(e.get("style", ""))
            icon_html = f'<span class="ico">{_html_esc(icon)}</span>' if icon else ''

            title = _html_esc(e.get("title", ""))
            style = _html_esc(e.get("style", "")) or "登録しない"
            start = _html_esc(e.get("start_time", "")) or "未定"

            mode1 = _html_esc(e.get("mode_primary", "")) or "—"
            mode2 = _html_esc(e.get("mode_secondary", "")) or "—"

            tags: List[str] = [
                f'<span class="tag"><strong>開始</strong> {start}</span>',
                f'<span class="tag"><strong>方式</strong> {style}</span>',
            ]
            if e.get("style") == "従来式":
                tags.append(f'<span class="tag"><strong>試合</strong> {_html_esc(e.get("matches") or 0)}</span>')
            tags.append(f'<span class="tag"><strong>モード</strong> {mode1} / {mode2}</span>')

            comp_html = ""
            if (e.get("mode_secondary") == "複合") and e.get("composite"):
                lines: List[str] = []
                for x in e.get("composite", []):
                    if not isinstance(x, dict):
                        continue
                    md = _html_esc(x.get("mode", ""))
                    try:
                        mm = int(x.get("matches") or 0)
                    except Exception:
                        mm = 0
                    if md:
                        lines.append(f"・{md} {mm}試合")
                if lines:
                    comp_html = '<div class="note"><b>複合内訳</b><br>' + "<br>".join(lines) + "</div>"

            note_html = ""
            note = (e.get("note") or "").strip()
            if note:
                note_html = f'<div class="note"><b>備考</b> {_html_esc(note)}</div>'

            card = (
                '<div class="card">'
                '<div class="row1"><div class="name">'
                f'{icon_html}'
                f'<span class="truncate">{title}</span>'
                '</div></div>'
                f'<div class="meta">{"".join(tags)}</div>'
                f'{comp_html}'
                f'{note_html}'
                '</div>'
            )
            parts.append(card)

        cards_html = "\n".join(parts)

    RAW_TODAY_PANEL_TEMPLATE = """<!doctype html>
<html lang="ja">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>本日のスクリム情報</title>

  <style>
    :root{
      --bg:#07080c;
      --card:#0d1017;
      --card2:#0b0e14;

      --text:#eef1ff;
      --muted:#b4bbd8;

      --line: rgba(255,255,255,.08);
      --lineSoft: rgba(255,255,255,.06);

      --gold: rgba(214,178,108,.55);
      --goldSoft: rgba(214,178,108,.20);

      --pill: rgba(255,255,255,.03);
    }

    *{ box-sizing:border-box }

    body{
      margin:0;
      background:
        radial-gradient(1200px 700px at 30% -20%, rgba(255,255,255,.05), transparent 60%),
        radial-gradient(900px 600px at 90% 0%, rgba(214,178,108,.10), transparent 55%),
        linear-gradient(180deg, rgba(255,255,255,.03), transparent 35%),
        var(--bg);
      color:var(--text);
      font-family: system-ui, -apple-system, Segoe UI, Roboto, "Noto Sans JP", sans-serif;
      padding:10px;
    }

    

    .wrap{ width:600px; margin:0; }
/* Discord用：左右の“半分空き”を無くす */
/* ===== 上部（横に散らさず、1ブロック化） ===== */
    .top{
      display:flex;
      flex-direction:column;
      align-items:flex-start;
      justify-content:flex-start;
      gap:4px;
      margin-bottom:10px;
      position:relative;
      padding-bottom:8px;
    }
    .top::after{
      content:"";
      position:absolute;
      left:0; right:0; bottom:0;
      height:1px;
      background: linear-gradient(90deg, transparent, rgba(214,178,108,.35), transparent);
    }

    .title{ display:flex; flex-direction:column; gap:4px }

    h1{
      margin:0;
      font-size:22px;
      letter-spacing:.7px;
      display:flex;
      align-items:center;
      gap:10px;
      text-shadow: 0 10px 30px rgba(0,0,0,.45);
    }

    .badge{
      font-size:12px;
      padding:4px 10px;
      border-radius:999px;
      background: linear-gradient(180deg, rgba(214,178,108,.24), rgba(214,178,108,.10));
      border:1px solid rgba(214,178,108,.28);
      box-shadow: 0 10px 25px rgba(0,0,0,.35);
    }

    .sub{
      color:var(--muted);
      font-size:12px;
      line-height:1.35;
    }

    .legend{
      display:flex;
      gap:8px;
      flex-wrap:wrap;
      justify-content:flex-start;
      font-size:12px;
      color:var(--muted);
    }
    .legend span{
      background: rgba(255,255,255,.025);
      border:1px solid var(--lineSoft);
      padding:4px 10px;
      border-radius:999px;
    }

    /* ===== レイアウト ===== */
    .grid{
      display:grid;
      grid-template-columns:1fr;
      gap:10px;
    }

    /* ===== パネル ===== */
    .panel{
      position:relative;
      background:
        linear-gradient(180deg, rgba(255,255,255,.05), rgba(255,255,255,.02) 45%, rgba(255,255,255,.01)),
        var(--card);
      border:1px solid var(--line);
      border-radius:18px;
      overflow:hidden;
      box-shadow:
        0 18px 55px rgba(0,0,0,.60),
        0 0 0 1px rgba(0,0,0,.25) inset;
    }
    .panel::before{
      content:"";
      position:absolute;
      inset:0;
      background:
        radial-gradient(900px 200px at 20% 0%, rgba(255,255,255,.10), transparent 60%),
        radial-gradient(800px 220px at 85% 0%, rgba(214,178,108,.10), transparent 62%);
      opacity:.55;
      pointer-events:none;
    }

    .panelHead{
      padding:10px 12px;
      border-bottom:1px solid var(--lineSoft);
      display:flex;
      align-items:center;
      justify-content:space-between;
      gap:10px;
      background: linear-gradient(180deg, rgba(255,255,255,.05), rgba(255,255,255,.015));
    }
    .panelHead b{
      font-size:13px;
      letter-spacing:.7px;
    }

    .pill{
      background: var(--pill);
      border:1px solid var(--lineSoft);
      border-radius:999px;
      padding:4px 10px;
      font-size:11px;
      color:var(--muted);
      opacity:.65;
    }

    /* ===== カード（1件用：余白を詰める） ===== */
    .list{
      padding:10px;
      display:flex;
      flex-direction:column;
      gap:8px;
    }

    .card{
      background:
        linear-gradient(180deg, rgba(255,255,255,.04), rgba(255,255,255,.015)),
        var(--card2);
      border:1px solid var(--lineSoft);
      border-radius:16px;
      padding:10px;
      box-shadow:
        0 10px 26px rgba(0,0,0,.40),
        0 0 0 1px rgba(255,255,255,.03) inset;
    }

    .row1{
      display:flex;
      justify-content:space-between;
      gap:10px;
    }

    .name{
      font-size:15px;
      font-weight:900;
      display:flex;
      align-items:center;
      gap:8px;
      min-width:0;
    }
    .truncate{
      white-space:nowrap;
      overflow:hidden;
      text-overflow:ellipsis;
    }

    .ico{ font-size:16px }
    .ico.none{ opacity:.5 }

    .meta{
      margin-top:8px;
      display:flex;
      flex-wrap:wrap;
      gap:6px;
      font-size:12px;
      color:var(--muted);
    }

    .tag{
      background: var(--pill);
      border:1px solid var(--lineSoft);
      border-radius:999px;
      padding:4px 10px;
    }
    .tag strong{ color:var(--text) }

    .note{
      margin-top:10px;
      font-size:12px;
      color:var(--muted);
      line-height:1.45;
      border-top:1px dashed rgba(255,255,255,.15);
      padding-top:10px;
    }

    /* Discord用：フッターは出さない */
    .footer{ display:none; }
  </style>
</head>

<body>
<div class="wrap">
</div>
</div>

  <div class="grid">
    <div class="panel">
      <div class="panelHead">
        <b>本日のスクリム情報 {date_badge}</b>
        
      </div>

      <div class="list">
        {cards_html}
      </div>
    </div>
  </div>

  <div class="footer">
    <span class="smallPill">Tip: タイトルが長い場合は自動で省略表示</span>
    <span class="smallPill">表示内容は運用に合わせて調整可能</span>
  </div>

</div>
</body>
</html>
"""

    return _build_html(
        RAW_TODAY_PANEL_TEMPLATE,
        date_badge=_html_esc(date_badge),
        updated_at=_html_esc(updated_at),
        count=str(len(events)),
        server_name=_html_esc(server_name),
        cards_html=cards_html,
    )


async def _try_render_png_from_html_panel(html: str) -> Optional[bytes]:
    """Render HTML -> PNG (Discord用): 横幅600px厳守で.panelを切り抜く"""
    try:
        from playwright.async_api import async_playwright
    except Exception:
        return None

    html_path = None
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            page = await browser.new_page(viewport={"width": 600, "height": 900}, device_scale_factor=2)

            with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False, encoding="utf-8") as f:
                f.write(html)
                html_path = f.name

            await page.goto("file://" + html_path)
            try:
                await page.wait_for_selector(".panel", timeout=2000)
            except Exception:
                pass
            await page.wait_for_timeout(200)

            panel = page.locator(".panel")
            box = await panel.bounding_box()
            if not box:
                png = await page.screenshot(type="png")
                await browser.close()
                return png

            pad = 12
            x = max(0, int(box["x"]) - pad)
            y = max(0, int(box["y"]) - pad)
            h = int(box["height"]) + pad * 2

            png = await page.screenshot(
                type="png",
                clip={
                    "x": 0,
                    "y": y,
                    "width": 600,
                    "height": h,
                },
            )
            await browser.close()
            return png
    except Exception as e:
        print(f"[WARN] today panel render failed: {e}")
        return None
    finally:
        if html_path:
            try:
                os.remove(html_path)
            except Exception:
                pass

def _chunk_list(items: List[Any], n: int) -> List[List[Any]]:
    if n <= 0:
        n = 1
    return [items[i : i + n] for i in range(0, len(items), n)]


async def render_today_scrim_panel_png_pages(
    today_ymd: Optional[str] = None,
    *,
    max_events_per_page: Optional[int] = None,
) -> List[bytes]:
    """今日の予定を、N件ごとに分割して PNG(bytes) のリストで返す"""
    if not today_ymd:
        today_ymd = jst_date_str(utc_now())

    if max_events_per_page is None:
        max_events_per_page = TODAY_PANEL_MAX_EVENTS_PER_PAGE
    if max_events_per_page <= 0:
        max_events_per_page = 1

    events = _read_today_scrim_events_from_db(today_ymd)
    pages = _chunk_list(events, int(max_events_per_page))
    if not pages:
        pages = [[]]

    out: List[bytes] = []
    total = len(pages)
    for idx, evs in enumerate(pages, start=1):
        html = _build_today_panel_html(today_ymd, evs, page_no=idx, page_total=total)
        png = await _try_render_png_from_html_panel(html)
        if not png:
            raise RuntimeError("panel render failed (playwright not available?)")
        out.append(png)
    return out


async def render_today_scrim_panel_png(today_ymd: Optional[str] = None) -> bytes:
    """今日の予定をDBから集計し、Discord投稿用PNG(bytes)を返す"""
    if not today_ymd:
        today_ymd = jst_date_str(utc_now())

    events = _read_today_scrim_events_from_db(today_ymd)
    html = _build_today_panel_html(today_ymd, events)

    png = await _try_render_png_from_html_panel(html)
    if not png:
        raise RuntimeError("panel render failed (playwright not available?)")
    return png

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

    # 告知メッセージ（最後に投稿したもの）
    announce_message_id: Optional[int] = None
    announce_channel_id: Optional[int] = None

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
# Today Panel Buttons (per-scrim channels)
# =====================

class TodayTraditionalChannelView(discord.ui.View):
    def __init__(self, scrim_name: str):
        super().__init__(timeout=None)
        self.scrim_name = scrim_name

    @discord.ui.button(label="キーホスト募集", style=discord.ButtonStyle.primary, custom_id="scrim:today_trad_host_recruit")
    async def recruit(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            f"このスクリム（{self.scrim_name}）のキーホスト募集は、運営コマンドで開始してください。",
            ephemeral=True,
        )


class TodayRotationChannelView(discord.ui.View):
    def __init__(self, scrim_name: str):
        super().__init__(timeout=None)
        self.scrim_name = scrim_name

    @discord.ui.button(label="1試合目の枠予約", style=discord.ButtonStyle.primary, custom_id="scrim:today_rotation_match1_reserve")
    async def reserve(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            f"このスクリム（{self.scrim_name}）の1試合目枠予約は、運営コマンドで開始してください。",
            ephemeral=True,
        )


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
# Traditional Host Controls on Announcement (persistent)
# =====================

class TradHostRecruitButton(discord.ui.Button):
    def __init__(self, enabled: bool):
        style = discord.ButtonStyle.primary if enabled else discord.ButtonStyle.gray
        super().__init__(
            label="キーホストします",
            style=style,
            custom_id="scrim:trad_host_recruit",
            disabled=(not enabled),
        )

    async def callback(self, interaction: discord.Interaction):
        if not interaction.guild or not interaction.message:
            await interaction.response.defer()
            return
        bot: ScrimBot = interaction.client  # type: ignore
        cfg = bot.cfg(interaction.guild.id)
        scrim = cfg.scrim or {}

        if scrim.get("trad_host_user_id"):
            await interaction.response.send_message("既にキーホストは見つかっています。", ephemeral=True)
            return

        scrim["trad_host_user_id"] = interaction.user.id
        scrim["trad_host_selected_at"] = to_iso(utc_now())
        await bot._save_all()

        members = cfg.participations.get(str(interaction.message.id), []) or []
        embed = _announce_embed(interaction.guild, scrim, members)
        await interaction.response.edit_message(embed=embed, view=TraditionalAnnounceView(has_host=True))


class TradHostCancelButton(discord.ui.Button):
    def __init__(self, enabled: bool):
        style = discord.ButtonStyle.danger if enabled else discord.ButtonStyle.gray
        super().__init__(
            label="キーホストキャンセル",
            style=style,
            custom_id="scrim:trad_host_cancel",
            disabled=(not enabled),
        )

    async def callback(self, interaction: discord.Interaction):
        if not interaction.guild or not interaction.message:
            await interaction.response.defer()
            return
        bot: ScrimBot = interaction.client  # type: ignore
        cfg = bot.cfg(interaction.guild.id)
        scrim = cfg.scrim or {}

        host_id = scrim.get("trad_host_user_id")
        if not host_id:
            await interaction.response.send_message("現在、キーホストは募集中です。", ephemeral=True)
            return

        member = interaction.guild.get_member(interaction.user.id)
        can = (interaction.user.id == int(host_id))
        if member and member.guild_permissions and member.guild_permissions.manage_guild:
            can = True

        if not can:
            await interaction.response.send_message("キーホスト本人、または運営のみキャンセルできます。", ephemeral=True)
            return

        scrim.pop("trad_host_user_id", None)
        scrim.pop("trad_host_selected_at", None)
        await bot._save_all()

        members = cfg.participations.get(str(interaction.message.id), []) or []
        embed = _announce_embed(interaction.guild, scrim, members)
        await interaction.response.edit_message(embed=embed, view=TraditionalAnnounceView(has_host=False))


class TraditionalAnnounceView(discord.ui.View):
    def __init__(self, has_host: bool = False):
        super().__init__(timeout=None)
        self.add_item(TradHostRecruitButton(enabled=(not has_host)))
        self.add_item(TradHostCancelButton(enabled=has_host))



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

    start_raw = scrim.get("start_at_jst") or "未設定"
    start = start_raw
    try:
        if start_raw and start_raw != "未設定":
            # accept "YYYY-MM-DD HH:MM" or "YYYY/MM/DD HH:MM"
            date_part = start_raw.split(" ")[0].replace("/", "-")
            time_part = start_raw.split(" ")[1] if " " in start_raw else ""
            y, mo, d = date_part.split("-")
            import datetime as _dt
            wd = ["月", "火", "水", "木", "金", "土", "日"][_dt.date(int(y), int(mo), int(d)).weekday()]
            if time_part:
                start = f"{int(y):04d}/{int(mo):02d}/{int(d):02d}({wd}) {time_part} ～"
            else:
                start = f"{int(y):04d}/{int(mo):02d}/{int(d):02d}({wd}) ～"
    except Exception:
        start = start_raw

    team = scrim.get("team_mode")
    game = scrim.get("game_mode")
    team_label = {"solo": "ソロ", "duo": "デュオ", "trio": "トリオ", "squad": "スクワッド"}.get(team, "未設定")
    game_label = {"tournament": "トーナメントセッティング", "reload": "リロード"}.get(game, "")
    mode = f"{team_label}（{game_label}）" if game_label else team_label

    system_key = scrim.get("system")
    system_label = {"rotation": "回転型", "traditional": "従来型"}.get(system_key, "未設定")

    e = discord.Embed(title="⚔本日開催のスクリム", color=discord.Color.orange())

    e.add_field(name="開催団体：", value=org, inline=False)
    e.add_field(name="開始日時：", value=start, inline=False)
    e.add_field(name="モード：", value=(scrim.get("mode_text") or mode), inline=False)
    e.add_field(name="開催方式：", value=system_label, inline=False)

    if system_key == "rotation":
        e.description = "💡21:00より1試合目の参加枠受付を開始します。"
        return e

    if system_key == "traditional":
        hid = scrim.get("trad_host_user_id")
        host_line = "見つかりました" if hid else "募集中"
        e.add_field(name="キーホスト：", value=host_line, inline=False)

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

        cfg = self.bot.cfg(self.guild_id)
        announce_active = bool(cfg.announce_message_id)

        self.add_item(SetStartButton())

        self.add_item(SystemToggleButton("rotation", _is_selected("回転式", system == "rotation")))
        self.add_item(SystemToggleButton("traditional", _is_selected("従来式", system == "traditional")))
        self.add_item(SetMatchCountButton(enabled=(system == "traditional")))

        self.add_item(TeamToggleButton("solo", _is_selected("ソロ", team == "solo")))
        self.add_item(TeamToggleButton("duo", _is_selected("デュオ", team == "duo")))
        self.add_item(TeamToggleButton("trio", _is_selected("トリオ", team == "trio")))
        self.add_item(TeamToggleButton("squad", _is_selected("スクワッド", team == "squad")))
        if system == "traditional":
            self.add_item(SetTraditionalMultiButton())
        else:
            self.add_item(discord.ui.Button(label="複数モード", style=discord.ButtonStyle.gray, row=2, disabled=True))

        self.add_item(GameToggleButton("tournament", _is_selected("トーナメントセッティング", game == "tournament")))
        self.add_item(GameToggleButton("reload", _is_selected("リロード", game == "reload")))

        self.add_item(AnnounceButton(enabled=(not announce_active)))
        self.add_item(DeleteAnnounceButton(enabled=announce_active))
        self.add_item(ResetScrimButton(enabled=(not announce_active)))

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


class SetTraditionalMultiButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="複数モード", style=discord.ButtonStyle.secondary, row=2, custom_id="scrimadmin:tradmulti")

    async def callback(self, interaction: discord.Interaction):
        view: ScrimAdminPanelView = self.view  # type: ignore
        if view.scrim().get("system") != "traditional":
            await interaction.response.defer()
            return

        class MultiModeModal(discord.ui.Modal, title="複数モード表記"):
            def __init__(self, parent_view):
                super().__init__(timeout=None)
                self.parent_view = parent_view
                self.value = discord.ui.TextInput(
                    label="モード表記（例：ソロ 6戦 / デュオ 4戦）",
                    required=True,
                    max_length=100,
                )
                self.add_item(self.value)

            async def on_submit(self, modal_interaction: discord.Interaction):
                view = self.parent_view
                view.scrim()["mode_text"] = str(self.value).strip()
                await view.bot._save_all()
                await view.refresh(modal_interaction)
                await modal_interaction.response.defer()

        await interaction.response.send_modal(MultiModeModal(self.view))


class SetMatchCountButton(discord.ui.Button):
    def __init__(self, enabled: bool):
        style = discord.ButtonStyle.secondary if enabled else discord.ButtonStyle.gray
        super().__init__(label="従来式試合数", style=style, row=1, disabled=(not enabled), custom_id="scrimadmin:matchcount")

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
    def __init__(self, enabled: bool = True):
        super().__init__(label="告知投稿", style=discord.ButtonStyle.primary, row=4, custom_id="scrimadmin:announce", disabled=(not enabled))

    async def callback(self, interaction: discord.Interaction):
        view: ScrimAdminPanelView = self.view  # type: ignore
        if not interaction.guild:
            await interaction.response.defer()
            return

        cfg = view.bot.cfg(interaction.guild.id)

        # 既に告知があるなら何もしない（削除のみ有効）
        if cfg.announce_message_id:
            await interaction.response.defer()
            return

        # 告知先：global_channel があればそこ、なければ管理パネルのチャンネル
        ch = interaction.channel
        gch = await view.bot.get_global_channel(interaction.guild)
        if gch:
            ch = gch

        embed = _announce_embed(interaction.guild, cfg.scrim, [])
        # 告知のView：回転型は参加/キャンセル、従来式はキーホスト募集/キャンセル
        system = (cfg.scrim or {}).get("system")
        if system == "rotation":
            msg = await ch.send(embed=embed)
            cfg.participations[str(msg.id)] = []
        elif system == "traditional":
            msg = await ch.send(embed=embed, view=TraditionalAnnounceView(has_host=bool((cfg.scrim or {}).get("trad_host_user_id"))))
            cfg.participations.setdefault(str(msg.id), [])
        else:
            msg = await ch.send(embed=embed)
        cfg.announce_message_id = msg.id
        cfg.announce_channel_id = msg.channel.id
        await view.bot._save_all()

        # 管理パネル更新：告知投稿/リセット無効、削除のみ有効
        await view.refresh(interaction, use_edit_message=True)
        if not interaction.response.is_done():
            await interaction.response.defer()


class DeleteAnnounceButton(discord.ui.Button):
    def __init__(self, enabled: bool = True):
        super().__init__(label="削除", style=discord.ButtonStyle.secondary, row=4, custom_id="scrimadmin:delete", disabled=(not enabled))

    async def callback(self, interaction: discord.Interaction):
        view: ScrimAdminPanelView = self.view  # type: ignore
        if not interaction.guild:
            await interaction.response.defer()
            return

        cfg = view.bot.cfg(interaction.guild.id)

        if cfg.announce_message_id and cfg.announce_channel_id:
            ch = interaction.guild.get_channel(cfg.announce_channel_id)
            if isinstance(ch, discord.TextChannel):
                try:
                    msg = await ch.fetch_message(cfg.announce_message_id)
                    await msg.delete()
                except Exception:
                    pass

        cfg.announce_message_id = None
        cfg.announce_channel_id = None
        await view.bot._save_all()

        # 管理パネル更新：告知投稿/リセットを有効化
        await view.refresh(interaction, use_edit_message=True)
        if not interaction.response.is_done():
            await interaction.response.defer()


class ResetScrimButton(discord.ui.Button):
    def __init__(self, enabled: bool = True):
        super().__init__(label="リセット", style=discord.ButtonStyle.danger, row=4, custom_id="scrimadmin:reset", disabled=(not enabled))

    async def callback(self, interaction: discord.Interaction):
        view: ScrimAdminPanelView = self.view  # type: ignore
        view.bot.cfg(view.guild_id).scrim = {}
        await view.bot._save_all()
        await view.refresh(interaction)
        if not interaction.response.is_done():
            await interaction.response.defer()

# =====================
# Bot
# =====================

class ScrimBot(commands.Bot):
    async def on_interaction(self, interaction: discord.Interaction):
        # 既定の挙動は discord.py 側で処理されるため、ここでレスポンス削除などは行わない
        # （delete_original_response をここで行うと、コマンド結果がチャンネルに出ない等の不具合になる）
        return

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
        self._today_panel_last_post: Dict[int, str] = {}
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
                announce_message_id=v.get("announce_message_id"),
                announce_channel_id=v.get("announce_channel_id"),
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
        self.add_view(TraditionalAnnounceView())

        # generated key images cache
        os.makedirs(GENERATED_KEYS_DIR, exist_ok=True)
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
        for g in self.guilds:
            await self.tree.sync(guild=discord.Object(id=g.id))
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
                await self._auto_post_today_panel_if_due()
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
    async def _auto_post_today_panel_if_due(self):
        if not AUTOPOST_TODAY_PANEL:
            return

        now = utc_now()
        now_jst = to_jst(now)
        if (now_jst.hour, now_jst.minute) != (AUTOPOST_HOUR_JST, AUTOPOST_MINUTE_JST):
            return

        today = jst_date_str(now)

        for guild in list(self.guilds):
            if self._today_panel_last_post.get(guild.id) == today:
                continue

            gch = await self.get_global_channel(guild)
            if not gch:
                continue

            try:
                events = _read_today_scrim_events_from_db(today)
            except Exception as e:
                print(f"[AUTOPOST] read events failed ({guild.id}): {e}")
                continue

            try:
                # ① 全体用チャンネル：全件まとめて1枚（サマリーなし）
                html_all = _build_today_panel_html(today, events, page_no=1, page_total=1)
                png_all = await _try_render_png_from_html_panel(html_all)
                if not png_all:
                    raise RuntimeError("panel render failed")
                file_all = discord.File(fp=io.BytesIO(png_all), filename="today_scrim_all.png")
                await gch.send(file=file_all)

                # ② 団体別チャンネル：個別（1件=1枚）を送信
                for e in events:
                    scrim_name = str(e.get("title") or "").strip()
                    if not scrim_name:
                        continue

                    channel_ids = _lookup_scrim_channels_from_db(guild.id, scrim_name)
                    if not channel_ids:
                        continue

                    html_one = _build_today_panel_html(today, [e], page_no=1, page_total=1)
                    png_one = await _try_render_png_from_html_panel(html_one)
                    if not png_one:
                        continue

                    style = str(e.get("style") or "").strip()
                    view = None
                    if style == "従来式":
                        view = TodayTraditionalChannelView(scrim_name)
                    elif style == "回転式":
                        view = TodayRotationChannelView(scrim_name)

                    for cid in channel_ids:
                        ch = guild.get_channel(int(cid)) or self.get_channel(int(cid))
                        if not isinstance(ch, (discord.TextChannel, discord.Thread)):
                            continue
                        file_one = discord.File(fp=io.BytesIO(png_one), filename="today_scrim.png")
                        try:
                            await ch.send(file=file_one, view=view)  # type: ignore
                        except Exception:
                            try:
                                await ch.send(file=file_one)  # type: ignore
                            except Exception:
                                pass

                self._today_panel_last_post[guild.id] = today

            except Exception as e:
                print(f"[AUTOPOST] post failed ({guild.id}): {e}")
                continue

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


        @self.tree.command(name="scrim_channel_add", description="団体別チャンネルを登録（スクリム名→チャンネル）")
        async def scrim_channel_add(interaction: discord.Interaction, scrim_name: str, channel: discord.TextChannel):
            if not interaction.guild:
                await interaction.response.defer()
                return
            perms = getattr(interaction.user, "guild_permissions", None)
            if not (perms and perms.manage_guild):
                await interaction.response.send_message("権限がありません。", ephemeral=True)
                return

            name = (scrim_name or "").strip()
            if not name:
                await interaction.response.send_message("scrim_name が空です。", ephemeral=True)
                return

            db_path = SCRIM_CALENDAR_DB_PATH
            try:
                _ensure_scrim_channel_map_table(db_path)
                con = sqlite3.connect(db_path)
                try:
                    con.execute(
                        "INSERT OR IGNORE INTO scrim_channel_map (guild_id, scrim_name, channel_id) VALUES (?,?,?)",
                        (int(interaction.guild.id), name, int(channel.id)),
                    )
                    con.commit()
                finally:
                    con.close()
            except Exception as e:
                await interaction.response.send_message(f"登録に失敗しました: {e}", ephemeral=True)
                return

            await interaction.response.send_message(f"登録しました: **{name}** → {channel.mention}", ephemeral=True)

        @self.tree.command(name="scrim_channel_remove", description="団体別チャンネルを削除（スクリム名→チャンネル）")
        async def scrim_channel_remove(interaction: discord.Interaction, scrim_name: str, channel: discord.TextChannel):
            if not interaction.guild:
                await interaction.response.defer()
                return
            perms = getattr(interaction.user, "guild_permissions", None)
            if not (perms and perms.manage_guild):
                await interaction.response.send_message("権限がありません。", ephemeral=True)
                return

            name = (scrim_name or "").strip()
            if not name:
                await interaction.response.send_message("scrim_name が空です。", ephemeral=True)
                return

            db_path = SCRIM_CALENDAR_DB_PATH
            try:
                _ensure_scrim_channel_map_table(db_path)
                con = sqlite3.connect(db_path)
                try:
                    cur = con.execute(
                        "DELETE FROM scrim_channel_map WHERE guild_id = ? AND scrim_name = ? AND channel_id = ?",
                        (int(interaction.guild.id), name, int(channel.id)),
                    )
                    con.commit()
                finally:
                    con.close()
            except Exception as e:
                await interaction.response.send_message(f"削除に失敗しました: {e}", ephemeral=True)
                return

            if cur.rowcount == 0:
                await interaction.response.send_message("該当する登録が見つかりませんでした。", ephemeral=True)
                return

            await interaction.response.send_message(f"削除しました: **{name}** → {channel.mention}", ephemeral=True)

        @self.tree.command(name="scrim_channel_list", description="団体別チャンネル登録一覧を表示")
        async def scrim_channel_list(interaction: discord.Interaction, scrim_name: str = ""):
            if not interaction.guild:
                await interaction.response.defer()
                return
            perms = getattr(interaction.user, "guild_permissions", None)
            if not (perms and perms.manage_guild):
                await interaction.response.send_message("権限がありません。", ephemeral=True)
                return

            name = (scrim_name or "").strip()
            db_path = SCRIM_CALENDAR_DB_PATH
            try:
                _ensure_scrim_channel_map_table(db_path)
                con = sqlite3.connect(db_path)
                try:
                    con.row_factory = sqlite3.Row
                    if name:
                        rows = con.execute(
                            "SELECT scrim_name, channel_id FROM scrim_channel_map WHERE guild_id = ? AND scrim_name = ? ORDER BY scrim_name, channel_id",
                            (int(interaction.guild.id), name),
                        ).fetchall()
                    else:
                        rows = con.execute(
                            "SELECT scrim_name, channel_id FROM scrim_channel_map WHERE guild_id = ? ORDER BY scrim_name, channel_id",
                            (int(interaction.guild.id),),
                        ).fetchall()
                finally:
                    con.close()
            except Exception as e:
                await interaction.response.send_message(f"取得に失敗しました: {e}", ephemeral=True)
                return

            if not rows:
                await interaction.response.send_message("登録はありません。", ephemeral=True)
                return

            # 表示（最大2000文字に収める）
            lines = []
            for r in rows:
                sn = str(r["scrim_name"])
                cid = int(r["channel_id"])
                lines.append(f"- **{sn}** → <#{cid}>")
            msg = "\n".join(lines)
            if len(msg) > 1900:
                msg = msg[:1900] + "\n...(省略)"
            await interaction.response.send_message(msg, ephemeral=True)

        @self.tree.command(name="scrim_today", description="本日の自動投稿を手動で実行（全体1枚＋団体別個別）")
        async def scrim_today(interaction: discord.Interaction):
            if not interaction.guild:
                await interaction.response.defer()
                return
            # 管理者（サーバー管理）権限のみ
            perms = getattr(interaction.user, "guild_permissions", None)
            if not (perms and perms.manage_guild):
                await interaction.response.send_message("権限がありません。", ephemeral=True)
                return

            await interaction.response.defer(thinking=True, ephemeral=True)

            guild = interaction.guild
            gch = await self.get_global_channel(guild)
            if not gch:
                await interaction.followup.send("全体チャンネルが未設定です。/scrim_set_channel で設定してください。", ephemeral=True)
                return

            now = utc_now()
            today = jst_date_str(now)

            try:
                events = _read_today_scrim_events_from_db(today)
            except Exception as e:
                await interaction.followup.send(f"DB読込に失敗しました: {e}", ephemeral=True)
                return

            try:
                # ① 全体用チャンネル：全件まとめて1枚（サマリーなし）
                html_all = _build_today_panel_html(today, events, page_no=1, page_total=1)
                png_all = await _try_render_png_from_html_panel(html_all)
                if not png_all:
                    raise RuntimeError("panel render failed")
                file_all = discord.File(fp=io.BytesIO(png_all), filename="today_scrim_all.png")
                await gch.send(file=file_all)

                # ② 団体別チャンネル：個別（1件=1枚）を送信（未登録はスキップ）
                for e in events:
                    scrim_name = str(e.get("title") or "").strip()
                    if not scrim_name:
                        continue

                    channel_ids = _lookup_scrim_channels_from_db(guild.id, scrim_name)
                    if not channel_ids:
                        continue  # スキップ

                    html_one = _build_today_panel_html(today, [e], page_no=1, page_total=1)
                    png_one = await _try_render_png_from_html_panel(html_one)
                    if not png_one:
                        continue

                    view = None
                    _style = str(e.get("style") or "").strip()
                    if _style == "従来式":
                        view = TodayTraditionalChannelView(scrim_name)
                    elif _style == "回転式":
                        view = TodayRotationChannelView(scrim_name)

                    for cid in channel_ids:
                        ch = guild.get_channel(int(cid)) or self.get_channel(int(cid))
                        if not isinstance(ch, (discord.TextChannel, discord.Thread)):
                            continue
                        file_one = discord.File(fp=io.BytesIO(png_one), filename="today_scrim.png")
                        try:
                            await ch.send(file=file_one, view=view)  # type: ignore
                        except Exception:
                            try:
                                await ch.send(file=file_one)  # type: ignore
                            except Exception:
                                pass

            except Exception as e:
                await interaction.followup.send(f"投稿に失敗しました: {e}", ephemeral=True)
                return

            await interaction.followup.send("手動投稿しました。", ephemeral=True)

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
        # Posting: Today Scrim Panel (command)
        # =====================
        # (removed legacy tree.command scrim_today: use app_commands Cog version)


        @self.tree.command(name="scrim_today_preview", description="本日のスクリム情報（プレビュー）を画像で投稿")
        async def scrim_today_preview(interaction: discord.Interaction):
            # 先に応答してタイムアウト回避（エフェメラルは使わない）
            try:
                if not interaction.response.is_done():
                    await interaction.response.defer(ephemeral=True, thinking=True)
            except Exception:
                pass

            try:
                pages = await render_today_scrim_panel_png_pages()
            except Exception as e:
                msg = f"プレビュー生成に失敗しました: {e}\nDB: {SCRIM_CALENDAR_DB_PATH}"
                try:
                    if interaction.response.is_done():
                        await interaction.followup.send(msg)
                    else:
                        await interaction.response.send_message(msg)
                except Exception:
                    pass
                return

            try:
                # コマンド実行ログ（「○○さんがコマンドを使用しました」）を出さないため、
                # 最初の応答はephemeralで握り、実際の投稿はチャンネルへ直接送信する。
                total = len(pages)
                for i, png in enumerate(pages, start=1):
                    suffix = f"（{i}/{total}）" if total > 1 else ""
                    file = discord.File(fp=io.BytesIO(png), filename=f"today_scrim_preview_{i:02d}.png")
                    content = f"{suffix}"
                    if interaction.channel:
                        await interaction.channel.send(file=file)  # type: ignore
                # ephemereal応答は残さない
                try:
                    await interaction.delete_original_response()
                except Exception:
                    pass
            except Exception:
                if interaction.channel:
                    total = len(pages)
                    for i, png in enumerate(pages, start=1):
                        suffix = f"（{i}/{total}）" if total > 1 else ""
                        file = discord.File(fp=io.BytesIO(png), filename=f"today_scrim_preview_{i:02d}.png")
                        await interaction.channel.send(file=file)  # type: ignore
                try:
                    await interaction.delete_original_response()
                except Exception:
                    pass


    # =====================
    # Posting
    # =====================


def main():
    token = os.getenv("SCRIMKEY_TOKEN")
    if not token:
        raise RuntimeError("Environment variable SCRIMKEY_TOKEN is not set.")
    bot = ScrimBot()
    bot.run(token)


if __name__ == "__main__":
    main()




