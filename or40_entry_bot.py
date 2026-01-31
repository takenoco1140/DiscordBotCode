# -*- coding: utf-8 -*-
# Refactor-only output for easier patching (sections, normalized persistence)

import os
import re
import logging
import asyncio
from datetime import datetime, timezone, timedelta
JST = timezone(timedelta(hours=9))
from typing import Optional, Dict, Any, List, Tuple
import json

import secrets
import discord
from discord import app_commands
import gspread
from google.oauth2.service_account import Credentials
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
# =========================
# Google Sheets (固定)
# =========================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# service_account.json はこの .py と同じフォルダに置く前提
SERVICE_ACCOUNT_JSON = str(SERVICE_ACCOUNT_JSON)

# 既存スプレッドシートID（URLの /d/<ここ>/ の部分）
# ここは勝手に変えない。変える必要がある時だけ手動で書き換える。
SPREADSHEET_KEY = "1d0DRjoPJ0wy3WIYrOfCKhwtBp_Pde7kKXp5RzpV5Z8E"

# 0=一番左のシート。必要なら変更。
SHEET_INDEX = 0



# =========================
# Paths & persistence (panel_state.json)
# =========================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PANEL_STATE_JSON = str(DATA_DIR / "panel_state.json")

DEFAULT_CONFIG: Dict[str, Any] = {
    "tournament_id": "",
    "tournament_name": "OR40 SOLOリロード",
    "event_date": "2026/2/15",
    "start_time": "22:00",
    "period_start": "2026/2/1",
    "period_end": "2026/2/10",
    "mode_people": "ソロ",
    "mode_type": "リロード",
    "matches_count": 4,
    "capacity": 38,
    "need_ikigomi": True,
    "status_toggle": {"pre": False, "open": False, "post": False},
    "indiv_order": ["platform", "epic", "callname", "xid", "custom", "ikigomi"],
    "team_questions": {"register_mode": "off", "reserve": False},
    "panel_lock": {"is_posted": False, "post_locked": False},
    "active_threads": {},
    "next_draft_no": 1,
}


def load_config(base: Dict[str, Any]) -> Dict[str, Any]:
    """panel_state.json を読み込み、base(DEFAULT_CONFIG相当)にマージして返す。"""
    if not os.path.exists(PANEL_STATE_JSON):
        return dict(base)

    try:
        with open(PANEL_STATE_JSON, "r", encoding="utf-8") as f:
            data = json.load(f) or {}

        merged = dict(base)
        merged.update(data)

        # 型崩れ対策
        if not isinstance(merged.get("status_toggle"), dict):
            merged["status_toggle"] = dict(base.get("status_toggle", {}))
        if not isinstance(merged.get("panel_lock"), dict):
            merged["panel_lock"] = dict(base.get("panel_lock", {}))

        return merged
    except Exception:
        return dict(base)

def save_config(config: Dict[str, Any]) -> None:
    """panel_state.json に現在のconfigを保存する（原子的に置換）。"""
    tmp = PANEL_STATE_JSON + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)
    os.replace(tmp, PANEL_STATE_JSON)


def generate_tournament_id(now: Optional[datetime] = None) -> str:
    """大会ごとに一意な tournament_id を生成する（内部用）。"""
    now = now or datetime.now()
    # 例: T20260111-A3F9C2
    return f"T{now.strftime('%Y%m%d')}-{secrets.token_hex(3).upper()}"




# =========================
# Active thread lock (Discord-only, persisted in panel_state.json)
# =========================
def _active_threads() -> Dict[str, Any]:
    at = CONFIG.get("active_threads")
    if not isinstance(at, dict):
        at = {}
        CONFIG["active_threads"] = at
    return at


def get_next_draft_no() -> int:
    """仮No（記入中スレッド用の通し番号）を発行する。
    受理Noは受付完了時にスプレッドシート側で採番する。
    """
    try:
        cfg = load_config(DEFAULT_CONFIG)
        n = int(cfg.get("next_draft_no") or 1)
        if n < 1:
            n = 1
        cfg["next_draft_no"] = n + 1
        save_config(cfg)
        # グローバルCONFIGも合わせて更新（稼働中にズレないように）
        CONFIG["next_draft_no"] = cfg["next_draft_no"]
        return n
    except Exception:
        return int(datetime.now().timestamp())

def get_active_thread_id_for_user(user_id: int) -> Optional[int]:
    tid = str(_active_threads().get(str(user_id), "")).strip()
    return int(tid) if tid.isdigit() else None

def set_active_thread_for_user(user_id: int, thread_id: int) -> None:
    _active_threads()[str(user_id)] = int(thread_id)
    save_config(CONFIG)

def clear_active_thread_for_user(user_id: int) -> None:
    at = _active_threads()
    if str(user_id) in at:
        at.pop(str(user_id), None)
        save_config(CONFIG)
# =========================
# Logging
# =========================
logging.basicConfig(level=logging.ERROR)
logging.getLogger("discord").setLevel(logging.ERROR)

def run_log(msg: str):
    print(f"RUN: {msg}")

# =========================
# util (interaction ACK)
# =========================
async def silent_ack(interaction: discord.Interaction, *, ephemeral: bool = False):
    """
    成功時のエフェメラルを出さずに ACK。
    ⚠ delete_original_response は絶対しない（管理パネル本体が消える事故になる）
    """
    try:
        if not interaction.response.is_done():
            # NOTE:
            #   followup の ephemeral は「初回 response/defer が ephemeral かどうか」に引っ張られる挙動がある。
            #   運営コマンド等を“完全に裏側（エフェメラル）”で完結させたい場合は、defer も ephemeral=True にする。
            await interaction.response.defer(thinking=False, ephemeral=ephemeral)
    except Exception:
        pass

# =========================
# 設定（大会ごとに直書き）
# =========================
BOT_TOKEN = os.getenv("ADMIN_OR40_BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("環境変数 ADMIN_OR40_BOT_TOKEN が設定されていません")

ENTRY_CHANNEL_ID = 1456603529019003064                  # 大会概要チャンネル（受付パネルを置く）
THREAD_PARENT_CHANNEL_ID = 1456603529019003064          # スレッドをぶら下げるチャンネル（受付パネル設置チャンネル下に戻す）
NOTIFY_CHANNEL_ID = 1459220859133886652
# 運営問い合わせの通知先（フォーラム運用にしたい場合）
OPS_FORUM_ID = 1459920579657470105  # 運営フォーラム（問い合わせ管理）
NOTIFY_TO_FORUM = True  # True: フォーラムにスレッド生成 / False: 通知チャンネルへ送信

OPS_ROLE_ID = 1456621988704424058                       # 運営ロール（動作確認中の入力許可＆GoLive審査ボタン）

# 受付完了（エントリー済）ロール
# - まず環境変数 OR40_ENTRY_ACCEPT_ROLE_ID があればそれを優先
# - 未設定(0)の場合は、環境変数 OR40_ENTRY_ACCEPT_ROLE_NAME（既定: "エントリー済"）で名前検索
ENTRY_ACCEPT_ROLE_ID = int(os.getenv("OR40_ENTRY_ACCEPT_ROLE_ID", "1456603947857875006") or 0)
ENTRY_ACCEPT_ROLE_NAME = os.getenv("OR40_ENTRY_ACCEPT_ROLE_NAME", "エントリー済")

def resolve_entry_accept_role(guild: discord.Guild) -> Optional[discord.Role]:
    if guild is None:
        return None
    try:
        rid = int(ENTRY_ACCEPT_ROLE_ID or 0)
        if rid:
            r = guild.get_role(rid)
            if r:
                return r
    except Exception:
        pass
    try:
        target = str(ENTRY_ACCEPT_ROLE_NAME or "").strip()
        if not target:
            return None
        for r in (guild.roles or []):
            if (r.name or "").strip() == target:
                return r
    except Exception:
        pass
    return None


# =========================
# Status constants
# =========================
STATUS_DRAFT = "DRAFT"        # 記入中
STATUS_ACCEPTED = "受付完了"  # 受付完了
STATUS_CANCELED = "キャンセル"  # キャンセル


# =========================
# Ops progress (Forum + Private thread sync)
# =========================
OPS_STATUS_NEW = "NEW"          # ⬜️ 未対応
OPS_STATUS_INPROGRESS = "INPROGRESS"  # 🟨 対応中（運営が内容を確認した）
OPS_STATUS_ADDITIONAL = "ADDITIONAL"  # 🟪 追加連絡あり（対応中にボタン押下）
OPS_STATUS_DONE = "DONE"        # 🟩 対応完了（フォーラムのみ表示／参加者スレは無印）

OPS_STATUS_EMOJI_FORUM = {
    OPS_STATUS_NEW: "⬜️",
    OPS_STATUS_INPROGRESS: "🟨",
    OPS_STATUS_ADDITIONAL: "🟪",
    OPS_STATUS_DONE: "🟩",
}

OPS_STATUS_EMOJI_PRIVATE = {
    OPS_STATUS_NEW: "⬜️",
    OPS_STATUS_INPROGRESS: "🟨",
    OPS_STATUS_ADDITIONAL: "🟪",
    OPS_STATUS_DONE: "",  # 完了は無印
}

def _strip_leading_status_emoji(title: str) -> str:
    t = str(title or "").lstrip()
    while True:
        for e in ("🟧", "⬜️", "⬜", "🟨", "🟪", "🟩"):
            if t.startswith(e):
                t = t[len(e):].lstrip()
                break
        else:
            return t


def _extract_no_prefix_from_thread_title(title: str) -> str:
    """Return 'P-No.xxx' or 'E-No.xxx' (or similar) from a thread title, without leading status emoji."""
    base = _strip_leading_status_emoji(title or "")
    if "｜" in base:
        head = base.split("｜", 1)[0].strip()
        if head:
            return head
    return base.strip() or "No.---"

def _apply_status_emoji(title: str, status: str, *, for_forum: bool) -> str:
    base = _strip_leading_status_emoji(title)
    emoji = (OPS_STATUS_EMOJI_FORUM if for_forum else OPS_STATUS_EMOJI_PRIVATE).get(status, "")
    if emoji:
        return f"{emoji} {base}"[:95]
    return base[:95]

def _ops_links() -> dict:
    d = CONFIG.get("ops_links")
    if not isinstance(d, dict):
        d = {}
        CONFIG["ops_links"] = d
    return d

def _ops_status_map() -> dict:
    d = CONFIG.get("ops_status")
    if not isinstance(d, dict):
        d = {}
        CONFIG["ops_status"] = d
    return d

def _ops_status_msg_map() -> dict:
    d = CONFIG.get("ops_status_msg")
    if not isinstance(d, dict):
        d = {}
        CONFIG["ops_status_msg"] = d
    return d

async def _set_status_forum_and_private(guild: discord.Guild, forum_thread: discord.Thread, private_thread_id: int, status: str):
    # Update forum title (avoid redundant PATCH)
    try:
        desired = _apply_status_emoji(forum_thread.name, status, for_forum=True)
        if (forum_thread.name or "") != desired:
            await forum_thread.edit(name=desired)
    except Exception:
        pass

    # Update private thread title (DONE => remove emoji) (avoid redundant PATCH)
    try:
        pth = guild.get_channel(int(private_thread_id)) if private_thread_id else None
        if pth is None and private_thread_id:
            try:
                pth = await guild.fetch_channel(int(private_thread_id))
            except Exception:
                pth = None
        if isinstance(pth, discord.Thread):
            desired_p = _apply_status_emoji(pth.name, status, for_forum=False)
            if (pth.name or "") != desired_p:
                await pth.edit(name=desired_p)
    except Exception:
        pass


async def _refresh_ops_status_message(guild: discord.Guild, forum_thread: discord.Thread):
    """Ensure a single status message exists and refresh its view (✅ marks + disable rules)."""
    try:
        sid = _ops_status_msg_map().get(str(forum_thread.id))
        msg = None
        if sid:
            try:
                msg = await forum_thread.fetch_message(int(sid))
            except Exception:
                msg = None

        if msg is None:
            msg = await forum_thread.send("進捗を更新してください。", view=OpsStatusView())
            _ops_status_msg_map()[str(forum_thread.id)] = int(msg.id)
            save_config(CONFIG)
        else:
            await msg.edit(view=OpsStatusView())
    except Exception:
        pass

# =========================
# Ops notify / Inquiry marker (notify-only)
# =========================
def _ops_mention() -> str:
    return f"<@&{OPS_ROLE_ID}>"

def _is_inquiry_marked(title: str) -> bool:
    return False


def _mark_inquiry_title(title: str) -> str:
    # 🟧 旧マーカーは廃止（進捗は⬜️🟨🟪🟩で管理）
    return str(title or "")[:95]


async def mark_inquiry_and_notify(thread, st, *, reason_label: str):
    """問い合わせ/申請の通知。

    - スレッド名に🟧を付ける（stateにも保持）
    - 通知先は
        - NOTIFY_TO_FORUM=True かつ OPS_FORUM_ID!=0 のとき：運営フォーラムにスレッドを作成（=通知）
        - それ以外：従来どおり通知チャンネルへ1通送信

    ※同じスレッドで複数回押されても、運営フォーラム側は同一スレッドを再利用します。
    """
    # スレッド名に🟧（stateにも保持）
    try:
        st["has_inquiry"] = True
    except Exception:
        pass
    try:
        new_name = _mark_inquiry_title(thread.name or "")
        if new_name != (thread.name or ""):
            await thread.edit(name=new_name)
    except Exception:
        pass

    guild = getattr(thread, "guild", None)
    if guild is None:
        return

    # ---- フォーラム通知（優先） ----
    if bool(globals().get("NOTIFY_TO_FORUM", False)) and int(globals().get("OPS_FORUM_ID", 0) or 0) != 0:
        try:
            forum_id = int(globals().get("OPS_FORUM_ID"))
            forum = guild.get_channel(forum_id)
            if forum is None:
                try:
                    forum = await guild.fetch_channel(forum_id)
                except Exception:
                    forum = None
            if isinstance(forum, discord.ForumChannel):
                # 既存フォーラムスレがあれば再利用
                ftid = st.get("ops_forum_thread_id")
                forum_thread = None
                if ftid:
                    forum_thread = guild.get_channel(int(ftid))
                    if forum_thread is None:
                        try:
                            forum_thread = await guild.fetch_channel(int(ftid))
                        except Exception:
                            forum_thread = None

                if isinstance(forum_thread, discord.Thread):
                    # 既存に追記して通知を上げる
                    try:
                        # Status transition on re-contact (button press)
                        cur = _ops_status_map().get(str(forum_thread.id), OPS_STATUS_NEW)
                        nxt = cur
                        if cur == OPS_STATUS_DONE:
                            nxt = OPS_STATUS_NEW  # 完了後の再連絡は白から再スタート
                        elif cur == OPS_STATUS_INPROGRESS:
                            nxt = OPS_STATUS_ADDITIONAL  # 対応中の追加連絡は紫
                        elif cur == OPS_STATUS_ADDITIONAL:
                            nxt = OPS_STATUS_ADDITIONAL
                        else:
                            nxt = OPS_STATUS_NEW
                        _ops_status_map()[str(forum_thread.id)] = nxt
                        save_config(CONFIG)

                        # Sync titles (forum + private)
                        pvt_id = int(_ops_links().get(str(forum_thread.id), 0) or 0)
                        await _set_status_forum_and_private(guild, forum_thread, pvt_id, nxt)

                        # Mention only (re-notify)
                        await forum_thread.send("\n".join([
                            _ops_mention(),
                            "📣 追加の連絡がありました。",
                            f"🔗 {thread.mention}",
                        ]))
                        await _refresh_ops_status_message(guild, forum_thread)

                    except Exception:
                        pass
                    return

                # 新規作成
                receipt_no = st.get("receipt_no")
                owner_name = st.get("owner_name") or "user"
                base = _extract_no_prefix_from_thread_title(thread.name or "")
                title = f"{base}｜問い合わせ＠{owner_name}"[:95]

                content = "\n".join([
                    _ops_mention(),
                    "📣 問い合わせが届きました。",
                    f"🔗 {thread.mention}",
                    "参加者はこのあと、エントリースレッドに通常メッセージで内容を送信します。",
                ])

                created = await forum.create_thread(name=title, content=content)
                ft = created.thread


                # link forum thread <-> private entry thread
                try:
                    _ops_links()[str(ft.id)] = int(thread.id)
                    _ops_status_map()[str(ft.id)] = OPS_STATUS_NEW
                    save_config(CONFIG)
                except Exception:
                    pass

                # Sync private thread title to ⬜️ (新規)
                try:
                    await _set_status_forum_and_private(guild, ft, int(thread.id), OPS_STATUS_NEW)
                except Exception:
                    pass

                # status control message (buttons)
                await _refresh_ops_status_message(guild, ft)
                # 軽いガイド（運用が迷子にならないように）
                try:
                    embed = discord.Embed(
                        title="🔷対応方法",
                        description=("/entry_answer を実行してください。\n"
                                     "実行後に送信するメッセージは、回答として該当スレッドに転記されます。"),
                    )
                    await ft.send(embed=embed)
                except Exception:
                    pass

                try:
                    st["ops_forum_thread_id"] = ft.id
                except Exception:
                    pass

                return
        except Exception:
            # フォーラム通知が失敗したら、下のチャンネル通知へフォールバック
            pass

    # ---- 従来の通知チャンネル（フォールバック） ----
    try:
        ch = guild.get_channel(NOTIFY_CHANNEL_ID)
        if ch is None:
            try:
                ch = await guild.fetch_channel(NOTIFY_CHANNEL_ID)
            except Exception:
                ch = None
        if ch:
            msg = "\n".join([
                _ops_mention(),
                "📣 **{}** が発生しました。".format(reason_label),
                "🔗 {}".format(thread.mention),
                "参加者はこのあと、スレッドに通常メッセージで内容を送信します。"
            ])
            await ch.send(msg)
    except Exception:
        pass


def format_thread_title(status: str, receipt_no: int, owner_name: str) -> str:
    """
    スレッドタイトル規約（記号なし）:
      P-No.XXX｜記入中＠name
      E-No.XXX｜受付完了＠name
      E-No.XXX｜キャンセル＠name
    ※ 🟧 は問い合わせ/申請が発生した時のみ付与
    """
    owner_name = str(owner_name or "").strip() or "user"
    rn = int(receipt_no or 0)
    if status == STATUS_DRAFT:
        return f"P-No.{rn:03d}｜記入中＠{owner_name}"[:95]
    if status == STATUS_ACCEPTED:
        return f"E-No.{rn:03d}｜受付完了＠{owner_name}"[:95]
    if status == STATUS_CANCELED:
        return f"E-No.{rn:03d}｜キャンセル＠{owner_name}"[:95]
    return f"E-No.{rn:03d}｜{status}＠{owner_name}"[:95]

REQUIRED_HEADERS = [
    "timestamp(JST)",
    "受理No",
"当選No",
    "status",
    "Discord名",
    "DiscordID_1",              # ★ソロでも _1 を使用（代表者概念なし）
    "Discord名_1",
    "質問項目(ONのみ)",
    "threadID",
    "抽選ポイント(空欄OK)",
    "機種",
    "EPIC ID",
    "呼び名",
    "XのID",
    "XのURL",
    "カスタム権限",
    "意気込みメッセージ",
]

# =========================
# 大会設定（保持は「Bot稼働中メモリ」）
# =========================
CONFIG: Dict[str, Any] = {
    "tournament_name": "OR40 SOLOリロード",

    # ↓分離（開催日＋開始時間）
    "event_date": "",      # 例: 2026/2/15
    "start_time": "",      # 例: 22:00

    # モード（表示は 人数（種類））
    "mode_people": "ソロ",               # ソロ/デュオ/トリオ/スクワッド（※本大会はソロ固定運用でもボタンは残す）
    "mode_type": "リロード",             # 通常/トーナメントセッティング/リロード/リロードランク
    "matches_count": 4,

    "capacity": 38,

    # GoLiveは選択の余地なし（固定文言）
    "need_ikigomi": True,

    # 受付期間（入力は日付のみ。表示は end 23:59 ）
    "period_start": "",     # 例: 2026/2/1
    "period_end": "",       # 例: 2026/2/10

    # フェーズ別トグル
    # pre : 受付期間前 ⇔ 受付期間前（動作確認中）
    # open: 受付中 ⇔ メンテナンス中
    # post: 受付〆切 ⇔ 修正希望受付可
    "status_toggle": {"pre": False, "open": False, "post": False},

    # ===== 質問項目：チーム（大会後に設計 / 今はTODOだけ） =====
    "team_questions": {
        "register_mode": "off",   # off / immediate / later
        "reserve": False,
    },

    # ===== 質問項目：個人（押した順で番号付与） =====
    # 今回はソロの基本セットを初期で入れておく
    "indiv_order": ["platform", "epic", "callname", "xid", "custom", "ikigomi"],
    "active_threads": {},
    "next_draft_no": 1,
}




CONFIG = load_config(CONFIG)
# =========================
# Embed colors
# =========================
COLOR_PANEL = discord.Color.gold()
COLOR_ADMIN = discord.Color.teal()
COLOR_INFO = discord.Color.blurple()

# UI colors
COLOR_QUESTION_LIST = discord.Color.dark_teal()
COLOR_QUESTION = discord.Color.blue()
COLOR_CONFIRM = discord.Color.purple()
COLOR_RECEIPT = discord.Color.green()
COLOR_GOLIVE = discord.Color.red()

# =========================
# 状態（メモリ）
# =========================
ENTRY_PANEL_MSG: Dict[int, int] = {}        # guild_id -> message_id（受付パネル）
ADMIN_PANEL_MAIN_MSG: Dict[int, int] = {}   # guild_id -> message_id（1投稿目）
ADMIN_PANEL_TEAM_MSG: Dict[int, int] = {}   # guild_id -> message_id（2投稿目：チーム）
ADMIN_PANEL_INDIV_MSG: Dict[int, int] = {}  # guild_id -> message_id（3投稿目：個人）

# thread_id -> state
THREAD_STATE: Dict[int, Dict[str, Any]] = {}
TEMP_NO_COUNTER = 0

# =========================
# helpers
# =========================


def _clip_text(text: str, limit: int) -> str:
    """Clip text to a max length to avoid Discord embed/content errors."""
    t = '' if text is None else str(text)
    if limit <= 0:
        return ''
    if len(t) <= limit:
        return t
    # keep room for ellipsis
    if limit == 1:
        return '…'
    return t[: max(0, limit-1)] + '…'
def is_admin(interaction: discord.Interaction) -> bool:
    return bool(interaction.user.guild_permissions.administrator)

def has_ops_role(member: discord.Member) -> bool:
    return any(r.id == OPS_ROLE_ID for r in (member.roles or []))

def _weekday_jp(dt: datetime) -> str:
    w = ["月", "火", "水", "木", "金", "土", "日"]
    return w[dt.weekday()]

def _fmt_date_ymd_jp(s: str) -> str:
    """
    "2026/2/1" -> "2026/02/01(日)"
    """
    s = (s or "").strip()
    m = re.fullmatch(r"(\d{4})/(\d{1,2})/(\d{1,2})", s)
    if not m:
        return ""
    y, mo, d = map(int, m.groups())
    dt = datetime(y, mo, d, 0, 0, 0, tzinfo=JST)
    return f"{y:04d}/{mo:02d}/{d:02d}({_weekday_jp(dt)})"

def _parse_ymd(s: str) -> datetime:
    s = (s or "").strip()
    m = re.fullmatch(r"(\d{4})/(\d{1,2})/(\d{1,2})", s)
    if not m:
        raise ValueError(f"日付形式が不正です: {s}")
    y, mo, d = map(int, m.groups())
    return datetime(y, mo, d, 0, 0, 0, tzinfo=JST)

def _period_bounds() -> Tuple[datetime, datetime]:
    """
    入力は日付のみ。
    start: 00:00
    end  : 23:59
    """
    start = _parse_ymd(CONFIG.get("period_start", ""))
    end_d = _parse_ymd(CONFIG.get("period_end", ""))
    end = end_d.replace(hour=23, minute=59, second=59)
    return start, end

def current_phase() -> str:
    """returns: 'pre' / 'open' / 'post'"""
    if not (CONFIG.get("period_start") and CONFIG.get("period_end")):
        raise ValueError("period not set")
    start, end = _period_bounds()
    now = datetime.now(JST)
    if now < start:
        return "pre"
    if start <= now <= end:
        return "open"
    return "post"

def accept_status_text() -> str:
    """
    受付期間から自動判定 + フェーズ内2択をトグルで切替
    """
    if not (CONFIG.get("period_start") and CONFIG.get("period_end")):
        return "受付期間未設定"

    ph = current_phase()
    tg = bool((CONFIG.get("status_toggle") or {}).get(ph, False))
    if ph == "pre":
        return "受付期間前（動作確認中）" if tg else "受付期間前"
    if ph == "open":
        return "メンテナンス中" if tg else "受付中"
    return "修正希望受付可" if tg else "受付〆切"

def entry_button_label() -> str:
    s = accept_status_text()
    if s == "受付期間前":
        return "エントリー受付開始前"
    if s == "受付期間前（動作確認中）":
        return "運営専用・動作確認中"
    if s == "受付中":
        return "エントリーはこちら"
    if s == "メンテナンス中":
        return "メンテナンス中"
    if s in ("受付〆切", "修正希望受付可"):
        return "受付を締め切りました"
    return "エントリー受付"

def entry_button_enabled_for(member: discord.Member) -> bool:
    s = accept_status_text()
    if s == "受付中":
        return True
    if s == "受付期間前（動作確認中）":
        return has_ops_role(member)
    return False

def _golive_fixed_text() -> str:
    return "PC・Xboxユーザーの方は必須\n※配信不可の場合は事前申告、運営で可否を判断します"

def _mode_text() -> str:
    return f"{CONFIG.get('mode_people','ソロ')}({CONFIG.get('mode_type','リロード')}) {int(CONFIG.get('matches_count',4))}戦"

def _event_text() -> str:
    d = str(CONFIG.get("event_date", "")).strip()
    t = str(CONFIG.get("start_time", "")).strip()
    if not d and not t:
        return "（未設定）"
    if d and t:
        return f"{_fmt_date_ymd_jp(d)} {t}～"
    if d:
        return f"{_fmt_date_ymd_jp(d)}"
    return t

def _period_text() -> str:
    ps = str(CONFIG.get("period_start", "")).strip()
    pe = str(CONFIG.get("period_end", "")).strip()
    if ps and pe:
        return f"{_fmt_date_ymd_jp(ps)} ～ {_fmt_date_ymd_jp(pe)} 23:59"
    if ps:
        return _fmt_date_ymd_jp(ps)
    return "（未設定）"

def build_panel_embed() -> discord.Embed:
    title = CONFIG.get("tournament_name") or "（大会名未設定）"
    embed = discord.Embed(title=f"🏆 {title}", color=COLOR_PANEL)

    # 開催日時（集合/第1試合の目安を併記）
    base_event = _event_text()
    gather_text = ""
    match1_text = ""
    try:
        d = str(CONFIG.get("event_date", "")).strip()
        t = str(CONFIG.get("start_time", "")).strip()
        if re.fullmatch(r"\d{4}/\d{1,2}/\d{1,2}", d) and re.fullmatch(r"\d{1,2}:\d{2}", t):
            y, mo, da = map(int, d.split("/"))
            hh, mm = map(int, t.split(":"))
            dt0 = datetime(y, mo, da, hh, mm, 0, tzinfo=JST)
            gather = dt0 - timedelta(minutes=10)
            match1 = dt0 + timedelta(minutes=15)
            gather_text = f"（集合 {gather.strftime('%H:%M')}）"
            match1_text = f"　🚎第1試合 {match1.strftime('%H:%M')}開始予定"
    except Exception:
        pass

    reload_note = ""
    if str(CONFIG.get("mode_type", "")).strip() == "リロード":
        reload_note = "　※リロードのマップ切替の都合で、第1試合の開始時間は前後します。"

    event_block = f"{base_event}{gather_text}"
    if match1_text or reload_note:
        event_block = f"{event_block}\n{match1_text}\n{reload_note}".rstrip()

    embed.description = (
        "📌開催日時\n"
        f"{event_block}\n\n"
        "🔫モード\n"
        f"{_mode_text()}\n\n"
        "🙎定員\n"
        f"{int(CONFIG.get('capacity', 0))}名　※定員超過の際は抽選となります\n\n"
        "🎥GoLive配信\n"
        f"{_golive_fixed_text()}\n\n"
        "📋エントリー受付期間\n"
        f"{_period_text()}\n\n"
        "📢受付ステータス\n"
        f"{accept_status_text()}"
    )

    return embed

# ===== 質問表示用（チームは大会後） =====
TEAM_LABELS = {
    "immediate": "チーム登録：即時",
    "later": "チーム登録：後日",
    "reserve": "リザーブ登録",
}
INDIV_LABELS = {
    "platform": "機種",
    "epic": "EPIC ID（ディスプレイネーム）",
    "callname": "呼び名",
    "xid": "XのID",
    "custom": "カスタム権限",
    "ikigomi": "意気込みメッセージ",
}

def is_solo_mode() -> bool:
    return str(CONFIG.get("mode_people", "ソロ")) == "ソロ"

def team_status_summary() -> str:
    tq = CONFIG.get("team_questions") or {}
    reg = tq.get("register_mode", "off")
    reserve = bool(tq.get("reserve", False))
    parts: List[str] = []
    if reg == "immediate":
        parts.append("✅チーム登録：即時")
    elif reg == "later":
        parts.append("✅チーム登録：後日")
    else:
        parts.append("チーム登録：OFF")
    parts.append("✅リザーブ登録" if reserve else "リザーブ登録：OFF")
    return " / ".join(parts)

def indiv_status_summary() -> str:
    order: List[str] = list(CONFIG.get("indiv_order") or [])
    if not order:
        return "（未選択）"
    out = []
    for i, k in enumerate(order, start=1):
        out.append(f"[{i}]{INDIV_LABELS.get(k, k)}")
    return "｜".join(out)

# =========================
# Google Sheets
# =========================
def open_worksheet():
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_JSON, scopes=scopes)
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(SPREADSHEET_KEY)
    ws = sh.get_worksheet(SHEET_INDEX)
    ensure_headers(ws)
    return ws

# =========================
# Sheet header aliases (UI-friendly labels)
# =========================
# SS側は見出しを E-No./C-No. に変えてOK。ただしBot内部は互換のため canonical 名で扱う。
HEADER_ALIASES: Dict[str, str] = {
    "受理No.": "受理No",
    "E-No.": "受理No",
    "E-No": "受理No",
    "当選No.": "C-No",
    "当選No": "C-No",
    "C-No.": "C-No",
    "C-No": "C-No",
}

def _canon_header(h: str) -> str:
    h = str(h or "").strip()
    return HEADER_ALIASES.get(h, h)

def _present_canon_headers(headers: List[str]) -> set:
    return { _canon_header(h) for h in (headers or []) if str(h or "").strip() }

def ensure_headers(ws):
    current = ws.row_values(1)
    if not current:
        ws.update("1:1", [REQUIRED_HEADERS])
        return
    present = _present_canon_headers(current)
    missing = [h for h in REQUIRED_HEADERS if _canon_header(h) not in present]
    if missing:
        ws.update("1:1", [current + missing])

def header_index(ws) -> Dict[str, int]:
    headers = ws.row_values(1)
    idx: Dict[str, int] = {}
    for i, h in enumerate(headers, start=1):
        hs = str(h or '').strip()
        if not hs:
            continue
        idx[hs] = i
        ch = _canon_header(hs)
        if ch and ch not in idx:
            idx[ch] = i
    return idx  # 1-based

def _now_jst_str() -> str:
    return datetime.now(JST).strftime("%Y/%m/%d %H:%M:%S")

def _next_receipt_no(ws) -> int:
    """
    受理No の最大+1（空や非数値は無視）
    """
    idx = header_index(ws)
    col = idx.get("受理No")
    if not col:
        return 1
    vals = ws.col_values(col)[1:]  # skip header
    m = 0
    for v in vals:
        v = str(v).strip()
        if v.isdigit():
            m = max(m, int(v))
    return m + 1

# =========================
# Interaction recovery (after bot restart)
# =========================
async def ensure_thread_state(interaction: discord.Interaction) -> Optional[Dict[str, Any]]:
    """Recover THREAD_STATE for persistent button interactions.

    - If state exists: return it.
    - If missing and the interaction is inside a Thread:
        * If the thread is already accepted/canceled in the sheet -> rebuild minimal state and continue.
        * Otherwise -> reset to initial intro message (user must start over).
    """
    try:
        ch = interaction.channel
        if not isinstance(ch, discord.Thread):
            return THREAD_STATE.get(interaction.channel_id)

        # already in memory
        st = THREAD_STATE.get(ch.id)
        if st:
            return st

        
        # ACK early to avoid 'interaction failed' during slow I/O (e.g. Sheets)
        try:
            await silent_ack(interaction, ephemeral=True)
        except Exception:
            pass

# Attempt to restore from sheet by threadID (accepted/canceled only)
        restored = None
        try:
            ws = open_worksheet()
            restored = find_entry_by_thread_id(ws, int(ch.id))
        except Exception:
            restored = None

        if restored and str(restored.get("status") or "").strip():
            status = str(restored.get("status") or "").strip()
            if status in (STATUS_ACCEPTED, STATUS_CANCELED):
                owner_id = int(restored.get("owner_id") or 0) or 0
                owner_name = str(restored.get("owner_name") or "")
                receipt_no = int(restored.get("receipt_no") or 0) or 0
                st = {
                    "owner_id": owner_id,
                    "owner_name": owner_name,
                    "draft_no": receipt_no,
                    "receipt_no": receipt_no,
                    "sheet_row": int(restored.get("sheet_row") or 0) or None,
                    "status": status,
                    "answers": dict(restored.get("answers") or {}),
                    "in_entry": False,
                    "pending_key": None,
                    "in_edit": False,
                    "edit_from_index": None,
                    "golive_waiting": False,
                }
                THREAD_STATE[ch.id] = st
                if owner_id:
                    try:
                        set_active_thread_for_user(owner_id, ch.id)
                    except Exception:
                        pass
                return st

        # Not restorable (draft/in-progress) -> force reset to intro
        # まずACK（3秒以内）して Discord の「インタラクションに失敗しました」を防ぐ
        try:
            if not interaction.response.is_done():
                await interaction.response.defer(thinking=False, ephemeral=True)
        except Exception:
            pass

        # 受付票（受付完了/キャンセル）スレなら復元を試みる。
        # それ以外（質問途中など）は「最初からやり直し」に強制リセットする。
        try:
            tname = str(getattr(ch, "name", "") or "")
            is_post_accept = ("E-No." in tname) or ("受付完了" in tname) or ("キャンセル" in tname)
        except Exception:
            is_post_accept = False

        if not is_post_accept:
            # ① まず「再起動でリセットする」宣言を即表示
            restart_msg = None
            try:
                restart_msg = await ch.send("### ⚠恐れ入りますが、BOTが再起動されたため、最初からやり直します🙇")
            except Exception:
                restart_msg = None

            # ② 新しい初期メッセージ（開始ボタン）を表示
            user = interaction.user
            draft_no = get_next_draft_no()
            THREAD_STATE[ch.id] = {
                "owner_id": user.id,
                "owner_name": getattr(user, "display_name", str(user)),
                "draft_no": int(draft_no),
                "receipt_no": int(draft_no),
                "sheet_row": None,
                "status": STATUS_DRAFT,
                "answers": {},
                "in_entry": False,
                "pending_key": None,
                "in_edit": False,
                "edit_from_index": None,
                "golive_waiting": False,
            }
            try:
                await post_thread_intro(ch, user)
            except Exception:
                pass

            # ③ そのあとで古いBOT投稿（質問UIなど）を削除（新規の2投稿は残す）
            keep_ids = set()
            try:
                if restart_msg:
                    keep_ids.add(int(restart_msg.id))
            except Exception:
                pass
            try:
                intro_id = THREAD_STATE.get(ch.id, {}).get("intro_msg_id")
                if intro_id:
                    keep_ids.add(int(intro_id))
            except Exception:
                pass

            try:
                async for msg in ch.history(limit=200, oldest_first=False):
                    try:
                        if int(msg.id) in keep_ids:
                            continue
                        if getattr(msg.author, "bot", False):
                            await msg.delete()
                    except Exception:
                        pass
            except Exception:
                pass

            return THREAD_STATE.get(ch.id)

        # post-accept/canceled thread (try full restoration below)

        user = interaction.user
        draft_no = get_next_draft_no()
        THREAD_STATE[ch.id] = {
            "owner_id": user.id,
            "owner_name": getattr(user, "display_name", str(user)),
            "draft_no": int(draft_no),
            "receipt_no": int(draft_no),
            "sheet_row": None,
            "status": STATUS_DRAFT,
            "answers": {},
            "in_entry": False,
            "pending_key": None,
            "in_edit": False,
            "edit_from_index": None,
            "golive_waiting": False,
        }
        try:
            set_active_thread_for_user(user.id, ch.id)
        except Exception:
            pass

        try:
            await ch.send("### ⚠BOTが再起動されたため、最初からやり直してください")
        except Exception:
            pass
        try:
            # 初期メッセージ（開始ボタン）を再掲
            if isinstance(user, discord.Member):
                await post_thread_intro(ch, user)
            else:
                # Fallback: mention by id
                await ch.send(f"<@{user.id}>\nーーーーーーーーーーーーーーー\n📢このスレッドはあなた専用です\nーーーーーーーーーーーーーーー", view=ThreadEntryLoopView())
        except Exception:
            pass

        return None
    except Exception:
        return THREAD_STATE.get(interaction.channel_id)

def find_entry_by_thread_id(ws, thread_id: int) -> Optional[Dict[str, Any]]:
    """Scan sheet and return entry dict for the given thread_id (accepted/canceled rows)."""
    idx = header_index(ws)
    col_thread = idx.get("threadID")
    if not col_thread:
        return None

    col_status = idx.get("status")
    col_receipt = idx.get("受理No")
    col_did = idx.get("DiscordID_1")
    col_name = idx.get("Discord名_1") or idx.get("Discord名")

    # Answer columns (stored as UI labels)
    col_platform = idx.get("機種")
    col_epic = idx.get("EPIC ID")
    col_callname = idx.get("呼び名")
    col_xid = idx.get("XのID")
    col_xurl = idx.get("XのURL")
    col_custom = idx.get("カスタム権限")
    col_ikigomi = idx.get("意気込みメッセージ")

    vals = ws.get_all_values()
    tid_s = str(int(thread_id))
    for r_i in range(2, len(vals) + 1):
        row = vals[r_i - 1]
        try:
            if str(row[col_thread - 1]).strip() != tid_s:
                continue
        except Exception:
            continue

        status = str(row[col_status - 1]).strip() if col_status else ""
        receipt_no = str(row[col_receipt - 1]).strip() if col_receipt else ""
        owner_id = str(row[col_did - 1]).strip() if col_did else ""
        owner_name = str(row[col_name - 1]).strip() if col_name else ""

        answers = {
            "platform": str(row[col_platform - 1]).strip() if col_platform else "",
            "epic": str(row[col_epic - 1]).strip() if col_epic else "",
            "callname": str(row[col_callname - 1]).strip() if col_callname else "",
            "xid": str(row[col_xid - 1]).strip() if col_xid else "",
            "xurl": str(row[col_xurl - 1]).strip() if col_xurl else "",
            "custom": str(row[col_custom - 1]).strip() if col_custom else "",
            "ikigomi": str(row[col_ikigomi - 1]).strip() if col_ikigomi else "",
        }

        return {
            "sheet_row": r_i,
            "status": status,
            "receipt_no": int(receipt_no) if str(receipt_no).isdigit() else 0,
            "owner_id": int(owner_id) if str(owner_id).isdigit() else 0,
            "owner_name": owner_name,
            "answers": answers,
        }

    return None

def find_existing_thread_for_user(ws, discord_id_1: int) -> Optional[Tuple[int, str, int, int]]:
    """
    return (row, status, threadID, receipt_no) or None
    """
    idx = header_index(ws)
    col_id = idx.get("DiscordID_1")
    col_status = idx.get("status")
    col_thread = idx.get("threadID")
    col_receipt = idx.get("受理No")
    if not all([col_id, col_status, col_thread, col_receipt]):
        return None

    vals = ws.get_all_values()
    for r_i in range(2, len(vals) + 1):
        row = vals[r_i - 1]
        try:
            did = str(row[col_id - 1]).strip()
            if did and int(did) == int(discord_id_1):
                status = str(row[col_status - 1]).strip()
                thread_id_s = str(row[col_thread - 1]).strip()
                receipt_s = str(row[col_receipt - 1]).strip()
                thread_id = int(thread_id_s) if thread_id_s.isdigit() else 0
                receipt_no = int(receipt_s) if receipt_s.isdigit() else 0
                return (r_i, status, thread_id, receipt_no)
        except Exception:
            continue
    return None

def create_draft_row(ws, receipt_no: int, discord_id_1: int, discord_name: str, thread_id):
    idx = header_index(ws)
    row = [""] * len(ws.row_values(1))

    def setv(key: str, val: str):
        c = idx.get(key)
        if c:
            row[c - 1] = val

    setv("timestamp(JST)", _now_jst_str())
    setv("受理No", str(receipt_no))
    setv("C-No", "")
    setv("status", STATUS_DRAFT)
    setv("Discord名", discord_name)
    setv("DiscordID_1", str(discord_id_1))
    setv("Discord名_1", discord_name)
    setv("threadID", str(thread_id))
    setv("質問項目(ONのみ)", "")
    ws.append_row(row, value_input_option="RAW")

def update_row_answers(ws, row_num: int, answers: Dict[str, Any], status: str):
    idx = header_index(ws)

    def upd(key: str, val: str):
        c = idx.get(key)
        if c:
            ws.update_cell(row_num, c, val)

    upd("timestamp(JST)", _now_jst_str())
    upd("status", status)

    # answers mapping
    if "platform" in answers:
        upd("機種", str(answers.get("platform", "")))
    if "epic" in answers:
        upd("EPIC ID", str(answers.get("epic", "")))
    if "callname" in answers:
        upd("呼び名", str(answers.get("callname", "")))
    if "xid" in answers:
        upd("XのID", str(answers.get("xid", "")))
    if "xurl" in answers:
        upd("XのURL", str(answers.get("xurl", "")))
    if "custom" in answers:
        upd("カスタム権限", str(answers.get("custom", "")))
    if "ikigomi" in answers:
        upd("意気込みメッセージ", str(answers.get("ikigomi", "")))

    # ON only list
    on_list = []
    for k in CONFIG.get("indiv_order") or []:
        if k in answers and str(answers.get(k, "")).strip():
            on_list.append(k)
    upd("質問項目(ONのみ)", ",".join(on_list))


def _to_int(v: Any) -> int:
    """Best-effort int conversion (used for receipt numbers)."""
    try:
        return int(str(v).strip())
    except Exception:
        return 0



def _find_row_by_receipt_and_user(ws, receipt_no: int, discord_id_1: int) -> Optional[int]:
    idx = header_index(ws)
    col_id = idx.get("DiscordID_1")
    col_receipt = idx.get("受理No")
    if not col_id or not col_receipt:
        return None

    vals = ws.get_all_values()
    for r_i in range(2, len(vals) + 1):
        row = vals[r_i - 1]
        did = str(row[col_id - 1]).strip()
        rec = str(row[col_receipt - 1]).strip()
        if did.isdigit() and rec.isdigit() and int(did) == int(discord_id_1) and int(rec) == int(receipt_no):
            return r_i
    return None

def append_final_row(ws, receipt_no: int, discord_id_1: int, discord_name: str, thread_id: int, answers: Dict[str, Any]):
    """受付完了時にだけ append する（ドラフトは作らない）"""
    idx = header_index(ws)
    headers = ws.row_values(1)
    row = [""] * len(headers)

    def setv(key: str, val: str):
        c = idx.get(key)
        if c:
            row[c - 1] = val

    setv("timestamp(JST)", _now_jst_str())
    setv("受理No", str(receipt_no))
    setv("status", STATUS_ACCEPTED)
    setv("Discord名", discord_name)
    setv("DiscordID_1", str(discord_id_1))
    setv("Discord名_1", discord_name)
    setv("threadID", str(thread_id))
    setv("抽選ポイント(空欄OK)", "")

    setv("機種", str(answers.get("platform", "")))
    setv("EPIC ID", str(answers.get("epic", "")))
    setv("呼び名", str(answers.get("callname", "")))
    setv("XのID", str(answers.get("xid", "")))
    setv("XのURL", str(answers.get("xurl", "")))
    setv("カスタム権限", str(answers.get("custom", "")))
    setv("意気込みメッセージ", str(answers.get("ikigomi", "")))

    on_list = []
    for k in CONFIG.get("indiv_order") or []:
        if str(answers.get(k, "")).strip():
            on_list.append(k)
    setv("質問項目(ONのみ)", ",".join(on_list))

    ws.append_row(row, value_input_option="RAW")

# =========================
# Channel name control
# =========================
async def sync_entry_channel_name(client: discord.Client, guild_id: int):
    """
    受付パネルが無いなら '大会概要' に戻す。
    受付パネルがあるならステータスに応じて suffix を付ける。
    """
    ch = client.get_channel(ENTRY_CHANNEL_ID)
    if ch is None:
        try:
            ch = await client.fetch_channel(ENTRY_CHANNEL_ID)
        except Exception:
            return
    if not isinstance(ch, discord.TextChannel):
        return

    base = "大会概要"
    if not ENTRY_PANEL_MSG.get(guild_id):
        desired = base
    else:
        s = accept_status_text()
        if s in ("受付期間前", "受付期間前（動作確認中）", "受付期間未設定"):
            desired = f"{base}（受付開始前）"
        elif s in ("受付中", "メンテナンス中"):
            desired = f"{base}（エントリー受付中）"
        else:
            desired = f"{base}（受付〆切）"

    try:
        if ch.name != desired:
            await ch.edit(name=desired)
    except Exception:
        pass


async def refresh_entry_panel_message(client: discord.Client, guild_id: int) -> None:
    """既存の受付パネル（embed + view）を最新CONFIGで再描画する。無ければ何もしない。"""
    # message id: memory first, then config persistence (if any)
    mid = ENTRY_PANEL_MSG.get(guild_id)
    try:
        if not mid:
            pl = CONFIG.get("panel_lock") or {}
            mid2 = pl.get("entry_panel_msg_id")
            if str(mid2).isdigit():
                mid = int(mid2)
    except Exception:
        pass
    if not mid:
        return

    # fetch entry channel
    ch = client.get_channel(ENTRY_CHANNEL_ID)
    if ch is None:
        try:
            ch = await client.fetch_channel(ENTRY_CHANNEL_ID)
        except Exception:
            return
    if not isinstance(ch, discord.TextChannel):
        return

    try:
        msg = await ch.fetch_message(int(mid))
    except Exception:
        return

    # Rebuild embed + view
    try:
        await msg.edit(embed=build_panel_embed(), view=EntryPanelView())
    except Exception:
        pass

# =========================
# Entry panel (public)
# =========================
class EntryPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        for child in self.children:
            if isinstance(child, discord.ui.Button) and child.custom_id == "entry:main":
                child.label = entry_button_label()

    @discord.ui.button(
        label="エントリーはこちら",
        style=discord.ButtonStyle.success,
        custom_id="entry:main",
        row=0
    )
    async def entry_main(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):
        # 受付チャンネル制限
        if interaction.channel_id != ENTRY_CHANNEL_ID:
            await interaction.response.send_message(
                "受付チャンネルから操作してください。",
                ephemeral=True
            )
            return

        # フェーズ判定
        member = interaction.user
        if not isinstance(member, discord.Member):
            await interaction.response.send_message(
                "権限判定に失敗しました。",
                ephemeral=True
            )
            return

        if not entry_button_enabled_for(member):
            await interaction.response.send_message(
                "現在この操作はできません。",
                ephemeral=True
            )
            return

        # followup を使うので先に ACK（タイムアウト防止）
        # 既に ensure_thread_state 内で ACK 済みの場合があるため、二重応答を避ける
        await silent_ack(interaction, ephemeral=True)

        # 重複発行防止：一度作成した個スレを再利用する（threadタイトルはダミーなので使わない）
        try:
            threads_map = CONFIG.setdefault("threads", {})
            existing_tid = int(threads_map.get(str(interaction.user.id), 0) or 0)
        except Exception:
            existing_tid = 0

        if existing_tid:
            existing = interaction.client.get_channel(existing_tid)
            if existing is None:
                try:
                    existing = await interaction.client.fetch_channel(existing_tid)
                except Exception:
                    existing = None

            if isinstance(existing, discord.Thread):
                # 既存スレを案内して終了（新規発行しない）
                try:
                    await interaction.followup.send(
                        f"⚠️【発行済】エントリーのお手続きを進めてください：{existing.mention}　",
                        ephemeral=True
                    )
                except Exception:
                    pass
                return
            else:
                # 参照不能ならマップを掃除して作り直しを許可
                try:
                    threads_map.pop(str(interaction.user.id), None)
                    save_config(CONFIG)
                except Exception:
                    pass


        # スレッド作成
        parent = interaction.client.get_channel(THREAD_PARENT_CHANNEL_ID)
        if parent is None:
            try:
                parent = await interaction.client.fetch_channel(
                    THREAD_PARENT_CHANNEL_ID
                )
            except Exception:
                await interaction.followup.send(
                    "スレッド作成先チャンネルが見つかりません。",
                    ephemeral=True
                )
                return

        if not isinstance(parent, discord.TextChannel):
            await interaction.followup.send(
                "スレッド作成先がテキストチャンネルではありません。",
                ephemeral=True
            )
            return

        # シートを開く（採番・転記に使用）
        try:
            ws = open_worksheet()
        except Exception as e:
            await interaction.followup.send(
                f"シート参照エラー：{e}",
                ephemeral=True
            )
            return

        # 既存エントリー（SS）チェック：DiscordID_1 を検索 → status を見て分岐
        #  - 記入中(またはロック中): 既存スレッドへ誘導（新規生成しない）
        #  - 受付完了: 受理済み案内
        #  - キャンセル: 再エントリー可（= 既存なし扱い）
        try:
            row_info = find_existing_thread_for_user(ws, interaction.user.id)
        except Exception:
            row_info = None

        if row_info:
            _row, _status, _thread_id, _receipt = row_info

            # キャンセル済みは再エントリー可
            if str(_status) != STATUS_CANCELED:
                th = None
                if _thread_id:
                    try:
                        th = interaction.client.get_channel(int(_thread_id))
                        if th is None:
                            th = await interaction.client.fetch_channel(int(_thread_id))
                    except Exception:
                        th = None

                if str(_status) == STATUS_ACCEPTED:
                    if isinstance(th, discord.Thread):
                        await interaction.followup.send(
                            f"❌エントリー済｜{th.mention}",
                            ephemeral=True
                        )
                    else:
                        await interaction.followup.send(
                            "❌エントリー済｜スレッドが見つからないため、運営に問い合わせてください。",
                            ephemeral=True
                        )
                    return

                # 記入中 / ロック中 など（受付完了以外）
                if isinstance(th, discord.Thread):
                    await interaction.followup.send(
                        f"⚠️【発行済】エントリーのお手続きを進めてください：{existing.mention}",
                        ephemeral=True
                    )
                else:
                    await interaction.followup.send(
                        "❌エントリー済｜スレッドが見つからないため、運営に問い合わせてください。",
                        ephemeral=True
                    )
                return

        # 仮No 採番（スレッド作成時）
        draft_no = get_next_draft_no()
        receipt_no = int(draft_no)

        try:
            thread = await parent.create_thread(
                name=format_thread_title(STATUS_DRAFT, receipt_no, interaction.user.display_name),
                type=discord.ChannelType.private_thread,
                auto_archive_duration=10080,
                invitable=False,
            )
            await thread.add_user(interaction.user)
        except discord.HTTPException as e:
            await interaction.followup.send(
                f"スレッド作成に失敗しました：{e}",
                ephemeral=True
            )
            return
        # SSへの転記は「受付完了時のみ」行う（ここでは書かない）
        sheet_row = None


        # メモリ状態登録
        THREAD_STATE[thread.id] = {
            "owner_id": interaction.user.id,
            "owner_name": interaction.user.display_name,
            "draft_no": int(receipt_no),
            "receipt_no": int(receipt_no),
            "sheet_row": sheet_row,
            "status": STATUS_DRAFT,
            "answers": {},
            "in_entry": False,
            "pending_key": None,
            "in_edit": False,
            "edit_from_index": None,
            "golive_waiting": False,
        }

        # 永続マップ：user_id -> thread_id
        try:
            CONFIG.setdefault("threads", {})[str(interaction.user.id)] = int(thread.id)
            save_config(CONFIG)
        except Exception:
            pass

        await post_thread_intro(thread, interaction.user)

        await interaction.followup.send(
            f"あなた専用のエントリースレッドを作成しました。{thread.mention}：移動してお手続きをしてください。",
            ephemeral=True
        )

# =========================
# スレッド内：A→開始
# =========================
async def post_thread_intro(thread: discord.Thread, user: discord.Member):
    msg = await thread.send(
        f"{user.mention}\n"
        "ーーーーーーーーーーーーーーー\n"
        "📢このスレッドはあなた専用です\n"
        "ーーーーーーーーーーーーーーー",
        view=ThreadEntryLoopView()
    )
    st = THREAD_STATE.get(thread.id)
    if st:
        st["intro_msg_id"] = msg.id
        # フロー中に生成したメッセージID（質問・回答まとめ・内容確認など）を追跡
        st.setdefault("flow_msg_ids", [])

class ThreadEntryLoopView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="エントリーを開始する", style=discord.ButtonStyle.success, custom_id="thread:toggle_entry", row=0)
    async def toggle_entry(self, interaction: discord.Interaction, button: discord.ui.Button):
        st = await ensure_thread_state(interaction)
        if not st:
            return
        if interaction.user.id != st.get("owner_id"):
            await interaction.response.send_message("この操作は本人のみ実行できます。", ephemeral=True)
            return

        # 受付中以外は進めない（動作確認中は運営だけOK）
        member = interaction.user
        if isinstance(member, discord.Member):
            if not entry_button_enabled_for(member):
                await interaction.response.send_message("現在この操作はできません。", ephemeral=True)
                return

        thread = interaction.channel
        if not isinstance(thread, discord.Thread):
            return

        # 「開始」→開始／「クリア」→初期化、を1ボタンでループ
        if not st.get("in_entry"):
            # 開始：初期化してから質問へ
            # エフェメラルで確認を出したいので、最初のACKもephemeralでdeferする
            await interaction.response.defer(thinking=False, ephemeral=True)
            await reset_entry_flow(thread, st, to_initial=False)
            st["in_edit"] = False
            st["edit_from_index"] = None
            st["in_entry"] = True

            # ボタン文言を「クリア」に切替
            button.label = "入力内容をクリア（エントリーは中止されます）"
            button.style = discord.ButtonStyle.danger
            try:
                intro_mid = st.get("intro_msg_id")
                if intro_mid:
                    intro_msg = await thread.fetch_message(intro_mid)
                    await intro_msg.edit(view=self)
            except Exception:
                pass

            # 質問項目一覧（返信ではなく通常投稿）
            try:
                qmsg = await thread.send(embed=build_question_list_embed(st))
                st.setdefault("flow_msg_ids", []).append(qmsg.id)
            except Exception:
                pass

            await ask_next_question(thread)
            return

        # クリア：状態を初期化して導入状態へ戻す
        # 既に ensure_thread_state 内で ACK 済みの場合があるため、二重応答を避ける
        await silent_ack(interaction, ephemeral=True)
        await reset_entry_flow(thread, st, to_initial=True)

        st["in_entry"] = False
        st["pending_key"] = None
        st["in_edit"] = False
        st["edit_from_index"] = None

        # ボタン文言を「開始」に戻す
        button.label = "エントリーを開始する"
        button.style = discord.ButtonStyle.success
        try:
            intro_mid = st.get("intro_msg_id")
            if intro_mid:
                intro_msg = await thread.fetch_message(intro_mid)
                await intro_msg.edit(view=self)
        except Exception:
            pass
# =========================
# 運営フォーラム：進捗ボタン（対応中／対応完了）
# =========================
class OpsStatusView(discord.ui.View):
    """Forum thread only. Uses interaction.channel_id as the forum thread id.
    Status source of truth is CONFIG['ops_status'][forum_thread_id].
    """
    def __init__(self):
        super().__init__(timeout=None)

        # Apply ✅ marks + disable rules based on current status
        try:
            # channel_id is unknown at init time; set defaults.
            # We'll update labels/disabled inside callbacks and also in _apply_state when message is edited.
            pass
        except Exception:
            pass

    def _get_status(self, forum_thread_id: int) -> str:
        return str(_ops_status_map().get(str(forum_thread_id), OPS_STATUS_NEW) or OPS_STATUS_NEW)

    def _apply_state(self, forum_thread_id: int):
        st = self._get_status(forum_thread_id)

        for child in self.children:
            if not isinstance(child, discord.ui.Button):
                continue

            if child.custom_id == "ops:status:inprogress":
                base = "対応中"
                child.label = f"✅{base}" if st == OPS_STATUS_INPROGRESS else base
                child.disabled = False

            if child.custom_id == "ops:status:done":
                base = "対応完了"
                child.label = f"✅{base}" if st == OPS_STATUS_DONE else base

                # 🟪 から 🟩 へ直行は禁止（確認が必要）
                child.disabled = (st == OPS_STATUS_ADDITIONAL)

    async def _ensure_ops_only(self, interaction: discord.Interaction) -> bool:
        m = interaction.user
        if not isinstance(m, discord.Member):
            return False
        if not has_ops_role(m) and not m.guild_permissions.administrator:
            await interaction.response.send_message("運営のみ操作できます。", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="対応中", style=discord.ButtonStyle.primary, custom_id="ops:status:inprogress", row=0)
    async def set_inprogress(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._ensure_ops_only(interaction):
            return

        # ACK first (rate-limit safe)
        try:
            if not interaction.response.is_done():
                await interaction.response.defer(thinking=False)
        except Exception:
            pass

        forum_thread = interaction.channel
        if not isinstance(forum_thread, discord.Thread):
            return

        # Update status
        _ops_status_map()[str(forum_thread.id)] = OPS_STATUS_INPROGRESS
        save_config(CONFIG)

        # Sync titles (forum + private)
        guild = interaction.guild
        if guild:
            pvt_id = int(_ops_links().get(str(forum_thread.id), 0) or 0)
            await _set_status_forum_and_private(guild, forum_thread, pvt_id, OPS_STATUS_INPROGRESS)

        # Refresh view
        self._apply_state(forum_thread.id)
        try:
            try:
                if interaction.response.is_done():
                    await interaction.message.edit(view=self)
                else:
                    await interaction.response.edit_message(view=self)
            except discord.NotFound:
                pass
            except Exception:
                pass
        except Exception:
            try:
                await interaction.response.defer(thinking=False, ephemeral=True)
            except Exception:
                pass

    @discord.ui.button(label="対応完了", style=discord.ButtonStyle.success, custom_id="ops:status:done", row=0)
    async def set_done(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._ensure_ops_only(interaction):
            return

        # ACK first (rate-limit safe)
        try:
            if not interaction.response.is_done():
                await interaction.response.defer(thinking=False)
        except Exception:
            pass

        forum_thread = interaction.channel
        if not isinstance(forum_thread, discord.Thread):
            return

        cur = self._get_status(forum_thread.id)
        if cur == OPS_STATUS_ADDITIONAL:
            # 直行禁止（確認を挟む）
            await interaction.response.send_message("🟪（追加連絡あり）のまま完了にはできません。先に「対応中」で内容確認してください。", ephemeral=True)
            return

        _ops_status_map()[str(forum_thread.id)] = OPS_STATUS_DONE
        save_config(CONFIG)

        guild = interaction.guild
        if guild:
            pvt_id = int(_ops_links().get(str(forum_thread.id), 0) or 0)
            await _set_status_forum_and_private(guild, forum_thread, pvt_id, OPS_STATUS_DONE)

        self._apply_state(forum_thread.id)
        try:
            try:
                if interaction.response.is_done():
                    await interaction.message.edit(view=self)
                else:
                    await interaction.response.edit_message(view=self)
            except discord.NotFound:
                pass
            except Exception:
                pass
        except Exception:
            try:
                await interaction.response.defer(thinking=False, ephemeral=True)
            except Exception:
                pass




async def reset_entry_flow(thread: discord.Thread, st: Dict[str, Any], to_initial: bool):
    """フロー中に生成したメッセージを掃除し、入力状態を初期化。
    to_initial=True の場合は「スレッド作成直後の状態」に戻す（開始前）。
    """
    # 追跡しているフロー投稿を削除
    mids = list(st.get("flow_msg_ids", []))
    st["flow_msg_ids"] = []
    # pending 質問も削除候補
    pq = st.get("pending_question_msg_id")
    if pq:
        mids.append(pq)
    for mid in mids:
        try:
            if st.get("intro_msg_id") and mid == st.get("intro_msg_id"):
                continue
            msg = await thread.fetch_message(int(mid))
            await msg.delete()
        except Exception:
            pass

    # 回答クリア
    st["answers"] = {}
    st["pending_key"] = None
    st["pending_question_msg_id"] = None
    st["awaiting_text"] = False


    # ALWAYS reset edit pointers when restarting entry flow
    st["in_edit"] = False
    st["edit_from_index"] = None
    # 編集中状態解除（初期化）
    if to_initial:
        st["in_edit"] = False
        st["edit_from_index"] = None

def indiv_order() -> List[str]:
    """個人質問の表示順（CONFIGに基づく）。"""
    return list(CONFIG.get("indiv_order") or [])

def _q_total() -> int:
    return len(indiv_order())

def _q_no_for_key(key: str) -> int:
    try:
        return indiv_order().index(key) + 1
    except ValueError:
        return 0

def _summary_text(key: str, value: str) -> str:
    n = _q_no_for_key(key)
    label = INDIV_LABELS.get(key, key)

    v = str(value).strip()
    if key == "xid":
        xid = str(v).lstrip("@").strip()
        v = f"https://x.com/{xid}" if xid else ""

    # 「回答🔗」の改行は意気込みメッセージのみ
    if key == "ikigomi":
        return f"💬{n}｜{label}\n回答🔗\n{v}"
    return f"💬{n}｜{label}\n回答🔗{v}"


async def _post_summary(thread: discord.Thread, st: Dict[str, Any], key: str):
    val = str(st.get("answers", {}).get(key, "")).strip()
    if not val:
        return
    try:
        msg = await thread.send(_summary_text(key, val))
        st.setdefault("flow_msg_ids", []).append(msg.id)
    except Exception:
        pass

def _normalize_xid(raw: str) -> str:
    v = str(raw or "").strip()
    if v.startswith("@"):
        v = v[1:]
    return v.strip()

def _valid_xid(v: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_]+", v or ""))

def _valid_psn_name(v: str) -> bool:
    # 英字で開始、英数字・-・_、3-16文字
    return bool(re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{2,15}", v or ""))

def build_question_list_embed(st: Dict[str, Any]) -> discord.Embed:
    order: List[str] = list(CONFIG.get("indiv_order") or [])
    lines = []
    for i, k in enumerate(order, start=1):
        lines.append(f"💬{i}：{INDIV_LABELS.get(k, k)}")
    desc = "\n".join(lines) if lines else "（質問が未設定）"
    embed = discord.Embed(title="質問項目一覧", description=desc, color=COLOR_QUESTION_LIST)
    return embed

# =========================
# 質問UI
# =========================
def _q_index_for_key(key: str) -> int:
    order = list(CONFIG.get("indiv_order") or [])
    try:
        return order.index(key) + 1
    except Exception:
        return 0

async def ask_next_question(thread: discord.Thread):
    st = THREAD_STATE.get(thread.id)
    if not st:
        return
    if not st.get("in_entry"):
        return

    order: List[str] = list(CONFIG.get("indiv_order") or [])
    answers: Dict[str, Any] = st.get("answers", {})

    # 修正開始位置があるなら、その位置以降を優先して聞く
    start_i = st.get("edit_from_index")
    order_iter = order[start_i:] if start_i is not None else order

    for key in order_iter:
        if str(answers.get(key, "")).strip() != "":
            continue

        st["pending_key"] = key
        st["awaiting_text"] = False

        n = _q_no_for_key(key)
        label = INDIV_LABELS.get(key, key)

        # 質問ごとの文言
        if key == "platform":
            title = f"💬{n}｜機種"
            desc = "機種を選択してください\nPC/PS/Xbox/Switch/Mobile"
            embed = discord.Embed(title=title, description=desc, color=COLOR_QUESTION)
            msg = await thread.send(embed=embed, view=(PlatformSelectEditView() if st.get("in_edit") else PlatformSelectView()))
            st["pending_question_msg_id"] = msg.id
            st.setdefault("flow_msg_ids", []).append(msg.id)
            return

        if key == "epic":
            # PS注意書き（先に出す）
            if str(answers.get("platform", "")).strip() == "PS":
                ps_text = (
                    "📌PSユーザーの方\n"
                    "PSの方のディスプレイネームは［**PlaystationName**］です\n"
                    "・英数字、ハイフン（-）、アンダーバー（_）のみ\n"
                    "・3～16文字で最初の文字は英字"
                )
                try:
                    m0 = await thread.send(ps_text)
                    st["ps_note_msg_id"] = m0.id
                except Exception:
                    pass

            title = f"💬{n}｜EPIC ID（ディスプレイネーム）"
            desc = "ディスプレイネームを入力してください。"
            embed = discord.Embed(title=title, description=desc, color=COLOR_QUESTION)
            if st.get("in_edit"):
                msg = await thread.send(embed=embed, view=EditItemCancelView())
            else:
                msg = await thread.send(embed=embed)
            st["pending_question_msg_id"] = msg.id
            st["awaiting_text"] = True
            st.setdefault("flow_msg_ids", []).append(msg.id)
            return

        if key == "callname":
            title = f"💬{n}｜呼び名"
            desc = "配信でお呼びするお名前を入力してください（仮名で入力）"
            embed = discord.Embed(title=title, description=desc, color=COLOR_QUESTION)
            if st.get("in_edit"):
                msg = await thread.send(embed=embed, view=EditItemCancelView())
            else:
                msg = await thread.send(embed=embed)
            st["pending_question_msg_id"] = msg.id
            st["awaiting_text"] = True
            st.setdefault("flow_msg_ids", []).append(msg.id)
            return

        if key == "xid":
            title = f"💬{n}｜XのID"
            desc = "XのIDのみを入力してください。@は不要です。\n※英数字、アンダーバー（_)のみ"
            embed = discord.Embed(title=title, description=desc, color=COLOR_QUESTION)
            msg = await thread.send(embed=embed)
            st["pending_question_msg_id"] = msg.id
            st["awaiting_text"] = True
            st.setdefault("flow_msg_ids", []).append(msg.id)
            return

        if key == "custom":
            title = f"💬{n}｜カスタム権限"
            desc = "カスタム権限はお持ちですか？（キーホストをお願いする場合があります）"
            embed = discord.Embed(title=title, description=desc, color=COLOR_QUESTION)
            msg = await thread.send(embed=embed, view=(CustomSelectEditView() if st.get("in_edit") else CustomSelectView()))
            st["pending_question_msg_id"] = msg.id
            st.setdefault("flow_msg_ids", []).append(msg.id)
            return

        if key == "ikigomi":
            title = f"💬{n}｜意気込みメッセージ"
            desc = (
                "大会への意気込みをお聞かせください！\n"
                "配信でご紹介させていただくかもしれません。\n"
                "（配信で映したくない場合など、「なし」の一言で大丈夫です）"
            )
            embed = discord.Embed(title=title, description=desc, color=COLOR_QUESTION)
            msg = await thread.send(embed=embed)
            st["pending_question_msg_id"] = msg.id
            st["awaiting_text"] = True
            st.setdefault("flow_msg_ids", []).append(msg.id)
            return

    # ここまで来たら全部埋まっている → 内容確認へ
    st["pending_key"] = None
    st["edit_from_index"] = None
    await post_confirm(thread)



class PlatformSelectView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    def _refresh_marks(self, selected: str):
        # ボタンラベルに ✅ を付けて選択状態を分かりやすくする
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                if child.custom_id in {
                    "q:platform:pc",
                    "q:platform:ps",
                    "q:platform:xbox",
                    "q:platform:switch",
                    "q:platform:mobile",
                }:
                    base = str(child.label).replace("✅", "").strip()
                    child.label = f"✅{base}" if base == selected else base
                elif child.custom_id == "q:platform:next":
                    child.disabled = False

    async def _set(self, interaction: discord.Interaction, value: str):
        st = await ensure_thread_state(interaction)
        if not st:
            return
        if interaction.user.id != st["owner_id"]:
            await interaction.response.send_message("この操作は本人のみ実行できます。", ephemeral=True)
            return

        st["answers"]["platform"] = value
        if st.get("in_edit"):
            st.setdefault("edited_fields", set()).add("platform")
        self._refresh_marks(value)

        # 選択したら「次へ」を押して進む
        try:
            if interaction.response.is_done():
                await interaction.message.edit(view=self)
            else:
                await interaction.response.edit_message(view=self)
        except discord.NotFound:
            pass
        except Exception:
            pass
    @discord.ui.button(label="PC", style=discord.ButtonStyle.secondary, custom_id="q:platform:pc", row=0)
    async def pc(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._set(interaction, "PC")

    @discord.ui.button(label="PS", style=discord.ButtonStyle.secondary, custom_id="q:platform:ps", row=0)
    async def ps(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._set(interaction, "PS")

    @discord.ui.button(label="Xbox", style=discord.ButtonStyle.secondary, custom_id="q:platform:xbox", row=0)
    async def xbox(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._set(interaction, "Xbox")

    @discord.ui.button(label="Switch", style=discord.ButtonStyle.secondary, custom_id="q:platform:switch", row=0)
    async def sw(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._set(interaction, "Switch")

    @discord.ui.button(label="Mobile", style=discord.ButtonStyle.secondary, custom_id="q:platform:mobile", row=1)
    async def mobile(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._set(interaction, "Mobile")

    @discord.ui.button(label="次へ", style=discord.ButtonStyle.success, custom_id="q:platform:next", row=2, disabled=True)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        st = await ensure_thread_state(interaction)
        if not st:
            return
        if interaction.user.id != st["owner_id"]:
            await interaction.response.send_message("この操作は本人のみ実行できます。", ephemeral=True)
            return

        if not str(st.get("answers", {}).get("platform", "")).strip():
            await interaction.response.send_message("先に機種を選択してください。", ephemeral=True)
            return

        # 既に ensure_thread_state 内で ACK 済みの場合があるため、二重応答を避ける
        await silent_ack(interaction, ephemeral=True)

        # 質問メッセを削除して、ログ（質問+回答）だけ残す
        try:
            await interaction.message.delete()
        except Exception:
            pass
        st["pending_question_msg_id"] = None

        if isinstance(interaction.channel, discord.Thread):
            await _post_summary(interaction.channel, st, "platform")
            if st.get("in_edit"):
                await _return_to_edit_picker(interaction.channel, st)
            else:
                await ask_next_question(interaction.channel)


class CustomSelectView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    def _refresh_marks(self, selected: str):
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                if child.custom_id in {"q:custom:yes", "q:custom:no"}:
                    base = str(child.label).replace("✅", "").strip()
                    child.label = f"✅{base}" if base == selected else base
                elif child.custom_id == "q:custom:next":
                    child.disabled = False

    async def _set(self, interaction: discord.Interaction, value: str):
        st = await ensure_thread_state(interaction)
        if not st:
            return
        if interaction.user.id != st["owner_id"]:
            await interaction.response.send_message("この操作は本人のみ実行できます。", ephemeral=True)
            return

        st["answers"]["custom"] = value
        if st.get("in_edit"):
            st.setdefault("edited_fields", set()).add("custom")
        self._refresh_marks(value)
        try:
            if interaction.response.is_done():
                await interaction.message.edit(view=self)
            else:
                await interaction.response.edit_message(view=self)
        except discord.NotFound:
            pass
        except Exception:
            pass
    @discord.ui.button(label="はい", style=discord.ButtonStyle.secondary, custom_id="q:custom:yes", row=0)
    async def yes(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._set(interaction, "はい")

    @discord.ui.button(label="いいえ", style=discord.ButtonStyle.secondary, custom_id="q:custom:no", row=0)
    async def no(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._set(interaction, "いいえ")

    @discord.ui.button(label="次へ", style=discord.ButtonStyle.success, custom_id="q:custom:next", row=1, disabled=True)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        st = await ensure_thread_state(interaction)
        if not st:
            return
        if interaction.user.id != st["owner_id"]:
            await interaction.response.send_message("この操作は本人のみ実行できます。", ephemeral=True)
            return

        if not str(st.get("answers", {}).get("custom", "")).strip():
            await interaction.response.send_message("先に選択してください。", ephemeral=True)
            return

        # 既に ensure_thread_state 内で ACK 済みの場合があるため、二重応答を避ける
        await silent_ack(interaction, ephemeral=True)
        try:
            await interaction.message.delete()
        except Exception:
            pass
        st["pending_question_msg_id"] = None

        if isinstance(interaction.channel, discord.Thread):
            await _post_summary(interaction.channel, st, "custom")
            if st.get("in_edit"):
                await _return_to_edit_picker(interaction.channel, st)
            else:
                await ask_next_question(interaction.channel)


class TextInputModal(discord.ui.Modal):
    def __init__(self, key: str):
        self.key = key
        title = f"{INDIV_LABELS.get(key, key)}の入力"
        super().__init__(title=title)

        label = INDIV_LABELS.get(key, key)
        placeholder = ""
        max_len = 100

        if key == "epic":
            placeholder = "例：Takenoco1140"
            max_len = 50
        elif key == "callname":
            placeholder = "例：たけのこ"
            max_len = 20
        elif key == "xid":
            placeholder = "例：@xxxx"
            max_len = 30
        elif key == "ikigomi":
            placeholder = "一言どうぞ"
            max_len = 200

        self.inp = discord.ui.TextInput(
            label=label,
            required=True,
            placeholder=placeholder,
            max_length=max_len,
        )
        self.add_item(self.inp)
    async def on_submit(self, interaction: discord.Interaction):
        st = await ensure_thread_state(interaction)
        if not st:
            return
        if interaction.user.id != st["owner_id"]:
            await interaction.response.send_message("この操作は本人のみ実行できます。", ephemeral=True)
            return

        val = str(self.inp.value).strip()

        # 入力チェック
        if self.key == "xid":
            # @ は任意、保存は @無しで統一
            v = _normalize_xid(val)
            if not _valid_xid(v):
                await interaction.response.send_message("⚠️ XのIDは英数字とアンダーバー（_）のみです。", ephemeral=True)
                return
            val = "@" + v  # 表示は @ 付きのまま

        if self.key == "epic":
            # PSユーザーだけ厳格チェック（仕様に合わせる）
            if str(st.get("answers", {}).get("platform", "")).strip() == "PS":
                if not _valid_psn_name(val):
                    await interaction.response.send_message(
                        "⚠️ PSのディスプレイネーム形式が不正です。\n"
                        "・英字で開始\n・英数字/-(ハイフン)/_(アンダーバー)のみ\n・3〜16文字",
                        ephemeral=True
                    )
                    return

        st["answers"][self.key] = val

        # 質問メッセを削除してまとめを残す
        try:
            qid = st.get("pending_question_msg_id")
            if qid and isinstance(interaction.channel, discord.Thread):
                qmsg = await interaction.channel.fetch_message(int(qid))
                await qmsg.delete()
        except Exception:
            pass
        st["pending_question_msg_id"] = None

        # 既に ensure_thread_state 内で ACK 済みの場合があるため、二重応答を避ける
        await silent_ack(interaction, ephemeral=True)

        # まとめ投稿 → 次の質問へ
        if isinstance(interaction.channel, discord.Thread):
            await _post_summary(interaction.channel, st, self.key)
            if st.get("in_edit"):
                await _return_to_edit_picker(interaction.channel, st)
            else:
                await ask_next_question(interaction.channel)





class TextInputView(discord.ui.View):
    """テキスト入力用の「入力する」ボタンView。
    custom_id を key ごとに分けて、複数質問でも衝突しないようにする。
    """
    def __init__(self, key: str):
        super().__init__(timeout=None)
        self.key = key

        # custom_id はユニークにする（永続Viewでも衝突しない）
        btn = discord.ui.Button(
            label="入力する",
            style=discord.ButtonStyle.secondary,
            custom_id=f"q:text:open:{key}",
            row=0,
        )
        btn.callback = self._open_modal  # type: ignore
        self.add_item(btn)

    async def _open_modal(self, interaction: discord.Interaction):
        await interaction.response.send_modal(TextInputModal(self.key))

# =========================
# 内容確認（確定/修正/中断）
# =========================
def build_receipt_embed(st: Dict[str, Any]) -> discord.Embed:
    a = st.get("answers", {})
    receipt_no = st.get("receipt_no", 0)

    lines: list[str] = []

    def add(label: str, key: str, limit: int = 300):
        v = _clip_text(str(a.get(key, "")).strip(), limit)
        lines.append(f"**{label}**：{v if v else '（未入力）'}")

    add("機種", "platform", 64)
    add("EPIC ID", "epic", 128)
    add("呼び名", "callname", 128)
    add("XのID", "xid", 64)
    # XのURLは後段で送る想定だが一応項目として残す
    add("XのURL", "xurl", 256)
    add("カスタム権限", "custom", 128)
    if CONFIG.get("need_ikigomi", True):
        add("意気込みメッセージ", "ikigomi", 1200)

    desc = _clip_text('\n'.join(lines), 3900)
    embed = discord.Embed(
        title=_clip_text(f"受付票（E-No.{_to_int(receipt_no):03d}）", 256),
        description=desc,
        color=COLOR_INFO,
    )
    return embed

def build_final_receipt_embed(st: Dict[str, Any]) -> discord.Embed:
    """Receipt embed after accepted. Clipped for Discord limits."""
    a = st.get("answers", {})
    receipt_no = st.get("receipt_no", "")
    mention = f"<@{st.get('owner_id', '')}>" if st.get('owner_id') else st.get('owner_name', '')

    epic = _clip_text(str(a.get('epic', '')).strip(), 128)
    callname = _clip_text(str(a.get('callname', '')).strip(), 128)
    platform = _clip_text(str(a.get('platform', '')).strip(), 64)
    custom = _clip_text(str(a.get('custom', '')).strip(), 128)
    # Ikigomi is user-generated and can exceed embed limits.
    ikigomi = _clip_text(str(a.get('ikigomi', '')).strip(), 1200)

    lines = []
    lines.append('ーーーーーーーー')
    lines.append(f'{mention}さま')
    lines.append('以下の内容でエントリーを受け付けました')
    lines.append('ーーーーーーーー')
    lines.append(f'EPIC ID：{epic}')
    lines.append(f'呼び名：{callname}')
    lines.append(f'機種：{platform}')
    lines.append(f'カスタム権限：{custom}')
    if CONFIG.get('need_ikigomi', True):
        lines.append('ーーーーーーーーーー')
        lines.append('意気込みメッセージ：')
        lines.append(ikigomi if ikigomi else '（なし）')

    desc = _clip_text('\n'.join(lines), 3900)

    embed = discord.Embed(
        title=_clip_text(f'📙受付票｜E-No.{_to_int(receipt_no):03d}', 256),
        description=desc,
        color=COLOR_RECEIPT,
    )

    footer = st.get('_receipt_footer_override')
    if footer:
        try:
            embed.set_footer(text=_clip_text(str(footer), 2048))
        except Exception:
            pass

    return embed

def build_confirm_embed(
    st: Dict[str, Any],
    *,
    title: Optional[str] = None,
    edited_fields: Optional[set] = None,
    revision: bool = False,
) -> discord.Embed:
    a = st.get("answers", {})
    epic = str(a.get("epic", "")).strip()
    callname = str(a.get("callname", "")).strip()
    platform = str(a.get("platform", "")).strip()
    xid = _normalize_xid(str(a.get("xid", "")).strip())
    custom = str(a.get("custom", "")).strip()
    ikigomi = str(a.get("ikigomi", "")).strip()

    edited_fields = edited_fields or set()

    def mark(key: str) -> str:
        return " ✎" if key in edited_fields else ""

    lines: List[str] = []
    lines.append("ーーーーーーーー")
    lines.append("入力内容を確認してください。")
    lines.append("ーーーーーーーー")
    lines.append(f"EPIC ID：{epic}{mark('epic')}")
    lines.append(f"呼び名：{callname}{mark('callname')}")
    lines.append(f"機種：{platform}{mark('platform')}")
    lines.append(f"XのID：{xid}{mark('xid')}")
    lines.append(f"カスタム権限：{custom}{mark('custom')}")
    if CONFIG.get("need_ikigomi", True):
        lines.append("ーーーーーーーーーー")
        lines.append("意気込みメッセージ：")
        lines.append((ikigomi if ikigomi else "（なし）") + (mark('ikigomi') if 'ikigomi' in edited_fields else ""))

    if title is None:
        title = "📙内容確認"
    # revision label is used for pre-entry confirmation; for post-accept receipt ("🗂登録内容") keep title clean.
    if revision and (str(title) or "").strip() and not str(title).startswith("🗂登録内容"):
        title = f"{title}［修正版］"

    embed = discord.Embed(
        title=title,
        description="\n".join(lines),
        color=COLOR_CONFIRM
    )
    return embed

async def post_confirm(thread: discord.Thread):
    st = THREAD_STATE.get(thread.id)
    if not st:
        return

    edited_fields = set(st.get("edited_fields", set()) or set())
    in_edit = bool(st.get("in_edit"))

    if st.get("status") == STATUS_ACCEPTED:
        # Post-accept: show "修正あり" only after at least one field has been modified.
        has_mod = bool((st.get("has_modified") is True) or (edited_fields and len(edited_fields) > 0))
        title = "🗂登録内容［修正あり］" if has_mod else "🗂登録内容"
    else:
        title = "📙内容確認"

    embed = build_confirm_embed(
        st,
        title=title,
        edited_fields=edited_fields,
        revision=in_edit,
    )

    view: discord.ui.View = EditConfirmView() if in_edit else ConfirmView()

    # While editing an individual field, prevent cancel-all to avoid state mismatch
    if in_edit and st.get("pending_key"):
        try:
            for child in getattr(view, "children", []):
                if isinstance(child, discord.ui.Button) and getattr(child, "custom_id", None) in ("edit:cancel", "edit:commit"):
                    child.disabled = True
        except Exception:
            pass
    if in_edit and st.get("status") != STATUS_ACCEPTED:
        # エントリー前の修正はボタン表記だけ「✨エントリーする✨」に寄せる
        try:
            for child in getattr(view, "children", []):
                if isinstance(child, discord.ui.Button) and child.custom_id == "edit:send":
                    child.label = "✨エントリーする✨"
        except Exception:
            pass

    # 送信前確認メッセージ（📙内容確認のembedの前に1メッセージ）
    if st.get("status") != STATUS_ACCEPTED:
        pre_text = (
            "ーーーーーー送信前の確認ーーーーーー\n"
            "✨エントリーする✨を押す前に、内容の確認をお願いします。\n"
            "修正したい項目がある場合は、中断するボタンで、はじめからやりなおしてください。"
        )

        pre_id = st.get("pre_confirm_notice_msg_id")
        if pre_id:
            try:
                pre_msg = await thread.fetch_message(int(pre_id))
                # 内容が変わった場合に備えて上書き（既存メッセージを再利用）
                if (pre_msg.content or "") != pre_text:
                    await pre_msg.edit(content=pre_text)
            except Exception:
                # 取得できなければ送り直し
                pre_id = None

        if not pre_id:
            try:
                m = await thread.send(pre_text)
                st["pre_confirm_notice_msg_id"] = m.id
                st.setdefault("flow_msg_ids", []).append(m.id)
            except Exception:
                pass

        # 旧フラグ互換（残しておく）
        st["pre_confirm_notice_sent"] = True

    mid = st.get("confirm_msg_id")
    if mid:
        try:
            msg = await thread.fetch_message(int(mid))
            await msg.edit(embed=embed, view=view)
            return
        except Exception:
            pass

    try:
        msg = await thread.send(embed=embed, view=view)
        st["confirm_msg_id"] = msg.id
        st.setdefault("flow_msg_ids", []).append(msg.id)
    except Exception:
        pass

class ConfirmView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="✨エントリーする✨", style=discord.ButtonStyle.success, custom_id="confirm:ok", row=0)
    async def ok(self, interaction: discord.Interaction, button: discord.ui.Button):
        st = await ensure_thread_state(interaction)
        if not st:
            return
        if interaction.user.id != st.get("owner_id"):
            await interaction.response.send_message("この操作は本人のみ実行できます。", ephemeral=True)
            return
        if accept_status_text() not in ("受付中", "受付期間前（動作確認中）"):
            await interaction.response.send_message("現在受付中ではありません。", ephemeral=True)
            return

        thread = interaction.channel
        if not isinstance(thread, discord.Thread):
            return

        # 既に ensure_thread_state 内で ACK 済みの場合があるため、二重応答を避ける
        await silent_ack(interaction, ephemeral=True)

        sending_msg = None
        try:
            sending_msg = await thread.send("ーーーーーーーーーーー\n⌛送信中･･･受付票を発行しています。このまましばらくお待ちください。\nーーーーーーーーーーー")
        except Exception:
            pass

        # 受付完了ロール付与
        try:
            guild = thread.guild
            if guild:
                member = guild.get_member(st.get("owner_id"))
                if member:
                    role = resolve_entry_accept_role(guild)
                    if role:
                        try:
                            await member.add_roles(role, reason="OR40 entry accepted")
                        except Exception:
                            pass
        except Exception:
            pass

        # X URL
        try:
            a = st.get("answers", {})
            xid = _normalize_xid(str(a.get("xid", "")).strip())
            if xid:
                a["xid"] = xid
                a["xurl"] = f"https://x.com/{xid}"
        except Exception:
            pass

        # シート転記
        try:
            ws = open_worksheet()
            row = st.get("sheet_row")
            if row:
                update_row_answers(ws, int(row), st.get("answers", {}), STATUS_ACCEPTED)
            else:
                # 受理Noは「受付完了時」にスプレッドシート側で採番する
                try:
                    accepted_no = _next_receipt_no(ws)
                except Exception:
                    accepted_no = int(datetime.now().timestamp())
                st["receipt_no"] = int(accepted_no)

                append_final_row(
                    ws,
                    int(st.get("receipt_no", 0)),
                    int(st.get("owner_id", 0)),
                    str(st.get("owner_name", "")),
                    int(thread.id),
                    st.get("answers", {}),
                )
                r2 = _find_row_by_receipt_and_user(ws, int(st.get("receipt_no", 0)), int(st.get("owner_id", 0)))
                st["sheet_row"] = r2
        except Exception as e:
            try:
                await thread.send(f"シート更新エラー：{e}")
            except Exception:
                pass
            return

        st["status"] = STATUS_ACCEPTED

        # スレッド名
        try:
            await thread.edit(
                name=format_thread_title(STATUS_ACCEPTED, int(st.get("receipt_no", 0)), str(st.get("owner_name", "")))
            )
        except Exception:
            pass

        # フロー投稿の掃除（質問・回答ログは残す）
        try:
            mids = list(st.get("flow_msg_ids", []))
            st["flow_msg_ids"] = []
            pq = st.get("pending_question_msg_id")
            if pq:
                mids.append(pq)
            for mid in mids:
                try:
                    msg = await thread.fetch_message(int(mid))
                    await msg.delete()
                except Exception:
                    pass
            st["pending_question_msg_id"] = None
            st["awaiting_text"] = False
        except Exception:
            pass

        # 導入メッセージのボタンを削除
        try:
            intro_mid = st.get("intro_msg_id")
            if intro_mid:
                intro_msg = await thread.fetch_message(int(intro_mid))
                await intro_msg.edit(view=None)
        except Exception:
            pass

        if sending_msg:
            try:
                await sending_msg.delete()
            except Exception:
                pass

        await post_final_receipt(thread)

        return
        st = await ensure_thread_state(interaction)
        if not st:
            return
        if interaction.user.id != st.get("owner_id"):
            await interaction.response.send_message("この操作は本人のみ実行できます。", ephemeral=True)
            return

        thread = interaction.channel
        if not isinstance(thread, discord.Thread):
            return

        # 既に ensure_thread_state 内で ACK 済みの場合があるため、二重応答を避ける
        await silent_ack(interaction, ephemeral=True)


        # 修正開始の目印（中断しても「今何をしていたか」が残る）
        try:
            await thread.send("ーーー✄ここから、エントリーの修正を開始します✄ーーー")
        except Exception:
            pass

        st["in_edit"] = True
        st["edit_from_index"] = None
        # 項目選択フェーズ：確定/中止ボタンは有効（pending_key を必ずクリア）
        st["pending_key"] = None
        st["pending_question_msg_id"] = None
        st["awaiting_text"] = False
        st.setdefault("edited_fields", set())

        await post_confirm(thread)

        try:
            pmid = st.get("edit_picker_msg_id")
            if pmid:
                msg = await thread.fetch_message(int(pmid))
                await msg.delete()
        except Exception:
            pass

        try:
            m = await thread.send("修正したい項目を選択してください。", view=EditPickView())
            st["edit_picker_msg_id"] = m.id
            try:
                st.setdefault("flow_msg_ids", []).append(int(m.id))
            except Exception:
                pass
        except Exception:
            pass

    @discord.ui.button(label="中断する（初期化）", style=discord.ButtonStyle.danger, custom_id="confirm:abort", row=0)
    async def abort(self, interaction: discord.Interaction, button: discord.ui.Button):
        st = await ensure_thread_state(interaction)
        if not st:
            return
        if interaction.user.id != st.get("owner_id"):
            await interaction.response.send_message("この操作は本人のみ実行できます。", ephemeral=True)
            return
        thread = interaction.channel
        if not isinstance(thread, discord.Thread):
            return

        # 既に ensure_thread_state 内で ACK 済みの場合があるため、二重応答を避ける
        await silent_ack(interaction, ephemeral=True)
        await reset_entry_flow(thread, st, to_initial=True)
        st["in_entry"] = False

        try:
            intro_mid = st.get("intro_msg_id")
            if intro_mid:
                intro_msg = await thread.fetch_message(int(intro_mid))
                await intro_msg.edit(view=ThreadEntryLoopView())
        except Exception:
            pass


async def post_final_receipt(thread: discord.Thread):
    """受付完了後に「受付票など」を送信する。"""
    st = THREAD_STATE.get(thread.id)
    if not st:
        return

    # ① 受付票
    try:
        msg = await thread.send(embed=build_final_receipt_embed(st))
        st.setdefault("flow_msg_ids", []).append(msg.id)
        st["receipt_anchor_msg_id"] = msg.id
        st.setdefault("receipt_set_msg_ids", []).append(msg.id)
    except Exception as ex:
        # ここで落ちると受付票が一切出ないので、最低限 embed だけでも送る（Viewが原因のケースが多い）
        run_log(f"post_final_receipt: failed to send receipt with view: {ex}")
        try:
            msg = await thread.send(embed=build_final_receipt_embed(st))
            st.setdefault("flow_msg_ids", []).append(msg.id)
            st["receipt_anchor_msg_id"] = msg.id
            st.setdefault("receipt_set_msg_ids", []).append(msg.id)
        except Exception as ex2:
            run_log(f"post_final_receipt: failed to send receipt (no view): {ex2}")
            try:
                # 最終手段：テキストだけでも残す
                await thread.send("⚠️受付票の表示に失敗しました。運営に連絡してください。")
            except Exception:
                pass

    # ② XのURL（XのIDから生成）
    try:
        xid = _normalize_xid(str(st.get("answers", {}).get("xid", "")).strip())
        if xid:
            xurl = f"https://x.com/{xid}"
            msg2 = await thread.send(f"🔗XのURL\n{xurl}")
            st.setdefault("receipt_set_msg_ids", []).append(msg2.id)
        else:
            msg2 = await thread.send("🔗XのURL\n（未入力）")
            st.setdefault("receipt_set_msg_ids", []).append(msg2.id)
    except Exception:
        pass

    # ③ PC・Xboxユーザー向け GoLive
    try:
        platform = str(st.get("answers", {}).get("platform", "")).strip()
        if platform in ("PC", "Xbox"):
            # 表示順を強制：埋め込み → テキスト → ボタン
            # ※Discordの仕様上、ボタン(View)はメッセージに紐づくため、投稿を分ける。
            embed = discord.Embed(title="🔴GoLive配信の案内", color=COLOR_GOLIVE)
            body = (
                "当大会では、競技の公平性維持およびトラブル確認、そして円滑な配信のため、\n"
                "PC・Xboxユーザーの方には「GoLive配信による画面共有」を必須としております。\n"
                "・PCのスペック不足等を理由とし、配信によりゲームの挙動が著しく低下する場合は、事前申請により配信免除の可否を運営で判断します。\n"
                "・事前の申請は、配信の免除をお約束するものではありません。\n"
                "・事前申請がない場合、いかなる場合でも配信免除の対応はいたしかねます。"
            )
            try:
                msg_e = await thread.send(embed=embed)
                st.setdefault("receipt_set_msg_ids", []).append(msg_e.id)
            except Exception:
                msg_e = None

            try:
                msg_t = await thread.send(body)
                st.setdefault("receipt_set_msg_ids", []).append(msg_t.id)
            except Exception:
                pass
    except Exception:
        pass

    # ③ 受付票セット末尾：問い合わせ＆エントリー管理（案内＋ボタン3種）

    try:

        # 見出しEmbed（保持）

        embed = discord.Embed(

            title="🔷問い合わせ＆エントリー管理",

            color=COLOR_INFO,

        )

        msg_e = await thread.send(embed=embed)

        st.setdefault("receipt_set_msg_ids", []).append(msg_e.id)


        # 通常テキスト案内 + ボタン3種（AfterAcceptView）

        body = (

            "今後、質問などがある場合は、このスレッド内からご連絡ください。\n"
            "ただし、内容をご記入ただいただけでは、運営はご質問に気付くことができませんので、\n"
            "必ず下記のボタンを押すようにしてください。\n"
            "なお、PCユーザーの方の、GoLive配信に関するお問い合わせも、こちらよりお願いします。"

        )

        msg3 = await thread.send(content=body, view=AfterAcceptView())

        st.setdefault("receipt_set_msg_ids", []).append(msg3.id)

    except Exception:

        pass# =========================
# Edit / 修正フロー
# =========================

def _order_index(key: str) -> Optional[int]:
    try:
        return (CONFIG.get("indiv_order") or []).index(key)
    except ValueError:
        return None

async def _delete_edit_picker(thread: discord.Thread, st: Dict[str, Any]):
    try:
        pmid = st.get("edit_picker_msg_id")
        if pmid:
            msg = await thread.fetch_message(int(pmid))
            await msg.delete()
    except Exception:
        pass
    st["edit_picker_msg_id"] = None


async def _return_to_edit_picker(thread: discord.Thread, st: Dict[str, Any]):
    """修正時：1項目の入力が終わったら、すぐ『修正項目選択』へ戻す。"""
    # 質問フローを一旦抜ける（ここで ask_next_question に進ませない）
    st["pending_question_msg_id"] = None
    st["awaiting_text"] = False
    st["pending_key"] = None
    st["edit_from_index"] = None
    st["in_entry"] = False

    try:
        await _delete_edit_picker(thread, st)
    except Exception:
        pass

    try:
        await post_confirm(thread)
    except Exception:
        pass

    try:
        m = await thread.send("修正したい項目を選択してください。", view=EditPickView())
        st["edit_picker_msg_id"] = m.id
    except Exception:
        pass

async def start_edit_for_key(thread: discord.Thread, st: Dict[str, Any], key: str):
    idx = _order_index(key)
    if idx is None:
        return

    st.setdefault("answers", {})
    # 修正前の値を保存（キャンセル時に復元する）
    prev = str(st["answers"].get(key, ""))
    st.setdefault("_edit_prev", {})[key] = prev
    st["answers"][key] = ""
    st["pending_key"] = None
    st["pending_question_msg_id"] = None
    st["awaiting_text"] = False

    st["in_edit"] = True
    st["edit_from_index"] = idx
    st["in_entry"] = True  # 編集でも質問フローを使う
    st.setdefault("edited_fields", set())

    await _delete_edit_picker(thread, st)
    await ask_next_question(thread)

    # disable '修正を中止する' while an item is being edited
    try:
        await post_confirm(thread)
    except Exception:
        pass

# (removed duplicate _now_jst_str)

class EditItemCancelView(discord.ui.View):
    """編集中の質問で『この項目の修正をやめる』を出すためのView（通常エントリーでは使わない）"""
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="この項目の修正をやめる", style=discord.ButtonStyle.danger, custom_id="edit:item_cancel", row=2)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        st = await ensure_thread_state(interaction)
        if not st:
            return
        if interaction.user.id != st.get("owner_id"):
            await interaction.response.send_message("この操作は本人のみ実行できます。", ephemeral=True)
            return
        thread = interaction.channel
        if not isinstance(thread, discord.Thread):
            return

        # 既に ensure_thread_state 内で ACK 済みの場合があるため、二重応答を避ける
        await silent_ack(interaction, ephemeral=True)
        await cancel_current_edit_item(thread, st)


async def cancel_current_edit_item(thread: discord.Thread, st: Dict[str, Any]):
    """現在の1項目修正を中止し、元の値を復元して『修正項目選択』へ戻す。"""
    key = st.get("pending_key")
    if not key:
        return

    # 質問メッセージを削除（ログが散らからないように）
    try:
        qid = st.get("pending_question_msg_id")
        if qid:
            qmsg = await thread.fetch_message(int(qid))
            await qmsg.delete()
    except Exception:
        pass

    st["pending_question_msg_id"] = None
    st["awaiting_text"] = False
    st["pending_key"] = None
    st["edit_from_index"] = None
    st["in_entry"] = False  # 一旦質問フローを抜ける

    # 値を復元
    prev_map = st.get("_edit_prev", {}) or {}
    prev = str(prev_map.get(str(key), prev_map.get(key, "")))
    st.setdefault("answers", {})[str(key)] = prev

    # edited_fields から外す（修正版扱いにしない）
    try:
        ef = set(st.get("edited_fields", set()) or set())
        ef.discard(str(key))
        st["edited_fields"] = ef
    except Exception:
        pass

    # 編集項目選択へ戻す（EditPickViewには『修正をやめる』は出さない）
    try:
        await _delete_edit_picker(thread, st)
    except Exception:
        pass

    try:
        m = await thread.send("修正したい項目を選択してください。", view=EditPickView())
        st["edit_picker_msg_id"] = m.id
    except Exception:
        pass


async def _delete_messages_after_anchor(thread: discord.Thread, anchor_id: int, limit: int = 200):
    to_delete = []
    async for msg in thread.history(limit=limit, oldest_first=False):
        if msg.id == anchor_id:
            break
        if msg.author == thread.guild.me:
            to_delete.append(msg)
    for msg in to_delete:
        try:
            await msg.delete()
        except Exception:
            pass

async def reissue_receipt_set(thread: discord.Thread, st: Dict[str, Any]):
    """受付票を再発行する（修正確定後）。

    Spec:
      ① 罫線付きの再発行中メッセージを投稿（本文1行）
      ② ①より前の Bot投稿を全削除（※スレッド初期メッセージは残す）
      ③ 新しい受付票セットを投稿（Embed footer に更新日時(JST)を明記）
      ④ ①のメッセージを削除
    """
    reissue_msg = None

    # ①
    try:
        reissue_msg = await thread.send(
            "ーーーーーーーーーーーーーーーーーーーーーー\n"
            "⌛受付票を再発行しています。このまましばらくお待ちください。\n"
            "ーーーーーーーーーーーーーーーーーーーーーー"
        )
    except Exception:
        reissue_msg = None

    # ②
    try:
        pivot_id = int(reissue_msg.id) if reissue_msg else 0
        keep = set()
        intro_id = st.get("intro_msg_id")
        if intro_id:
            try:
                keep.add(int(intro_id))
            except Exception:
                pass
        if pivot_id:
            await _delete_messages_before_pivot(thread, pivot_id, keep_ids=keep, limit=300)
    except Exception:
        pass

    # ③
    try:
        st["receipt_set_msg_ids"] = []
        st.pop("receipt_anchor_msg_id", None)
    except Exception:
        pass

    try:
        st["_receipt_footer_override"] = f"更新日時：{_now_jst_str()}（JST）"
    except Exception:
        pass

    try:
        await post_final_receipt(thread)
    except Exception:
        pass

    try:
        st.pop("_receipt_footer_override", None)
    except Exception:
        pass

    # 編集状態を解除（確定後）
    try:
        st["in_edit"] = False
        st["edit_from_index"] = None
        st["pending_key"] = None
        st["pending_question_msg_id"] = None
    except Exception:
        pass

    # ④
    if reissue_msg:
        try:
            await reissue_msg.delete()
        except Exception:
            pass


async def _delete_messages_before_pivot(thread: discord.Thread, pivot_id: int, *, keep_ids: Optional[set] = None, limit: int = 200):
    """Delete bot messages that are older than the pivot message (exclusive).
    keep_ids: message IDs to preserve (e.g., the thread intro message).
    """
    keep_ids = keep_ids or set()
    to_delete = []
    async for msg in thread.history(limit=limit, oldest_first=True):
        if msg.id == pivot_id:
            break
        if msg.id in keep_ids:
            continue
        try:
            if msg.author == thread.guild.me:
                to_delete.append(msg)
        except Exception:
            continue
    # delete from newest to oldest to reduce NotFound churn
    for msg in reversed(to_delete):
        try:
            await msg.delete()
        except Exception:
            pass


class EditPickSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="EPIC ID", value="epic"),
            discord.SelectOption(label="呼び名", value="callname"),
            discord.SelectOption(label="機種", value="platform"),
            discord.SelectOption(label="XのID", value="xid"),
            discord.SelectOption(label="カスタム権限", value="custom"),
            discord.SelectOption(label="意気込みメッセージ", value="ikigomi"),
        ]
        super().__init__(placeholder="修正したい項目を選択", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        st = await ensure_thread_state(interaction)
        if not st:
            return
        if interaction.user.id != st.get("owner_id"):
            await interaction.response.send_message("この操作は本人のみ実行できます。", ephemeral=True)
            return

        thread = interaction.channel
        if not isinstance(thread, discord.Thread):
            return

        # 既に ensure_thread_state 内で ACK 済みの場合があるため、二重応答を避ける
        await silent_ack(interaction, ephemeral=True)

        key = self.values[0]
        await start_edit_for_key(thread, st, key)

class EditPickView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(EditPickSelect())

class EditConfirmView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="この内容で確定する", style=discord.ButtonStyle.success, custom_id="edit:commit", row=0)
    async def commit(self, interaction: discord.Interaction, button: discord.ui.Button):
        st = await ensure_thread_state(interaction)
        if not st:
            return
        if interaction.user.id != st.get("owner_id"):
            await interaction.response.send_message("この操作は本人のみ実行できます。", ephemeral=True)
            return

        thread = interaction.channel
        if not isinstance(thread, discord.Thread):
            return

        # 既に ensure_thread_state 内で ACK 済みの場合があるため、二重応答を避ける
        await silent_ack(interaction, ephemeral=True)

        # 修正フローを終了
        st["in_edit"] = False
        st["pending_key"] = None
        st["edit_from_index"] = None
        # 修正UI（選択/質問）を掃除
        try:
            await _delete_edit_picker(thread, st)
        except Exception:
            pass

        # 受付完了後：シートへ反映し、受付票セットを更新
        if st.get("status") == STATUS_ACCEPTED:
            # XのID/URLは修正でズレやすいので、確定時に必ず同期してからSSへ反映する
            try:
                a = st.get("answers", {}) or {}
                xid = _normalize_xid(str(a.get("xid", "")).strip())
                if xid:
                    a["xid"] = xid
                    a["xurl"] = f"https://x.com/{xid}"
                else:
                    # X未設定の場合はURLも空にする（SS上書き）
                    a["xurl"] = ""
                st["answers"] = a
            except Exception:
                pass

            try:
                ws = open_worksheet()
                row = st.get("sheet_row")
                if not row:
                    row = _find_row_by_receipt_and_user(ws, int(st.get("receipt_no", 0) or 0), int(st.get("owner_id", 0) or 0))
                    st["sheet_row"] = row
                if row:
                    update_row_answers(ws, int(row), st.get("answers", {}), STATUS_ACCEPTED)
            except Exception:
                pass

            # 既存の受付票セットを作り直す（古いのを消して再投稿）
            try:
                ids = [int(x) for x in (st.get("receipt_set_msg_ids") or []) if str(x).isdigit()]
                for mid in ids:
                    try:
                        msg = await thread.fetch_message(int(mid))
                        await msg.delete()
                    except Exception:
                        pass
                st["receipt_set_msg_ids"] = []
                st.pop("receipt_anchor_msg_id", None)
            except Exception:
                pass

            try:
                await post_final_receipt(thread)
            except Exception:
                pass

            # 修正関連メッセージIDを掃除
            try:
                for k in ("edit_intro_msg_id", "edit_picker_msg_id", "confirm_msg_id", "pending_question_msg_id"):
                    st.pop(k, None)
            except Exception:
                pass

            try:
                await interaction.followup.send("修正内容を確定しました。", ephemeral=True)
            except Exception:
                pass

            # 追加の修正がある場合に備え、項目選択メッセージを再表示
            # 項目選択フェーズへ戻す：確定/中止ボタンを有効にする
            st["pending_key"] = None
            st["pending_question_msg_id"] = None
            st["awaiting_text"] = False
            try:
                await post_confirm(thread)
            except Exception:
                pass

            try:
                m = await thread.send("修正したい項目を選択してください。", view=EditPickView())
                st["edit_picker_msg_id"] = m.id
                st["in_edit"] = True
            except Exception:
                pass
            return

        # エントリー前：通常の確認へ戻す
        try:
            await post_confirm(thread)
        except Exception:
            pass
        try:
            await interaction.followup.send("修正内容を確定しました。続けてエントリーする場合は「✨エントリーする✨」を押してください。", ephemeral=True)
        except Exception:
            pass
        return

    @discord.ui.button(label="すべての修正を中止する", style=discord.ButtonStyle.danger, custom_id="edit:cancel", row=0)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        st = await ensure_thread_state(interaction)
        if not st:
            return
        if interaction.user.id != st.get("owner_id"):
            await interaction.response.send_message("この操作は本人のみ実行できます。", ephemeral=True)
            return

        thread = interaction.channel
        if not isinstance(thread, discord.Thread):
            return

        # 既に ensure_thread_state 内で ACK 済みの場合があるため、二重応答を避ける
        await silent_ack(interaction, ephemeral=True)

        # Stop edit mode and clean up picker UI
        st["in_edit"] = False
        st["edit_from_index"] = None
        await _delete_edit_picker(thread, st)

        # Pre-entry: just refresh the normal confirmation
        if st.get("status") != STATUS_ACCEPTED:
            await post_confirm(thread)
            try:
                await interaction.followup.send("修正を中止しました。続けてエントリーする場合は「✨エントリーする✨」を押してください。", ephemeral=True)
            except Exception:
                pass
            return

        # Post-accept: cancel edit -> delete messages posted by '内容を修正する' and keep the existing receipt set
        try:
            anchor = st.get("receipt_anchor_msg_id")
            if anchor:
                await _delete_messages_after_anchor(thread, int(anchor))
            else:
                mids = []
                for k in ("edit_intro_msg_id", "edit_picker_msg_id", "pending_question_msg_id"):
                    v = st.get(k)
                    if v:
                        mids.append(int(v))
                for mid in mids:
                    try:
                        msg = await thread.fetch_message(int(mid))
                        await msg.delete()
                    except Exception:
                        pass
        except Exception:
            pass

        # Post-accept: when canceling edit, also delete the "🗂登録内容" message that was posted during edit.
        try:
            cmid = st.get("confirm_msg_id")
            if cmid:
                rset = set()
                try:
                    for x in (st.get("receipt_set_msg_ids") or []):
                        if str(x).isdigit():
                            rset.add(int(x))
                except Exception:
                    rset = set()
                if int(cmid) not in rset:
                    try:
                        cmsg = await thread.fetch_message(int(cmid))
                        await cmsg.delete()
                    except Exception:
                        pass
        except Exception:
            pass

        # cleanup flags
        try:
            st.pop("edit_intro_msg_id", None)
            st.pop("edit_picker_msg_id", None)
            st.pop("confirm_msg_id", None)
            st["pending_question_msg_id"] = None
        except Exception:
            pass

        try:
            await interaction.followup.send("修正を中止しました。", ephemeral=True)
        except Exception:
            pass
        return

# =========================
# Cancel / 受付キャンセル
# =========================
async def perform_cancel_entry(thread: discord.Thread, st: Dict[str, Any]):
    """受付キャンセル確定後の処理。"""
    user_id = int(st.get("owner_id", 0))
    receipt_no = int(st.get("receipt_no", 0))
    owner_name = str(st.get("owner_name", ""))

    # ロール外し
    try:
        guild = thread.guild
        if guild and user_id:
            member = guild.get_member(user_id)
            if member:
                role = resolve_entry_accept_role(guild)
                if role:
                    try:
                        await member.remove_roles(role, reason="OR40 entry canceled")
                    except Exception:
                        pass
    except Exception:
        pass

    # シート status 更新
    try:
        ws = open_worksheet()
        row = st.get("sheet_row")
        if not row:
            row = _find_row_by_receipt_and_user(ws, receipt_no, user_id)
            st["sheet_row"] = row
        if row:
            update_row_answers(ws, int(row), st.get("answers", {}), STATUS_CANCELED)
    except Exception:
        pass

    st["status"] = STATUS_CANCELED

    # スレッド名変更
    try:
        await thread.edit(name=format_thread_title(STATUS_CANCELED, receipt_no, owner_name))
    except Exception:
        pass

    # 通知
    try:
        await thread.send("エントリーのキャンセルを承りました。")
        await thread.send("10秒後に、このスレッドは閉じられます。")
    except Exception:
        pass

    await asyncio.sleep(10)

    # 退室
    try:
        if thread.guild and user_id:
            member = thread.guild.get_member(user_id)
            if member:
                await thread.remove_user(member)
    except Exception:
        pass

class CancelConfirmView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=60)

    @discord.ui.button(label="はい", style=discord.ButtonStyle.danger, custom_id="cancel:yes", row=0)
    async def yes(self, interaction: discord.Interaction, button: discord.ui.Button):
        st = await ensure_thread_state(interaction)
        if not st:
            return
        if interaction.user.id != st.get("owner_id"):
            await interaction.response.send_message("この操作は本人のみ実行できます。", ephemeral=True)
            return
        thread = interaction.channel
        if not isinstance(thread, discord.Thread):
            return

        # 既に ensure_thread_state 内で ACK 済みの場合があるため、二重応答を避ける
        await silent_ack(interaction, ephemeral=True)
        try:
            await interaction.message.edit(content="キャンセル処理を実行します。", view=None)
        except Exception:
            pass

        asyncio.create_task(perform_cancel_entry(thread, st))

    @discord.ui.button(label="いいえ", style=discord.ButtonStyle.secondary, custom_id="cancel:no", row=0)
    async def no(self, interaction: discord.Interaction, button: discord.ui.Button):
        st = await ensure_thread_state(interaction)
        if not st:
            return
        if interaction.user.id != st.get("owner_id"):
            await interaction.response.send_message("この操作は本人のみ実行できます。", ephemeral=True)
            return
        # 既に ensure_thread_state 内で ACK 済みの場合があるため、二重応答を避ける
        await silent_ack(interaction, ephemeral=True)
        try:
            await interaction.message.edit(content="キャンセルを中止しました。", view=None)
        except Exception:
            pass

class AfterAcceptView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="運営へ連絡する", style=discord.ButtonStyle.success, custom_id="after:contact_ops", row=0)
    async def contact_ops(self, interaction: discord.Interaction, button: discord.ui.Button):
        st = await ensure_thread_state(interaction)
        if not st:
            return
        if interaction.user.id != st.get("owner_id"):
            await interaction.response.send_message("この操作は本人のみ実行できます。", ephemeral=True)
            return

        thread = interaction.channel
        if not isinstance(thread, discord.Thread):
            await interaction.response.send_message("この操作はスレッド内で実行してください。", ephemeral=True)
            return

        await silent_ack(interaction, ephemeral=True)
        await mark_inquiry_and_notify(thread, st, reason_label="問い合わせ")
        try:
            await interaction.followup.send("運営に通知しました。内容をこのスレッドにそのまま送ってください。", ephemeral=True)
        except Exception:
            pass


    @discord.ui.button(label="内容を修正する", style=discord.ButtonStyle.primary, custom_id="after:edit", row=0)
    async def edit(self, interaction: discord.Interaction, button: discord.ui.Button):
        st = await ensure_thread_state(interaction)
        if not st:
            return
        if interaction.user.id != st.get("owner_id"):
            await interaction.response.send_message("この操作は本人のみ実行できます。", ephemeral=True)
            return

        s = accept_status_text()
        if s not in ("受付中", "受付期間前（動作確認中）"):
            await interaction.response.send_message("修正できるのは受付中/動作確認中のみです。", ephemeral=True)
            return

        thread = interaction.channel
        if not isinstance(thread, discord.Thread):
            return

        # 既に ensure_thread_state 内で ACK 済みの場合があるため、二重応答を避ける
        await silent_ack(interaction, ephemeral=True)

        st["in_edit"] = True
        st["edit_from_index"] = None
        st.setdefault("edited_fields", set())
        st["in_entry"] = True

        # 追加メッセージ（修正開始案内）: 埋め込み(🗂登録内容)より先に表示する
        try:
            m0 = await thread.send("\n".join([
                "ーーーーーー以下、登録内容の修正をしますーーーーーー",
                "項目ごとに選択して修正していただきます。",
                "修正した項目は、🗂登録内容のメッセージ内に「✎」マークがつきます。",
                "また、すべての修正が完了しましたら、この内容で送信ボタンを押してください。",
            ]))
            try:
                st["edit_intro_msg_id"] = int(getattr(m0, "id", 0) or 0)
                if st.get("edit_intro_msg_id"):
                    st.setdefault("flow_msg_ids", []).append(int(st.get("edit_intro_msg_id")))
            except Exception:
                pass
        except Exception:
            pass

        except Exception:
            pass

        await post_confirm(thread)

        try:
            m = await thread.send("修正したい項目を選択してください。", view=EditPickView())
            st["edit_picker_msg_id"] = m.id
        except Exception:
            pass

    @discord.ui.button(label="キャンセル", style=discord.ButtonStyle.danger, custom_id="after:cancel", row=0)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        st = await ensure_thread_state(interaction)
        if not st:
            return
        if interaction.user.id != st.get("owner_id"):
            await interaction.response.send_message("この操作は本人のみ実行できます。", ephemeral=True)
            return

        thread = interaction.channel
        if not isinstance(thread, discord.Thread):
            return

        # 受付完了後のキャンセル確認（スレッド内）
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "本当に受付をキャンセルしますか？",
                    view=CancelConfirmView(),
                    ephemeral=False,
                )
            else:
                await interaction.followup.send(
                    "本当に受付をキャンセルしますか？",
                    view=CancelConfirmView(),
                )
        except Exception:
            try:
                await interaction.followup.send("本当に受付をキャンセルしますか？", view=CancelConfirmView())
            except Exception:
                pass


class ReceiptContactView(discord.ui.View):
    """受付票セット末尾用：運営へ連絡する（通知のみ）"""
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="運営へ連絡する", style=discord.ButtonStyle.secondary, custom_id="receipt:contact_ops", row=0)
    async def contact_ops(self, interaction: discord.Interaction, button: discord.ui.Button):
        st = await ensure_thread_state(interaction)
        if not st:
            return
        if interaction.user.id != st.get("owner_id"):
            await interaction.response.send_message("この操作は本人のみ実行できます。", ephemeral=True)
            return

        thread = interaction.channel
        if not isinstance(thread, discord.Thread):
            await interaction.response.send_message("この操作はスレッド内で実行してください。", ephemeral=True)
            return

        await silent_ack(interaction, ephemeral=True)
        await mark_inquiry_and_notify(thread, st, reason_label="問い合わせ")
        try:
            await interaction.followup.send("運営に通知しました。内容をこのスレッドにそのまま送ってください。", ephemeral=True)
        except Exception:
            pass

class GoLiveDeclView(discord.ui.View):
    """(removed)"""
    def __init__(self):
        super().__init__(timeout=None)


class GoLiveOpsReviewView(discord.ui.View):
    def __init__(self, applicant_id: int):
        super().__init__(timeout=None)
        self.applicant_id = applicant_id

    async def _guard_ops(self, interaction: discord.Interaction) -> bool:
        if not isinstance(interaction.user, discord.Member):
            return False
        if has_ops_role(interaction.user) or is_admin(interaction):
            return True
        await interaction.response.send_message("運営のみ操作できます。", ephemeral=True)
        return False

    async def _post_template(self, interaction: discord.Interaction, kind: str):
        # とりあえず仮文（あとで差し替え）
        if kind == "allow":
            txt = "✅【運営】申告を確認しました。今回は許可します。"
        elif kind == "hearing":
            txt = "🟨【運営】要ヒアリングです。追加で状況を教えてください。"
        else:
            txt = "⛔【運営】一旦中断します。運営から連絡します。"

        # 既に ensure_thread_state 内で ACK 済みの場合があるため、二重応答を避ける
        await silent_ack(interaction, ephemeral=True)
        await interaction.channel.send(txt)

    @discord.ui.button(label="許可", style=discord.ButtonStyle.success, custom_id="ops:allow", row=0)
    async def allow(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._guard_ops(interaction):
            return
        await self._post_template(interaction, "allow")

    @discord.ui.button(label="要ヒアリング", style=discord.ButtonStyle.secondary, custom_id="ops:hearing", row=0)
    async def hearing(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._guard_ops(interaction):
            return
        await self._post_template(interaction, "hearing")

    @discord.ui.button(label="中断", style=discord.ButtonStyle.danger, custom_id="ops:stop", row=0)
    async def stop(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._guard_ops(interaction):
            return
        await self._post_template(interaction, "stop")

# =========================
# 管理パネル：更新系
# =========================
def build_team_embed() -> discord.Embed:
    if is_solo_mode():
        return discord.Embed(
            title="質問項目の設定（チーム）",
            description="ソロのため、この項目は無効です。（大会後に設計）",
            color=COLOR_ADMIN,
        )
    return discord.Embed(
        title="質問項目の設定（チーム）",
        description=f"現在：{team_status_summary()}",
        color=COLOR_ADMIN,
    )

def build_indiv_embed() -> discord.Embed:
    return discord.Embed(
        title="質問項目の設定（個人）",
        description=f"質問の順番（押した順）：\n{indiv_status_summary()}",
        color=COLOR_ADMIN,
    )

async def refresh_all_panels(interaction: discord.Interaction):
    """管理パネル（3投稿）＆受付パネルを全部更新 + チャンネル名同期

    ※管理パネルは「置いたチャンネル」と「各メッセージID」を panel_state.json に保持し、
      interaction.channel が別でも更新できるようにする。
    """
    gid = interaction.guild_id

    # --- admin panel location (persisted) ---
    pl = CONFIG.get("panel_lock") or {}
    admin_ch_id = int(pl.get("admin_channel_id") or 0) if str(pl.get("admin_channel_id") or "").isdigit() else 0

    ch = interaction.channel  # type: ignore
    if admin_ch_id:
        try:
            ch = interaction.client.get_channel(admin_ch_id) or await interaction.client.fetch_channel(admin_ch_id)  # type: ignore
        except Exception:
            ch = interaction.channel  # type: ignore

    def _persist_admin_ids():
        try:
            pl2 = CONFIG.get("panel_lock") or {}
            pl2["admin_channel_id"] = int(getattr(ch, "id", 0) or 0)
            pl2["admin_main_msg_id"] = int(ADMIN_PANEL_MAIN_MSG.get(gid) or 0) or None
            pl2["admin_team_msg_id"] = int(ADMIN_PANEL_TEAM_MSG.get(gid) or 0) or None
            pl2["admin_indiv_msg_id"] = int(ADMIN_PANEL_INDIV_MSG.get(gid) or 0) or None
            CONFIG["panel_lock"] = pl2
            save_config(CONFIG)
        except Exception:
            pass

    # --- 1投稿目 ---
    try:
        mid = ADMIN_PANEL_MAIN_MSG.get(gid) or (int(pl.get("admin_main_msg_id")) if str(pl.get("admin_main_msg_id") or "").isdigit() else None)
        if mid:
            msg = await ch.fetch_message(int(mid))
            await msg.edit(embed=build_panel_embed(), view=AdminPanelMainView())
            ADMIN_PANEL_MAIN_MSG[gid] = int(mid)
        else:
            ADMIN_PANEL_MAIN_MSG.pop(gid, None)
    except Exception:
        ADMIN_PANEL_MAIN_MSG.pop(gid, None)

    # --- 2投稿目 ---
    try:
        tid = ADMIN_PANEL_TEAM_MSG.get(gid) or (int(pl.get("admin_team_msg_id")) if str(pl.get("admin_team_msg_id") or "").isdigit() else None)
        if tid:
            msg2 = await ch.fetch_message(int(tid))
            await msg2.edit(embed=build_team_embed(), view=AdminTeamQuestionsView())
            ADMIN_PANEL_TEAM_MSG[gid] = int(tid)
        else:
            ADMIN_PANEL_TEAM_MSG.pop(gid, None)
    except Exception:
        ADMIN_PANEL_TEAM_MSG.pop(gid, None)

    # --- 3投稿目 ---
    try:
        iid = ADMIN_PANEL_INDIV_MSG.get(gid) or (int(pl.get("admin_indiv_msg_id")) if str(pl.get("admin_indiv_msg_id") or "").isdigit() else None)
        if iid:
            msg3 = await ch.fetch_message(int(iid))
            await msg3.edit(embed=build_indiv_embed(), view=AdminIndivQuestionsView())
            ADMIN_PANEL_INDIV_MSG[gid] = int(iid)
        else:
            ADMIN_PANEL_INDIV_MSG.pop(gid, None)
    except Exception:
        ADMIN_PANEL_INDIV_MSG.pop(gid, None)

    _persist_admin_ids()

    # --- entry panel refresh (channel is fixed) ---
    try:
        await refresh_entry_panel_message(interaction.client, gid)
    except Exception:
        pass

    # --- channel name sync (best-effort) ---
    try:
        await sync_entry_channel_name(interaction.client, gid)
    except Exception:
        pass

# =========================
# 管理パネル：モーダル
# =========================
class TournamentNameModal(discord.ui.Modal, title="大会名の設定"):
    name = discord.ui.TextInput(
        label="大会名",
        required=True,
        placeholder="",
        default="OR40 SOLOリロード",   # ★初期値は空欄（入力内容保持は CONFIG に保存されるためOK）
        max_length=50,
    )

    async def on_submit(self, interaction: discord.Interaction):
        CONFIG["tournament_name"] = str(self.name.value).strip()
        save_config(CONFIG)
        # 既に ensure_thread_state 内で ACK 済みの場合があるため、二重応答を避ける
        await silent_ack(interaction, ephemeral=True)
        await refresh_all_panels(interaction)

class EventDateModal(discord.ui.Modal):
    def __init__(self):
        super().__init__(title="開催日と開始時間の設定")

        y = datetime.now(JST).year  # ★開いた時点の年

        self.event_date = discord.ui.TextInput(
            label="開催日（YYYY/M/D）",
            required=True,
            default=f"{y}/",   # ★ここが「今年/」
            max_length=12,
            placeholder="",    # ★入力例いらない
        )
        self.start_time = discord.ui.TextInput(
            label="開始時間（HH:MM）",
            required=True,
            default="22:00",
            max_length=5,
            placeholder="",    # ★入力例いらない
        )

        self.add_item(self.event_date)
        self.add_item(self.start_time)

    async def on_submit(self, interaction: discord.Interaction):
        d = str(self.event_date.value).strip()
        t = str(self.start_time.value).strip()

        if not re.fullmatch(r"\d{4}/\d{1,2}/\d{1,2}", d):
            await interaction.response.send_message("⚠️ 開催日の形式が不正です。YYYY/M/D", ephemeral=True)
            return
        if not re.fullmatch(r"\d{1,2}:\d{2}", t):
            await interaction.response.send_message("⚠️ 開始時間の形式が不正です。HH:MM", ephemeral=True)
            return

        CONFIG["event_date"] = d
        save_config(CONFIG)
        CONFIG["start_time"] = t
        # 既に ensure_thread_state 内で ACK 済みの場合があるため、二重応答を避ける
        await silent_ack(interaction, ephemeral=True)
        await refresh_all_panels(interaction)


class MatchesModal(discord.ui.Modal, title="試合数の設定"):
    matches = discord.ui.TextInput(
        label="試合数（数字）",
        required=True,
        placeholder="例：4",
        max_length=3,
        default=str(CONFIG.get("matches_count", 4)),
    )

    async def on_submit(self, interaction: discord.Interaction):
        raw = str(self.matches.value).strip()
        if not raw.isdigit():
            await interaction.response.send_message("⚠️ 試合数は数字で入力してください。", ephemeral=True)
            return
        v = int(raw)
        if v <= 0 or v > 99:
            await interaction.response.send_message("⚠️ 試合数は 1〜99 の範囲にしてください。", ephemeral=True)
            return
        CONFIG["matches_count"] = v
        save_config(CONFIG)
        # 既に ensure_thread_state 内で ACK 済みの場合があるため、二重応答を避ける
        await silent_ack(interaction, ephemeral=True)
        await refresh_all_panels(interaction)

class CapacityModal(discord.ui.Modal, title="定員の設定"):
    capacity = discord.ui.TextInput(
        label="定員（数字）",
        required=True,
        placeholder="例：38",
        max_length=4,
        default=str(CONFIG.get("capacity", 38)),
    )

    async def on_submit(self, interaction: discord.Interaction):
        raw = str(self.capacity.value).strip()
        if not raw.isdigit():
            await interaction.response.send_message("⚠️ 定員は数字で入力してください。", ephemeral=True)
            return
        v = int(raw)
        if v <= 0 or v > 9999:
            await interaction.response.send_message("⚠️ 定員は 1〜9999 の範囲にしてください。", ephemeral=True)
            return
        CONFIG["capacity"] = v
        save_config(CONFIG)
        # 既に ensure_thread_state 内で ACK 済みの場合があるため、二重応答を避ける
        await silent_ack(interaction, ephemeral=True)
        await refresh_all_panels(interaction)

class PeriodModal(discord.ui.Modal):
    def __init__(self):
        super().__init__(title="エントリー受付期間の設定")

        y = datetime.now(JST).year  # ★開いた時点の年

        # 既存値があればそれを初期表示（毎回ブランクになるのを防ぐ）
        ps = str(CONFIG.get("period_start", "")).strip()
        pe = str(CONFIG.get("period_end", "")).strip()
        default_start = ps if ps else f"{y}/"
        default_end = pe if pe else f"{y}/"

        self.start = discord.ui.TextInput(
            label="開始日（YYYY/M/D）",
            required=True,
            default=default_start,
            max_length=12,
            placeholder="",
        )
        self.end = discord.ui.TextInput(
            label="終了日（YYYY/M/D）",
            required=True,
            default=default_end,
            max_length=12,
            placeholder="",
        )

        self.add_item(self.start)
        self.add_item(self.end)

    async def on_submit(self, interaction: discord.Interaction):
        s = str(self.start.value).strip()
        e = str(self.end.value).strip()

        try:
            sd = _parse_ymd(s)
            ed = _parse_ymd(e)
            if ed < sd:
                await interaction.response.send_message("⚠️ 終了日は開始日より後にしてください。", ephemeral=True)
                return
        except Exception as ex:
            await interaction.response.send_message(f"⚠️ 日付の形式が不正です：{ex}", ephemeral=True)
            return

        CONFIG["period_start"] = s
        CONFIG["period_end"] = e
        save_config(CONFIG)
        # 既に ensure_thread_state 内で ACK 済みの場合があるため、二重応答を避ける
        await silent_ack(interaction, ephemeral=True)
        await refresh_all_panels(interaction)

# =========================
# 管理パネル Views
# =========================
class AdminPanelMainView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        # ボタン側は短く（入力内容は反映させない方針）
        # 表記はあなたが変えたやつに合わせる
        for child in self.children:
            if isinstance(child, discord.ui.Button) and child.custom_id == "admin:t:matches":
                child.label = f"試合数：{int(CONFIG.get('matches_count',4))}"
            if isinstance(child, discord.ui.Button) and child.custom_id == "admin:t:capacity":
                child.label = f"定員：{int(CONFIG.get('capacity',38))}"
            if isinstance(child, discord.ui.Button) and child.custom_id == "admin:toggle_status":
                cur = accept_status_text()
                child.label = f"ステータス切替：{cur}" if cur else "ステータス切替：未設定"
            if isinstance(child, discord.ui.Button) and child.custom_id == "admin:t:ikigomi":
                child.label = f"意気込み：{'ON' if CONFIG.get('need_ikigomi', True) else 'OFF'}"


    async def _guard_admin(self, interaction: discord.Interaction) -> bool:
        if not is_admin(interaction):
            await interaction.response.send_message("権限がありません。", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="受付パネルを設置する", style=discord.ButtonStyle.success, custom_id="admin:post_entry_panel", row=0)
    async def post_entry_panel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._guard_admin(interaction):
            return
        ch = interaction.client.get_channel(ENTRY_CHANNEL_ID) or await interaction.client.fetch_channel(ENTRY_CHANNEL_ID)
        msg = await ch.send(embed=build_panel_embed(), view=EntryPanelView())
        ENTRY_PANEL_MSG[interaction.guild_id] = msg.id
        # 大会ID（内部用）を受付パネル設置時に新規発行
        CONFIG["tournament_id"] = generate_tournament_id()
        try:
            pl = CONFIG.get('panel_lock') or {}
            pl['is_posted'] = True
            pl['entry_panel_msg_id'] = int(msg.id)
            CONFIG['panel_lock'] = pl
            save_config(CONFIG)
        except Exception:
            pass
        # 既に ensure_thread_state 内で ACK 済みの場合があるため、二重応答を避ける
        await silent_ack(interaction, ephemeral=True)
        await sync_entry_channel_name(interaction.client, interaction.guild_id)

    @discord.ui.button(label="現在の受付パネルを削除する", style=discord.ButtonStyle.danger, custom_id="admin:delete_entry_panel", row=0)
    async def delete_entry_panel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._guard_admin(interaction):
            return
        gid = interaction.guild_id
        mid = ENTRY_PANEL_MSG.get(gid)
        if not mid:
            await interaction.response.send_message("削除対象の受付パネルが見つかりません。", ephemeral=True)
            return
        ch = interaction.client.get_channel(ENTRY_CHANNEL_ID) or await interaction.client.fetch_channel(ENTRY_CHANNEL_ID)
        try:
            m = await ch.fetch_message(mid)
            await m.delete()
        except Exception:
            pass
        ENTRY_PANEL_MSG.pop(gid, None)
        await interaction.response.send_message("現在のパネルを削除しました", ephemeral=True)
        try:
            pl = CONFIG.get('panel_lock') or {}
            pl['is_posted'] = False
            pl['entry_panel_msg_id'] = None
            CONFIG['panel_lock'] = pl
            save_config(CONFIG)
        except Exception:
            pass  # ★成功時は残す
        await sync_entry_channel_name(interaction.client, interaction.guild_id)

    @discord.ui.button(label="ステータス切替", style=discord.ButtonStyle.secondary, custom_id="admin:toggle_status", row=0)
    async def toggle_status(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._guard_admin(interaction):
            return

        if accept_status_text() == "受付期間未設定":
            await interaction.response.send_message("⚠️ 受付期間が未設定です。先に『エントリー受付期間』を設定してください。", ephemeral=True)
            return

        ph = current_phase()
        st = CONFIG.get("status_toggle") or {"pre": False, "open": False, "post": False}
        st[ph] = not bool(st.get(ph, False))
        CONFIG["status_toggle"] = st

        # 既に ensure_thread_state 内で ACK 済みの場合があるため、二重応答を避ける
        await silent_ack(interaction, ephemeral=True)
        await refresh_all_panels(interaction)

    @discord.ui.button(label="大会名", style=discord.ButtonStyle.secondary, custom_id="admin:t:tournament_name", row=1)
    async def t_tournament_name(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._guard_admin(interaction):
            return
        await interaction.response.send_modal(TournamentNameModal())

    @discord.ui.button(label="開催日/開始時間", style=discord.ButtonStyle.secondary, custom_id="admin:t:event", row=1)
    async def t_event(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._guard_admin(interaction):
            return
        await interaction.response.send_modal(EventDateModal())

    @discord.ui.button(label="モード（種類）", style=discord.ButtonStyle.secondary, custom_id="admin:t:mode_type", row=2)
    async def t_mode_type(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._guard_admin(interaction):
            return
        order = ["通常", "トーナメントセッティング", "リロード"]
        cur = str(CONFIG.get("mode_type", order[2]))
        CONFIG["mode_type"] = order[(order.index(cur) + 1) % len(order)] if cur in order else order[2]
        # 既に ensure_thread_state 内で ACK 済みの場合があるため、二重応答を避ける
        await silent_ack(interaction, ephemeral=True)
        await refresh_all_panels(interaction)

    @discord.ui.button(label="モード（人数）", style=discord.ButtonStyle.secondary, custom_id="admin:t:mode_people", row=2)
    async def t_mode_people(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._guard_admin(interaction):
            return
        order = ["ソロ", "デュオ", "トリオ", "スクワッド"]
        cur = str(CONFIG.get("mode_people", order[0]))
        CONFIG["mode_people"] = order[(order.index(cur) + 1) % len(order)] if cur in order else order[0]
        # 既に ensure_thread_state 内で ACK 済みの場合があるため、二重応答を避ける
        await silent_ack(interaction, ephemeral=True)
        await refresh_all_panels(interaction)

    @discord.ui.button(label="試合数：4", style=discord.ButtonStyle.secondary, custom_id="admin:t:matches", row=3)
    async def t_matches(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._guard_admin(interaction):
            return
        await interaction.response.send_modal(MatchesModal())

    @discord.ui.button(label="定員：38", style=discord.ButtonStyle.secondary, custom_id="admin:t:capacity", row=3)
    async def t_capacity(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._guard_admin(interaction):
            return
        await interaction.response.send_modal(CapacityModal())

    @discord.ui.button(label="エントリー受付期間", style=discord.ButtonStyle.secondary, custom_id="admin:t:period", row=4)
    async def t_period(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._guard_admin(interaction):
            return
        await interaction.response.send_modal(PeriodModal())

    @discord.ui.button(label="意気込み：ON/OFF", style=discord.ButtonStyle.secondary, custom_id="admin:t:ikigomi", row=4)
    async def t_ikigomi(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._guard_admin(interaction):
            return
        CONFIG["need_ikigomi"] = not bool(CONFIG.get("need_ikigomi", True))
        # indiv_order から ikigomi を外す/戻す（見た目整合）
        order = list(CONFIG.get("indiv_order") or [])
        if not CONFIG["need_ikigomi"]:
            if "ikigomi" in order:
                order.remove("ikigomi")
        else:
            if "ikigomi" not in order:
                order.append("ikigomi")
        CONFIG["indiv_order"] = order
        # 既に ensure_thread_state 内で ACK 済みの場合があるため、二重応答を避ける
        await silent_ack(interaction, ephemeral=True)
        await refresh_all_panels(interaction)

class AdminTeamQuestionsView(discord.ui.View):
    """2投稿目：チーム質問（ソロ時は無効）"""
    def __init__(self):
        super().__init__(timeout=None)

        if is_solo_mode():
            for child in self.children:
                if isinstance(child, discord.ui.Button):
                    child.disabled = True
            return

        tq = CONFIG.get("team_questions") or {}
        reg = tq.get("register_mode", "off")
        reserve = bool(tq.get("reserve", False))

        for child in self.children:
            if isinstance(child, discord.ui.Button):
                if child.custom_id == "admin:team:immediate":
                    child.label = f"{'✅' if reg == 'immediate' else ''}{TEAM_LABELS['immediate']}"
                elif child.custom_id == "admin:team:later":
                    child.label = f"{'✅' if reg == 'later' else ''}{TEAM_LABELS['later']}"
                elif child.custom_id == "admin:team:reserve":
                    child.label = f"{'✅' if reserve else ''}{TEAM_LABELS['reserve']}"

    async def _guard_admin(self, interaction: discord.Interaction) -> bool:
        if not is_admin(interaction):
            await interaction.response.send_message("権限がありません。", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="チーム登録：即時", style=discord.ButtonStyle.secondary, custom_id="admin:team:immediate", row=0)
    async def team_immediate(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._guard_admin(interaction):
            return
        if is_solo_mode():
            await interaction.response.send_message("ソロのため無効です。", ephemeral=True)
            return

        tq = CONFIG.get("team_questions") or {"register_mode": "off", "reserve": False}
        cur = tq.get("register_mode", "off")
        tq["register_mode"] = "off" if cur == "immediate" else "immediate"
        CONFIG["team_questions"] = tq
        # 既に ensure_thread_state 内で ACK 済みの場合があるため、二重応答を避ける
        await silent_ack(interaction, ephemeral=True)
        await refresh_all_panels(interaction)

    @discord.ui.button(label="チーム登録：後日", style=discord.ButtonStyle.secondary, custom_id="admin:team:later", row=0)
    async def team_later(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._guard_admin(interaction):
            return
        if is_solo_mode():
            await interaction.response.send_message("ソロのため無効です。", ephemeral=True)
            return

        tq = CONFIG.get("team_questions") or {"register_mode": "off", "reserve": False}
        cur = tq.get("register_mode", "off")
        tq["register_mode"] = "off" if cur == "later" else "later"
        CONFIG["team_questions"] = tq
        # 既に ensure_thread_state 内で ACK 済みの場合があるため、二重応答を避ける
        await silent_ack(interaction, ephemeral=True)
        await refresh_all_panels(interaction)

    @discord.ui.button(label="リザーブ登録", style=discord.ButtonStyle.secondary, custom_id="admin:team:reserve", row=0)
    async def team_reserve(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._guard_admin(interaction):
            return
        if is_solo_mode():
            await interaction.response.send_message("ソロのため無効です。", ephemeral=True)
            return

        tq = CONFIG.get("team_questions") or {"register_mode": "off", "reserve": False}
        tq["reserve"] = not bool(tq.get("reserve", False))
        CONFIG["team_questions"] = tq
        # 既に ensure_thread_state 内で ACK 済みの場合があるため、二重応答を避ける
        await silent_ack(interaction, ephemeral=True)
        await refresh_all_panels(interaction)

class AdminIndivQuestionsView(discord.ui.View):
    """3投稿目：個人質問（押した順で番号付与）"""
    def __init__(self):
        super().__init__(timeout=None)
        order: List[str] = list(CONFIG.get("indiv_order") or [])
        pos = {k: i + 1 for i, k in enumerate(order)}

        def label_for(key: str) -> str:
            if key in pos:
                return f"[{pos[key]}]{INDIV_LABELS[key]}"
            return INDIV_LABELS[key]

        for child in self.children:
            if isinstance(child, discord.ui.Button):
                if child.custom_id == "admin:indiv:epic":
                    child.label = label_for("epic")
                elif child.custom_id == "admin:indiv:callname":
                    child.label = label_for("callname")
                elif child.custom_id == "admin:indiv:platform":
                    child.label = label_for("platform")
                elif child.custom_id == "admin:indiv:xid":
                    child.label = label_for("xid")
                elif child.custom_id == "admin:indiv:custom":
                    child.label = label_for("custom")
                elif child.custom_id == "admin:indiv:ikigomi":
                    child.label = label_for("ikigomi")

    async def _guard_admin(self, interaction: discord.Interaction) -> bool:
        if not is_admin(interaction):
            await interaction.response.send_message("権限がありません。", ephemeral=True)
            return False
        return True

    async def _toggle(self, interaction: discord.Interaction, key: str):
        if not await self._guard_admin(interaction):
            return

        order: List[str] = list(CONFIG.get("indiv_order") or [])
        if key in order:
            order.remove(key)
        else:
            order.append(key)

        CONFIG["indiv_order"] = order
        # 既に ensure_thread_state 内で ACK 済みの場合があるため、二重応答を避ける
        await silent_ack(interaction, ephemeral=True)
        await refresh_all_panels(interaction)

    @discord.ui.button(label="EPIC ID", style=discord.ButtonStyle.secondary, custom_id="admin:indiv:epic", row=0)
    async def indiv_epic(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._toggle(interaction, "epic")

    @discord.ui.button(label="呼び名", style=discord.ButtonStyle.secondary, custom_id="admin:indiv:callname", row=0)
    async def indiv_callname(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._toggle(interaction, "callname")

    @discord.ui.button(label="機種", style=discord.ButtonStyle.secondary, custom_id="admin:indiv:platform", row=0)
    async def indiv_platform(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._toggle(interaction, "platform")

    @discord.ui.button(label="XのID", style=discord.ButtonStyle.secondary, custom_id="admin:indiv:xid", row=1)
    async def indiv_xid(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._toggle(interaction, "xid")

    @discord.ui.button(label="カスタム権限", style=discord.ButtonStyle.secondary, custom_id="admin:indiv:custom", row=1)
    async def indiv_custom(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._toggle(interaction, "custom")

    @discord.ui.button(label="意気込み", style=discord.ButtonStyle.secondary, custom_id="admin:indiv:ikigomi", row=1)
    async def indiv_ikigomi(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._toggle(interaction, "ikigomi")


class PlatformSelectEditView(PlatformSelectView):
    """編集モード用：『この項目の修正をやめる』付き"""
    @discord.ui.button(label="この項目の修正をやめる", style=discord.ButtonStyle.danger, custom_id="edit:platform_cancel", row=3)
    async def cancel_item(self, interaction: discord.Interaction, button: discord.ui.Button):
        st = await ensure_thread_state(interaction)
        if not st:
            return
        if interaction.user.id != st.get("owner_id"):
            await interaction.response.send_message("この操作は本人のみ実行できます。", ephemeral=True)
            return
        # 既に ensure_thread_state 内で ACK 済みの場合があるため、二重応答を避ける
        await silent_ack(interaction, ephemeral=True)
        if isinstance(interaction.channel, discord.Thread):
            await cancel_current_edit_item(interaction.channel, st)

class CustomSelectEditView(CustomSelectView):
    """編集モード用：『この項目の修正をやめる』付き"""
    @discord.ui.button(label="この項目の修正をやめる", style=discord.ButtonStyle.danger, custom_id="edit:custom_cancel", row=3)
    async def cancel_item(self, interaction: discord.Interaction, button: discord.ui.Button):
        st = await ensure_thread_state(interaction)
        if not st:
            return
        if interaction.user.id != st.get("owner_id"):
            await interaction.response.send_message("この操作は本人のみ実行できます。", ephemeral=True)
            return
        # 既に ensure_thread_state 内で ACK 済みの場合があるため、二重応答を避ける
        await silent_ack(interaction, ephemeral=True)
        if isinstance(interaction.channel, discord.Thread):
            await cancel_current_edit_item(interaction.channel, st)


# =========================
# Entry thread helper panel (admin post)
# =========================
HELPER_PANEL_TEXT = (
    "ーーーーーーーーーーーーーー\n"
    "📌スレッドが見えない場合･･･ \n"
    "ーーーーーーーーーーーーーー\n"
    "一定期間操作がないと、スレッドは一覧から非表示になることがあります。 \n"
    "下のボタンから、あなた専用エントリースレッドを開けます。"
)

class EntryThreadHelperView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="📋 自分のエントリースレッドを開く",
        style=discord.ButtonStyle.primary,
        custom_id="helper:open_my_entry",
        row=0,
    )
    async def open_my_entry(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await silent_ack(interaction, ephemeral=True)
        except Exception:
            pass

        uid = interaction.user.id

        # Prefer panel_state.json active_threads mapping
        tid = None
        try:
            tid = get_active_thread_id_for_user(uid)
        except Exception:
            tid = None

        # Backward compatibility (legacy CONFIG['threads'])
        if not tid:
            try:
                legacy = CONFIG.get("threads")
                if isinstance(legacy, dict):
                    v = str(legacy.get(str(uid), "")).strip()
                    if v.isdigit():
                        tid = int(v)
            except Exception:
                tid = None

        thread = None
        if tid:
            try:
                thread = interaction.client.get_channel(int(tid))
                if thread is None:
                    thread = await interaction.client.fetch_channel(int(tid))
            except Exception:
                thread = None

        if isinstance(thread, discord.Thread):
            try:
                await interaction.followup.send(f"🔗 {thread.mention}", ephemeral=True)
            except Exception:
                pass
        else:
            try:
                await interaction.followup.send(
                    "❌ あなたのエントリースレッドが見つかりませんでした。\n"
                    "まだエントリーしていない場合は、受付パネルからエントリーしてください。",
                    ephemeral=True
                )
            except Exception:
                pass


@app_commands.command(
    name="post_entry_thread_helper",
    description="エントリースレッド案内（救済パネル）をこのチャンネルに投稿します",
)
@app_commands.checks.has_permissions(administrator=True)
async def post_entry_thread_helper(interaction: discord.Interaction):
    try:
        await silent_ack(interaction, ephemeral=True)
    except Exception:
        pass

    try:
        await interaction.channel.send(HELPER_PANEL_TEXT, view=EntryThreadHelperView())
    except Exception:
        pass

    try:
        await interaction.followup.send("設置しました。", ephemeral=True)
    except Exception:
        pass


# =========================
# Bot
# =========================
class AdminOR40Bot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True
        intents.message_content = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        # persistent views
        self.add_view(EntryPanelView())
        self.add_view(AdminPanelMainView())
        self.add_view(AdminTeamQuestionsView())
        self.add_view(AdminIndivQuestionsView())
        self.add_view(ThreadEntryLoopView())
        self.add_view(ConfirmView())
        self.add_view(AfterAcceptView())
        self.add_view(ReceiptContactView())
        self.add_view(OpsStatusView())
        # EditPickView は Select を含むので timeout 付き生成が必要。ここでは persistent 不要（ephemeralで都度生成）
        try:
            self.add_view(EntryThreadHelperView())
        except Exception:
            pass
        try:
            self.tree.add_command(post_entry_thread_helper)
        except Exception:
            pass
        await self.tree.sync()

    async def on_ready(self):
        run_log(f"Logged in as {self.user}")

        # ---- restore & refresh persistent messages after restart ----
        try:
            # reload persisted config (in case file changed while offline)
            global CONFIG
            CONFIG = load_config(CONFIG)
        except Exception:
            pass

        # Restore entry panel message id from persistence, then refresh embeds/views
        try:
            pl = CONFIG.get('panel_lock') or {}
            mid = pl.get('entry_panel_msg_id')
            if str(mid).isdigit():
                for g in list(self.guilds):
                    ENTRY_PANEL_MSG[g.id] = int(mid)
        except Exception:
            pass

        # Restore admin panel message ids from persistence (best-effort)
        try:
            pl = CONFIG.get('panel_lock') or {}
            main_id = pl.get('admin_main_msg_id')
            team_id = pl.get('admin_team_msg_id')
            indiv_id = pl.get('admin_indiv_msg_id')
            for g in list(self.guilds):
                if str(main_id).isdigit():
                    ADMIN_PANEL_MAIN_MSG[g.id] = int(main_id)
                if str(team_id).isdigit():
                    ADMIN_PANEL_TEAM_MSG[g.id] = int(team_id)
                if str(indiv_id).isdigit():
                    ADMIN_PANEL_INDIV_MSG[g.id] = int(indiv_id)
        except Exception:
            pass

# Refresh entry panel + channel name (best-effort)
        try:
            for g in list(self.guilds):
                await refresh_entry_panel_message(self, g.id)
                await sync_entry_channel_name(self, g.id)
        except Exception:
            pass

        # Refresh ops forum status control messages (so buttons remain live after restart)
        try:
            smap = _ops_status_msg_map()
            for forum_tid_s, msg_id in list(smap.items()):
                if not str(forum_tid_s).isdigit() or not str(msg_id).isdigit():
                    continue
                forum_tid = int(forum_tid_s)
                try:
                    ch = self.get_channel(forum_tid)
                    if ch is None:
                        ch = await self.fetch_channel(forum_tid)
                except Exception:
                    ch = None
                if not isinstance(ch, discord.Thread):
                    continue
                try:
                    m = await ch.fetch_message(int(msg_id))
                except Exception:
                    continue
                try:
                    v = OpsStatusView()
                    v._apply_state(ch.id)
                    await m.edit(view=v)
                except Exception:
                    pass
        except Exception:
            pass

    async def on_message(self, message: discord.Message):
        # 参加者入力（通常メッセで回答） + GoLive添付検知
        if message.author.bot:
            return
        if not isinstance(message.channel, discord.Thread):
            return

        # ---- 運営回答転記（/entry_answer 後の次の1通） ----
        try:
            mode_map = CONFIG.get("entry_answer_mode")
            mode = mode_map.get(str(message.author.id)) if isinstance(mode_map, dict) else None
            if isinstance(mode, dict) and int(mode.get("forum_thread_id", 0) or 0) == int(message.channel.id):
                # 念のため運営限定（二重チェック）
                member = None
                if message.guild:
                    try:
                        member = message.guild.get_member(message.author.id)
                        if member is None:
                            member = await message.guild.fetch_member(message.author.id)
                    except Exception:
                        member = None

                if isinstance(member, discord.Member) and (has_ops_role(member) or member.guild_permissions.administrator):
                    tgt_id = int(mode.get("target_thread_id", 0) or 0)
                    tgt = self.get_channel(tgt_id)
                    if tgt is None:
                        try:
                            tgt = await self.fetch_channel(tgt_id)
                        except Exception:
                            tgt = None

                    if isinstance(tgt, discord.Thread):
                        body = (message.content or "").strip()

                        # 添付がある場合はURLも転記（画像/動画など）
                        urls = []
                        for a in (message.attachments or []):
                            try:
                                if getattr(a, "url", None):
                                    urls.append(str(a.url))
                            except Exception:
                                pass
                        if urls:
                            body = (body + ("\n" if body else "") + "\n".join(urls)).strip()

                        if not body:
                            body = "（内容なし）"

                        await tgt.send("【運営回答】\n" + body)

                        try:
                            await message.add_reaction("✅")
                        except Exception:
                            pass
                    else:
                        try:
                            await message.reply("❌転記先のエントリースレッドが見つかりません。", mention_author=False)
                        except Exception:
                            pass
                else:
                    try:
                        await message.reply("運営のみ使用できます。", mention_author=False)
                    except Exception:
                        pass

                # 1回きり：必ず解除
                try:
                    if isinstance(mode_map, dict):
                        mode_map.pop(str(message.author.id), None)
                        CONFIG["entry_answer_mode"] = mode_map
                        save_config(CONFIG)
                except Exception:
                    pass
                return
        except Exception:
            pass

        st = THREAD_STATE.get(message.channel.id)
        if not st:
            return
    
        # ---- 質問回答（通常入力） ----
        if message.author.id == st.get("owner_id") and st.get("in_entry") and st.get("awaiting_text"):
            key = st.get("pending_key")
            if key in ("epic", "callname", "xid", "ikigomi"):
                raw = (message.content or "").strip()
                # 入力なしはエラー（進めない）
                if not raw:
                    try:
                        await message.reply(
                            "❌ 未記入のまま送信はできません。\n"
                            "意気込みがない場合は「なし」と入力しておいてください。（処理の都合上）",
                            mention_author=False,
                        )
                    except Exception:
                        try:
                            await message.channel.send(
                                "❌ 未記入のまま送信はできません。\n"
                                "意気込みがない場合は「なし」と入力しておいてください。（処理の都合上）"
                            )
                        except Exception:
                            pass
                    # 空白だけのメッセージはログを汚すので削除（権限があれば）
                    try:
                        await message.delete()
                    except Exception:
                        pass
                    return

    
                # バリデーション
                if key == "epic" and str(st.get("answers", {}).get("platform", "")).strip() == "PS":
                    if not _valid_psn_name(raw):
                        # エラー表示（すぐ消す）
                        try:
                            err = await message.channel.send("❌PSNを入力してください。")
                            await message.delete()
                            await asyncio.sleep(3)
                            await err.delete()
                        except Exception:
                            pass
                        return
    
                if key == "xid":
                    xid = _normalize_xid(raw)
                    if not _valid_xid(xid):
                        try:
                            err = await message.channel.send("・英数字、アンダーバー（_)のみ")
                            await message.delete()
                            await asyncio.sleep(3)
                            await err.delete()
                        except Exception:
                            pass
                        return
                    raw = xid  # 正規化して保存
    
                # 保存
                st.setdefault("answers", {})[key] = raw

                if st.get("in_edit"):
                    st.setdefault("edited_fields", set()).add(str(key))
                    st["has_modified"] = True

                # PS注意書きは回答後に削除（ログを質問+回答のみにする）
                if key == "epic":
                    try:
                        nid = st.get("ps_note_msg_id")
                        if nid:
                            nmsg = await message.channel.fetch_message(int(nid))
                            await nmsg.delete()
                    except Exception:
                        pass
                    st["ps_note_msg_id"] = None

    
                # 質問と回答を削除
                try:
                    await message.delete()
                except Exception:
                    pass
                try:
                    qid = st.get("pending_question_msg_id")
                    if qid:
                        qmsg = await message.channel.fetch_message(int(qid))
                        await qmsg.delete()
                except Exception:
                    pass
    
                st["pending_question_msg_id"] = None
                st["awaiting_text"] = False
    
                # まとめ投稿
                await _post_summary(message.channel, st, key)
    
                # 次へ
                if st.get("in_edit") and isinstance(message.channel, discord.Thread):
                    await _return_to_edit_picker(message.channel, st)
                else:
                    await ask_next_question(message.channel)
                return
    
        # ---- GoLiveスクショ検知：本人が添付を投げたら運営ボタン出す ----
        if not st.get("golive_waiting"):
            return
    
        # 本人のみ
        if message.author.id != st.get("owner_id"):
            return
    
        # 添付必須
        if not message.attachments:
            return
    
        st["golive_waiting"] = False
    
        # 運営にメンション（可能なら）
        try:
            guild = message.guild
            if guild:
                ops_role = guild.get_role(OPS_ROLE_ID)
                ops_mention = ops_role.mention if ops_role else "@運営"
            else:
                ops_mention = "@運営"
        except Exception:
            ops_mention = "@運営"
    
        try:
            await message.reply(f"{ops_mention}\nスクショを受領しました。運営の確認をお待ちください。", mention_author=False)
        except Exception:
            pass
    
        # 運営用ボタンを表示（Botメッセに）
        try:
            await message.channel.send(
                "【運営用】対応を選択してください。",
                view=GoLiveOpsReviewView(applicant_id=message.author.id)
            )
        except Exception:
            pass

client = AdminOR40Bot()


# =========================
# Thread listing helpers
# =========================
def _is_entry_thread_name(name: str) -> bool:
    name = str(name or "")
    return ("P-No." in name) or ("E-No." in name) or ("仮No." in name) or ("受理No." in name) or name.startswith("entry") or ("🟨記入中" in name) or ("🟦受付完了" in name) or ("🟥キャンセル" in name)

async def fetch_entry_threads(parent: discord.TextChannel, *, limit: int = 50) -> List[discord.Thread]:
    """
    THREAD_PARENT_CHANNEL_ID 配下のスレッド（アクティブ＋アーカイブ）をできる限り拾う。
    private thread は「Botが参加しているもの」しか取得できない（Discord仕様）。
    """
    threads: List[discord.Thread] = []

    # active threads
    try:
        for th in getattr(parent, "threads", []) or []:
            if isinstance(th, discord.Thread) and _is_entry_thread_name(th.name):
                threads.append(th)
    except Exception:
        pass

    # archived threads (best-effort: discord.py versions differ)
    async def _extend_from_async_iter(ait):
        nonlocal threads
        try:
            async for th in ait:
                if isinstance(th, discord.Thread) and _is_entry_thread_name(th.name):
                    threads.append(th)
                if len(threads) >= limit:
                    break
        except Exception:
            pass

    # Try common APIs
    try:
        # discord.py 2.x: archived_threads(private=..., limit=...)
        try:
            ait = parent.archived_threads(limit=limit, private=True)  # type: ignore
            await _extend_from_async_iter(ait)
        except TypeError:
            ait = parent.archived_threads(limit=limit)  # type: ignore
            await _extend_from_async_iter(ait)
    except Exception:
        pass

    try:
        # some forks: archived_private_threads / archived_public_threads
        if hasattr(parent, "archived_private_threads"):
            ait = parent.archived_private_threads(limit=limit)  # type: ignore
            await _extend_from_async_iter(ait)
    except Exception:
        pass

    # de-dup by id
    uniq: Dict[int, discord.Thread] = {}
    for th in threads:
        try:
            uniq[int(th.id)] = th
        except Exception:
            continue
    threads = list(uniq.values())

    # sort: created_at ascending (thread creation order)
    def _key_created(th: discord.Thread):
        try:
            return th.created_at or datetime.min.replace(tzinfo=timezone.utc)
        except Exception:
            return datetime.min.replace(tzinfo=timezone.utc)
    threads.sort(key=_key_created, reverse=False)
    return threads[:limit]

def _chunk_lines(lines: List[str], *, max_chars: int = 1800) -> List[str]:
    out: List[str] = []
    buf = ""
    for ln in lines:
        if not buf:
            buf = ln
            continue
        if len(buf) + 1 + len(ln) > max_chars:
            out.append(buf)
            buf = ln
        else:
            buf += "\n" + ln
    if buf:
        out.append(buf)
    return out

# =========================
# Commands
# =========================


@client.tree.command(name="entry_answer", description="運営フォーラムで回答モードに切り替えます（次の1通をエントリースレッドへ転記）")
async def entry_answer(interaction: discord.Interaction):
    # 運営のみ
    m = interaction.user
    if not isinstance(m, discord.Member):
        await interaction.response.send_message("権限判定に失敗しました。", ephemeral=True)
        return
    if not has_ops_role(m) and not m.guild_permissions.administrator:
        await interaction.response.send_message("運営のみ実行できます。", ephemeral=True)
        return

    # フォーラムスレッド内のみ
    ch = interaction.channel
    if not isinstance(ch, discord.Thread):
        await interaction.response.send_message("このコマンドは運営フォーラムのスレッド内で実行してください。", ephemeral=True)
        return

    forum_thread_id = int(ch.id)
    pvt_id = int(_ops_links().get(str(forum_thread_id), 0) or 0)
    if not pvt_id:
        await interaction.response.send_message("紐付けが見つかりません。通知スレッド（運営フォーラム）から実行してください。", ephemeral=True)
        return

    # 返信先（エントリースレ）を確認
    target = interaction.client.get_channel(pvt_id)
    if target is None:
        try:
            target = await interaction.client.fetch_channel(pvt_id)
        except Exception:
            target = None
    if not isinstance(target, discord.Thread):
        await interaction.response.send_message("エントリースレッドが見つかりません。運営に問い合わせてください。", ephemeral=True)
        return

    # 回答モード（次の1通だけ）をセット
    try:
        d = CONFIG.get("entry_answer_mode")
        if not isinstance(d, dict):
            d = {}
            CONFIG["entry_answer_mode"] = d
        d[str(m.id)] = {
            "forum_thread_id": int(forum_thread_id),
            "target_thread_id": int(target.id),
            "set_at": _now_jst_str(),
        }
        save_config(CONFIG)
    except Exception:
        pass

    await interaction.response.send_message(
        "✅回答モードにしました。**このあと送信する次の1通**が、回答としてエントリースレッドに転記されます。",
        ephemeral=True
    )

@client.tree.command(name="panel", description="管理パネルを設置します（管理者のみ）")
async def panel(interaction: discord.Interaction):
    if not is_admin(interaction):
        await interaction.response.send_message("権限がありません。", ephemeral=True)
        return

    # ★これ（考え中を出さない＆表示を残さないACK）
    await interaction.response.defer(ephemeral=True, thinking=False)

    gid = interaction.guild_id
    ch = interaction.channel  # type: ignore

    # --- ここに3投稿の処理（既存のまま） ---
    # msg = await ch.send(...)
    # ...

    # ★最後に「ACKメッセージ」を消す（=何も残らない）
    try:
        await interaction.delete_original_response()
    except Exception:
        pass

    gid = interaction.guild_id
    ch = interaction.channel  # type: ignore

    # --- 1投稿目：大会情報 ---
    main_id = ADMIN_PANEL_MAIN_MSG.get(gid)
    if main_id:
        try:
            msg = await ch.fetch_message(main_id)
            await msg.edit(embed=build_panel_embed(), view=AdminPanelMainView())
        except Exception:
            main_id = None
    if not main_id:
        msg = await ch.send(embed=build_panel_embed(), view=AdminPanelMainView())
        ADMIN_PANEL_MAIN_MSG[gid] = msg.id

    # --- 2投稿目：チーム質問 ---
    team_id = ADMIN_PANEL_TEAM_MSG.get(gid)
    if team_id:
        try:
            msg2 = await ch.fetch_message(team_id)
            await msg2.edit(embed=build_team_embed(), view=AdminTeamQuestionsView())
        except Exception:
            team_id = None
    if not team_id:
        msg2 = await ch.send(embed=build_team_embed(), view=AdminTeamQuestionsView())
        ADMIN_PANEL_TEAM_MSG[gid] = msg2.id

    # --- 3投稿目：個人質問 ---
    indiv_id = ADMIN_PANEL_INDIV_MSG.get(gid)
    if indiv_id:
        try:
            msg3 = await ch.fetch_message(indiv_id)
            await msg3.edit(embed=build_indiv_embed(), view=AdminIndivQuestionsView())
        except Exception:
            indiv_id = None
    if not indiv_id:
        msg3 = await ch.send(embed=build_indiv_embed(), view=AdminIndivQuestionsView())
        ADMIN_PANEL_INDIV_MSG[gid] = msg3.id

    # persist admin panel location + message ids (so refresh works even after delete/repost/restart)
    try:
        pl = CONFIG.get("panel_lock") or {}
        pl["admin_channel_id"] = int(getattr(ch, "id", 0) or 0)
        pl["admin_main_msg_id"] = int(ADMIN_PANEL_MAIN_MSG.get(gid) or 0) or None
        pl["admin_team_msg_id"] = int(ADMIN_PANEL_TEAM_MSG.get(gid) or 0) or None
        pl["admin_indiv_msg_id"] = int(ADMIN_PANEL_INDIV_MSG.get(gid) or 0) or None
        CONFIG["panel_lock"] = pl
        save_config(CONFIG)
    except Exception:
        pass

    await silent_ack(interaction, ephemeral=True)



@client.tree.command(name="entry_threads_list", description="エントリースレッド一覧を表示します（運営のみ）")
async def threads_cmd(interaction: discord.Interaction):
    # 権限：管理者 or 運営ロール
    member = interaction.user
    if isinstance(member, discord.Member):
        if not (is_admin(interaction) or has_ops_role(member)):
            await interaction.response.send_message("このコマンドは運営のみ使用できます。", ephemeral=True)
            return
    else:
        await interaction.response.send_message("権限判定に失敗しました。", ephemeral=True)
        return

    await interaction.response.defer(thinking=False, ephemeral=True)

    parent = interaction.client.get_channel(THREAD_PARENT_CHANNEL_ID)
    if parent is None:
        try:
            parent = await interaction.client.fetch_channel(THREAD_PARENT_CHANNEL_ID)
        except Exception:
            await interaction.followup.send("スレッド親チャンネルが見つかりません。", ephemeral=True)
            return
    if not isinstance(parent, discord.TextChannel):
        await interaction.followup.send("スレッド親がテキストチャンネルではありません。", ephemeral=True)
        return

    ths = await fetch_entry_threads(parent, limit=60)
    if not ths:
        await interaction.followup.send("スレッドが見つかりませんでした。（Botが参加していないprivate threadは取得できません）", ephemeral=True)
        return

    lines: List[str] = []
    for i, th in enumerate(ths, start=1):
        try:
            # thread.mention が「スレッドリンク」になる
            lines.append(f"{i:02d}. {th.mention}｜{th.name}")
        except Exception:
            continue

    chunks = _chunk_lines(lines, max_chars=1800)

    # 表示先：コマンドを打った場所（チャンネル or スレッド）
    target = interaction.channel
    if target is None:
        await interaction.followup.send("表示先チャンネルが見つかりません。", ephemeral=True)
        return

    # 送信（本文は公開でもOK、ただし必要ならここを ephemeral=False に変更）
    # まずは「コマンド実行した場所に表示」＝公開投稿
    for n, body in enumerate(chunks, start=1):
        header = "🧾エントリースレッド一覧（生成順）"
        if len(chunks) > 1:
            header += f" [{n}/{len(chunks)}]"
        try:
            await target.send(f"{header}\n{body}")
        except Exception:
            pass

    await interaction.followup.send("スレッド一覧を表示しました。", ephemeral=True)
@client.tree.command(name="adminpanel", description="（互換用）管理パネルを設置します（管理者のみ）")
async def adminpanel(interaction: discord.Interaction):
    if not is_admin(interaction):
        await interaction.response.send_message("権限がありません。", ephemeral=True)
        return
    await panel(interaction)


# =========================
# Entry cleanup (bulk delete threads created from entry panel)
# =========================
def _is_cleanup_target_thread(th: discord.Thread) -> bool:
    name = (th.name or "")
    return (name.startswith("P-No.") or name.startswith("E-No.") or name.startswith("仮No.") or name.startswith("受理No.")) and int(getattr(th, "parent_id", 0) or 0) == int(THREAD_PARENT_CHANNEL_ID)

async def _collect_entry_threads(parent: discord.TextChannel) -> List[discord.Thread]:
    targets: List[discord.Thread] = []

    # Active threads
    try:
        for th in list(getattr(parent, "threads", []) or []):
            if isinstance(th, discord.Thread) and _is_cleanup_target_thread(th):
                targets.append(th)
    except Exception:
        pass

    # Archived threads (public + private)
    async def _add_archived(private: bool):
        try:
            async for th in parent.archived_threads(limit=200, private=private):
                if isinstance(th, discord.Thread) and _is_cleanup_target_thread(th):
                    # avoid duplicates
                    if all(x.id != th.id for x in targets):
                        targets.append(th)
        except TypeError:
            # older discord.py signature fallback (may not support private=)
            try:
                async for th in parent.archived_threads(limit=200):
                    if isinstance(th, discord.Thread) and _is_cleanup_target_thread(th):
                        if all(x.id != th.id for x in targets):
                            targets.append(th)
            except Exception:
                pass
        except Exception:
            pass

    await _add_archived(private=False)
    await _add_archived(private=True)

    return targets

class EntryCleanupConfirmView(discord.ui.View):
    def __init__(self, thread_ids: List[int]):
        super().__init__(timeout=60)
        self.thread_ids = thread_ids

    @discord.ui.button(label="削除する", style=discord.ButtonStyle.danger, custom_id="cleanup:do")
    async def do(self, interaction: discord.Interaction, button: discord.ui.Button):
        member = interaction.user
        if not isinstance(member, discord.Member) or not (is_admin(interaction) or has_ops_role(member)):
            await interaction.response.send_message("権限がありません。", ephemeral=True)
            return

        await silent_ack(interaction, ephemeral=True)

        # clear active_threads first to prevent deadlocks even if deletion errors
        try:
            CONFIG["active_threads"] = {}
            CONFIG["threads"] = {}
            CONFIG["next_draft_no"] = 1
            save_config(CONFIG)
        except Exception:
            pass

        ok = 0
        ng = 0

        for tid in list(self.thread_ids):
            try:
                ch = interaction.client.get_channel(int(tid))
                if ch is None:
                    try:
                        ch = await interaction.client.fetch_channel(int(tid))
                    except Exception:
                        ch = None
                if isinstance(ch, discord.Thread):
                    await ch.delete()
                    ok += 1
                else:
                    ng += 1
            except Exception:
                ng += 1

        try:
            await interaction.followup.send(f"🧹 エントリースレッドを {ok} 件削除しました。（失敗 {ng} 件）", ephemeral=True)
        except Exception:
            pass

        self.stop()

    @discord.ui.button(label="やめる", style=discord.ButtonStyle.secondary, custom_id="cleanup:cancel")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("キャンセルしました。", ephemeral=True)
        self.stop()

@client.tree.command(name="entry_cleanup", description="受付パネルで作成されたエントリースレッドを一括削除します（運営のみ）")
async def entry_cleanup(interaction: discord.Interaction):
    member = interaction.user
    if not isinstance(member, discord.Member) or not (is_admin(interaction) or has_ops_role(member)):
        await interaction.response.send_message("権限がありません。", ephemeral=True)
        return

    # parent channel
    parent = interaction.client.get_channel(THREAD_PARENT_CHANNEL_ID)
    if parent is None:
        try:
            parent = await interaction.client.fetch_channel(THREAD_PARENT_CHANNEL_ID)
        except Exception:
            parent = None
    if not isinstance(parent, discord.TextChannel):
        await interaction.response.send_message("スレッド親チャンネルが見つかりません。", ephemeral=True)
        return

    # エフェメラルで確認を出したいので、最初のACKもephemeralでdeferする
    await interaction.response.defer(thinking=False, ephemeral=True)

    targets = await _collect_entry_threads(parent)
    if not targets:
        # 削除対象がなくても「番号リセット（フルリセット）」は可能にする
        try:
            CONFIG["active_threads"] = {}
            CONFIG["threads"] = {}
            CONFIG["next_draft_no"] = 1
            save_config(CONFIG)
        except Exception:
            pass
        await interaction.followup.send("🧹 削除対象スレッドはありませんでしたが、番号をリセットしました。（next_draft_no=1）", ephemeral=True)
        return

    ids = [int(t.id) for t in targets]
    sample = "\n".join([f"・{t.name}" for t in targets[:5]])
    more = "" if len(targets) <= 5 else f"\n…ほか {len(targets)-5} 件"

    await interaction.followup.send(
        f"⚠️ エントリースレッドを **{len(targets)}件** 削除します。\n"
        f"（例）\n{sample}{more}\n\n"
        "続行しますか？",
        view=EntryCleanupConfirmView(ids),
        ephemeral=True
    )

# =========================
# 起動
# =========================
if __name__ == "__main__":
    client.run(BOT_TOKEN)
