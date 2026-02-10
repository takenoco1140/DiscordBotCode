# -*- coding: utf-8 -*-
# lottely_bot.py（全文差し替え：抽選パネル刷新）
# OR40 抽選BOT
#
# 既存仕様:
# - 抽選結果「確定」時、status==当選 の行を上から順に走査し
#   当選No（数値）を 1,2,3... と付与（0埋めはSS表示形式に委譲）
#
# 今回仕様（運営パネル）:
# - 当選人数の登録は別ボタン
#   [当選人数登録]（初回抽選 / 追加抽選 / 対象status）
#   [リセット]
# - 抽選ボタンは人数入力なしで実行
#   [初回抽選] [追加抽選]
# - 抽選後は当選者リストを表示し、[確定] [やり直し] ボタン
# - 初回抽選→確定 したら 初回抽選は無効化、追加抽選が有効化
#   （リセットで戻す）

import os
import json
import random
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import discord
from discord import app_commands
from discord.ext import commands

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from pathlib import Path


# =========================
# Path helpers
# =========================
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

STATE_FILE = str(DATA_DIR / "lottery_state.json")

# =========================
# 固定設定
# =========================
GUILD_ID = 1456602929959010441
TOKEN_ENV = "LOTTELY_TOKEN"

SPREADSHEET_ID = "1d0DRjoPJ0wy3WIYrOfCKhwtBp_Pde7kKXp5RzpV5Z8E"
ENTRY_SHEET_GID = 1279994579
GOOGLE_CREDENTIALS_PATH = r"D:\DiscordBot\secrets\service_account.json"

STATUS_ACCEPTED = "受付完了"
STATUS_LOSE = "落選"
STATUS_WIN = "当選"

# =========================
# Discord setup
# =========================
intents = discord.Intents.default()
intents.guilds = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)
GUILD_OBJ = discord.Object(id=GUILD_ID)

# =========================
# 権限
# =========================
def is_staff(interaction: discord.Interaction) -> bool:
    return isinstance(interaction.user, discord.Member) and (
        interaction.user.guild_permissions.manage_guild
        or interaction.user.guild_permissions.administrator
    )


def _staff_guard(interaction: discord.Interaction) -> Optional[discord.Embed]:
    if is_staff(interaction):
        return None
    return discord.Embed(
        title="権限がありません",
        description="この操作は運営のみ実行できます。",
        color=discord.Color.red(),
    )


# =========================
# Google Sheets helpers
# =========================
def build_sheets(scopes):
    creds = Credentials.from_service_account_file(GOOGLE_CREDENTIALS_PATH, scopes=scopes)
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def get_sheet_title(service, gid: int) -> str:
    meta = service.spreadsheets().get(spreadsheetId=SPREADSHEET_ID).execute()
    for s in meta.get("sheets", []):
        if s["properties"]["sheetId"] == gid:
            return s["properties"]["title"]
    raise RuntimeError("sheet not found")


def read_all(service, title: str):
    return (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=SPREADSHEET_ID, range=f"'{title}'!A1:Z")
        .execute()
        .get("values", [])
    )


def sheet_update_cells(service, title: str, a1_range: str, values: List[List[str]]):
    return (
        service.spreadsheets()
        .values()
        .update(
            spreadsheetId=SPREADSHEET_ID,
            range=f"'{title}'!{a1_range}",
            valueInputOption="USER_ENTERED",
            body={"values": values},
        )
        .execute()
    )


def col_index_to_a1(col_idx_zero_based: int) -> str:
    """
    Convert 0-based column index to A1 column letters.
    0 -> A, 25 -> Z, 26 -> AA, ...
    """
    if col_idx_zero_based < 0:
        raise ValueError("col_idx_zero_based must be >= 0")
    n = col_idx_zero_based + 1
    letters = []
    while n > 0:
        n, rem = divmod(n - 1, 26)
        letters.append(chr(ord("A") + rem))
    return "".join(reversed(letters))


# =========================
# 抽選データ
# =========================
@dataclass(frozen=True)
class Entry:
    row_index_1based: int
    discord_id: int
    thread_id: int
    receipt: str


def _load_sheet_rows_with_header() -> Tuple[List[str], List[List[str]], str]:
    service = build_sheets(["https://www.googleapis.com/auth/spreadsheets.readonly"])
    title = get_sheet_title(service, ENTRY_SHEET_GID)
    rows = read_all(service, title)
    if not rows:
        return [], [], title
    return rows[0], rows[1:], title


def load_entries_by_status(status_value: str) -> List[Entry]:
    header, body, _title = _load_sheet_rows_with_header()
    if not header:
        return []

    idx_status = header.index("status")
    idx_did = header.index("DiscordID_1") if "DiscordID_1" in header else header.index("DiscordID")
    idx_tid = header.index("threadID")
    idx_receipt = header.index("受理No")

    entries: List[Entry] = []
    for i, r in enumerate(body, start=2):
        if len(r) <= max(idx_status, idx_did, idx_tid, idx_receipt):
            continue
        if str(r[idx_status]).strip() != status_value:
            continue
        try:
            entries.append(
                Entry(
                    row_index_1based=i,
                    discord_id=int(str(r[idx_did]).strip()),
                    thread_id=int(str(r[idx_tid]).strip()),
                    receipt=str(r[idx_receipt]).strip(),
                )
            )
        except Exception:
            continue
    return entries


def update_status_bulk(row_updates: List[Tuple[int, str]]) -> None:
    if not row_updates:
        return

    service = build_sheets(["https://www.googleapis.com/auth/spreadsheets"])
    title = get_sheet_title(service, ENTRY_SHEET_GID)

    service_ro = build_sheets(["https://www.googleapis.com/auth/spreadsheets.readonly"])
    rows = read_all(service_ro, title)
    if not rows:
        return
    header = rows[0]
    idx_status = header.index("status")

    for row_1b, status in row_updates:
        col_letter = col_index_to_a1(idx_status)
        a1 = f"{col_letter}{row_1b}"
        sheet_update_cells(service, title, a1, [[status]])


# =========================
# state
# =========================
def _default_state() -> Dict:
    return {
        "current": None,  # 今の抽選（未確定 or 確定）
        "panel_message_id": None,
        "panel_channel_id": None,
        "draw_summary_message_id": None,
        "panel_defaults": {
            "initial_winners": 40,
            "additional_winners": 5,
        },
        "flow": {
            "initial_confirmed": False,  # 初回抽選が確定済みか
        },
        "tournaments": {},
        "current_tournament_id": None,
    }


def load_state() -> Dict:
    if not os.path.exists(STATE_FILE):
        return _default_state()
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
        base = _default_state()
        # shallow merge
        for k, v in base.items():
            if k not in data:
                data[k] = v
        # nested merge (panel_defaults / flow)
        if not isinstance(data.get("panel_defaults"), dict):
            data["panel_defaults"] = base["panel_defaults"]
        else:
            for k, v in base["panel_defaults"].items():
                data["panel_defaults"].setdefault(k, v)
        if not isinstance(data.get("flow"), dict):
            data["flow"] = base["flow"]
        else:
            for k, v in base["flow"].items():
                data["flow"].setdefault(k, v)
        return data
    except Exception:
        return _default_state()


def save_state(s: Dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=2)


def _ensure_tournament_bucket(state: Dict, tid: str) -> Dict:
    state.setdefault("tournaments", {})
    bucket = state["tournaments"].setdefault(tid, {})
    bucket.setdefault("issued_invites", {})
    return bucket


async def _fetch_invite_safe(client: discord.Client, code: str) -> Optional[discord.Invite]:
    try:
        return await client.fetch_invite(code)
    except Exception:
        return None


def get_current_tournament_id() -> str:
    state = load_state()
    tid = state.get("current_tournament_id")
    if isinstance(tid, str) and tid.strip():
        return tid.strip()
    return "LEGACY"


# =========================
# 抽選処理（未確定を作るだけ）
# =========================
async def draw_lottery(target_status: str, winners_requested: int, mode: str):
    pool = load_entries_by_status(target_status)
    if not pool:
        raise RuntimeError(f"抽選対象が0（status={target_status} が0）")
    if winners_requested > len(pool):
        raise RuntimeError(f"当選人数({winners_requested})が抽選対象({len(pool)})を超えています")

    picked = set(random.sample(pool, winners_requested))

    results = {}
    for e in pool:
        results[str(e.thread_id)] = {
            "row": e.row_index_1based,
            "discord_id": e.discord_id,
            "receipt": e.receipt,
            "win": (e in picked),
        }

    st = load_state()
    st["current"] = {
        "mode": mode,
        "target_status": target_status,
        "pool_size": len(pool),
        "winners_requested": winners_requested,
        "results": results,
        "confirmed": False,
        "drawn_at": datetime.now(timezone.utc).isoformat(),
    }
    save_state(st)


def _mode_text(mode: str) -> str:
    return "初回抽選" if mode == "initial" else ("追加抽選" if mode == "additional" else "—")


def build_draw_summary_embed(cur: Dict) -> discord.Embed:
    mode = cur.get("mode", "")
    mode_text = _mode_text(mode)
    target_status = cur.get("target_status", "—")
    pool_size = cur.get("pool_size", "—")
    winners_req = cur.get("winners_requested", "—")
    confirmed = "確定済み" if cur.get("confirmed") else "未確定"
    drawn_at = cur.get("drawn_at")

    # 抽選当選（今回の抽選で win=True）
    drawn_win_lines: List[str] = []
    drawn_thread_ids = set()
    for tid, info in (cur.get("results") or {}).items():
        if info.get("win"):
            drawn_thread_ids.add(str(tid))
            drawn_win_lines.append(
                f"#{info.get('receipt','?')}  <@{info.get('discord_id','0')}>  (thread:{tid})"
            )

    # 確定当選（事前に status=当選 を入れている人）を抽選結果にも表示する
    # ※ threadID が未設定でも載せる（表示用）
    pre_win_lines: List[str] = []
    try:
        header, body, _title = _load_sheet_rows_with_header()
        if header:
            idx_status = header.index("status")
            idx_did = header.index("DiscordID_1") if "DiscordID_1" in header else header.index("DiscordID")
            idx_tid = header.index("threadID")
            idx_receipt = header.index("受理No")

            for r in body:
                if len(r) <= max(idx_status, idx_did, idx_tid, idx_receipt):
                    continue
                if str(r[idx_status]).strip() != STATUS_WIN:
                    continue

                raw_did = str(r[idx_did]).strip()
                if not raw_did.isdigit():
                    continue
                did = raw_did

                receipt = str(r[idx_receipt]).strip() or "?"

                raw_tid = str(r[idx_tid]).strip()
                if raw_tid and raw_tid.isdigit():
                    tid = raw_tid
                else:
                    tid = "0"

                # 今回の抽選で当選として既に載っている人は二重表示しない（threadIDが取れている場合のみ）
                if tid != "0" and tid in drawn_thread_ids:
                    continue

                if tid == "0":
                    pre_win_lines.append(f"#{receipt}  <@{did}>  (thread:未設定)")
                else:
                    pre_win_lines.append(f"#{receipt}  <@{did}>  (thread:{tid})")
    except Exception:
        pre_win_lines = []

    if not pre_win_lines:
        pre_win_lines = ["（なし）"]
    if not drawn_win_lines:
        drawn_win_lines = ["（当選者なし）"]

    pre_cnt = 0 if pre_win_lines == ["（なし）"] else len(pre_win_lines)
    draw_cnt = 0 if drawn_win_lines == ["（当選者なし）"] else len(drawn_win_lines)
    total_cnt = pre_cnt + draw_cnt

    desc = (
        f"対象ステータス：{target_status}\n"
        f"抽選対象：{pool_size}名\n"
        f"当選人数（抽選）：{winners_req}名\n"
        f"確定当選（事前当選）：{pre_cnt}名 / 抽選当選：{draw_cnt}名 / 合計：{total_cnt}名\n"
        f"状態：{confirmed}"
    )

    embed = discord.Embed(
        title=f"🎲 抽選結果（{mode_text} / 運営確認用）",
        description=desc,
        color=discord.Color.blurple(),
    )
    if drawn_at:
        embed.set_footer(text=f"抽選時刻(UTC): {drawn_at}")

    embed.add_field(
        name="確定当選（事前に status=当選）",
        value="\n".join(pre_win_lines),
        inline=False,
    )
    embed.add_field(
        name="抽選当選（今回の抽選）",
        value="\n".join(drawn_win_lines),
        inline=False,
    )
    return embed


# =========================
# 確定処理（SS更新 + 当選No付与 + 通知）
# =========================
async def confirm_and_notify():
    st = load_state()
    cur = st.get("current")
    if not isinstance(cur, dict):
        raise RuntimeError("未抽選です")
    if cur.get("confirmed"):
        raise RuntimeError("既に確定済みです")

    updates: List[Tuple[int, str]] = []
    results = cur.get("results") or {}
    mode = cur.get("mode")

    # status 更新
    for _tid, info in results.items():
        row = int(info.get("row", 0))
        if row <= 0:
            continue
        if info.get("win"):
            updates.append((row, STATUS_WIN))
        else:
            if mode == "initial":
                updates.append((row, STATUS_LOSE))

    update_status_bulk(updates)

    # ===== 当選No（追番のみ / 再採番しない） =====
    # ルール:
    # - 既存の当選Noが入っている行は絶対に触らない（再採番禁止）
    # - status=当選 かつ 当選Noが空の行だけ、既存最大+1 から追番する
    service = build_sheets(["https://www.googleapis.com/auth/spreadsheets"])
    service_ro = build_sheets(["https://www.googleapis.com/auth/spreadsheets.readonly"])
    title = get_sheet_title(service, ENTRY_SHEET_GID)
    rows = read_all(service_ro, title)

    if rows:
        header = rows[0]
        if "当選No" in header and "status" in header:
            idx_cno = header.index("当選No")
            idx_status = header.index("status")

            # 既存最大値を計算（数字だけ拾う）
            max_no = 0
            for r in rows[1:]:
                if len(r) <= max(idx_cno, idx_status):
                    continue
                if str(r[idx_status]).strip() != STATUS_WIN:
                    continue
                raw = str(r[idx_cno]).strip()
                if raw.isdigit():
                    max_no = max(max_no, int(raw))

            # 追番対象（status=当選 かつ 当選Noが未設定）
            target_rows: List[int] = []
            for i, r in enumerate(rows[1:], start=2):
                if len(r) <= max(idx_cno, idx_status):
                    continue
                if str(r[idx_status]).strip() != STATUS_WIN:
                    continue
                raw = str(r[idx_cno]).strip()
                if raw.isdigit():
                    continue  # 既に番号がある = 触らない
                target_rows.append(i)

            target_rows.sort()
            next_no = max_no + 1
            for row_1b in target_rows:
                col_letter = col_index_to_a1(idx_cno)
                a1 = f"{col_letter}{row_1b}"
                sheet_update_cells(service, title, a1, [[next_no]])
                next_no += 1


    cur["confirmed"] = True
    cur["confirmed_at"] = datetime.now(timezone.utc).isoformat()

    # 初回確定フラグ
    if str(cur.get("mode")) == "initial":
        flow = st.get("flow") if isinstance(st.get("flow"), dict) else {}
        flow["initial_confirmed"] = True
        st["flow"] = flow

    st["current"] = cur
    save_state(st)

    # 通知
    for tid in results.keys():
        try:
            ch = await bot.fetch_channel(int(tid))
            if isinstance(ch, discord.Thread):
                await ch.send(
                    "📮 **抽選結果のお知らせ**\n"
                    "抽選結果が確定しました。下のボタンを押して確認してください。",
                    view=ResultView(),
                )
        except Exception:
            pass


# =========================
# 抽選パネル（運営）
# =========================
def _panel_defaults(state: Dict) -> Dict:
    pd = state.get("panel_defaults")
    return pd if isinstance(pd, dict) else _default_state()["panel_defaults"]


def _flow(state: Dict) -> Dict:
    f = state.get("flow")
    return f if isinstance(f, dict) else _default_state()["flow"]


def build_panel_embed(state: Dict) -> discord.Embed:
    pd = _panel_defaults(state)
    flow = _flow(state)
    cur = state.get("current") if isinstance(state.get("current"), dict) else None

    initial_w = pd.get("initial_winners", 40)
    add_w = pd.get("additional_winners", 5)

    initial_confirmed = bool(flow.get("initial_confirmed"))

    lines = [
        f"当選人数（初回）：{initial_w}",
        f"初回status（固定）：{STATUS_ACCEPTED}",
        f"当選人数（追加）：{add_w}",
        f"追加status（固定）：{STATUS_LOSE}",
        f"初回確定：{'はい' if initial_confirmed else 'いいえ'}",
    ]
    if cur:
        lines.append("")
        lines.append("【現在の抽選】")
        lines.append(f"区分：{_mode_text(str(cur.get('mode','')))}")
        lines.append(f"状態：{'確定済み' if cur.get('confirmed') else '未確定'}")
        lines.append(f"抽選対象：{cur.get('pool_size','—')}名 / 当選：{cur.get('winners_requested','—')}名")
        if cur.get("drawn_at"):
            lines.append(f"抽選時刻(UTC)：{cur.get('drawn_at')}")

    color = discord.Color.green() if (cur and cur.get("confirmed")) else (discord.Color.orange() if cur else discord.Color.blurple())

    embed = discord.Embed(
        title="🎛️ 抽選パネル（運営用）",
        description="\n".join(lines),
        color=color,
    )
    embed.add_field(
        name="操作",
        value=(
            "1) 当選人数登録\n"
            "2) 初回抽選 or 追加抽選（抽選後に当選者リスト表示）\n"
            "3) 当選者リストを確認 → 確定 or やり直し\n"
            "※ 初回を確定すると初回抽選は無効化され、追加抽選が有効化されます（リセットで戻す）"
        ),
        inline=False,
    )
    return embed


async def _update_panel_message() -> None:
    st = load_state()
    ch_id = st.get("panel_channel_id")
    msg_id = st.get("panel_message_id")
    if not ch_id or not msg_id:
        return
    try:
        ch = await bot.fetch_channel(int(ch_id))
        if not isinstance(ch, (discord.TextChannel, discord.Thread)):
            return
        msg = await ch.fetch_message(int(msg_id))
        await msg.edit(embed=build_panel_embed(st), view=LotteryPanelView())
    except Exception:
        return


async def _post_or_reuse_panel(channel: discord.abc.Messageable) -> discord.Message:
    st = load_state()
    ch_id = st.get("panel_channel_id")
    msg_id = st.get("panel_message_id")

    if ch_id and msg_id:
        try:
            ch2 = await bot.fetch_channel(int(ch_id))
            if isinstance(ch2, (discord.TextChannel, discord.Thread)) and int(ch2.id) == int(getattr(channel, "id", 0)):
                msg2 = await ch2.fetch_message(int(msg_id))
                await msg2.edit(embed=build_panel_embed(st), view=LotteryPanelView())
                return msg2
        except Exception:
            pass

    msg = await channel.send(embed=build_panel_embed(st), view=LotteryPanelView())
    st["panel_channel_id"] = int(getattr(channel, "id", 0))
    st["panel_message_id"] = int(msg.id)
    save_state(st)
    return msg


async def upsert_draw_summary_message(cur: Dict) -> None:
    st = load_state()
    ch_id = st.get("panel_channel_id")
    msg_id = st.get("draw_summary_message_id")
    if not ch_id:
        return
    try:
        ch = await bot.fetch_channel(int(ch_id))
        if not isinstance(ch, (discord.TextChannel, discord.Thread)):
            return
    except Exception:
        return

    embed = build_draw_summary_embed(cur)
    try:
        if msg_id:
            m = await ch.fetch_message(int(msg_id))
            if bool(cur.get('confirmed')):
                await m.edit(embed=embed, view=None)
            else:
                await m.edit(embed=embed, view=ConfirmRedoView())
            return
    except Exception:
        pass

    if bool(cur.get('confirmed')):
        m = await ch.send(embed=embed)
    else:
        m = await ch.send(embed=embed, view=ConfirmRedoView())
    st["draw_summary_message_id"] = int(m.id)
    save_state(st)


def _has_pending_draw(state: Dict) -> bool:
    cur = state.get("current")
    return isinstance(cur, dict) and not bool(cur.get("confirmed"))


def _can_use_initial_draw(state: Dict) -> bool:
    # 初回確定したら初回抽選は死ぬ
    flow = _flow(state)
    if bool(flow.get("initial_confirmed")):
        return False
    # 未確定の抽選がある間は押せない
    return not _has_pending_draw(state)


def _can_use_additional_draw(state: Dict) -> bool:
    flow = _flow(state)
    if not bool(flow.get("initial_confirmed")):
        return False
    return not _has_pending_draw(state)


class InitialConfigModal(discord.ui.Modal):
    def __init__(self):
        super().__init__(title="当選人数登録（初回）")
        st = load_state()
        pd = _panel_defaults(st)

        self.initial_winners = discord.ui.TextInput(
            label="初回抽選の当選人数",
            placeholder="例：40",
            required=True,
            default=str(pd.get("initial_winners", 40)),
            max_length=4,
        )
        self.add_item(self.initial_winners)

    async def on_submit(self, interaction: discord.Interaction):
        guard = _staff_guard(interaction)
        if guard:
            return await interaction.response.send_message(embed=guard)

        try:
            iw = int(str(self.initial_winners.value).strip())
            if iw <= 0:
                raise ValueError
        except Exception:
            return await interaction.response.send_message("当選人数は 1以上の整数で入力してください。")

        st = load_state()
        pd = _panel_defaults(st)
        pd["initial_winners"] = iw
        st["panel_defaults"] = pd
        save_state(st)

        await _update_panel_message()
        await interaction.response.send_message("登録しました。")


class AdditionalConfigModal(discord.ui.Modal):
    def __init__(self):
        super().__init__(title="当選人数登録（追加）")
        st = load_state()
        pd = _panel_defaults(st)

        self.additional_winners = discord.ui.TextInput(
            label="追加抽選の当選人数",
            placeholder="例：5",
            required=True,
            default=str(pd.get("additional_winners", 5)),
            max_length=4,
        )
        self.add_item(self.additional_winners)

    async def on_submit(self, interaction: discord.Interaction):
        guard = _staff_guard(interaction)
        if guard:
            return await interaction.response.send_message(embed=guard)

        try:
            aw = int(str(self.additional_winners.value).strip())
            if aw <= 0:
                raise ValueError
        except Exception:
            return await interaction.response.send_message("当選人数は 1以上の整数で入力してください。")

        st = load_state()
        pd = _panel_defaults(st)
        pd["additional_winners"] = aw
        st["panel_defaults"] = pd
        save_state(st)

        await _update_panel_message()
        await interaction.response.send_message("登録しました。")


class ConfirmRedoView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="✅ 確定", style=discord.ButtonStyle.success, custom_id="lottery:draw:confirm")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        guard = _staff_guard(interaction)
        if guard:
            return await interaction.response.send_message(embed=guard)

        st = load_state()
        if not _has_pending_draw(st):
            return await interaction.response.send_message("未確定の抽選がありません。")

        await interaction.response.defer(thinking=True)
        try:
            await confirm_and_notify()
        except Exception as e:
            return await interaction.followup.send(f"確定に失敗しました：{e}")

        st2 = load_state()
        cur = st2.get("current") if isinstance(st2.get("current"), dict) else None
        if cur:
            await upsert_draw_summary_message(cur)
        await _update_panel_message()
        await interaction.followup.send("確定しました。")

    @discord.ui.button(label="🔁 やり直し", style=discord.ButtonStyle.danger, custom_id="lottery:draw:redo")
    async def redo(self, interaction: discord.Interaction, button: discord.ui.Button):
        guard = _staff_guard(interaction)
        if guard:
            return await interaction.response.send_message(embed=guard)

        st = load_state()
        cur = st.get("current") if isinstance(st.get("current"), dict) else None
        if not cur or bool(cur.get("confirmed")):
            return await interaction.response.send_message("やり直しできる未確定の抽選がありません。")

        mode = str(cur.get("mode") or "")
        pd = _panel_defaults(st)
        target_status = STATUS_ACCEPTED if mode == "initial" else STATUS_LOSE
        winners = int(pd.get("initial_winners", 40) if mode == "initial" else pd.get("additional_winners", 5))

        await interaction.response.defer(thinking=True)
        try:
            await draw_lottery(target_status, winners, mode)
        except Exception as e:
            return await interaction.followup.send(f"やり直しに失敗しました：{e}")

        st2 = load_state()
        cur2 = st2.get("current") if isinstance(st2.get("current"), dict) else None
        if cur2:
            await upsert_draw_summary_message(cur2)
        await _update_panel_message()
        await interaction.followup.send("やり直しました（未確定）。")


class LotteryPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

        # ボタン有効/無効は描画時にstateで決める
        st = load_state()
        # 設置時点の状態で反映（押した後は _update_panel_message() で再描画される）
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                pass
        # dynamic disable
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                if child.custom_id == "lottery:panel:draw_initial":
                    child.disabled = not _can_use_initial_draw(st)
                elif child.custom_id == "lottery:panel:draw_additional":
                    child.disabled = not _can_use_additional_draw(st)
                elif child.custom_id == "lottery:panel:reset":
                    child.disabled = False
                elif child.custom_id in ("lottery:panel:config_initial", "lottery:panel:config_additional"):
                    child.disabled = False

    @discord.ui.button(label="🧾 当選人数登録（初回）", style=discord.ButtonStyle.secondary, custom_id="lottery:panel:config_initial")
    async def config_initial(self, interaction: discord.Interaction, button: discord.ui.Button):
        guard = _staff_guard(interaction)
        if guard:
            return await interaction.response.send_message(embed=guard)
        await interaction.response.send_modal(InitialConfigModal())

    @discord.ui.button(label="🧾 当選人数登録（追加）", style=discord.ButtonStyle.secondary, custom_id="lottery:panel:config_additional")
    async def config_additional(self, interaction: discord.Interaction, button: discord.ui.Button):
        guard = _staff_guard(interaction)
        if guard:
            return await interaction.response.send_message(embed=guard)
        await interaction.response.send_modal(AdditionalConfigModal())

    @discord.ui.button(label="🎲 初回抽選", style=discord.ButtonStyle.primary, custom_id="lottery:panel:draw_initial")
    async def draw_initial(self, interaction: discord.Interaction, button: discord.ui.Button):
        guard = _staff_guard(interaction)
        if guard:
            return await interaction.response.send_message(embed=guard)

        st = load_state()
        if not _can_use_initial_draw(st):
            return await interaction.response.send_message("初回抽選は現在実行できません。")

        pd = _panel_defaults(st)
        target_status = STATUS_ACCEPTED  # fixed
        winners = int(pd.get("initial_winners", 40))

        await interaction.response.defer(thinking=True)
        try:
            await draw_lottery(target_status, winners, "initial")
        except Exception as e:
            return await interaction.followup.send(f"抽選に失敗しました：{e}")

        st2 = load_state()
        cur = st2.get("current") if isinstance(st2.get("current"), dict) else None
        if cur:
            await upsert_draw_summary_message(cur)
        await _update_panel_message()
        await interaction.followup.send("初回抽選しました（未確定）。当選者リストを確認して、確定 or やり直ししてください。")

    @discord.ui.button(label="➕ 追加抽選", style=discord.ButtonStyle.primary, custom_id="lottery:panel:draw_additional")
    async def draw_additional(self, interaction: discord.Interaction, button: discord.ui.Button):
        guard = _staff_guard(interaction)
        if guard:
            return await interaction.response.send_message(embed=guard)

        st = load_state()
        if not _can_use_additional_draw(st):
            return await interaction.response.send_message("追加抽選は現在実行できません（初回確定が必要 / 未確定抽選がある等）。")

        pd = _panel_defaults(st)
        target_status = STATUS_LOSE  # fixed
        winners = int(pd.get("additional_winners", 5))

        await interaction.response.defer(thinking=True)
        try:
            await draw_lottery(target_status, winners, "additional")
        except Exception as e:
            return await interaction.followup.send(f"抽選に失敗しました：{e}")

        st2 = load_state()
        cur = st2.get("current") if isinstance(st2.get("current"), dict) else None
        if cur:
            await upsert_draw_summary_message(cur)
        await _update_panel_message()
        await interaction.followup.send("追加抽選しました（未確定）。当選者リストを確認して、確定 or やり直ししてください。")

    @discord.ui.button(label="♻️ リセット", style=discord.ButtonStyle.danger, custom_id="lottery:panel:reset")
    async def reset(self, interaction: discord.Interaction, button: discord.ui.Button):
        guard = _staff_guard(interaction)
        if guard:
            return await interaction.response.send_message(embed=guard)

        st = load_state()
        st["current"] = None
        flow = _flow(st)
        flow["initial_confirmed"] = False
        st["flow"] = flow
        save_state(st)

        await _update_panel_message()
        await interaction.response.send_message("リセットしました（初回抽選からやり直せます）。")


# =========================
# 結果表示（参加者）
# =========================
class ResultView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🔎 抽選結果を確認する", style=discord.ButtonStyle.primary, custom_id="lottery:check")
    async def check(self, interaction: discord.Interaction, button: discord.ui.Button):
        st = load_state()
        cur = st.get("current") or {}
        res = (cur.get("results") or {}).get(str(interaction.channel.id))

        if not res:
            return await interaction.response.send_message("今回の抽選対象ではありません。")

        if int(res.get("discord_id", 0)) != int(interaction.user.id):
            return await interaction.response.send_message("この操作は本人のみ実行できます。")

        if not res.get("win"):
            embed = discord.Embed(
                title="🙇 今回は大会にご参加いただく枠をご用意できませんでした。",
                        description="""下のボタンから、招待リンクを受け取ってください。
（リンクは1回限り有効のURLです）

なお、今大会にご参加いただく方を対象に、
2/12（木）22:00〜
新しい画面共有システムのテストマッチ（2戦）を行います。

こちらは大会用サーバーでのカスタムキー案内となりますので、
招待リンク取得後は、お早めにご入室をお願いいたします。""",

                color=discord.Color.dark_grey(),
            )
            return await interaction.response.send_message(embed=embed)

        embed = discord.Embed(
    title="🎉 大会専用サーバーのご案内",
            description="""下のボタンから、招待リンクを受け取ってください。
（リンクは1回限り有効のURLです）

なお、今大会にご参加いただく方を対象に、
2/12（木）22:00〜
新しい画面共有システムのテストマッチ（2戦）を行います。

こちらは大会用サーバーでのカスタムキー案内となりますので、
招待リンク取得後は、お早めにご入室をお願いいたします。""",

    color=discord.Color.gold(),
)
await interaction.response.send_message(embed=embed, view=InviteIssueView())


class InviteIssueView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🚪 招待リンクを受け取る", style=discord.ButtonStyle.success, custom_id="lottery:invite")
    async def issue_invite(self, interaction: discord.Interaction, button: discord.ui.Button):
        """
        招待リンクは「Discord上のuses」を真実として扱う。
        - 既存inviteが取得でき、uses < 1 なら再掲（未使用）
        - uses >= 1 なら「使用済み」で再発行不可（既に参加済み想定）
        - 取得できない（失効/無効）場合のみ、未使用前提で再発行
        """
        tid = get_current_tournament_id()
        state = load_state()
        bucket = _ensure_tournament_bucket(state, tid)
        issued: Dict[str, Dict] = bucket.get("issued_invites", {})
        uid = str(interaction.user.id)

        prev = issued.get(uid)
        if isinstance(prev, dict):
            code = str(prev.get("invite_code") or "").strip()
            if code:
                inv = await _fetch_invite_safe(interaction.client, code)
                if inv is not None:
                    uses = int(getattr(inv, "uses", 0) or 0)
                    if uses >= 1:
                        return await interaction.response.send_message(
                            "この大会で、あなたの招待リンクは **既に使用済み** です（既に参加済みのはずです）。再発行できません。"
                        )
                    url = inv.url
                    embed = discord.Embed(
                        title="🚪 招待リンクをお届けします",
                        description=(
                            "このリンクは **1回限り有効** です。また **10分以内に使用してください。**\n\n"
                            f"{url}"
                        ),
                    )
                    return await interaction.response.send_message(embed=embed)

        # 既存が失効/無効 or 未発行 → 新規発行
        try:
            base_ch = interaction.guild.text_channels[0]
            invite = await base_ch.create_invite(max_uses=1, max_age=60 * 10, reason="OR40 抽選当選者")
        except Exception:
            return await interaction.response.send_message(
                "招待リンクの発行に失敗しました。運営に連絡してください。"
            )

        issued[uid] = {
            "invite_code": invite.code,
            "invite_url": invite.url,
            "issued_at": datetime.now(timezone.utc).isoformat(),
        }
        bucket["issued_invites"] = issued
        state["tournaments"][tid] = bucket
        state["current_tournament_id"] = tid
        save_state(state)

        embed = discord.Embed(
            title="🚪 招待リンクを発行しました",
            description=(
                "このリンクは **1回限り有効** です。また **10分以内に使用してください。**\n"
                "⚠リンクが失効した場合、**未使用（uses=0）** のときのみ再発行できます。\n\n"
                f"{invite.url}"
            ),
        )
        await interaction.response.send_message(embed=embed)



# =========================
# Slash Commands（運営）
# =========================
@bot.tree.command(name="lottery_panel", description="抽選パネル（運営用）をこのチャンネルに設置/更新します", guild=GUILD_OBJ)
async def lottery_panel(interaction: discord.Interaction):
    guard = _staff_guard(interaction)
    if guard:
        return await interaction.response.send_message(embed=guard)

    await interaction.response.defer()
    await _post_or_reuse_panel(interaction.channel)

    # パネルチャンネルで運営用結果メッセージがあれば、viewだけ付け直す（保険）
    st = load_state()
    cur = st.get("current") if isinstance(st.get("current"), dict) else None
    if cur:
        await upsert_draw_summary_message(cur)

    await interaction.followup.send("抽選パネルを設置/更新しました。")


@bot.tree.command(name="lottery_panel_reset", description="抽選パネルの保存ID（message/channel）をリセットします", guild=GUILD_OBJ)
async def lottery_panel_reset(interaction: discord.Interaction):
    guard = _staff_guard(interaction)
    if guard:
        return await interaction.response.send_message(embed=guard)

    st = load_state()
    st["panel_message_id"] = None
    st["panel_channel_id"] = None
    st["draw_summary_message_id"] = None
    save_state(st)
    await interaction.response.send_message("抽選パネルの保存情報をリセットしました。")



# =========================
# 運営用：結果告知コマンド
# =========================
@bot.tree.command(name="lottery_announce_result", description="このスレッドに「抽選結果が出ました」告知を送信します（個別用）", guild=GUILD_OBJ)
async def lottery_announce_result(interaction: discord.Interaction):
    guard = _staff_guard(interaction)
    if guard:
        return await interaction.response.send_message(embed=guard)

    if not isinstance(interaction.channel, discord.Thread):
        return await interaction.response.send_message("このコマンドは参加者の個別スレッドで実行してください。")

    st = load_state()
    cur = st.get("current")
    if not isinstance(cur, dict):
        return await interaction.response.send_message("抽選結果が存在しません。")

    tid = str(interaction.channel.id)
    res = (cur.get("results") or {}).get(tid)
    if not res:
        return await interaction.response.send_message("このスレッドは今回の抽選対象ではありません。")

    await interaction.channel.send(
        "📣 **抽選結果が出ました**\n"
        "このスレッド内の **ボタン** から、あなたの抽選結果を確認してください。",
        view=ResultView(),
    )

    # 即時ACK（考え中…を残さない）
    await interaction.response.send_message(" ", ephemeral=True, delete_after=1)


# =========================
# 運営用：抽選なし案内コマンド
# =========================
@bot.tree.command(
    name="lottery_send_invite",
    description="このスレッドに大会サーバー案内（招待リンク付き）を送信します（抽選なし）",
    guild=GUILD_OBJ,
)
async def lottery_send_invite(interaction: discord.Interaction):
    guard = _staff_guard(interaction)
    if guard:
        return await interaction.response.send_message(embed=guard)

    if not isinstance(interaction.channel, discord.Thread):
        return await interaction.response.send_message("このコマンドは参加者の個別スレッドで実行してください。")

    embed = discord.Embed(
        title="🎉 大会専用サーバーのご案内",
                description="""下のボタンから、招待リンクを受け取ってください。
（リンクは1回限り有効のURLです）

なお、今大会にご参加いただく方を対象に、
2/12（木）22:00〜
新しい画面共有システムのテストマッチ（2戦）を行います。

こちらは大会用サーバーでのカスタムキー案内となりますので、
招待リンク取得後は、お早めにご入室をお願いいたします。""",

        color=discord.Color.gold(),
    )

    await interaction.channel.send(embed=embed, view=InviteIssueView())

    # 即時ACK（考え中…を残さない）
    await interaction.response.send_message("送信しました。", ephemeral=True)

# =========================
# lifecycle
# =========================
@bot.event
async def on_ready():
    await bot.change_presence(status=discord.Status.online, activity=discord.Game("抽選待機中"))
    bot.add_view(ResultView())
    bot.add_view(LotteryPanelView())
    bot.add_view(ConfirmRedoView())
    await bot.tree.sync(guild=GUILD_OBJ)
    print("Lottery bot ready")


def main():
    token = os.getenv(TOKEN_ENV)
    if not token:
        raise RuntimeError(f"{TOKEN_ENV} not set")
    bot.run(token)


if __name__ == "__main__":
    main()
