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
# Daily Scrim Panel Rendering (HTML -> PNG)  [NEW]
# =====================

def _scrim_panel_icon(style: str) -> str:
    if style == "従来式":
        return "🔵"
    if style == "回転式":
        return "🟠"
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

    try:
        rows = con.execute(
            """
            SELECT id, date, title, style, start_time, matches, mode_primary, mode_secondary, composite_json, note
            FROM events
            WHERE date = ?
            ORDER BY start_time, id
            """,
            (today_ymd,),
        ).fetchall()
    except sqlite3.OperationalError as e:
        # 典型: DB未初期化で events テーブルが無い
        msg = str(e)
        if "no such table" in msg and "events" in msg:
            try:
                tbls = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
            except Exception:
                tbls = []
            print(f"[WARN] scrim_calendar DB has no 'events' table. tables={tbls} db={db_path}")
            rows = []
        else:
            con.close()
            raise
    finally:
        try:
            con.close()
        except Exception:
            pass


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


def _build_today_panel_html(today_ymd: str, events: List[Dict[str, Any]]) -> str:
    # date badge
    y, m, d = map(int, today_ymd.split("-"))
    date_badge = f"{y}/{str(m).zfill(2)}/{str(d).zfill(2)}"
    updated = fmt_hhmm_jst(utc_now())

    # KPIs
    kpi_trad = sum(1 for e in events if e.get("style") == "従来式")
    kpi_rot = sum(1 for e in events if e.get("style") == "回転式")
    kpi_none = len(events) - kpi_trad - kpi_rot
    times = sorted([e.get("start_time") for e in events if e.get("start_time")])
    kpi_first = times[0] if times else "—"

    # cards
    cards_html = ""
    if not events:
        cards_html = '<div class="card"><div class="sub">本日の予定はありません</div></div>'
    else:
        parts = []
        for e in events:
            icon = _scrim_panel_icon(e.get("style", ""))
            icon_html = f'<span class="ico">{_html_esc(icon)}</span>' if icon else '<span class="ico none">⚪</span>'
            title = _html_esc(e.get("title", ""))
            style = _html_esc(e.get("style", ""))
            start_time = _html_esc(e.get("start_time", "")) or "未定"
            mode_primary = _html_esc(e.get("mode_primary", "")) or "—"
            mode_secondary = _html_esc(e.get("mode_secondary", "")) or "—"

            match_tag = ""
            if e.get("style") == "従来式":
                match_tag = f'<span class="tag"><strong>試合</strong> {_html_esc(e.get("matches") or 0)}</span>'

            comp_html = ""
            if e.get("mode_secondary") == "複合" and e.get("composite"):
                lines = []
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
            if e.get("note"):
                note_html = f'<div class="note"><b>備考</b> {_html_esc(e.get("note"))}</div>'

            parts.append(
                f'''
                <div class="card">
                  <div class="row1">
                    <div class="name">{icon_html}<span class="truncate">{title}</span></div>
                  </div>
                  <div class="meta">
                    <span class="tag"><strong>開始</strong> {start_time}</span>
                    <span class="tag"><strong>方式</strong> {style}</span>
                    {match_tag}
                    <span class="tag"><strong>モード</strong> {mode_primary} / {mode_secondary}</span>
                  </div>
                  {comp_html}
                  {note_html}
                </div>
                '''
            )
        cards_html = "\n".join(parts)

    # main html
    return f"""<!doctype html>
<html lang="ja">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>本日のスクリム情報</title>
  <style>
    :root{{
      --bg:#0b0c10; --card:#11131a; --card2:#0f1117; --line:#232636;
      --text:#e9ecff; --muted:#aab0d6; --pill:#171a25;
    }}
    *{{box-sizing:border-box}}
    body{{
      margin:0;
      background: radial-gradient(1200px 600px at 15% -10%, rgba(122,162,255,.18), transparent 60%),
                  radial-gradient(800px 500px at 85% 10%, rgba(255,176,32,.12), transparent 55%),
                  var(--bg);
      color:var(--text);
      font-family: system-ui, -apple-system, Segoe UI, Roboto, "Noto Sans JP", sans-serif;
      padding:18px;
    }}
    .wrap{{max-width:980px;margin:0 auto}}
    .top{{display:flex;align-items:flex-end;justify-content:space-between;gap:12px;margin-bottom:14px}}
    .title{{display:flex;flex-direction:column;gap:6px}}
    h1{{margin:0;font-size:20px;letter-spacing:.3px;display:flex;align-items:center;gap:10px}}
    .badge{{font-size:12px;padding:3px 10px;border-radius:999px;background: rgba(122,162,255,.14);border:1px solid rgba(122,162,255,.30);color: var(--text);}}
    .sub{{color:var(--muted);font-size:12px;line-height:1.4}}
    .legend{{display:flex;gap:8px;flex-wrap:wrap;justify-content:flex-end;font-size:12px;color:var(--muted)}}
    .legend span{{background:var(--pill);border:1px solid var(--line);padding:4px 10px;border-radius:999px}}

    .grid{{display:grid;grid-template-columns: 1.2fr .8fr;gap:12px;align-items:start}}
    .panel{{background: linear-gradient(180deg, rgba(255,255,255,.03), transparent 40%), var(--card);
      border:1px solid var(--line);border-radius:16px;overflow:hidden;box-shadow: 0 10px 35px rgba(0,0,0,.35);}}
    .panelHead{{padding:12px 14px;border-bottom:1px solid var(--line);display:flex;align-items:center;justify-content:space-between;gap:10px;background: rgba(255,255,255,.02);}}
    .panelHead b{{font-size:13px}}
    .panelHead .right{{font-size:12px;color:var(--muted);display:flex;gap:8px;align-items:center;flex-wrap:wrap;justify-content:flex-end}}
    .pill{{background:var(--pill);border:1px solid var(--line);padding:4px 10px;border-radius:999px;color:var(--muted)}}

    .list{{padding:12px;display:flex;flex-direction:column;gap:10px}}
    .card{{background: var(--card2);border:1px solid var(--line);border-radius:14px;padding:12px}}
    .row1{{display:flex;align-items:flex-start;justify-content:space-between;gap:10px}}
    .name{{font-size:14px;font-weight:900;line-height:1.25;display:flex;align-items:center;gap:8px;min-width:0}}
    .name .truncate{{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:100%}}
    .meta{{margin-top:8px;display:flex;flex-wrap:wrap;gap:6px;font-size:12px;color:var(--muted)}}
    .tag{{border:1px solid var(--line);background: rgba(255,255,255,.02);padding:4px 10px;border-radius:999px}}
    .tag strong{{color:var(--text)}}
    .note{{margin-top:10px;font-size:12px;color:var(--muted);line-height:1.45;border-top:1px dashed rgba(170,176,214,.25);padding-top:10px}}
    .note b{{color:var(--text)}}

    .kpi{{display:grid;grid-template-columns:1fr 1fr;gap:10px;padding:12px}}
    .stat{{background: rgba(255,255,255,.02);border:1px solid var(--line);border-radius:14px;padding:12px}}
    .stat .label{{font-size:12px;color:var(--muted)}}
    .stat .value{{font-size:20px;font-weight:900;margin-top:6px}}
    .stat .hint{{font-size:12px;color:var(--muted);margin-top:4px}}

    .ico{{font-size:16px;line-height:1}}
    .ico.none{{opacity:.6}}

    @media (max-width: 860px){{ .grid{{grid-template-columns:1fr}} }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="top">
      <div class="title">
        <h1>本日のスクリム情報 <span class="badge">{_html_esc(date_badge)}</span></h1>
        <div class="sub">🟠 回転式｜🔵 従来式｜（方式「登録しない」はアイコン無し）</div>
      </div>
      <div class="legend">
        <span>更新: <b>{_html_esc(updated)}</b></span>
        <span>予定数: <b>{len(events)}</b></span>
        <span>表示: スクリム名 / モード / 時間 / 試合数 / 備考</span>
      </div>
    </div>

    <div class="grid">
      <div class="panel">
        <div class="panelHead">
          <b>今日の予定</b>
          <div class="right"><span class="pill">JP</span><span class="pill">Discord用</span></div>
        </div>
        <div class="list">
          {cards_html}
        </div>
      </div>

      <div class="panel">
        <div class="panelHead"><b>サマリー</b><div class="right"><span class="pill">集計</span></div></div>
        <div class="kpi">
          <div class="stat"><div class="label">従来式 🔵</div><div class="value">{kpi_trad}</div><div class="hint">試合数があるタイプ</div></div>
          <div class="stat"><div class="label">回転式 🟠</div><div class="value">{kpi_rot}</div><div class="hint">回転式 / 形式固定なし</div></div>
          <div class="stat"><div class="label">登録しない</div><div class="value">{kpi_none}</div><div class="hint">アイコン無しで表示</div></div>
          <div class="stat"><div class="label">最初の開始</div><div class="value">{_html_esc(kpi_first)}</div><div class="hint">時間未入力は除外</div></div>
        </div>
        <div class="list" style="padding-top:0">
          <div class="card">
            <div class="row1"><div class="name"><span class="ico">📌</span><span class="truncate">自動生成</span></div></div>
            <div class="note"><b>DB</b> { _html_esc(SCRIM_CALENDAR_DB_PATH) }</div>
          </div>
        </div>
      </div>
    </div>
  </div>
</body>
</html>"""


async def _try_render_png_from_html_panel(html: str, width: int = 980, height: int = 820) -> Optional[bytes]:
    try:
        from playwright.async_api import async_playwright
    except Exception:
        return None

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            page = await browser.new_page(viewport={"width": int(width), "height": int(height)})
            with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False, encoding="utf-8") as f:
                f.write(html)
                html_path = f.name
            await page.goto("file://" + html_path)
            try:
                await page.wait_for_timeout(250)
            except Exception:
                pass
            png = await page.screenshot(type="png")
            await browser.close()
        try:
            os.remove(html_path)
        except Exception:
            pass
        return png
    except Exception as e:
        print(f"[WARN] today panel render failed: {e}")
        return None


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
                png = await render_today_scrim_panel_png(today)
            except Exception as e:
                print(f"[AUTOPOST] render failed ({guild.id}): {e}")
                continue

            try:
                file = discord.File(fp=io.BytesIO(png), filename="today_scrim.png")
                await gch.send(content="📅【本日のスクリム情報】（自動投稿）", file=file)
                self._today_panel_last_post[guild.id] = today
            except Exception as e:
                print(f"[AUTOPOST] send failed ({guild.id}): {e}")
                continue


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
        # Posting: Today Scrim Panel (command)
        # =====================
        @self.tree.command(name="scrim_today", description="本日のスクリム情報を画像で投稿")
        async def scrim_today(interaction: discord.Interaction):
            # 先に応答してタイムアウト回避
            try:
                if not interaction.response.is_done():
                    await interaction.response.defer(thinking=False)
            except Exception:
                pass

            try:
                png = await render_today_scrim_panel_png()
            except Exception as e:
                msg = f"本日のスクリム情報の生成に失敗しました: {e}"
                try:
                    if interaction.response.is_done():
                        await interaction.followup.send(msg, ephemeral=True)
                    else:
                        await interaction.response.send_message(msg, ephemeral=True)
                except Exception:
                    pass
                return

            file = discord.File(fp=io.BytesIO(png), filename="today_scrim.png")
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(content="📅【本日のスクリム情報】", file=file)
                else:
                    await interaction.response.send_message(content="📅【本日のスクリム情報】", file=file)
            except Exception:
                if interaction.channel:
                    await interaction.channel.send(content="📅【本日のスクリム情報】", file=file)


        @self.tree.command(name="scrim_today_preview", description="本日のスクリム情報（プレビュー）を画像で表示")
        async def scrim_today_preview(interaction: discord.Interaction):
            # 管理者（サーバー管理）だけ実行可
            try:
                if interaction.guild and (not interaction.user.guild_permissions.manage_guild):
                    if not interaction.response.is_done():
                        await interaction.response.send_message("権限がありません（サーバー管理権限が必要です）。", ephemeral=True)
                    else:
                        await interaction.followup.send("権限がありません（サーバー管理権限が必要です）。", ephemeral=True)
                    return
            except Exception:
                pass

            # 先に応答してタイムアウト回避（ephemeral）
            try:
                if not interaction.response.is_done():
                    await interaction.response.defer(ephemeral=True, thinking=False)
            except Exception:
                pass

            try:
                png = await render_today_scrim_panel_png()
            except Exception as e:
                msg = f"本日のスクリム情報（プレビュー）の生成に失敗しました: {e}"
                try:
                    if interaction.response.is_done():
                        await interaction.followup.send(msg, ephemeral=True)
                    else:
                        await interaction.response.send_message(msg, ephemeral=True)
                except Exception:
                    pass
                return

            file = discord.File(fp=io.BytesIO(png), filename="today_scrim_preview.png")
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(content="🧪【本日のスクリム情報｜プレビュー】", file=file, ephemeral=True)
                else:
                    await interaction.response.send_message(content="🧪【本日のスクリム情報｜プレビュー】", file=file, ephemeral=True)
            except Exception:
                # 最終フォールバック（チャンネルに投げない：プレビューのため）
                pass



def main():
    token = os.getenv("SCRIMKEY_TOKEN")
    if not token:
        raise RuntimeError("Environment variable SCRIMKEY_TOKEN is not set.")
    bot = ScrimBot()
    bot.run(token)


if __name__ == "__main__":
    main()

