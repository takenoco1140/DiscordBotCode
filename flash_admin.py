from __future__ import annotations

import io
import json
import os
import re
import sqlite3
import tempfile
import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands


# 固定URL（Cloudflare Tunnel）
ADMIN_URL_PC = "https://usually-rack-astronomy-flash.trycloudflare.com/admin"
ADMIN_URL_MOBILE = "https://usually-rack-astronomy-flash.trycloudflare.com/admin-m"

FLASH_ADMIN_BUILD = "2026-02-17-fix-namespace"

# 設定保存先（このファイルと同じ階層に data/ を作って保存）
_SETTINGS_DIR = Path(__file__).resolve().parent / "data"
_SETTINGS_PATH = _SETTINGS_DIR / "scrim_admin_settings.json"

# 時刻入力（24h HH:MM）
_TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


# JST（日付切り替え用）
_JST = ZoneInfo("Asia/Tokyo")


def _today_jst() -> str:
    return datetime.now(_JST).strftime("%Y-%m-%d")


def _has_today_scrim_excluding_tournament(guild_id: int | None = None) -> bool:
    """scrim_calendar.py のDB（scrim.db）に「本日分のスクリム」があるか判定する。

    判定条件：
    - 大会(kind='大会')は除外（kind='スクリム' のみ対象）
    - 登録しない(style='登録しない')は除外
    - 管理パネルで選択中スクリム（selected_scrim）が設定されている場合は、それと同名(title一致)のみ対象
      - selected_scrim が未設定/ default の場合は「本日のスクリムが1件でもあれば True」
    - 何らかの理由でDB参照に失敗した場合は False（安全側）とする
    """
    today = _today_jst()

    # 対象スクリム名（未設定なら None 扱い）
    selected_title: str | None = None
    try:
        if guild_id is not None:
            s = _get_selected_scrim(int(guild_id))
            if isinstance(s, str) and s.strip() and s.strip() != "default":
                selected_title = s.strip()
    except Exception:
        selected_title = None

    base_dir = Path(__file__).resolve().parent
    db_path = base_dir / "scrim.db"
    if not db_path.exists():
        return False

    try:
        db = sqlite3.connect(str(db_path))
        try:
            if selected_title:
                cur = db.execute(
                    """
                    SELECT 1
                    FROM events
                    WHERE date = ?
                      AND kind = 'スクリム'
                      AND title = ?
                      AND (style IS NULL OR style <> ?)
                    LIMIT 1
                    """,
                    (today, selected_title, "登録しない"),
                )
            else:
                cur = db.execute(
                    """
                    SELECT 1
                    FROM events
                    WHERE date = ?
                      AND kind = 'スクリム'
                      AND (style IS NULL OR style <> ?)
                    LIMIT 1
                    """,
                    (today, "登録しない"),
                )
            return cur.fetchone() is not None
        finally:
            try:
                db.close()
            except Exception:
                pass
    except Exception:
        return False



def _next_match_no(guild_id: int) -> str:
    """自動モード用：マッチ番号を日次で 01,02,... と自動インクリメントして返す。

    - 保存先は scrim_admin_settings.json（選択中スクリム配下）に保持
    - 日付が変わったら 01 にリセット
    - スクリム名ごとに独立したカウンタ
    """
    data = _load_settings()
    gid = str(guild_id)
    g = data.setdefault(gid, {})

    # 新形式へ整形
    scrims = g.setdefault("scrims", {"default": {}})
    sel = g.get("selected_scrim")
    if not isinstance(sel, str) or not sel.strip():
        sel = "default"
        g["selected_scrim"] = sel
    block = scrims.setdefault(sel, {})

    # 日別カウンタ（YYYY-MM-DD -> {"next": int}）
    counter = block.setdefault("match_counter", {})
    today = _today_jst()
    day = counter.setdefault(today, {})

    try:
        n_int = int(day.get("next", 1))
    except Exception:
        n_int = 1
    if n_int < 1:
        n_int = 1

    day["next"] = n_int + 1
    _save_settings(data)
    return f"{n_int:02d}"


def _set_manual_match_counter(guild_id: int, value: int) -> None:
    """手動入力された番号を次回の自動採番基準に反映する。

    例：手動で「03」を送った場合、同日の自動採番は次回「04」になる。
    """
    data = _load_settings()
    gid = str(guild_id)
    g = data.setdefault(gid, {})

    scrims = g.setdefault("scrims", {"default": {}})
    sel = g.get("selected_scrim")
    if not isinstance(sel, str) or not sel.strip():
        sel = "default"
        g["selected_scrim"] = sel
    block = scrims.setdefault(sel, {})

    counter = block.setdefault("match_counter", {})
    today = _today_jst()
    day = counter.setdefault(today, {})
    day["next"] = int(value) + 1
    _save_settings(data)

def _set_next_match_no(guild_id: int, value: int) -> None:
    """次回の自動採番（= 次Match開始番号）を直接設定する。

    例：次Matchを 05 から始めたい → value=5 を渡す（同日の次回自動採番が 05 になる）。
    """
    data = _load_settings()
    gid = str(guild_id)
    g = data.setdefault(gid, {})

    scrims = g.setdefault("scrims", {"default": {}})
    sel = g.get("selected_scrim")
    if not isinstance(sel, str) or not sel.strip():
        sel = "default"
        g["selected_scrim"] = sel
    block = scrims.setdefault(sel, {})

    counter = block.setdefault("match_counter", {})
    today = _today_jst()
    day = counter.setdefault(today, {})
    day["next"] = int(value)
    _save_settings(data)
def _default_individual_thread_name(guild_id: int) -> str:
    """個別スレッドのデフォルト名。
    selected_scrim + Match #NN（NN は自動採番）を使う。
    """
    scrim = _get_selected_scrim(guild_id)
    no = _next_match_no(guild_id)
    return f"{scrim} Match #{no}"



# ============
# Settings Keys
# ============
# autosend_channel_id: int
# autosend_time: str "HH:MM"
# keydrop_admin_channel_id: int
# keydrop_host_channel_id: int
# keydrop_view_channel_id: int
# keyhost_allowed_role_id: int
# keydrop_mode: str  ("auto" or "manual")
# end_message_text: str
# replay_submit_channel_id: int

# ----------------------------
# Flash panel namespace (avoid collision with /admin_panel settings)
# ----------------------------
_FLASH_KEY_PREFIX = "flash_"
_FLASH_KEYS = (
    "autosend_channel_id",
    "autosend_time",
    "keydrop_admin_channel_id",
    "keydrop_host_channel_id",
    "keydrop_view_channel_id",
    "keyhost_allowed_role_id",
    "keydrop_mode",
    "end_message_text",
    "replay_submit_channel_id",
)

def _flash_key(key: str) -> str:
    return f"{_FLASH_KEY_PREFIX}{key}"



def _is_admin(interaction: discord.Interaction) -> bool:
    """ボタン/モーダル側でも権限制御する（コマンド以外から叩ける可能性があるため）。"""
    if interaction.guild is None or interaction.user is None:
        return False
    perms = interaction.user.guild_permissions
    return bool(perms.administrator)


def _load_settings() -> dict:
    try:
        with _SETTINGS_PATH.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception:
        # 壊れていても落とさない（まずは空として扱う）
        return {}


def _save_settings(data: dict) -> None:
    _SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(prefix="scrim_admin_", suffix=".json", dir=str(_SETTINGS_DIR))
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, _SETTINGS_PATH)
    finally:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass



def _flash_migrate_namespace_once() -> None:
    """flash_admin の設定キー衝突を解消するための一回限りのマイグレーション。

    - scrim_admin と同じ settings ファイルを共有しているが、キー名が衝突すると
      /admin_panel と /flash_panel が同じ値を上書きしてしまう。
    - flash_admin 側は flash_* キーへ分離する。
    - 既存環境を壊さないため、初回だけ旧キー（未プレフィックス）を flash_* に COPY する。
      （scrim_admin 側の旧キーは残す）
    """
    try:
        data = _load_settings()
        changed = False

        for gid, g in list(data.items()):
            if not isinstance(g, dict):
                continue

            # すでに移行済みならスキップ
            if g.get("flash_namespace_migrated") is True:
                continue

            scrims = g.get("scrims")
            if not isinstance(scrims, dict):
                continue

            for sname, block in list(scrims.items()):
                if not isinstance(block, dict):
                    continue
                for k in _FLASH_KEYS:
                    fk = _flash_key(k)
                    if fk not in block and k in block:
                        block[fk] = block.get(k)
                        changed = True

            g["flash_namespace_migrated"] = True
            changed = True

        if changed:
            _save_settings(data)
    except Exception:
        # 失敗してもBotを落とさない
        pass


def _get_guild_container(guild_id: int) -> dict:
    """ギルド設定のコンテナを返す（旧形式は自動マイグレーション）。"""
    data = _load_settings()
    gid = str(guild_id)
    g = data.get(gid)
    if not isinstance(g, dict):
        g = {}
        data[gid] = g

    # 旧形式（ギルド直下に設定値が並んでいる）→ 新形式へマイグレーション
    if "scrims" not in g:
        old_keys = (
            "autosend_channel_id",
            "autosend_time",
            "keydrop_host_channel_id",
            "keydrop_view_channel_id",
            "keyhost_allowed_role_id",
            "keydrop_mode",
            "end_message_text",
        )
        default_block = {}
        for k in old_keys:
            if k in g:
                default_block[k] = g.pop(k)

        g["selected_scrim"] = g.get("selected_scrim") or "default"
        g["scrims"] = {"default": default_block}
        _save_settings(data)

    # 新形式の整形
    if not isinstance(g.get("scrims"), dict):
        g["scrims"] = {"default": {}}
        g["selected_scrim"] = g.get("selected_scrim") or "default"
        _save_settings(data)

    if not isinstance(g.get("selected_scrim"), str) or not g["selected_scrim"].strip():
        g["selected_scrim"] = "default"
        _save_settings(data)


    return g

# ----------------------------
# Flash-specific guild-level settings (not per-scrim)
# ----------------------------
def _get_guild_value(guild_id: int, key: str, default=None):
    data = _load_settings()
    g = data.get(str(guild_id))
    if isinstance(g, dict) and key in g:
        return g.get(key)
    return default


def _set_guild_value(guild_id: int, key: str, value) -> None:
    data = _load_settings()
    gid = str(guild_id)
    g = data.get(gid)
    if not isinstance(g, dict):
        g = {}
        data[gid] = g
    g[key] = value
    _save_settings(data)


def _get_flash_auto_start(guild_id: int) -> bool:
    v = _get_guild_value(guild_id, "flash_auto_start", False)
    return bool(v)


def _set_flash_auto_start(guild_id: int, enabled: bool) -> None:
    _set_guild_value(guild_id, "flash_auto_start", bool(enabled))


def _flash_auto_started_key(guild_id: int) -> str:
    scrim = _get_selected_scrim(guild_id)
    return f"{_today_jst()}|{scrim}"


def _is_flash_auto_started_today(guild_id: int) -> bool:
    d = _get_guild_value(guild_id, "flash_auto_started", {})
    if not isinstance(d, dict):
        return False
    return bool(d.get(_flash_auto_started_key(guild_id)))


def _mark_flash_auto_started_today(guild_id: int) -> None:
    d = _get_guild_value(guild_id, "flash_auto_started", {})
    if not isinstance(d, dict):
        d = {}
    d[_flash_auto_started_key(guild_id)] = True
    _set_guild_value(guild_id, "flash_auto_started", d)


def _get_flash_thresholds(guild_id: int) -> dict:
    d = _get_guild_value(guild_id, "flash_thresholds", {})
    return d if isinstance(d, dict) else {}


def _set_flash_thresholds(guild_id: int, thresholds: dict) -> None:
    _set_guild_value(guild_id, "flash_thresholds", thresholds if isinstance(thresholds, dict) else {})




def _get_selected_scrim(guild_id: int) -> str:
    g = _get_guild_container(guild_id)
    v = g.get("selected_scrim")
    if isinstance(v, str) and v.strip():
        return v.strip()
    return "default"


def _is_rotation_active(guild_id: int) -> bool:
    """flash(infinite_mode)が“稼働中”かを、settings内の rotation_messages で推定する。"""
    g = _get_guild_container(guild_id)
    rm = g.get("rotation_messages")
    if not isinstance(rm, dict):
        return False
    cid = rm.get("channel_id")
    mid = rm.get("message_id")
    return isinstance(cid, int) and isinstance(mid, int) and cid > 0 and mid > 0


async def _trigger_custom_key_send(interaction: discord.Interaction, match_no: str) -> None:
    """管理パネルの“合図”から、実際のキー画像送信（normal/infinite）を呼び出す。

    NOTE:
      - 本番では拡張機能として `modules.scrim_admin` のようにロードされる想定。
      - その場合、`normal_mode` は同一パッケージ配下（例: `modules.normal_mode`）にいるため、
        import は相対/絶対の両方に対応させる。
    """
    if interaction.guild is None:
        await interaction.response.send_message("サーバー内で実行してください。", ephemeral=True)
        return

    target_mod_basename = "infinite_mode" if _is_rotation_active(interaction.guild.id) else "normal_mode"

    def _import_handle(mod_basename: str):
        import importlib

        # 1) 同一パッケージ相対（modules配下でロードされるケース）
        pkg = __package__  # 例: "modules" / None
        if pkg:
            for name in (f"{pkg}.{mod_basename}",):
                try:
                    mod = importlib.import_module(name)
                    fn = getattr(mod, "handle_custom_key_send", None)
                    if callable(fn):
                        return fn
                except Exception:
                    pass

        # 2) ルート直下（開発時にPYTHONPATHへ通しているケース）
        try:
            mod = importlib.import_module(mod_basename)
            fn = getattr(mod, "handle_custom_key_send", None)
            if callable(fn):
                return fn
        except Exception:
            pass

        # 3) 互換: "modules.<name>" を直指定（pkgが取れないケース）
        try:
            mod = importlib.import_module(f"modules.{mod_basename}")
            fn = getattr(mod, "handle_custom_key_send", None)
            if callable(fn):
                return fn
        except Exception as e:
            raise e

        raise ModuleNotFoundError(mod_basename)

    try:
        _send_impl = _import_handle(target_mod_basename)
    except Exception as e:
        await interaction.response.send_message(f"送信処理の呼び出しに失敗しました: {e}", ephemeral=True)
        return

    # 選択中スクリム名を keydrop 側へ渡す（タイトル生成に使用）
    try:
        os.environ["KEYDROP_SCRIM_NAME"] = _get_selected_scrim(interaction.guild.id)
    except Exception:
        pass

    await _send_impl(interaction, match_no)


def _set_selected_scrim(guild_id: int, scrim_name: str) -> None:
    scrim_name = (scrim_name or "").strip()
    if not scrim_name:
        scrim_name = "default"

    data = _load_settings()
    gid = str(guild_id)
    g = data.get(gid)
    if not isinstance(g, dict):
        g = {}
        data[gid] = g

    # 確実に新形式へ
    if "scrims" not in g or not isinstance(g.get("scrims"), dict):
        g["scrims"] = {"default": {}}
    if scrim_name not in g["scrims"]:
        g["scrims"][scrim_name] = {}
    g["selected_scrim"] = scrim_name
    _save_settings(data)


def _list_scrims(guild_id: int) -> list[str]:
    g = _get_guild_container(guild_id)
    scrims = g.get("scrims")
    if isinstance(scrims, dict):
        names = [k for k in scrims.keys() if isinstance(k, str) and k.strip()]
        # default を先頭に
        names_sorted = sorted([n for n in names if n != "default"])
        if "default" in names:
            return ["default"] + names_sorted
        return names_sorted
    return ["default"]


def _get_scrim_block(guild_id: int) -> dict:
    g = _get_guild_container(guild_id)
    scrims = g.get("scrims")
    if not isinstance(scrims, dict):
        return {}
    sel = _get_selected_scrim(guild_id)
    b = scrims.get(sel)
    if not isinstance(b, dict):
        scrims[sel] = {}
        _save_settings(_load_settings())
        return {}
    return b


def _set_scrim_value(guild_id: int, key: str, value) -> None:
    data = _load_settings()
    gid = str(guild_id)
    g = data.get(gid)
    if not isinstance(g, dict):
        g = {}
        data[gid] = g
    # 新形式へ
    if "scrims" not in g or not isinstance(g.get("scrims"), dict):
        g["scrims"] = {"default": {}}
    sel = g.get("selected_scrim")
    if not isinstance(sel, str) or not sel.strip():
        sel = "default"
        g["selected_scrim"] = sel
    if sel not in g["scrims"] or not isinstance(g["scrims"].get(sel), dict):
        g["scrims"][sel] = {}
    g["scrims"][sel][_flash_key(key)] = value
    _save_settings(data)


def _get_int_setting(guild_id: int, key: str) -> int | None:
    g = _get_scrim_block(guild_id)
    v = g.get(_flash_key(key))
    if isinstance(v, int) and v > 0:
        return v
    if isinstance(v, str) and v.isdigit():
        iv = int(v)
        return iv if iv > 0 else None
    return None


def _get_str_setting(guild_id: int, key: str) -> str | None:
    g = _get_scrim_block(guild_id)
    v = g.get(_flash_key(key))
    if isinstance(v, str) and v.strip():
        return v
    return None


def _get_guild_autosend_time(guild_id: int) -> str | None:
    g = _get_scrim_block(guild_id)
    t = g.get(_flash_key("autosend_time"))
    if isinstance(t, str) and _TIME_RE.match(t):
        return t
    return None


def _get_guild_autosend_channel_id(guild_id: int) -> int | None:
    return _get_int_setting(guild_id, "autosend_channel_id")


def _get_guild_keyhost_role_id(guild_id: int) -> int | None:
    return _get_int_setting(guild_id, "keyhost_allowed_role_id")


def _get_keydrop_host_channel_id(guild_id: int) -> int | None:
    return _get_int_setting(guild_id, "keydrop_host_channel_id")


def _get_keydrop_admin_channel_id(guild_id: int) -> int | None:
    return _get_int_setting(guild_id, "keydrop_admin_channel_id")


def _get_keydrop_view_channel_id(guild_id: int) -> int | None:
    return _get_int_setting(guild_id, "keydrop_view_channel_id")


def _get_replay_submit_channel_id(guild_id: int) -> int | None:
    return _get_int_setting(guild_id, "replay_submit_channel_id")


def _get_keydrop_mode(guild_id: int) -> str:
    v = _get_str_setting(guild_id, "keydrop_mode")
    return v if v in ("auto", "manual") else "auto"


def _get_end_message_text(guild_id: int) -> str | None:
    return _get_str_setting(guild_id, "end_message_text")


def _channel_mention(guild: discord.Guild, channel_id: int | None) -> str:
    if not channel_id:
        return "未設定"
    ch = guild.get_channel(channel_id)
    if ch is None:
        # スレッドも対象（public/private/news thread）
        try:
            ch = guild.get_thread(channel_id)
        except Exception:
            ch = None
    if ch is None:
        return "未設定（存在しない / 権限不足）"
    return ch.mention


def _resolve_messageable(guild: discord.Guild, channel_id: int | None) -> discord.abc.Messageable | None:
    if not channel_id:
        return None
    ch = guild.get_channel(channel_id)
    if ch is None:
        try:
            ch = guild.get_thread(channel_id)
        except Exception:
            ch = None
    return ch  # TextChannel / Thread など（send があればOK）


def _role_mention(guild: discord.Guild, role_id: int | None) -> str:
    if not role_id:
        return "未設定"
    role = guild.get_role(role_id)
    if role is None:
        return "未設定（存在しない / 権限不足）"
    return role.mention


def _shorten(text: str, max_len: int = 120) -> str:
    t = (text or "").strip()
    if not t:
        return "未設定"
    if len(t) <= max_len:
        return t
    return t[: max_len - 1] + "…"


def _build_admin_view(guild: discord.Guild | None = None) -> "AdminPanelView":
    """ギルド設定に応じてボタン表示（ラベル/無効化）を切り替えたViewを返す。"""
    return AdminPanelView(guild=guild)


def _build_admin_embed(guild: discord.Guild) -> discord.Embed:
    t = _get_guild_autosend_time(guild.id)
    autosend_cid = _get_guild_autosend_channel_id(guild.id)

    keydrop_admin_cid = _get_keydrop_admin_channel_id(guild.id)
    keydrop_host_cid = _get_keydrop_host_channel_id(guild.id)
    keydrop_view_cid = _get_keydrop_view_channel_id(guild.id)

    replay_cid = _get_replay_submit_channel_id(guild.id)

    keyhost_rid = _get_guild_keyhost_role_id(guild.id)
    keydrop_mode = _get_keydrop_mode(guild.id)

    embed = discord.Embed(
        title="🛠️ Flash Scrim管理パネル",
        description="下のボタンから各設定・送信を実行します。\n\u200b",
        color=discord.Color.blurple(),
    )

    embed.add_field(
        name="🔗 登録URL",
        value=f"PC版：{ADMIN_URL_PC}\nスマホ版：{ADMIN_URL_MOBILE}",
        inline=False,
    )

    selected_scrim = _get_selected_scrim(guild.id)
    if selected_scrim and selected_scrim != "default":
        scrim_value = f"{selected_scrim}　※入力したスクリムに対してのみ有効です。"
    else:
        scrim_value = "スクリム名を入力してください。\n※入力したスクリムに対してのみ有効です。"

    embed.add_field(
        name="🔹 対象スクリム",
        value=scrim_value,
        inline=False,
    )
    embed.add_field(
        name="📢 スクリム案内",
        value=(
            f"送信先：{_channel_mention(guild, autosend_cid)}\n"
            f"自動案内時間：{f'`{t}`' if t else '未設定'}\n"
            "└毎日実行し、対象スクリムと同名のスクリムがある場合のみ送信します"
        ),
        inline=False,
    )
    embed.add_field(
        name="🔑 送信チャンネルの設定",
        value=(
            f"運営用：{_channel_mention(guild, keydrop_admin_cid)}\n"\
            f"配布方式　{keydrop_mode}（auto=自動 / manual=手動）"
        ),
        inline=False,
    )

    embed.add_field(
        name="👑 キーホスト募集",
        value=(
            f"ロール制限：{_role_mention(guild, keyhost_rid)}\n"
            "└このロールを持っている人が、キーホストに立候補できます"
        ),
        inline=False,
    )
    return embed





def _build_scrim_today_announce_content(guild: discord.Guild) -> str:
    """/scrim_today_one と同一の案内文生成に使う共通関数。
    - 管理パネルで設定した「対象スクリム（selected_scrim）」を必ず使用する
    """
    selected_scrim = _get_selected_scrim(guild.id)

    view_ch = _resolve_messageable(guild, _get_keydrop_view_channel_id(guild.id))
    view_mention = view_ch.mention if hasattr(view_ch, "mention") else "（未設定）"

    title = "📢 本日のスクリム案内"
    if selected_scrim and selected_scrim != "default":
        title += f"\n【{selected_scrim}】"

    return (
        f"{title}\n"
        f"🔗 登録URL：\n"
        f"PC：{ADMIN_URL_PC}\n"
        f"スマホ：{ADMIN_URL_MOBILE}\n"
        f"🔑 カスタムキーは {view_mention} にて案内します。"
    )


# ------------------------------------------------------------
# Compatibility shim:
# Some modules call `WaitingLineDoneView` without importing it.
# To prevent runtime NameError, provide a permissive fallback.
# (Lookup falls back to builtins when not found in module globals.)
# ------------------------------------------------------------
import builtins as _builtins

class WaitingLineDoneView(discord.ui.View):
    """互換用の空View（ボタン実装は呼び出し元モジュールに委譲）。
    - どんな引数で呼ばれても落ちないようにする
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(timeout=None)

# builtins にも注入して NameError を回避
try:
    if not hasattr(_builtins, "WaitingLineDoneView"):
        setattr(_builtins, "WaitingLineDoneView", WaitingLineDoneView)
except Exception:
    pass

class AutoSendTimeModal(discord.ui.Modal):
    title = "スクリム案内：時間設定"

    time_input: discord.ui.TextInput = discord.ui.TextInput(
        label="自動案内の時刻（24時間 HH:MM）",
        placeholder="例：17:00",
        required=True,
        max_length=5,
    )

    def __init__(self, *, panel_message: discord.Message | None = None) -> None:
        super().__init__(timeout=180)
        self._panel_message = panel_message

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("サーバー内で実行してください。", ephemeral=True)
            return
        if not _is_admin(interaction):
            await interaction.response.send_message("この操作は管理者のみ実行できます。", ephemeral=True)
            return

        value = (self.time_input.value or "").strip()
        if not _TIME_RE.match(value):
            await interaction.response.send_message(
                "時刻の形式が正しくありません。`HH:MM`（24時間、例：`17:00`）で入力してください。",
                ephemeral=True,
            )
            return

        _set_scrim_value(interaction.guild.id, "autosend_time", value)

        try:
            if self._panel_message is not None:
                await self._panel_message.edit(embed=_build_admin_embed(interaction.guild), view=_build_admin_view(interaction.guild))
        except Exception:
            pass

        await interaction.response.send_message(f"自動案内の時刻を `{value}` に設定しました。", ephemeral=True)


class ScrimAnnounceConfigView(discord.ui.View):
    """スクリム案内の設定ビュー（チャンネル選択 + 時間設定）。"""

    def __init__(self, *, panel_message: discord.Message | None = None) -> None:
        super().__init__(timeout=600)
        self._panel_message = panel_message

        self.add_item(
            discord.ui.ChannelSelect(
                placeholder="送信先チャンネルを選択",
                channel_types=[discord.ChannelType.text],
                min_values=1,
                max_values=1,
                custom_id="scrim_admin:autosend_channel_select",
            )
        )

        self.add_item(
            discord.ui.ChannelSelect(
                placeholder="運営用チャンネルを選択",
                channel_types=[
                    discord.ChannelType.text,
                    discord.ChannelType.public_thread,
                    discord.ChannelType.private_thread,
                    discord.ChannelType.news_thread,
                ],
                min_values=1,
                max_values=1,
                custom_id="scrim_admin:keydrop_admin_channel_select",
            )
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not _is_admin(interaction):
            await interaction.response.send_message("この操作は管理者のみ実行できます。", ephemeral=True)
            return False

        # ChannelSelect
        if interaction.data and interaction.data.get("component_type") == 8:
            if interaction.guild is None:
                await interaction.response.send_message("サーバー内で実行してください。", ephemeral=True)
                return False

            custom_id = interaction.data.get("custom_id")
            values = interaction.data.get("values") or []
            if not values:
                await interaction.response.send_message("チャンネルが選択されていません。", ephemeral=True)
                return False

            ch_id = int(values[0])
            if custom_id == "scrim_admin:autosend_channel_select":
                _set_scrim_value(interaction.guild.id, "autosend_channel_id", ch_id)
                msg = f"送信先チャンネルを {_channel_mention(interaction.guild, ch_id)} に設定しました。"
            elif custom_id == "scrim_admin:keydrop_admin_channel_select":
                _set_scrim_value(interaction.guild.id, "keydrop_admin_channel_id", ch_id)
                msg = f"運営用チャンネルを {_channel_mention(interaction.guild, ch_id)} に設定しました。"
            else:
                msg = "不明なチャンネル選択です。"

            try:
                if self._panel_message is not None:
                    await self._panel_message.edit(embed=_build_admin_embed(interaction.guild), view=_build_admin_view(interaction.guild))
            except Exception:
                pass

            await interaction.response.send_message(msg, ephemeral=True)
            return False

        return True

    @discord.ui.button(label="時間設定", style=discord.ButtonStyle.primary, custom_id="scrim_admin:autosend_time_modal", row=1)
    async def time_modal(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(AutoSendTimeModal(panel_message=self._panel_message))

    @discord.ui.button(label="閉じる", style=discord.ButtonStyle.secondary, custom_id="scrim_admin:autosend_close", row=4)
    async def close(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.edit_message(content="スクリム案内設定を閉じました。", view=None)


class KeydropChannelsConfigView(discord.ui.View):
    """キー配布CHの設定（キーホスト用 / 閲覧用）。"""

    def __init__(self, *, panel_message: discord.Message | None = None) -> None:
        super().__init__(timeout=600)
        self._panel_message = panel_message

        self.add_item(
            discord.ui.ChannelSelect(
                placeholder="運営用チャンネルを選択",
                channel_types=[
                    discord.ChannelType.text,
                    discord.ChannelType.public_thread,
                    discord.ChannelType.private_thread,
                    discord.ChannelType.news_thread,
                ],
                min_values=1,
                max_values=1,
                custom_id="scrim_admin:keydrop_admin_channel_select",
                row=0,
            )
        )

        self.add_item(
            discord.ui.ChannelSelect(
                placeholder="キーホスト用チャンネルを選択",
                channel_types=[
                    discord.ChannelType.text,
                    discord.ChannelType.public_thread,
                    discord.ChannelType.private_thread,
                    discord.ChannelType.news_thread,
                ],
                min_values=1,
                max_values=1,
                custom_id="scrim_admin:keydrop_host_channel_select",
                row=1,
            )
        )
        self.add_item(
            discord.ui.ChannelSelect(
                placeholder="閲覧用チャンネルを選択",
                channel_types=[
                    discord.ChannelType.text,
                    discord.ChannelType.public_thread,
                    discord.ChannelType.private_thread,
                    discord.ChannelType.news_thread,
                ],
                min_values=1,
                max_values=1,
                custom_id="scrim_admin:keydrop_view_channel_select",
                row=2,
            )
        )

        self.add_item(
            discord.ui.ChannelSelect(
                placeholder="リプレイデータ提出チャンネルを選択",
                channel_types=[
                    discord.ChannelType.text,
                    discord.ChannelType.public_thread,
                    discord.ChannelType.private_thread,
                    discord.ChannelType.news_thread,
                ],
                min_values=1,
                max_values=1,
                custom_id="scrim_admin:replay_submit_channel_select",
                row=3,
            )
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not _is_admin(interaction):
            await interaction.response.send_message("この操作は管理者のみ実行できます。", ephemeral=True)
            return False

        # ChannelSelect
        if interaction.data and interaction.data.get("component_type") == 8:
            if interaction.guild is None:
                await interaction.response.send_message("サーバー内で実行してください。", ephemeral=True)
                return False

            custom_id = interaction.data.get("custom_id")
            values = interaction.data.get("values") or []
            if not values:
                await interaction.response.send_message("チャンネルが選択されていません。", ephemeral=True)
                return False

            ch_id = int(values[0])

            if custom_id == "scrim_admin:keydrop_admin_channel_select":
                _set_scrim_value(interaction.guild.id, "keydrop_admin_channel_id", ch_id)
                msg = f"運営用チャンネルを {_channel_mention(interaction.guild, ch_id)} に設定しました。"
            elif custom_id == "scrim_admin:keydrop_host_channel_select":
                _set_scrim_value(interaction.guild.id, "keydrop_host_channel_id", ch_id)
                msg = f"キーホスト用チャンネルを {_channel_mention(interaction.guild, ch_id)} に設定しました。"
            elif custom_id == "scrim_admin:keydrop_view_channel_select":
                _set_scrim_value(interaction.guild.id, "keydrop_view_channel_id", ch_id)
                msg = f"閲覧用チャンネルを {_channel_mention(interaction.guild, ch_id)} に設定しました。"
            elif custom_id == "scrim_admin:replay_submit_channel_select":
                _set_scrim_value(interaction.guild.id, "replay_submit_channel_id", ch_id)
                msg = f"リプレイデータ提出チャンネルを {_channel_mention(interaction.guild, ch_id)} に設定しました。"
            else:
                msg = "不明なチャンネル選択です。"

            try:
                if self._panel_message is not None:
                    await self._panel_message.edit(embed=_build_admin_embed(interaction.guild), view=_build_admin_view(interaction.guild))
            except Exception:
                pass

            await interaction.response.send_message(msg, ephemeral=True)
            return False

        return True

    @discord.ui.button(label="閉じる", style=discord.ButtonStyle.secondary, custom_id="scrim_admin:keydrop_close", row=4)
    async def close(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.edit_message(content="送信CH設定を閉じました。", view=None)


def _member_has_role(member: discord.Member, role_id: int) -> bool:
    return any(r.id == role_id for r in member.roles)


class KeyhostRecruitView(discord.ui.View):
    """募集投稿につく応募ボタン（指定ロール保持者のみ押せる）。"""

    def __init__(self, *, allowed_role_id: int) -> None:
        super().__init__(timeout=None)
        self.allowed_role_id = allowed_role_id
        self.claimed_user_id: int | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("サーバー内で実行してください。", ephemeral=True)
            return False

        member: discord.Member = interaction.user
        if not _member_has_role(member, self.allowed_role_id) and not member.guild_permissions.administrator:
            await interaction.response.send_message("このボタンは指定ロール所持者のみ押せます。", ephemeral=True)
            return False
        return True

    def _disable_all(self) -> None:
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True

    @discord.ui.button(label="キーホストします", style=discord.ButtonStyle.success, custom_id="scrim_keyhost:apply")
    async def apply(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if self.claimed_user_id is not None:
            await interaction.response.send_message("すでに応募者が確定しています。", ephemeral=True)
            return

        self.claimed_user_id = interaction.user.id
        self._disable_all()

        content = interaction.message.content if interaction.message else ""
        content = content + f"\n\n✅ キーホスト：{interaction.user.mention}"
        try:
            await interaction.response.edit_message(content=content, view=self)
        except Exception:
            await interaction.response.send_message(f"✅ キーホスト：{interaction.user.mention}", ephemeral=True)

    @discord.ui.button(label="キャンセル", style=discord.ButtonStyle.danger, custom_id="scrim_keyhost:cancel")
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if self.claimed_user_id is None:
            self._disable_all()
            content = interaction.message.content if interaction.message else ""
            content = content + "\n\n❌ 募集はキャンセルされました。"
            try:
                await interaction.response.edit_message(content=content, view=self)
            except Exception:
                await interaction.response.send_message("❌ 募集はキャンセルされました。", ephemeral=True)
            return

        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("サーバー内で実行してください。", ephemeral=True)
            return
        member: discord.Member = interaction.user
        if interaction.user.id != self.claimed_user_id and not member.guild_permissions.administrator:
            await interaction.response.send_message("確定後のキャンセルは、確定した本人か管理者のみ可能です。", ephemeral=True)
            return

        self._disable_all()
        content = interaction.message.content if interaction.message else ""
        content = content + "\n\n❌ キーホスト確定がキャンセルされました。"
        try:
            await interaction.response.edit_message(content=content, view=self)
        except Exception:
            await interaction.response.send_message("❌ キーホスト確定がキャンセルされました。", ephemeral=True)


def _build_keyhost_recruit_message(role_mention: str) -> str:
    return (
        "🔸キーホスト募集\n"
        "本日のスクリムでキーホストをしていただける方を1名募集します。\n"
        "下記のボタンにて申請をしてください。\n"
        f"※ボタンは{role_mention}を持っている人だけが押せます"
    )


class KeyhostConfigView(discord.ui.View):
    """キーホスト募集の設定（ロール選択 + 送信）。"""

    def __init__(self, *, panel_message: discord.Message | None = None) -> None:
        super().__init__(timeout=600)
        self._panel_message = panel_message

        self.add_item(
            discord.ui.RoleSelect(
                placeholder="募集ボタンを押せるロールを選択",
                min_values=1,
                max_values=1,
                custom_id="scrim_admin:keyhost_role_select",
            )
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not _is_admin(interaction):
            await interaction.response.send_message("この操作は管理者のみ実行できます。", ephemeral=True)
            return False

        # RoleSelect
        if interaction.data and interaction.data.get("component_type") == 6:
            if interaction.guild is None:
                await interaction.response.send_message("サーバー内で実行してください。", ephemeral=True)
                return False

            values = interaction.data.get("values") or []
            if not values:
                await interaction.response.send_message("ロールが選択されていません。", ephemeral=True)
                return False

            role_id = int(values[0])
            _set_scrim_value(interaction.guild.id, "keyhost_allowed_role_id", role_id)

            try:
                if self._panel_message is not None:
                    await self._panel_message.edit(embed=_build_admin_embed(interaction.guild), view=_build_admin_view(interaction.guild))
            except Exception:
                pass

            await interaction.response.send_message(
                f"募集ボタンを押せるロールを {_role_mention(interaction.guild, role_id)} に設定しました。",
                ephemeral=True,
            )
            return False

        return True

    @discord.ui.button(label="送信", style=discord.ButtonStyle.success, custom_id="scrim_admin:keyhost_send", row=1)
    async def send(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("サーバー内で実行してください。", ephemeral=True)
            return

        cid = _get_guild_autosend_channel_id(interaction.guild.id)
        if not cid:
            await interaction.response.send_message(
                "送信先CHが未設定です。先に「スクリム案内設定」で送信先チャンネルを設定してください。",
                ephemeral=True,
            )
            return

        ch = interaction.guild.get_channel(cid)
        if ch is None or not isinstance(ch, discord.TextChannel):
            await interaction.response.send_message("送信先チャンネルが見つかりません。", ephemeral=True)
            return

        rid = _get_guild_keyhost_role_id(interaction.guild.id)
        if not rid:
            await interaction.response.send_message("募集ボタンを押せるロールが未設定です。先にロールを選択してください。", ephemeral=True)
            return

        role_mention = _role_mention(interaction.guild, rid)
        content = _build_keyhost_recruit_message(role_mention)

        try:
            await ch.send(content=content, view=KeyhostRecruitView(allowed_role_id=rid))
        except discord.Forbidden:
            await interaction.response.send_message("送信先チャンネルに送信する権限がありません。", ephemeral=True)
            return
        except Exception:
            await interaction.response.send_message("送信に失敗しました（不明なエラー）。", ephemeral=True)
            return

        await interaction.response.send_message(f"キーホスト募集を {ch.mention} に送信しました。", ephemeral=True)

    @discord.ui.button(label="閉じる", style=discord.ButtonStyle.secondary, custom_id="scrim_admin:keyhost_close", row=1)
    async def close(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.edit_message(content="キーホスト募集設定を閉じました。", view=None)



class KeyhostPermissionView(discord.ui.View):
    """キーホスト権限設定（ロール選択のみ）。"""

    def __init__(self, *, panel_message: discord.Message | None = None) -> None:
        super().__init__(timeout=600)
        self._panel_message = panel_message

        self.add_item(
            discord.ui.RoleSelect(
                placeholder="キーホスト募集ボタンを押せるロールを選択",
                min_values=1,
                max_values=1,
                custom_id="scrim_admin:keyhost_role_select",
            )
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not _is_admin(interaction):
            await interaction.response.send_message("この操作は管理者のみ実行できます。", ephemeral=True)
            return False

        # RoleSelect
        if interaction.data and interaction.data.get("component_type") == 6:
            if interaction.guild is None:
                await interaction.response.send_message("サーバー内で実行してください。", ephemeral=True)
                return False

            values = interaction.data.get("values") or []
            if not values:
                await interaction.response.send_message("ロールが選択されていません。", ephemeral=True)
                return False

            role_id = int(values[0])
            _set_scrim_value(interaction.guild.id, "keyhost_allowed_role_id", role_id)

            try:
                if self._panel_message is not None:
                    await self._panel_message.edit(embed=_build_admin_embed(interaction.guild), view=_build_admin_view(interaction.guild))
            except Exception:
                pass

            await interaction.response.send_message(
                f"キーホスト権限ロールを {_role_mention(interaction.guild, role_id)} に設定しました。",
                ephemeral=True,
            )
            return False

        return True

    @discord.ui.button(label="閉じる", style=discord.ButtonStyle.secondary, custom_id="scrim_admin:keyhost_perm_close", row=1)
    async def close(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.edit_message(content="キーホスト権限設定を閉じました。", view=None)

class EndMessageModal(discord.ui.Modal):
    title = "終了案内文の設定"

    text_input: discord.ui.TextInput = discord.ui.TextInput(
        label="終了案内文（送信する文章）",
        style=discord.TextStyle.paragraph,
        placeholder="例：本日のスクリムは終了しました。ご参加ありがとうございました！",
        required=True,
        max_length=1800,
    )

    def __init__(self, *, panel_message: discord.Message | None = None, initial: str | None = None) -> None:
        super().__init__(timeout=180)
        self._panel_message = panel_message
        if initial:
            try:
                self.text_input.default = initial[:1800]
            except Exception:
                pass

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("サーバー内で実行してください。", ephemeral=True)
            return
        if not _is_admin(interaction):
            await interaction.response.send_message("この操作は管理者のみ実行できます。", ephemeral=True)
            return

        text = (self.text_input.value or "").strip()
        if not text:
            await interaction.response.send_message("文章が空です。", ephemeral=True)
            return

        _set_scrim_value(interaction.guild.id, "end_message_text", text)

        try:
            if self._panel_message is not None:
                await self._panel_message.edit(embed=_build_admin_embed(interaction.guild), view=_build_admin_view(interaction.guild))
        except Exception:
            pass

        await interaction.response.send_message("終了案内文を保存しました。", ephemeral=True)


class CustomKeySendModal(discord.ui.Modal):
    title = "カスタムキー送信（手動）"

    match_no: discord.ui.TextInput = discord.ui.TextInput(
        label="何試合目か",
        placeholder="（未入力なら自動採番）",
        required=False,
        max_length=8,
    )

    def __init__(self, *, panel_message: discord.Message | None = None) -> None:
        super().__init__(timeout=180)
        self._panel_message = panel_message

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("サーバー内で実行してください。", ephemeral=True)
            return
        if not _is_admin(interaction):
            await interaction.response.send_message("この操作は管理者のみ実行できます。", ephemeral=True)
            return

        mode = _get_keydrop_mode(interaction.guild.id)
        if mode != "manual":
            await interaction.response.send_message("現在 `auto` です。手動送信は `manual` に切り替えてください。", ephemeral=True)
            return

        raw = (self.match_no.value or "").strip()
        if not raw:
            # 未入力なら自動採番（日付ごと＆スクリム名ごとに 01 から）
            match_no = _next_match_no(interaction.guild.id)
        else:
            # 数字のみ抽出（01 など対応）
            try:
                m = int(raw)
            except Exception:
                digits = re.sub(r"[^0-9]", "", raw)
                if not digits:
                    await interaction.response.send_message("試合番号は数字で入力してください（例：01）。", ephemeral=True)
                    return
                m = int(digits)
            if m < 1:
                await interaction.response.send_message("試合番号は 1 以上で入力してください。", ephemeral=True)
                return
            match_no = f"{m:02d}"
            _set_manual_match_counter(interaction.guild.id, m)


        # 管理パネル側は「合図」だけ。実際のキー画像送信は normal_mode / infinite_mode が担当。
        await _trigger_custom_key_send(interaction, match_no)


class FlashThresholdModal(discord.ui.Modal):
    title = "次Match募集開始の基準値"

    solo: discord.ui.TextInput = discord.ui.TextInput(
        label="ソロ：基準枠（残り枠がこの数以下で次Match募集）",
        placeholder="例：90（未設定は0）",
        required=True,
        max_length=4,
        default="0",
    )
    duo: discord.ui.TextInput = discord.ui.TextInput(
        label="デュオ：基準枠",
        placeholder="例：40（未設定は0）",
        required=True,
        max_length=4,
        default="0",
    )
    trio: discord.ui.TextInput = discord.ui.TextInput(
        label="トリオ：基準枠",
        placeholder="例：25（未設定は0）",
        required=True,
        max_length=4,
        default="0",
    )
    squad: discord.ui.TextInput = discord.ui.TextInput(
        label="スクワッド：基準枠",
        placeholder="例：20（未設定は0）",
        required=True,
        max_length=4,
        default="0",
    )

    def __init__(self, *, panel_message: discord.Message | None = None, initial: dict | None = None) -> None:
        super().__init__(timeout=180)
        self._panel_message = panel_message
        ini = initial if isinstance(initial, dict) else {}
        try:
            self.solo.default = str(ini.get("ソロ", 0))
            self.duo.default = str(ini.get("デュオ", 0))
            self.trio.default = str(ini.get("トリオ", 0))
            self.squad.default = str(ini.get("スクワッド", 0))
        except Exception:
            pass

    @staticmethod
    def _to_int(v: str) -> int | None:
        v = (v or "").strip()
        if not v:
            return None
        digits = re.sub(r"[^0-9]", "", v)
        if digits == "":
            return None
        try:
            return int(digits)
        except Exception:
            return None

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("サーバー内で実行してください。", ephemeral=True)
            return
        if not _is_admin(interaction):
            await interaction.response.send_message("この操作は管理者のみ実行できます。", ephemeral=True)
            return

        s = self._to_int(self.solo.value)
        d = self._to_int(self.duo.value)
        t = self._to_int(self.trio.value)
        q = self._to_int(self.squad.value)
        if s is None or d is None or t is None or q is None:
            await interaction.response.send_message("数値を入力してください（未設定は 0）。", ephemeral=True)
            return

        thresholds = {"ソロ": max(0, s), "デュオ": max(0, d), "トリオ": max(0, t), "スクワッド": max(0, q)}
        _set_flash_thresholds(interaction.guild.id, thresholds)

        try:
            if self._panel_message is not None:
                await self._panel_message.edit(embed=_build_admin_embed(interaction.guild), view=_build_admin_view(interaction.guild))
        except Exception:
            pass

        await interaction.response.send_message("基準値を保存しました。", ephemeral=True)


class InputScrimModal(discord.ui.Modal):
    title = "対象スクリム入力"

    name_input: discord.ui.TextInput = discord.ui.TextInput(
        label="スクリム名（入力）",
        placeholder="例：2/15 Aブロック",
        required=True,
        max_length=64,
    )

    def __init__(self, *, panel_message: discord.Message | None = None, initial: str | None = None) -> None:
        super().__init__(timeout=180)
        self._panel_message = panel_message
        if initial:
            try:
                self.name_input.default = initial[:64]
            except Exception:
                pass

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("サーバー内で実行してください。", ephemeral=True)
            return
        if not _is_admin(interaction):
            await interaction.response.send_message("この操作は管理者のみ実行できます。", ephemeral=True)
            return

        name = (self.name_input.value or "").strip()
        if not name:
            await interaction.response.send_message("スクリム名が空です。", ephemeral=True)
            return

        _set_selected_scrim(interaction.guild.id, name)

        # パネル更新
        try:
            if self._panel_message is not None:
                await self._panel_message.edit(embed=_build_admin_embed(interaction.guild), view=_build_admin_view(interaction.guild))
        except Exception:
            pass

        await interaction.response.send_message(f"対象スクリムを `{name}` に設定しました。", ephemeral=True)



class AdminPanelView(discord.ui.View):
    """管理パネル用のView。"""

    def __init__(self, *, guild: discord.Guild | None = None) -> None:
        super().__init__(timeout=None)
        # Flash 自動開始（スケジュール）状態表示
        try:
            auto_enabled = _get_flash_auto_start(guild.id) if guild is not None else False
            if hasattr(self, "auto_on") and isinstance(self.auto_on, discord.ui.Button):
                self.auto_on.label = ("✅" if auto_enabled else "") + "自動オン"
            if hasattr(self, "auto_off_start") and isinstance(self.auto_off_start, discord.ui.Button):
                self.auto_off_start.label = ("✅" if (not auto_enabled) else "") + "自動オフ（スクリム開始）"
        except Exception:
            pass

    async def _deny_if_not_admin(self, interaction: discord.Interaction) -> bool:
        if _is_admin(interaction):
            return True
        await interaction.response.send_message("この操作は管理者のみ実行できます。", ephemeral=True)
        return False

    # ======================
    # ROW0: 対象スクリム｜CH・時間設定｜案内手動送信
    # ======================
    @discord.ui.button(label="対象スクリム", style=discord.ButtonStyle.danger, custom_id="scrim_admin:open_scrim_select", row=0)
    async def open_scrim_select(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self._deny_if_not_admin(interaction):
            return
        if interaction.guild is None:
            await interaction.response.send_message("サーバー内で実行してください。", ephemeral=True)
            return

        panel_message = interaction.message if isinstance(interaction.message, discord.Message) else None
        current = _get_selected_scrim(interaction.guild.id)
        await interaction.response.send_modal(InputScrimModal(panel_message=panel_message, initial=current))

    @discord.ui.button(label="CH・時間設定", style=discord.ButtonStyle.secondary, custom_id="scrim_admin:open_announce_config", row=0)
    async def open_announce_config(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self._deny_if_not_admin(interaction):
            return
        panel_message = interaction.message if isinstance(interaction.message, discord.Message) else None
        await interaction.response.send_message(
            "CH・時間設定",
            ephemeral=True,
            view=ScrimAnnounceConfigView(panel_message=panel_message),
        )

    @discord.ui.button(label="案内手動送信", style=discord.ButtonStyle.success, custom_id="scrim_admin:send_announce_now", row=0)
    async def send_announce_now(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self._deny_if_not_admin(interaction):
            return

        if interaction.guild is None:
            await interaction.response.send_message("サーバー内で実行してください。", ephemeral=True)
            return

        # 当日のスクリムが無い場合は送信しない（大会は除外）
        if not _has_today_scrim_excluding_tournament(interaction.guild.id):
            await interaction.response.send_message("本日はスクリムがありません。", ephemeral=True)
            return

        # 画像生成が重いので、先に defer（実行者のみ表示）
        await interaction.response.defer(thinking=True, ephemeral=True)

        cid = _get_guild_autosend_channel_id(interaction.guild.id)
        if not cid:
            await interaction.followup.send(
                "送信先CHが未設定です。先に「CH・時間設定」で送信先を設定してください。",
                ephemeral=True,
            )
            return

        ch = _resolve_messageable(interaction.guild, cid)
        if ch is None:
            await interaction.followup.send("送信先チャンネルが見つかりません。", ephemeral=True)
            return

        # ボタン = /scrim_today_one スクリム名 と同じ挙動（画像投稿）
        scrim_name = (_get_selected_scrim(interaction.guild.id) or "").strip()
        if not scrim_name or scrim_name == "default":
            await interaction.followup.send("対象スクリム名が未設定です。先に「対象スクリム」で入力してください。", ephemeral=True)
            return

        try:
            try:
                from . import scrim_today as st  # type: ignore
            except Exception:
                import scrim_today as st  # type: ignore

            events = st.load_today_events(st._db_path())
            key = scrim_name.strip()
            picked = [e for e in events if key.casefold() in (e.title or "").casefold()]

            if not picked:
                await interaction.followup.send(f"本日の予定に「{scrim_name}」は見つかりませんでした。", ephemeral=True)
                return

            server_name = interaction.guild.name
            html = st.render_today_html(picked, server_name)
            png = await st.html_to_png_bytes_like_legacy(html)

            safe = "".join(c for c in key if c.isalnum() or c in ("-", "_"))[:24] or "one"
            filename = f"scrim_today_{datetime.now(_JST).strftime('%Y%m%d')}_{safe}.png"

            await ch.send(file=discord.File(fp=io.BytesIO(png), filename=filename))

            # flash なら 2通目（メッセージ①）を投稿（scrim_today 側と同じ）
            try:
                await st._maybe_post_rotation_message(ch, interaction.guild.id, picked)
            except Exception:
                pass

        except Exception as e:
            await interaction.followup.send(f"送信に失敗しました: {e}", ephemeral=True)
            return

        await interaction.followup.send(f"スクリム案内（画像）を {getattr(ch, 'mention', '指定CH')} に送信しました。", ephemeral=True)

    # ======================
    # ROW1: 自動オン｜自動オフ（スクリム開始）｜Match募集送信
    # ======================
    @discord.ui.button(label="自動オン", style=discord.ButtonStyle.primary, custom_id="scrim_admin:auto_on", row=1)
    async def auto_on(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self._deny_if_not_admin(interaction):
            return
        if interaction.guild is None:
            await interaction.response.send_message("サーバー内で実行してください。", ephemeral=True)
            return

        _set_flash_auto_start(interaction.guild.id, True)

        # パネル更新（✅表示）
        try:
            if isinstance(interaction.message, discord.Message):
                await interaction.message.edit(embed=_build_admin_embed(interaction.guild), view=_build_admin_view(interaction.guild))
        except Exception:
            pass

        await interaction.response.send_message("自動開始を ON にしました。", ephemeral=True)

    @discord.ui.button(label="自動オフ（スクリム開始）", style=discord.ButtonStyle.secondary, custom_id="scrim_admin:auto_off_start", row=1)
    async def auto_off_start(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self._deny_if_not_admin(interaction):
            return
        if interaction.guild is None:
            await interaction.response.send_message("サーバー内で実行してください。", ephemeral=True)
            return

        # 当日のスクリムが無い場合は開始しない（大会は除外）
        if not _has_today_scrim_excluding_tournament(interaction.guild.id):
            await interaction.response.send_message("本日はスクリムがありません。", ephemeral=True)
            return

        # 自動開始をOFF（手動開始）
        _set_flash_auto_start(interaction.guild.id, False)
        _mark_flash_auto_started_today(interaction.guild.id)

        # パネル更新（✅表示）
        try:
            if isinstance(interaction.message, discord.Message):
                await interaction.message.edit(embed=_build_admin_embed(interaction.guild), view=_build_admin_view(interaction.guild))
        except Exception:
            pass

        # 手動開始：Match #NN を開始（未入力扱いで自動採番）
        match_no = _next_match_no(interaction.guild.id)
        await _trigger_custom_key_send(interaction, match_no)

    @discord.ui.button(label="Match募集送信", style=discord.ButtonStyle.success, custom_id="scrim_admin:send_match_recruit", row=1)
    async def send_match_recruit(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self._deny_if_not_admin(interaction):
            return

        if interaction.guild is None:
            await interaction.response.send_message("サーバー内で実行してください。", ephemeral=True)
            return

        # 当日のスクリムが無い場合は送信しない（大会は除外）
        if not _has_today_scrim_excluding_tournament(interaction.guild.id):
            await interaction.response.send_message("本日はスクリムがありません。", ephemeral=True)
            return

        cid = _get_guild_autosend_channel_id(interaction.guild.id)
        if not cid:
            await interaction.response.send_message(
                "送信先CHが未設定です。先に「CH・時間設定」で送信先チャンネルを設定してください。",
                ephemeral=True,
            )
            return

        ch = _resolve_messageable(interaction.guild, cid)
        if ch is None:
            await interaction.response.send_message("送信先チャンネルが見つかりません。", ephemeral=True)
            return

        scrim_name = (_get_selected_scrim(interaction.guild.id) or "").strip()
        if not scrim_name or scrim_name == "default":
            await interaction.response.send_message("対象スクリム名が未設定です。先に「対象スクリム」で入力してください。", ephemeral=True)
            return

        await interaction.response.defer(thinking=True, ephemeral=True)

        # 本日のイベントを拾って mode_flash 側へ渡す
        try:
            try:
                from . import scrim_today as st  # type: ignore
            except Exception:
                import scrim_today as st  # type: ignore

            events = st.load_today_events(st._db_path())
            key = scrim_name.strip()
            picked = [e for e in events if key.casefold() in (e.title or "").casefold()]
            if not picked:
                await interaction.followup.send(f"本日の予定に「{scrim_name}」は見つかりませんでした。", ephemeral=True)
                return

            # mode_flash（flash）へ送信
            try:
                from . import mode_flash as mf  # type: ignore
            except Exception:
                try:
                    import mode_flash as mf  # type: ignore
                except Exception:
                    import modules.mode_flash as mf  # type: ignore

            await mf.maybe_post_rotation_message(ch, interaction.guild.id, picked)
        except Exception as e:
            await interaction.followup.send(f"Match募集の送信に失敗しました: {e}", ephemeral=True)
            return

        await interaction.followup.send("Match募集を送信しました。", ephemeral=True)

    # ======================
    # ROW2: キーホスト権限設定｜リプレイデータ提出
    # ======================
    # ======================
    @discord.ui.button(label="キーホスト権限設定", style=discord.ButtonStyle.danger, custom_id="scrim_admin:open_keyhost_perm", row=2)
    async def open_keyhost_perm(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self._deny_if_not_admin(interaction):
            return
        panel_message = interaction.message if isinstance(interaction.message, discord.Message) else None
        await interaction.response.send_message(
            "キーホスト権限設定",
            ephemeral=True,
            view=KeyhostPermissionView(panel_message=panel_message),
        )

    @discord.ui.button(label="リプレイデータ提出", style=discord.ButtonStyle.success, custom_id="scrim_admin:replay_request", row=2)
    async def replay_request(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self._deny_if_not_admin(interaction):
            return
        if interaction.guild is None:
            await interaction.response.send_message("サーバー内で実行してください。", ephemeral=True)
            return

        panel_message = interaction.message if isinstance(interaction.message, discord.Message) else None
        await interaction.response.send_modal(ReplayRequestModal(panel_message=panel_message))

    # ======================
    # ROW3: 次Match募集開始の基準値
    # ======================
    @discord.ui.button(label="次Match募集開始の基準値", style=discord.ButtonStyle.secondary, custom_id="scrim_admin:set_next_match_base", row=3)
    async def set_next_match_base(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self._deny_if_not_admin(interaction):
            return
        if interaction.guild is None:
            await interaction.response.send_message("サーバー内で実行してください。", ephemeral=True)
            return

        panel_message = interaction.message if isinstance(interaction.message, discord.Message) else None
        await interaction.response.send_modal(FlashThresholdModal(panel_message=panel_message, initial=_get_flash_thresholds(interaction.guild.id)))


class ThreadInviteSelectView(discord.ui.View):
    def __init__(self, thread: discord.Thread):
        super().__init__(timeout=180)
        self.thread = thread
        # UserSelect からは discord.User / discord.Member の両方が来る可能性があるため、
        # 後段で Member 解決できるように user_id で保持する。
        self.selected_user_ids: list[int] = []

        # discord.py では decorator の user_select が無い版があるため、
        # UserSelect を動的に add_item する方式で実装する。
        self._user_select_item: discord.ui.UserSelect = discord.ui.UserSelect(
            placeholder="招待するユーザーを選択（最大25人）",
            min_values=1,
            max_values=25,
        )
        self._user_select_item.callback = self._on_user_select  # type: ignore
        self.add_item(self._user_select_item)

    async def _on_user_select(self, interaction: discord.Interaction):
        users = list(getattr(self._user_select_item, "values", []) or [])
        self.selected_user_ids = [u.id for u in users if getattr(u, "id", None)]

        def _disp(u) -> str:
            if isinstance(u, discord.Member):
                return u.display_name
            return getattr(u, "name", None) or getattr(u, "global_name", None) or str(u)

        names = ", ".join(_disp(u) for u in users) if users else "（なし）"
        await interaction.response.send_message(f"選択: {names}\n下の「招待する」を押してください。", ephemeral=True)

    @discord.ui.button(label="招待する", style=discord.ButtonStyle.primary)
    async def invite_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not _is_admin(interaction):
            await interaction.response.send_message("この操作は管理者のみ実行できます。", ephemeral=True)
            return

        if not isinstance(interaction.channel, discord.Thread) or interaction.channel.id != self.thread.id:
            await interaction.response.send_message("このコマンドは招待したいスレッド内で実行してください。", ephemeral=True)
            return

        if not self.selected_user_ids:
            await interaction.response.send_message("招待するユーザーを選択してください。", ephemeral=True)
            return

        if interaction.guild is None:
            await interaction.response.send_message("サーバー内で実行してください。", ephemeral=True)
            return

        ok = 0
        ng: list[str] = []
        for uid in self.selected_user_ids:
            try:
                m = interaction.guild.get_member(uid)
                if m is None:
                    try:
                        m = await interaction.guild.fetch_member(uid)
                    except Exception:
                        m = None

                if m is None:
                    ng.append(str(uid))
                    continue

                await self.thread.add_user(m)
                ok += 1
            except Exception:
                # 可能なら表示名
                m2 = interaction.guild.get_member(uid)
                ng.append(m2.display_name if m2 else str(uid))

        msg = f"招待しました: {ok}人"
        if ng:
            msg += f"（失敗: {', '.join(ng)}）"
        await interaction.response.send_message(msg, ephemeral=True)

    @discord.ui.button(label="閉じる", style=discord.ButtonStyle.secondary)
    async def close_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await interaction.message.delete()
        except Exception:
            pass
        await interaction.response.send_message("閉じました。", ephemeral=True)

class FlashAdmin(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._guild_sync_done = False  # guild-scoped sync for instant command visibility
        self._flash_auto_task: asyncio.Task | None = None


    @commands.Cog.listener()
    async def on_ready(self) -> None:
        """起動完了時の処理。

        /flash_panel が2つ表示される原因の多くは、過去に **Guild Sync** された同名コマンドが残っていることです。
        現在の運用は scrim_keydrop_bot 側で **GLOBAL sync** を行う前提のため、ここでは **残存する Guild コマンドを削除** します。

        ※ここで copy_global_to / guild sync を行うと、GLOBAL と GUILD の両方に同名コマンドが登録され、二重表示になります。
        """
        if self._guild_sync_done:
            return
        self._guild_sync_done = True

        # 以前の Guild Sync による残骸を消して二重表示を防ぐ（GLOBAL だけに統一）
        try:
            tree = getattr(self.bot, "tree", None)
            if tree is not None and getattr(self.bot, "guilds", None):
                for g in list(self.bot.guilds):
                    try:
                        tree.clear_commands(guild=g)  # type: ignore[arg-type]
                        await tree.sync(guild=g)  # guild 側から削除を反映
                    except Exception:
                        # ギルド単位の削除に失敗しても起動自体は継続する
                        continue
        except Exception:
            pass

        # Flash: 自動開始（スケジュール）ループ起動（1回だけ）
        try:
            if self._flash_auto_task is None or self._flash_auto_task.done():
                self._flash_auto_task = asyncio.create_task(self._flash_auto_start_loop())
        except Exception:
            pass

        return
    @app_commands.command(name="flash_panel", description="Flash用 運営管理パネルを表示します")
    @app_commands.default_permissions(administrator=True)
    async def flash_panel(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("サーバー内で実行してください。", ephemeral=True)
            return

        embed = _build_admin_embed(interaction.guild)
        await interaction.response.send_message(embed=embed, view=_build_admin_view(interaction.guild))

    

async def setup(bot: commands.Bot) -> None:
    _flash_migrate_namespace_once()
    try:
        import logging
        logging.getLogger("scrim_keydrop_bot").info(f"flash_admin build: {FLASH_ADMIN_BUILD}")
    except Exception:
        pass
    await bot.add_cog(FlashAdmin(bot))
    bot.add_view(AdminPanelView())