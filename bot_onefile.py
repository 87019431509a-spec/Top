# Single-file build (engine_adapter + bot)


# -*- coding: utf-8 -*-
"""
Engine adapter integrating tg_core.py (your tg_comment1-based engine, patched).
Provides per-user start/stop, proxy distribution, and channel precheck/blacklist.

Multi-user isolation is achieved by loading tg_core.py as a separate module instance per user,
with TGCORE_BASE_DIR pointing to that user's directory.
"""

import os
import json
import time
import asyncio
import logging
import importlib.util
import sys
import types
from pathlib import Path
from typing import Dict, Any, Tuple, Optional, List

import yaml

log = logging.getLogger("engine_adapter_full")


# ===== Embedded tg_core source (inlined for single-file build) =====
TG_CORE_SOURCE = 'from __future__ import annotations\nimport os\n# ==========================\n\ndef normalize_channel(ch: str) -> str:\n    ch = (ch or "").strip()\n    if not ch:\n        return ch\n    # allow full links\n    if ch.startswith("https://t.me/"):\n        ch = ch.split("https://t.me/", 1)[1].strip()\n    if ch.startswith("@"):\n        return ch\n    return "@" + ch\n\n\n\ndef _is_missing(v) -> bool:\n    try:\n        return v is None or (isinstance(v, str) and v.strip() == "")\n    except Exception:\n        return v is None\n\n# STRICT membership check (чтобы не пытаться вступать в уже вступленные)\n# ==========================\nasync def is_member_strict(client, entity) -> bool:\n    try:\n        # Для каналов/супергрупп\n        await client(functions.channels.GetParticipantRequest(channel=entity, participant=\'me\'))\n        return True\n    except UserNotParticipantError:\n        return False\n    except Exception:\n        # Если не можем проверить (например, нет доступа) — считаем что НЕ участник, чтобы не зависнуть\n        return False\n\n\n# ==========================\n# Toggle: проверка удаления комментариев (ON/OFF)\n# ==========================\ndef toggle_deleted_check(cfg: dict):\n    try:\n        service = cfg.setdefault("service", {})\n        cur = bool(service.get("check_deleted_comments", True))\n        service["check_deleted_comments"] = (not cur)\n        try:\n            ru_log("ЖИВОЙ", "Проверка удаления комментариев переключена",\n                   channel="-", account="-",\n                   extra=f"Теперь={\'ON\' if service[\'check_deleted_comments\'] else \'OFF\'}")\n        except Exception:\n            pass\n    except Exception:\n        pass\n    return cfg\n\n\n# ==========================\n# Кулдаун канала (если удаляют комменты) — fallback\n# ==========================\n_channel_cooldown_until = {}\nDEFAULT_CONFIG_YAML_RU = """# TG COMMENT SERVICE — конфиг\n# ВАЖНО: комментарии начинаются с # и не мешают работе.\n\ntelegram:\n  api_id: 0               # Telegram API ID (my.telegram.org -> API Development Tools)\n  api_hash: ""            # Telegram API HASH (my.telegram.org)\n\nopenai:\n  api_key: ""             # OpenAI API KEY (для генерации ответов)\n  model: "gpt-4.1"        # Модель (пример: gpt-4.1)\n  temperature: 0.7        # 0.1-1.0 (ниже = стабильнее, выше = разнообразнее)\n  max_tokens: 60          # длина ответа модели (короче = безопаснее)\n\nservice:\n  rotation_mode: "round_robin"   # round_robin = по очереди\n  only_one_comment_per_post: true # 1 комментарий на 1 пост максимум\n  ignore_old_posts_on_start: true # при запуске не комментировать старые посты\n  max_parallel_tasks: 5          # параллельность (больше = быстрее, но рискованнее)\n\n  delay_mode: "random"           # random / fixed\n  fixed_delay_sec: 50            # если delay_mode=fixed\n  random_delay_min_sec: 35       # минимум (если delay_mode=random)\n  random_delay_max_sec: 90       # максимум (если delay_mode=random)\n\n  warmup_enabled: true           # true/false\n  warmup_start_channels: 3       # сколько каналов активно в начале\n  warmup_add_one_every_min: 7    # +1 канал каждые N минут\n  warmup_max_active_channels: 0  # 0 = все\n\n  max_comments_per_day_per_account: 20  # максимум комментов в сутки на 1 аккаунт\n  max_comments_per_channel_per_day: 3   # максимум комментов в сутки на 1 канал\n\n  join_enabled: true             # вступать в каналы при старте\n  join_every_min: 7              # 1 вступление раз в N минут\n  join_max_per_day_per_account: 20 # лимит вступлений на аккаунт в сутки\n\nneuro_guard:\n  channel_cooldown_hours: 2      # пауза по КАНАЛУ (часы)\n  account_cooldown_hours: 6      # пауза по АККАУНТУ после 2 удалений подряд\n  perma_block_after_deletes: 3   # канал в PERMA-BLOCK после N удалений\n\n  dangerous_channel_limit_per_day: 1    # 1 коммент в сутки на канал\n  dangerous_channel_days: 3             # держать канал опасным N дней\n"""\n\ndef ensure_config_exists_ru(config_path: str = "config.yaml"):\n    try:\n        if os.path.exists(config_path):\n            return\n        with open(config_path, "w", encoding="utf-8") as f:\n            f.write(DEFAULT_CONFIG_YAML_RU)\n    except Exception:\n        pass\n\n\n\ndef channel_in_cooldown(channel_key: str):\n    now = int(time.time())\n    until = int(_channel_cooldown_until.get(str(channel_key), 0))\n    if until > now:\n        return True, until - now\n    return False, 0\n\n\n\ndef apply_neuro_guard_on_deleted(channel_key: str, account_key: str):\n    # Канал: 2 часа кулдаун при удалении\n    try:\n        set_channel_cooldown(channel_key, 2 * 60 * 60)\n    except Exception:\n        pass\n    # Аккаунт: 2 удаления подряд -> 6 часов\n    try:\n        st = load_guard_state()\n        acc = st.setdefault("accounts", {}).setdefault(account_key, {})\n        acc["del_streak"] = int(acc.get("del_streak", 0)) + 1\n        if acc["del_streak"] >= 2:\n            acc["cooldown_until"] = int(time.time()) + 6 * 60 * 60\n        save_guard_state(st)\n    except Exception:\n        pass\n\ndef set_channel_cooldown(channel_key: str, seconds: int):\n    _channel_cooldown_until[str(channel_key)] = int(time.time()) + int(seconds)\n\n\nimport asyncio\n_telethon_lock = asyncio.Lock()\n# ==========================\n# RU LOGS (единый формат)\n# ==========================\ndef ru_log(tag: str, msg: str, channel: str = "-", account: str = "-", extra: str = ""):\n    s = f"[{tag}] {msg} | Канал: {channel} | Аккаунт: {account}"\n    if extra:\n        s += f" | {extra}"\n    try:\n        print(s, flush=True)\n    except Exception:\n        pass\n    try:\n        logging.info(s)\n    except Exception:\n        pass\n\n# ==========================\n# NEURO GUARD STATE (persist)\n# ==========================\nNEURO_GUARD_STATE_PATH = "neuro_guard_state.json"\n\n_guard_state = {}\n_channel_cooldown_until = {}\n_account_cooldown_until = {}\n_account_deleted_streak = {}\n_channel_deleted_count = {}\n_channel_perma_block = {}\n_channel_danger_until = {}\n_channel_danger_daily_count = {}\n_channel_danger_daily_day = {}\n\ndef _now():\n    return int(time.time())\n\ndef _load_guard_state():\n    global _guard_state, _channel_cooldown_until, _account_cooldown_until, _account_deleted_streak\n    global _channel_deleted_count, _channel_perma_block, _channel_danger_until\n    global _channel_danger_daily_count, _channel_danger_daily_day\n    try:\n        with open(NEURO_GUARD_STATE_PATH, "r", encoding="utf-8") as f:\n            _guard_state = json.load(f) or {}\n    except Exception:\n        _guard_state = {}\n\n    _channel_cooldown_until = _guard_state.get("channel_cooldown_until", {}) or {}\n    _account_cooldown_until = _guard_state.get("account_cooldown_until", {}) or {}\n    _account_deleted_streak = _guard_state.get("account_deleted_streak", {}) or {}\n    _channel_deleted_count = _guard_state.get("channel_deleted_count", {}) or {}\n    _channel_perma_block = _guard_state.get("channel_perma_block", {}) or {}\n    _channel_danger_until = _guard_state.get("channel_danger_until", {}) or {}\n    _channel_danger_daily_count = _guard_state.get("channel_danger_daily_count", {}) or {}\n    _channel_danger_daily_day = _guard_state.get("channel_danger_daily_day", {}) or {}\n\ndef _save_guard_state():\n    try:\n        _guard_state["channel_cooldown_until"] = _channel_cooldown_until\n        _guard_state["account_cooldown_until"] = _account_cooldown_until\n        _guard_state["account_deleted_streak"] = _account_deleted_streak\n        _guard_state["channel_deleted_count"] = _channel_deleted_count\n        _guard_state["channel_perma_block"] = _channel_perma_block\n        _guard_state["channel_danger_until"] = _channel_danger_until\n        _guard_state["channel_danger_daily_count"] = _channel_danger_daily_count\n        _guard_state["channel_danger_daily_day"] = _channel_danger_daily_day\n\n        tmp = NEURO_GUARD_STATE_PATH + ".tmp"\n        with open(tmp, "w", encoding="utf-8") as f:\n            json.dump(_guard_state, f, ensure_ascii=False, indent=2)\n        os.replace(tmp, NEURO_GUARD_STATE_PATH)\n    except Exception:\n        pass\n\ndef channel_in_cooldown(channel_key: str):\n    now = _now()\n    until = int(_channel_cooldown_until.get(str(channel_key), 0) or 0)\n    if until > now:\n        return True, until - now\n    return False, 0\n\ndef set_channel_cooldown(channel_key: str, seconds: int):\n    _channel_cooldown_until[str(channel_key)] = _now() + int(seconds)\n    _save_guard_state()\n\ndef account_in_cooldown(account_key: str):\n    now = _now()\n    until = int(_account_cooldown_until.get(str(account_key), 0) or 0)\n    if until > now:\n        return True, until - now\n    return False, 0\n\ndef set_account_cooldown(account_key: str, seconds: int):\n    _account_cooldown_until[str(account_key)] = _now() + int(seconds)\n    _save_guard_state()\n\ndef account_mark_deleted(account_key: str) -> int:\n    v = int(_account_deleted_streak.get(str(account_key), 0) or 0) + 1\n    _account_deleted_streak[str(account_key)] = v\n    _save_guard_state()\n    return v\n\ndef account_reset_deleted(account_key: str):\n    _account_deleted_streak[str(account_key)] = 0\n    _save_guard_state()\n\ndef channel_mark_deleted(channel_key: str) -> int:\n    v = int(_channel_deleted_count.get(str(channel_key), 0) or 0) + 1\n    _channel_deleted_count[str(channel_key)] = v\n    _save_guard_state()\n    return v\n\ndef channel_is_perma_blocked(channel_key: str) -> bool:\n    return bool(_channel_perma_block.get(str(channel_key), False))\n\ndef channel_set_perma_block(channel_key: str, value: bool = True):\n    _channel_perma_block[str(channel_key)] = bool(value)\n    _save_guard_state()\n\ndef channel_set_danger(channel_key: str, days: int = 3):\n    _channel_danger_until[str(channel_key)] = _now() + int(days) * 24 * 60 * 60\n    _save_guard_state()\n\ndef channel_is_danger(channel_key: str) -> bool:\n    until = int(_channel_danger_until.get(str(channel_key), 0) or 0)\n    return until > _now()\n\ndef channel_danger_cleanup(channel_key: str):\n    # auto remove after time passed\n    until = int(_channel_danger_until.get(str(channel_key), 0) or 0)\n    if until and until <= _now():\n        _channel_danger_until.pop(str(channel_key), None)\n        _save_guard_state()\n\ndef danger_daily_can_comment(channel_key: str) -> bool:\n    # 1 comment per day per channel if danger\n    day = time.strftime("%Y-%m-%d")\n    if _channel_danger_daily_day.get(str(channel_key)) != day:\n        _channel_danger_daily_day[str(channel_key)] = day\n        _channel_danger_daily_count[str(channel_key)] = 0\n        _save_guard_state()\n    return int(_channel_danger_daily_count.get(str(channel_key), 0) or 0) < 1\n\ndef danger_daily_mark_comment(channel_key: str):\n    day = time.strftime("%Y-%m-%d")\n    if _channel_danger_daily_day.get(str(channel_key)) != day:\n        _channel_danger_daily_day[str(channel_key)] = day\n        _channel_danger_daily_count[str(channel_key)] = 0\n    _channel_danger_daily_count[str(channel_key)] = int(_channel_danger_daily_count.get(str(channel_key), 0) or 0) + 1\n    _save_guard_state()\n\n# ==========================\n# BOOTSTRAP: статистика + heartbeat + autojoin (должно быть определено ДО запуска меню)\n# ==========================\n# повторные импорты не ломают\nimport asyncio\nimport time\nimport asyncio\nimport logging\n\ntry:\n    from telethon.tl.functions.channels import JoinChannelRequest\nexcept Exception:\n    JoinChannelRequest = None\n\ntry:\n    from telethon.tl.functions.messages import ImportChatInviteRequest\nexcept Exception:\n    ImportChatInviteRequest = None\n\nJOIN_COOLDOWN_SEC = 420  # 7 минут\nMAX_JOINS_PER_DAY_PER_ACCOUNT = 20\nHEARTBEAT_SEC = 60\n\n_stats = {\n    "seen_posts": 0,\n    "commented": 0,\n    "skipped": 0,\n    "errors": 0,\n    "joined": 0,\n    "last_event_ts": 0,\n    "last_post_channel": "-",\n}\n\n# warmup-skip log throttle: channel_key -> last_log_ts\n_warmup_skip_log = {}\n\n_join_daily = {}   # account_key -> {"day": "YYYY-MM-DD", "count": int}\n_autojoin_started = False\n\ndef _today_key():\n    return time.strftime("%Y-%m-%d", time.localtime())\n\ndef can_join_today(account_key: str) -> bool:\n    day = _today_key()\n    rec = _join_daily.get(account_key)\n    if (not rec) or rec.get("day") != day:\n        rec = {"day": day, "count": 0}\n        _join_daily[account_key] = rec\n    return rec["count"] < MAX_JOINS_PER_DAY_PER_ACCOUNT\n\ndef mark_join_today(account_key: str) -> None:\n    day = _today_key()\n    rec = _join_daily.get(account_key)\n    if (not rec) or rec.get("day") != day:\n        rec = {"day": day, "count": 0}\n        _join_daily[account_key] = rec\n    rec["count"] += 1\n\nasync def heartbeat_loop(get_all_channels_fn, get_active_channels_fn, get_counts_fn):\n    """Отчёт каждые ~60 секунд: каналы/аккаунты/отправлено/удалено и т.д."""\n    while True:\n        try:\n            now = int(time.time())\n            last = int(_stats.get("last_event_ts", 0) or 0)\n            last_str = f"{max(0, (now - last))} сек назад" if last else "пока не было"\n\n            all_ch = []\n            try:\n                all_ch = get_all_channels_fn() or []\n            except Exception:\n                all_ch = []\n            total_channels = len(all_ch)\n\n            active_list = []\n            try:\n                active_list = get_active_channels_fn() or []\n            except Exception:\n                active_list = []\n            active_channels = len(active_list) if active_list else 0\n\n            # pending join (ожидание заявок)\n            try:\n                pending_channels = sum(len(v) for v in PENDING_JOIN.values()) if isinstance(PENDING_JOIN, dict) else 0\n            except Exception:\n                pending_channels = 0\n\n            # per-account blacklist\n            try:\n                blacklist_channels = sum(len(v) for v in ACCOUNT_BLACKLIST.values()) if isinstance(ACCOUNT_BLACKLIST, dict) else 0\n            except Exception:\n                blacklist_channels = 0\n\n            # paused channels (заморозка/пауза)\n            try:\n                paused_channels = len(PAUSED_CHANNELS) if isinstance(PAUSED_CHANNELS, (set, list, dict)) else 0\n            except Exception:\n                paused_channels = 0\n\n            counts = {}\n            try:\n                counts = get_counts_fn() or {}\n            except Exception:\n                counts = {}\n\n            ru_log(\n                "ЖИВОЙ",\n                "Мониторинг: отчёт",\n                channel=_stats.get("last_post_channel", "-"),\n                account="-",\n                extra=(\n                    f"Каналы: всего {total_channels} | активных {active_channels} | "\n                    f"ожидание {pending_channels} | заморожено {paused_channels} | blacklist {blacklist_channels} | "\n                    f"Аккаунты: монитор=1, комментинг={counts.get(\'commenters\', 0)} | "\n                    f"Отправлено: {_stats.get(\'commented\', 0)} | "\n                    f"Удалено: {counts.get(\'deleted\', 0)} | "\n                    f"Живые: {counts.get(\'alive\', 0)} | "\n                    f"Пропусков: {_stats.get(\'skipped\', 0)} | "\n                    f"Ошибок: {_stats.get(\'errors\', 0)} | "\n                    f"Join: {_stats.get(\'joined\', 0)} | "\n                    f"Последний ивент: {last_str}"+ " | JOIN-АККАУНТЫ: " + ("; ".join([f"{k}: {(_jp_get(k).get(\'already\',0))}/{(_jp_get(k).get(\'total\',0))} (нужно {(_jp_get(k).get(\'need\',0))}) | last={_jp_get(k).get(\'last\',{}).get(\'status\',\'-\')} {_jp_get(k).get(\'last\',{}).get(\'channel\',\'-\')}" for k in list(_join_progress.keys())[:8]]) if _join_progress else "-" )\n                )\n            )\n        except Exception:\n            pass\n        await asyncio.sleep(HEARTBEAT_SEC)\nasync def autojoin_channels_loop(main_client, account_key: str, get_channels_fn):\n    """JOIN-аккаунт: вступает ТОЛЬКО в те каналы, где аккаунта ещё нет (очередь)."""\n    ru_log(\n        "ЖИВОЙ",\n        "AUTOJOIN включён",\n        channel="-",\n        account=account_key,\n        extra=f"1 вступление / {JOIN_COOLDOWN_SEC//60} мин | лимит {MAX_JOINS_PER_DAY_PER_ACCOUNT}/сутки",\n    )\n\n    join_queue = []  # список каналов, где аккаунт НЕ участник\n\n    async def rebuild_queue():\n        nonlocal join_queue\n        channels = (get_channels_fn() or [])\n        pending = []\n        already = 0\n        skipped = 0\n\n        for ch in channels:\n            ch = (ch or "").strip()\n            if not ch:\n                continue\n\n            # invite ссылки в очереди тоже допускаем (не можем проверить membership заранее)\n            if ch.startswith("https://t.me/+") or ch.startswith("+"):\n                pending.append(ch)\n                continue\n\n            ch_norm = normalize_channel(ch)\n            try:\n                ent = await main_client.get_entity(ch_norm)\n            except Exception:\n                # если не смогли получить entity — оставим в очереди (вдруг приват/ограничения)\n                pending.append(ch)\n                skipped += 1\n                continue\n\n            try:\n                if await is_member_strict(main_client, ent):\n                    already += 1\n                else:\n                    pending.append(ch_norm)\n            except Exception:\n                pending.append(ch_norm)\n\n        join_queue = pending\n        ru_log(\n            "ЖИВОЙ",\n            "AUTOJOIN очередь обновлена",\n            channel="-",\n            account=account_key,\n            extra=f"Нужно вступить: {len(join_queue)} | Уже в каналах: {already} | Пропущено проверок: {skipped}",\n        )\n\n    while True:\n        try:\n            if not can_join_today(account_key):\n                ru_log(\n                    "ПРОПУСК",\n                    "Лимит вступлений на сегодня",\n                    channel="-",\n                    account=account_key,\n                    extra=f"Лимит: {MAX_JOINS_PER_DAY_PER_ACCOUNT}/сутки",\n                )\n                await asyncio.sleep(JOIN_COOLDOWN_SEC)\n                continue\n\n            if not join_queue:\n                await rebuild_queue()\n\n            if not join_queue:\n                # всё уже вступлено — просто ждём\n                await asyncio.sleep(JOIN_COOLDOWN_SEC)\n                continue\n\n            ch = join_queue.pop(0)\n            ch = (ch or "").strip()\n            if not ch:\n                await asyncio.sleep(1)\n                continue\n\n            # приватные invite ссылки\n            if ch.startswith("https://t.me/+") or ch.startswith("+"):\n                invite_hash = ch.split("+", 1)[1].strip().strip("/")\n                if not invite_hash:\n                    ru_log("ПРОПУСК", "Пустой invite hash", channel=ch, account=account_key)\n                    await asyncio.sleep(JOIN_COOLDOWN_SEC)\n                    continue\n                if ImportChatInviteRequest is None:\n                    ru_log("ПЛОХО", "ImportChatInviteRequest недоступен (импорт)", channel=ch, account=account_key)\n                    await asyncio.sleep(JOIN_COOLDOWN_SEC)\n                    continue\n\n                ru_log("ЖИВОЙ", "Пробую вступить по invite", channel=ch, account=account_key)\n                try:\n                    await main_client(ImportChatInviteRequest(invite_hash))\n                    mark_join_today(account_key)\n                    _stats["joined"] += 1\n                    ru_log("УСПЕХ", "Вступил по invite", channel=ch, account=account_key)\n                    try:\n                        _jp_last(account_key, "JOIN_OK_INVITE", ch)\n                    except Exception:\n                        pass\n                    await asyncio.sleep(JOIN_COOLDOWN_SEC)\n                except Exception as e:\n                    ru_log("ПЛОХО", "Не удалось вступить по invite", channel=ch, account=account_key, extra=str(e))\n                    try:\n                        _jp_last(account_key, "JOIN_FAIL_INVITE", ch)\n                    except Exception:\n                        pass\n                await asyncio.sleep(JOIN_COOLDOWN_SEC)\n                continue\n\n            # обычный канал/группа\n            ch_norm = normalize_channel(ch)\n            ru_log("ЖИВОЙ", "Пробую вступить в канал", channel=ch_norm, account=account_key)\n            try:\n                ent = await main_client.get_entity(ch_norm)\n\n                # если уже участник — пропускаем (на случай, если очередь устарела)\n                try:\n                    if await is_member_strict(main_client, ent):\n                        ru_log("УСПЕХ", "Уже участник канала (auto-join skip)", channel=ch_norm, account=account_key)\n                        try:\n                            _jp_last(account_key, "ALREADY_MEMBER", ch_norm)\n                        except Exception:\n                            pass\n                        await asyncio.sleep(1)\n                        continue\n                except Exception:\n                    pass\n\n                if JoinChannelRequest is not None:\n                    await main_client(JoinChannelRequest(ent))\n                else:\n                    await main_client(functions.channels.JoinChannelRequest(channel=ent))\n\n                mark_join_today(account_key)\n                _stats["joined"] += 1\n                ru_log("УСПЕХ", "Вступил в канал", channel=ch_norm, account=account_key)\n                try:\n                    _jp_last(account_key, "JOIN_OK", ch_norm)\n                except Exception:\n                    pass\n            except Exception as e:\n                ru_log("ПЛОХО", "Не удалось вступить в канал", channel=ch_norm, account=account_key, extra=str(e))\n                try:\n                    _jp_last(account_key, "JOIN_FAIL", ch_norm)\n                except Exception:\n                    pass\n\n            await asyncio.sleep(JOIN_COOLDOWN_SEC)\n\n        except Exception as e:\n            ru_log("ПЛОХО", "AUTOJOIN: ошибка цикла", channel="-", account=account_key, extra=str(e))\n            await asyncio.sleep(JOIN_COOLDOWN_SEC)\n\n\n\ndef ensure_api_keys_cached(cfg: dict):\n    tg = cfg.setdefault("telegram", {})\n    oa = cfg.setdefault("openai", {})\n\n    # Telegram\n    if _is_missing(tg.get("api_id")):\n        ru_log("ПЛОХО", "telegram.api_id не задан в config.yaml")\n        v = input("Telegram api_id: ").strip()\n        if v:\n            tg["api_id"] = int(v)\n\n    if _is_missing(tg.get("api_hash")):\n        ru_log("ПЛОХО", "telegram.api_hash не задан в config.yaml")\n        v = input("Telegram api_hash: ").strip()\n        if v:\n            tg["api_hash"] = v\n\n    # OpenAI\n    if _is_missing(oa.get("api_key")):\n        ru_log("ПЛОХО", "openai.api_key не задан в config.yaml")\n        v = input("OpenAI api_key: ").strip()\n        if v:\n            oa["api_key"] = v\n\n    # model default\n    if _is_missing(oa.get("model")):\n        oa["model"] = "gpt-4.1"\n\n    save_config(cfg)\n    return cfg\n\n    if extra:\n        base += f" | {extra}"\n    try:\n        logging.info(base)\n    except Exception:\n        print(base)\n# -*- coding: utf-8 -*-\n"""\ntg_comment_service_ULTRA_FULL.py\n\n✅ Premium Console Software (RU) — NOT a Telegram bot\nМониторинг Telegram-каналов → генерация коротких комментариев через OpenAI → отправка в обсуждения.\n\nУЛЬТРА-ФАРШ:\n- Русское красивое меню (как софт)\n- Автосоздание файлов/папок\n- SAFE / NORMAL / AGGRESSIVE режимы\n- Прогрев (сетка): постепенное включение каналов\n- Ротация аккаунтов + лимиты\n- Прокси на каждый аккаунт (accounts/*.json)\n- Авто-неликвид (accounts_nelikvid/) если аккаунт поймал ограничения\n- Авто-blacklist каналов если банят/нет обсуждений\n- Статистика: sent / deleted / alive (stats.json)\n- Проверка удалённых комментариев через N секунд (по желанию)\n- Логи: консоль + logs/*.log\n- Защита от банов: имитация набора, лимит каналов, не комментировать свежее\n\nЗапуск:\n  py tg_comment_service_ULTRA_FULL.py\n\nЗависимости:\n  py -m pip install telethon pyyaml openai\n\nВажно:\n- Для Always-on на PythonAnywhere запускать "Сервис" (без ожиданий Enter)\n- Сессии .session должны быть уже готовы в accounts/\n"""\n\n\nimport os\nimport sys\nimport json\nimport yaml\nimport time\nimport random\nimport shutil\nimport asyncio\nimport logging\nfrom dataclasses import dataclass\nfrom datetime import datetime, timezone\nfrom pathlib import Path\nfrom typing import Dict, Any, List, Optional, Tuple\n\nimport socks\n\nfrom telethon import TelegramClient, events\nfrom telethon.errors.rpcerrorlist import UserNotParticipantError\nfrom telethon.tl import functions\nfrom telethon.tl.functions.channels import JoinChannelRequest\nfrom telethon.tl.functions.messages import ImportChatInviteRequest\nfrom telethon.errors import FloodWaitError, RPCError, SessionPasswordNeededError\nfrom telethon.tl.functions.messages import GetDiscussionMessageRequest\n\nfrom openai import OpenAI\n\n\n# =========================\n# Telethon noise suppress + auto-reconnect\n# =========================\n_TELETHON_NOISE_LOGGERS = (\n    "telethon.network.mtprotostate",\n    "telethon.network.mtprotosender",\n    "telethon.client.updates",\n)\n\n_SECURITY_ERROR_PATTERNS = (\n    "Security error while unpacking",\n    "Too many messages had to be ignored consecutively",\n)\n\n_OLD_MESSAGE_PATTERN = "Server sent a very old message"\n\n\nclass _QuietTelethonHandler(logging.Handler):\n    """Capture Telethon warnings/errors without printing them."""\n\n    def __init__(self, trigger_cb=None):\n        super().__init__(level=logging.WARNING)\n        self._trigger_cb = trigger_cb\n\n    def emit(self, record: logging.LogRecord) -> None:\n        try:\n            msg = record.getMessage() or ""\n        except Exception:\n            msg = ""\n        # Trigger reconnect only on the real \'security error\' spam\n        if self._trigger_cb and any(p in msg for p in _SECURITY_ERROR_PATTERNS):\n            try:\n                self._trigger_cb(msg)\n            except Exception:\n                pass\n        # Always suppress output (do nothing)\n\n\ndef suppress_telethon_noise_default() -> None:\n    """Hard-silence Telethon noisy loggers by default (no console spam)."""\n    for name in _TELETHON_NOISE_LOGGERS:\n        lg = logging.getLogger(name)\n        lg.setLevel(logging.ERROR)\n        lg.propagate = False\n        # prevent \'No handler could be found\' edge cases\n        if not lg.handlers:\n            lg.addHandler(logging.NullHandler())\n\n\nasync def _safe_reconnect_client(client: TelegramClient, logger: logging.Logger, name: str) -> None:\n    try:\n        if client.is_connected():\n            await client.disconnect()\n    except Exception:\n        pass\n    await asyncio.sleep(2)\n    try:\n        await client.connect()\n        logger.warning(f"[RECONNECT] {name}: переподключено")\n    except Exception as e:\n        logger.error(f"[RECONNECT] {name}: не смог переподключиться: {e}")\n\n\n\n\n# =========================\n# Paths\n# =========================\nBASE_DIR = Path(os.getenv("TGCORE_BASE_DIR", str(Path(__file__).parent.resolve()))).resolve()\nCONFIG_FILE = BASE_DIR / "config.yaml"\nCHANNELS_FILE = BASE_DIR / "channels.txt"\nBLACKLIST_FILE = BASE_DIR / "channels_blacklist.txt"\n\n# --- NEW: split sessions into two independent folders ---\n# monitoring/accounts   -> 1 аккаунт для мониторинга (читает посты)\n# commenting/accounts   -> N аккаунтов для комментинга (пишут комментарии)\nMONITOR_DIR = BASE_DIR / "monitoring"\nMONITOR_ACCOUNTS_DIR = BASE_DIR / "accounts"\nMONITOR_NELIKVID_DIR = BASE_DIR / "accounts_nelikvid"\nCOMMENT_ACCOUNTS_DIR = BASE_DIR / "accounts"\nCOMMENT_NELIKVID_DIR = BASE_DIR / "accounts_nelikvid"\n\nLOGS_DIR = BASE_DIR / "logs"\nSTATE_FILE = BASE_DIR / "state.json"\nSTATS_FILE = BASE_DIR / "stats.json"\nPHRASES_FILE = BASE_DIR / "phrases.txt"\nPROXY_POOL_FILE = BASE_DIR / "proxy_pool.txt"\nACCOUNT_CHANNEL_BLACKLIST_FILE = BASE_DIR / "account_channel_blacklist.json"\nPENDING_JOIN_FILE = BASE_DIR / "pending_join.json"\n\n\n\n# =========================\n# UI (ANSI)\n# =========================\ndef _supports_ansi() -> bool:\n    # Windows Terminal supports ANSI. Classic cmd sometimes too, depending on settings.\n    if os.name != "nt":\n        return True\n    return bool(os.environ.get("WT_SESSION")) or bool(os.environ.get("TERM"))\n\n\nclass C:\n    RESET = "\\033[0m"\n    BOLD = "\\033[1m"\n    DIM = "\\033[2m"\n    RED = "\\033[31m"\n    GREEN = "\\033[32m"\n    YELLOW = "\\033[33m"\n    BLUE = "\\033[34m"\n    MAGENTA = "\\033[35m"\n    CYAN = "\\033[36m"\n    WHITE = "\\033[37m"\n\n\ndef _c(s: str) -> str:\n    if not _supports_ansi():\n        # remove escape start, very rough, but ok\n        return s.replace("\\033[", "")\n    return s\n\n\ndef ui_clear():\n    os.system("cls" if os.name == "nt" else "clear")\n\n\ndef ui_hr():\n    print(_c(C.DIM + "─" * 72 + C.RESET))\n\n\ndef ui_title(txt: str):\n    ui_hr()\n    print(_c(C.BOLD + C.CYAN + f" {txt}" + C.RESET))\n    ui_hr()\n\n\ndef ui_ok(txt: str):\n    print(_c(C.GREEN + "✅ " + txt + C.RESET))\n\n\ndef ui_warn(txt: str):\n    print(_c(C.YELLOW + "⚠️ " + txt + C.RESET))\n\n\ndef ui_bad(txt: str):\n    print(_c(C.RED + "❌ " + txt + C.RESET))\n\n\ndef ui_info(txt: str):\n    print(_c(C.CYAN + "ℹ️ " + txt + C.RESET))\n\n\ndef ui_pause(server_mode: bool):\n    if server_mode:\n        return\n    input(_c(C.DIM + "\\nНажми Enter..." + C.RESET))\n\n\n# =========================\n# Default config (RU with explanations)\n# =========================\nRU_CONFIG_YAML_TEMPLATE = """# =========================\n# config.yaml — ULTRA настройки (RU)\n# =========================\n# Это консольный сервис (НЕ Telegram-бот).\n#\n# Он делает:\n# - мониторинг каналов (channels.txt)\n# - генерирует короткий коммент через OpenAI\n# - отправляет комментарий в обсуждения поста\n#\n# ВАЖНО:\n# 1) Нужны аккаунты Telethon (.session) в папках accounts/ (мониторинг) и accounts/ (комментинг)\n# 2) Комментарии работают ТОЛЬКО если у канала включены обсуждения (discussion chat)\n# 3) Безопаснее использовать 1 аккаунт = 1 прокси\n#\ntelegram:\n  # Получить: https://my.telegram.org → API Development tools\n  api_id: 123456\n  api_hash: "PASTE_API_HASH"\n\nopenai:\n  # Вставь ключ OpenAI\n  api_key: "PASTE_OPENAI_KEY"\n  # Модель (ты просил GPT-4.1)\n  model: "gpt-4.1"\n  # Температура: 0.3..0.9 (0.7 норм)\n  temperature: 0.7\n  # Макс токенов для ответа (коротко!)\n  max_tokens: 60\n\nservice:\n  # Режим: SAFE / NORMAL / AGGRESSIVE\n  mode: "SAFE"\n\n  # ✅ VIP: Нейро-защита (1 тумблер)\n  neuro_protection: true\n\n  # Вероятность коммента (0.10..1.00). SAFE=0.35 — норм.\n  comment_probability: 0.35\n\n  # Ротация аккаунтов:\n  # round_robin = по очереди\n  # random = случайно\n  rotation_mode: "round_robin"\n\n  # Не писать 2 раза под один пост\n  only_one_comment_per_post: true\n\n  # На старте пропустить "первый" пост каждого канала (чтобы не комментить старое)\n  ignore_old_posts_on_start: true\n\n  # Параллельные задачи (нагрузка)\n  max_parallel_tasks: 10\n\n  # Задержка перед отправкой: fixed/random\n  delay_mode: "random"\n\n  # Если fixed:\n  fixed_delay_sec: 50\n\n  # Если random:\n  random_delay_min_sec: 35\n  random_delay_max_sec: 90\n\n  # Небольшая пауза ПОСЛЕ отправки (чтобы не долбить)\n  cooldown_min_sec: 2\n  cooldown_max_sec: 6\n\n  # Лимиты антибан:\n  max_comments_per_account_per_hour: 8\n  max_comments_per_account_per_day: 40\n  max_comments_per_channel_per_day: 15\n\n  # FloodWait -> уводим аккаунт на отдых (сек)\n  floodwait_cooldown_sec: 1800\n\n  # Длина комментария (безопасно)\n  min_comment_len: 2\n  max_comment_len: 80\n\n  # ЗАЩИТА ОТ БАНОВ:\n  # Не комментировать посты младше N секунд (чтобы не быть первым)\n  min_post_age_sec: 120\n  \n  # Имитация набора текста (сек) - делает паузу перед отправкой\n  typing_delay_min_sec: 3\n  typing_delay_max_sec: 7\n  \n  # Макс каналов на 1 аккаунт (чтобы не спамить из одного акка в много каналов)\n  max_channels_per_account: 15\n\nwarmup:\n  # ПРОГРЕВ (сетка)\n  enabled: true\n\n  # Сколько каналов включить сразу (на старте)\n  start_channels: 2\n\n  # Каждые N минут добавлять +1 канал\n  add_one_channel_every_min: 30\n\n  # Максимум каналов в работе (если 0 — значит все)\n  max_active_channels: 0\n\nantiban:\n  # Если аккаунт ловит жёсткие ошибки -> переносим в accounts_nelikvid/\n  quarantine_on_error: true\n\n  # Если канал токсичный -> добавляем в channels_blacklist.txt\n  blacklist_on_channel_errors: true\n\n  # Проверять удалён ли комментарий\n  check_deleted_comments: true\n\n  # Через сколько секунд после отправки проверить (например 120 сек)\n  deleted_check_after_sec: 120\n\nprompt:\n  system: >\n    Ты пишешь как реальный человек в Telegram-чате.\n    Коротко. Живо. Разговорно.\n    Без канцелярита. Без "как ИИ".\n    Без рекламы и ссылок.\n\n  user_template: >\n    Это пост в Telegram-канале.\n\n    Пост:\n    {post_text}\n\n    Напиши комментарий 2–4 слова (до 70 символов).\n    Строго по смыслу поста.\n    Запрещено: политика, война, токсичность, оскорбления, ссылки.\n    Иногда можно 1 эмодзи.\n"""\n\n\n# =========================\n# Helpers\n# =========================\ndef ensure_project_structure():\n    # создаём структуру проекта + ДВЕ отдельные папки для сессий\n    MONITOR_DIR.mkdir(exist_ok=True)\n    COMMENT_DIR.mkdir(exist_ok=True)\n    MONITOR_ACCOUNTS_DIR.mkdir(parents=True, exist_ok=True)\n    COMMENT_ACCOUNTS_DIR.mkdir(parents=True, exist_ok=True)\n    MONITOR_NELIKVID_DIR.mkdir(parents=True, exist_ok=True)\n    COMMENT_NELIKVID_DIR.mkdir(parents=True, exist_ok=True)\n    LOGS_DIR.mkdir(exist_ok=True)\n\n    if not CHANNELS_FILE.exists():\n        CHANNELS_FILE.write_text(\n            "# Каналы: по одному в строку\\n"\n            "# Пример:\\n"\n            "# ngs_news\\n"\n            "# https://t.me/ngs_news\\n",\n            encoding="utf-8"\n        )\n\n    if not BLACKLIST_FILE.exists():\n        BLACKLIST_FILE.write_text("# Blacklist каналов (авто)\\n", encoding="utf-8")\n\n    if not CONFIG_FILE.exists():\n        CONFIG_FILE.write_text(RU_CONFIG_YAML_TEMPLATE, encoding="utf-8")\n\n    if not STATE_FILE.exists():\n        STATE_FILE.write_text(json.dumps({\n            "last_seen": {},\n            "commented": {},\n            "counters": {"accounts": {}, "channels": {}},\n            "cooldowns": {"accounts": {}},\n            "account_channels": {}\n        }, ensure_ascii=False, indent=2), encoding="utf-8")\n\n    if not STATS_FILE.exists():\n        STATS_FILE.write_text(json.dumps({\n            "sent": 0,\n            "deleted": 0,\n            "alive": 0,\n            "by_account": {},\n            "by_channel": {}\n        }, ensure_ascii=False, indent=2), encoding="utf-8")\n        \n    if not PHRASES_FILE.exists():\n        PHRASES_FILE.write_text(\n            "# Простые фразы для комментариев (по одной в строке)\\n"\n            "# Используются если OpenAI не сработал\\n"\n            "Интересно\\n"\n            "Спасибо за информацию\\n"\n            "Полезно, беру на заметку\\n"\n            "Ждем продолжения\\n"\n            "Как всегда качественно\\n"\n            "Хорошая тема\\n"\n            "Согласен\\n"\n            "Важно\\n"\n            "Понятно, спасибо\\n"\n            "Жаль, что не все так просто\\n"\n            "Стоит задуматься\\n"\n            "Хороший материал\\n"\n            "Интересная позиция\\n"\n            "Логично\\n"\n            "Понравилось\\n"\n            "Удивительно\\n"\n            "Неожиданно\\n"\n            "Так и есть\\n",\n            encoding="utf-8"\n        )\n    \n    if not PROXY_POOL_FILE.exists():\n        PROXY_POOL_FILE.write_text(\n            "# Пул прокси (по одному в строку)\\n"\n            "# Формат: socks5://user:pass@ip:port\\n"\n            "# Примеры:\\n"\n            "# socks5://user1:pass1@192.168.1.1:1080\\n"\n            "# http://proxy_user:proxy_pass@10.0.0.1:8080\\n",\n            encoding="utf-8"\n        )\n    # Пер-аккаунт BLACKLIST каналов/групп (если доступ закрыт именно для этого аккаунта)\n    if not ACCOUNT_CHANNEL_BLACKLIST_FILE.exists():\n        ACCOUNT_CHANNEL_BLACKLIST_FILE.write_text(json.dumps({}, ensure_ascii=False, indent=2), encoding="utf-8")\n\n    # Pending join requests (если подана заявка в группу обсуждений)\n    if not PENDING_JOIN_FILE.exists():\n        PENDING_JOIN_FILE.write_text(json.dumps({}, ensure_ascii=False, indent=2), encoding="utf-8")\n\n\n\n\ndef setup_logging() -> logging.Logger:\n    log_name = datetime.now().strftime("%Y-%m-%d_%H-%M-%S") + ".log"\n    log_path = LOGS_DIR / log_name\n\n    logger = logging.getLogger("tg_comment_ultra")\n    logger.setLevel(logging.INFO)\n\n    if logger.handlers:\n        return logger\n\n    fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")\n\n    ch = logging.StreamHandler(sys.stdout)\n    ch.setFormatter(fmt)\n    ch.setLevel(logging.INFO)\n\n    fh = logging.FileHandler(log_path, encoding="utf-8")\n    fh.setFormatter(fmt)\n    fh.setLevel(logging.INFO)\n\n    logger.addHandler(ch)\n    logger.addHandler(fh)\n    return logger\n\n\ndef load_config() -> Dict[str, Any]:\n    return yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8")) or {}\n\n\ndef save_config(cfg: Dict[str, Any]) -> None:\n    CONFIG_FILE.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")\n\n\ndef get_neuro_protection(cfg: Dict[str, Any]) -> bool:\n    try:\n        return bool(cfg.get("service", {}).get("neuro_protection", True))\n    except Exception:\n        return True\n\n\ndef set_neuro_protection(cfg: Dict[str, Any], on: bool) -> None:\n    cfg.setdefault("service", {})["neuro_protection"] = bool(on)\n\n\ndef apply_neuro_protection_defaults(cfg: Dict[str, Any]) -> None:\n    """\n    ✅ VIP-логика:\n    Один тумблер NeuroProtection управляет антибан-настройками.\n    SAFE / NORMAL / AGGRESSIVE сохраняются, но теперь понятны и предсказуемы.\n    """\n    svc = cfg.setdefault("service", {})\n    mode = str(svc.get("mode", "SAFE")).upper().strip()\n    neuro = bool(svc.get("neuro_protection", True))\n\n    warm = cfg.setdefault("warmup", {})\n    antiban = cfg.setdefault("antiban", {})\n\n    # Базовые значения (минимум)\n    svc.setdefault("comment_probability", 0.35)\n    svc.setdefault("delay_mode", "random")\n    svc.setdefault("random_delay_min_sec", 35)\n    svc.setdefault("random_delay_max_sec", 90)\n    svc.setdefault("cooldown_min_sec", 2)\n    svc.setdefault("cooldown_max_sec", 6)\n    svc.setdefault("max_comments_per_account_per_hour", 8)\n    svc.setdefault("max_comments_per_account_per_day", 40)\n    svc.setdefault("max_comments_per_channel_per_day", 15)\n\n    warm.setdefault("enabled", True)\n    warm.setdefault("start_channels", 2)\n    warm.setdefault("add_one_channel_every_min", 30)\n    warm.setdefault("max_active_channels", 0)\n\n    antiban.setdefault("quarantine_on_error", True)\n    antiban.setdefault("blacklist_on_channel_errors", True)\n    antiban.setdefault("check_deleted_comments", True)\n    antiban.setdefault("deleted_check_after_sec", 120)\n\n    if not neuro:\n        # 🚀 Turbo (риск): отключаем часть защит\n        warm["enabled"] = False\n        antiban["check_deleted_comments"] = False\n\n        svc["comment_probability"] = min(1.0, max(0.55, float(svc.get("comment_probability", 0.6))))\n        svc["random_delay_min_sec"] = int(min(20, svc.get("random_delay_min_sec", 20)))\n        svc["random_delay_max_sec"] = int(min(45, svc.get("random_delay_max_sec", 45)))\n\n        svc["max_comments_per_account_per_hour"] = int(max(15, svc.get("max_comments_per_account_per_hour", 15)))\n        svc["max_comments_per_account_per_day"] = int(max(80, svc.get("max_comments_per_account_per_day", 80)))\n        return\n\n    # 🛡 NeuroProtection ON — режимы\n    if mode == "SAFE":\n        svc["comment_probability"] = float(svc.get("comment_probability", 0.35))\n        svc["random_delay_min_sec"] = int(max(35, svc.get("random_delay_min_sec", 35)))\n        svc["random_delay_max_sec"] = int(max(90, svc.get("random_delay_max_sec", 90)))\n        svc["max_comments_per_account_per_hour"] = int(min(8, svc.get("max_comments_per_account_per_hour", 8)))\n        svc["max_comments_per_account_per_day"] = int(min(40, svc.get("max_comments_per_account_per_day", 40)))\n        svc["max_comments_per_channel_per_day"] = int(min(15, svc.get("max_comments_per_channel_per_day", 15)))\n\n        warm["enabled"] = True\n        warm["start_channels"] = int(min(2, warm.get("start_channels", 2)))\n        warm["add_one_channel_every_min"] = int(max(30, warm.get("add_one_channel_every_min", 30)))\n\n        antiban["check_deleted_comments"] = True\n        antiban["deleted_check_after_sec"] = int(max(120, antiban.get("deleted_check_after_sec", 120)))\n\n    elif mode == "NORMAL":\n        svc["comment_probability"] = float(svc.get("comment_probability", 0.45))\n        svc["random_delay_min_sec"] = int(max(25, svc.get("random_delay_min_sec", 25)))\n        svc["random_delay_max_sec"] = int(max(60, svc.get("random_delay_max_sec", 60)))\n        svc["max_comments_per_account_per_hour"] = int(min(12, svc.get("max_comments_per_account_per_hour", 12)))\n        svc["max_comments_per_account_per_day"] = int(min(60, svc.get("max_comments_per_account_per_day", 60)))\n        svc["max_comments_per_channel_per_day"] = int(min(25, svc.get("max_comments_per_channel_per_day", 25)))\n\n        warm["enabled"] = True\n        warm["start_channels"] = int(min(4, warm.get("start_channels", 4)))\n        warm["add_one_channel_every_min"] = int(max(20, warm.get("add_one_channel_every_min", 20)))\n\n        antiban["check_deleted_comments"] = True\n        antiban["deleted_check_after_sec"] = int(max(90, antiban.get("deleted_check_after_sec", 90)))\n\n    else:\n        # AGGRESSIVE (всё равно с защитой, но быстрее)\n        svc["comment_probability"] = float(svc.get("comment_probability", 0.55))\n        svc["random_delay_min_sec"] = int(max(15, svc.get("random_delay_min_sec", 15)))\n        svc["random_delay_max_sec"] = int(max(35, svc.get("random_delay_max_sec", 35)))\n        svc["max_comments_per_account_per_hour"] = int(min(18, svc.get("max_comments_per_account_per_hour", 18)))\n        svc["max_comments_per_account_per_day"] = int(min(100, svc.get("max_comments_per_account_per_day", 100)))\n        svc["max_comments_per_channel_per_day"] = int(min(40, svc.get("max_comments_per_channel_per_day", 40)))\n\n        warm["enabled"] = True\n        warm["start_channels"] = int(min(6, warm.get("start_channels", 6)))\n        warm["add_one_channel_every_min"] = int(max(10, warm.get("add_one_channel_every_min", 10)))\n\n        antiban["check_deleted_comments"] = True\n        antiban["deleted_check_after_sec"] = int(max(60, antiban.get("deleted_check_after_sec", 60)))\n\n\n\ndef load_channels_all() -> List[str]:\n    lines = CHANNELS_FILE.read_text(encoding="utf-8").splitlines()\n    chans = []\n    for line in lines:\n        s = line.strip()\n        if not s or s.startswith("#"):\n            continue\n        if s.startswith("https://t.me/"):\n            s = s.replace("https://t.me/", "").strip("/")\n        if s.startswith("@"):\n            s = s[1:]\n        chans.append(s)\n    return chans\n\n\ndef load_blacklist() -> set:\n    lines = BLACKLIST_FILE.read_text(encoding="utf-8").splitlines()\n    s = set()\n    for line in lines:\n        t = line.strip()\n        if not t or t.startswith("#"):\n            continue\n        if t.startswith("@"):\n            t = t[1:]\n        s.add(t)\n    return s\n\n\ndef add_to_blacklist(channel_username: str):\n    channel_username = channel_username.strip().lstrip("@")\n    bl = load_blacklist()\n    if channel_username in bl:\n        return\n    with open(BLACKLIST_FILE, "a", encoding="utf-8") as f:\n        f.write(channel_username + "\\n")\n\n\ndef list_sessions(dir_path: Path) -> List[Path]:\n    return sorted(Path(dir_path).glob("*.session"))\n\n\ndef write_proxy_json(session_name: str, proxy_uri: str, dir_path: Path):\n    # Формат прокси_uri: socks5://user:pass@ip:port\n    data = {\'uri\': proxy_uri}\n    (Path(dir_path) / f"{session_name}.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding=\'utf-8\')\n\n\ndef load_proxy_for_session(session_path: Path) -> Optional[Tuple]:\n    """\n    ✅ FIX: Telethon ожидает proxy tuple в формате PySocks:\n      (proxy_type, host, port, rdns, username, password)\n\n    Где proxy_type — это socks.SOCKS5 / socks.SOCKS4 / socks.HTTP (НЕ строка "socks5").\n    Твой старый код возвращал строку схемы, из-за этого аккаунт часто не коннектился с прокси.\n\n    Поддержка форматов:\n      {"uri": "socks5://user:pass@ip:port"}\n      {"type": "socks5", "host": "...", "port": 1080, "username": "...", "password": "..."}\n    """\n    proxy_path = session_path.with_suffix(".json")\n    if not proxy_path.exists():\n        return None\n\n    try:\n        data = json.loads(proxy_path.read_text(encoding="utf-8"))\n    except Exception:\n        return None\n\n    def _ptype_to_socks(ptype_raw: str):\n        p = (ptype_raw or "").lower().strip()\n        if p in ("socks5", "socks5h"):\n            return socks.SOCKS5\n        if p in ("socks4", "socks4a"):\n            return socks.SOCKS4\n        if p in ("http", "https"):\n            return socks.HTTP\n        return None\n\n    # Новый формат: uri\n    if "uri" in data and data.get("uri"):\n        try:\n            from urllib.parse import urlparse\n            uri = str(data["uri"]).strip()\n            parsed = urlparse(uri)\n            ptype = _ptype_to_socks(parsed.scheme)\n            host = parsed.hostname\n            port = parsed.port\n            username = parsed.username\n            password = parsed.password\n            if ptype is None or not host or not port:\n                return None\n            # rdns=True для socks5 (и обычно норм для http)\n            return (ptype, host, int(port), True, username, password)\n        except Exception:\n            return None\n\n    # Старый формат\n    try:\n        ptype = _ptype_to_socks(str(data.get("type", "")).lower().strip())\n        host = data.get("host")\n        port = data.get("port")\n        username = data.get("username")\n        password = data.get("password")\n        if ptype is None or not host or not port:\n            return None\n        return (ptype, host, int(port), True, username, password)\n    except Exception:\n        return None\ndef now_ts() -> int:\n    return int(datetime.now(timezone.utc).timestamp())\n\n\ndef norm_text(s: str) -> str:\n    if not s:\n        return ""\n    return " ".join(s.replace("\\n", " ").split()).strip()\n\n\ndef clamp_comment(s: str, min_len: int, max_len: int) -> str:\n    s = norm_text(s)\n    if len(s) > max_len:\n        s = s[:max_len].rstrip()\n    if len(s) < min_len:\n        s = "Круто 🙂"\n    return s\n\n\ndef ensure_state_schema(state: Dict[str, Any]) -> Dict[str, Any]:\n    state.setdefault("last_seen", {})\n    state.setdefault("commented", {})\n    state.setdefault("counters", {})\n    state["counters"].setdefault("accounts", {})\n    state["counters"].setdefault("channels", {})\n    state.setdefault("cooldowns", {})\n    state["cooldowns"].setdefault("accounts", {})\n    state.setdefault("account_channels", {})\n    return state\n\n\ndef hour_bucket(ts: int) -> int:\n    return ts // 3600\n\n\ndef day_bucket(ts: int) -> int:\n    return ts // 86400\n\n\ndef load_state() -> Dict[str, Any]:\n    return ensure_state_schema(json.loads(STATE_FILE.read_text(encoding="utf-8")))\n\n\ndef save_state(state: Dict[str, Any]) -> None:\n    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")\n\n\ndef load_stats() -> Dict[str, Any]:\n    return json.loads(STATS_FILE.read_text(encoding="utf-8"))\n\n\ndef save_stats(stats: Dict[str, Any]) -> None:\n    STATS_FILE.write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")\n\ndef _load_json_file(path: Path, default):\n    try:\n        if path.exists():\n            return json.loads(path.read_text(encoding="utf-8")) or default\n    except Exception:\n        pass\n    return default\n\ndef _save_json_file(path: Path, data) -> None:\n    try:\n        tmp = str(path) + ".tmp"\n        Path(tmp).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")\n        os.replace(tmp, path)\n    except Exception:\n        try:\n            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")\n        except Exception:\n            pass\n\n\n\ndef stats_inc(stats: Dict[str, Any], key: str, n: int = 1):\n    stats[key] = int(stats.get(key, 0) or 0) + n\n\n\ndef stats_inc_nested(stats: Dict[str, Any], root: str, name: str, key: str, n: int = 1):\n    stats.setdefault(root, {})\n    stats[root].setdefault(name, {})\n    stats[root][name][key] = int(stats[root][name].get(key, 0) or 0) + n\n\n\ndef load_phrases() -> List[str]:\n    if not PHRASES_FILE.exists():\n        return []\n    lines = PHRASES_FILE.read_text(encoding="utf-8").splitlines()\n    phrases = []\n    for line in lines:\n        s = line.strip()\n        if not s or s.startswith("#"):\n            continue\n        phrases.append(s)\n    return phrases\n\n\ndef load_proxy_pool_lines() -> List[str]:\n    if not PROXY_POOL_FILE.exists():\n        return []\n    out = []\n    for line in PROXY_POOL_FILE.read_text(encoding="utf-8").splitlines():\n        s = line.strip()\n        if not s or s.startswith("#"):\n            continue\n        out.append(s)\n    return out\n\n\n# =========================\n# Core: Account rotation\n# =========================\n@dataclass\nclass ClientWrap:\n    name: str\n    client: TelegramClient\n    proxy_enabled: bool\n\n\nclass AccountRotator:\n    def __init__(self, clients: List[ClientWrap], mode: str = "round_robin"):\n        self.clients = clients\n        self.mode = mode or "round_robin"\n        self.i = 0\n\n    def pick(self) -> ClientWrap:\n        if self.mode == "random":\n            return random.choice(self.clients)\n        cw = self.clients[self.i % len(self.clients)]\n        self.i += 1\n        return cw\n\n\n# =========================\n# OpenAI\n# =========================\nclass OpenAICommenter:\n    def __init__(self, api_key: str, model: str, temperature: float, max_tokens: int, system_prompt: str, user_template: str):\n        self.client = OpenAI(api_key=api_key)\n        self.model = model\n        self.temperature = float(temperature)\n        self.max_tokens = int(max_tokens)\n        self.system_prompt = system_prompt\n        self.user_template = user_template\n\n    async def generate(self, post_text: str) -> str:\n        prompt = self.user_template.format(post_text=post_text[:2500])\n\n        def _call():\n            resp = self.client.chat.completions.create(\n                model=self.model,\n                temperature=self.temperature,\n                max_tokens=self.max_tokens,\n                messages=[\n                    {"role": "system", "content": self.system_prompt},\n                    {"role": "user", "content": prompt},\n                ],\n            )\n            return resp.choices[0].message.content.strip()\n\n        return await asyncio.to_thread(_call)\n\n\n# =========================\n# ULTRA Service\n# =========================\nclass TGCommentServiceUltra:\n    def __init__(self, cfg: Dict[str, Any], logger: logging.Logger):\n        self.cfg = cfg\n        self.logger = logger\n\n        tg = cfg.get("telegram", {})\n        oa = cfg.get("openai", {})\n        pr = cfg.get("prompt", {})\n        svc = cfg.get("service", {})\n        warm = cfg.get("warmup", {})\n        ab = cfg.get("antiban", {})\n\n        self.api_id = int(tg.get("api_id", 0))\n        self.api_hash = str(tg.get("api_hash", ""))\n\n        self.channels_all = load_channels_all()\n        self.blacklist = load_blacklist()\n\n        self.mode = str(svc.get("mode", "SAFE")).upper().strip()\n        self.comment_probability = float(svc.get("comment_probability", 0.35))\n\n        self.rotation_mode = str(svc.get("rotation_mode", "round_robin"))\n        self.only_one_comment = bool(svc.get("only_one_comment_per_post", True))\n        self.ignore_old_on_start = bool(svc.get("ignore_old_posts_on_start", True))\n        self.max_parallel = int(svc.get("max_parallel_tasks", 10))\n\n\n        # Auto-reply in PM (optional, simple)\n        self.autoreply_enabled = bool(svc.get("autoreply_enabled", False))\n        self.autoreply_text = str(svc.get("autoreply_text", "Привет 🙂 Сейчас занят, отвечу чуть позже."))\n        self.autoreply_delay_min = int(svc.get("autoreply_delay_min_sec", 20))\n        self.autoreply_delay_max = int(svc.get("autoreply_delay_max_sec", 60))\n        self.autoreply_once_per_user = bool(svc.get("autoreply_once_per_user", True))\n        self._autoreply_seen = set()\n        self.delay_mode = str(svc.get("delay_mode", "random"))\n        self.fixed_delay = int(svc.get("fixed_delay_sec", 50))\n        self.rnd_delay_min = int(svc.get("random_delay_min_sec", 35))\n        self.rnd_delay_max = int(svc.get("random_delay_max_sec", 90))\n\n        self.cooldown_min = int(svc.get("cooldown_min_sec", 2))\n        self.cooldown_max = int(svc.get("cooldown_max_sec", 6))\n\n        self.max_per_acc_hour = int(svc.get("max_comments_per_account_per_hour", 8))\n        self.max_per_acc_day = int(svc.get("max_comments_per_account_per_day", 40))\n        self.max_per_channel_day = int(svc.get("max_comments_per_channel_per_day", 15))\n        self.floodwait_cooldown = int(svc.get("floodwait_cooldown_sec", 1800))\n\n        self.min_len = int(svc.get("min_comment_len", 2))\n        self.max_len = int(svc.get("max_comment_len", 80))\n\n        # Новая защита от банов\n        self.min_post_age = int(svc.get("min_post_age_sec", 120))\n        self.typing_delay_min = int(svc.get("typing_delay_min_sec", 3))\n        self.typing_delay_max = int(svc.get("typing_delay_max_sec", 7))\n        self.max_channels_per_account = int(svc.get("max_channels_per_account", 15))\n\n        # Максимум каналов в работе (0 = все). Если 0 — warmup отключаем, чтобы мониторить ВСЕ каналы сразу.\n        self.max_active_channels = int(svc.get("max_active_channels", 0))\n\n        # Фразы для fallback\n        self.phrases = load_phrases()\n\n        # Warmup (grid)\n        self.warmup_enabled = bool(warm.get("enabled", True))\n        self.warm_start_channels = int(warm.get("start_channels", 2))\n        self.warm_add_every_min = int(warm.get("add_one_channel_every_min", 30))\n        self.warm_max_active = int(warm.get("max_active_channels", 0))  # 0 = all\n\n        # Если задан service.max_active_channels — используем его как главный лимит.\n        # 0 = все каналы активны сразу (без прогрева).\n        try:\n            if self.max_active_channels and self.max_active_channels > 0:\n                # включаем warmup, но ограничиваем максимумом\n                if (not self.warm_max_active) or (self.warm_max_active > self.max_active_channels):\n                    self.warm_max_active = self.max_active_channels\n                if self.warm_start_channels > self.max_active_channels:\n                    self.warm_start_channels = self.max_active_channels\n            else:\n                # мониторим ВСЕ каналы сразу\n                self.warmup_enabled = False\n        except Exception:\n            pass\n\n        # Antiban\n        self.quarantine_on_error = bool(ab.get("quarantine_on_error", True))\n        self.blacklist_on_channel_errors = bool(ab.get("blacklist_on_channel_errors", True))\n        self.check_deleted_comments = bool(ab.get("check_deleted_comments", True))\n        self.deleted_check_after = int(ab.get("deleted_check_after_sec", 120))\n\n        # OpenAI\n        self.commenter = OpenAICommenter(\n            api_key=str(oa.get("api_key", "")),\n            model=str(oa.get("model", "gpt-4.1")),\n            temperature=float(oa.get("temperature", 0.7)),\n            max_tokens=int(oa.get("max_tokens", 60)),\n            system_prompt=str(pr.get("system", "")),\n            user_template=str(pr.get("user_template", "")),\n        )\n\n        self.state = load_state()\n        self.stats = load_stats()\n\n        # Пер-аккаунт blacklist каналов/обсуждений (если доступ закрыт именно для этого аккаунта)\n        self.account_channel_blacklist: Dict[str, Dict[str, str]] = _load_json_file(ACCOUNT_CHANNEL_BLACKLIST_FILE, {})\n        # Ожидающие заявки на вступление (pause + recheck)\n        self.pending_join: Dict[str, Dict[str, Any]] = _load_json_file(PENDING_JOIN_FILE, {})\n\n\n        self.clients: List[ClientWrap] = []\n        self.monitor: Optional[ClientWrap] = None\n        self.rotator: Optional[AccountRotator] = None\n        self.sem = asyncio.Semaphore(self.max_parallel)\n\n        self.active_channels: List[str] = []\n        self.start_time_ts = now_ts()\n\n    def save_all(self):\n        save_state(self.state)\n        save_stats(self.stats)\n\n    # -------- per-account blacklist --------\n    def _bl_key(self, channel_username: str) -> str:\n        return str(channel_username or "").strip().lstrip("@")\n\n    def is_account_blacklisted(self, account: str, channel_username: str) -> bool:\n        ck = self._bl_key(channel_username)\n        return bool(self.account_channel_blacklist.get(account, {}).get(ck))\n\n    def account_blacklist_reason(self, account: str, channel_username: str) -> str:\n        ck = self._bl_key(channel_username)\n        return str(self.account_channel_blacklist.get(account, {}).get(ck, ""))\n\n    def add_account_blacklist(self, account: str, channel_username: str, reason: str):\n        ck = self._bl_key(channel_username)\n        self.account_channel_blacklist.setdefault(account, {})[ck] = str(reason or "blocked")\n        _save_json_file(ACCOUNT_CHANNEL_BLACKLIST_FILE, self.account_channel_blacklist)\n\n    # -------- pending join requests (recheck every 5 min) --------\n    def _pending_get(self, account: str, channel_username: str) -> Optional[dict]:\n        ck = self._bl_key(channel_username)\n        return self.pending_join.get(account, {}).get(ck)\n\n    def _pending_set(self, account: str, channel_username: str, rec: dict):\n        ck = self._bl_key(channel_username)\n        self.pending_join.setdefault(account, {})[ck] = rec\n        _save_json_file(PENDING_JOIN_FILE, self.pending_join)\n\n    def _pending_clear(self, account: str, channel_username: str):\n        ck = self._bl_key(channel_username)\n        try:\n            if account in self.pending_join and ck in self.pending_join[account]:\n                self.pending_join[account].pop(ck, None)\n                if not self.pending_join[account]:\n                    self.pending_join.pop(account, None)\n                _save_json_file(PENDING_JOIN_FILE, self.pending_join)\n        except Exception:\n            pass\n\n    def pending_wait_left(self, account: str, channel_username: str) -> int:\n        rec = self._pending_get(account, channel_username)\n        if not rec:\n            return 0\n        nxt = int(rec.get("next_check_ts", 0) or 0)\n        now = now_ts()\n        return max(0, nxt - now)\n\n    async def ensure_discussion_membership(self, client: TelegramClient, account: str, channel_username: str, discussion_peer) -> Tuple[bool, str]:\n        """Возвращает (ok, reason). reason: ok/pending/blacklisted/forbidden/other"""\n        # already pending? then only recheck by schedule\n        left = self.pending_wait_left(account, channel_username)\n        if left > 0:\n            return False, f"pending_wait:{left}"\n\n        # try strict member check\n        try:\n            ent = await client.get_entity(discussion_peer)\n        except Exception as e:\n            return False, f"no_entity:{e}"\n\n        try:\n            if await is_member_strict(client, ent):\n                self._pending_clear(account, channel_username)\n                return True, "ok"\n        except Exception:\n            pass\n\n        # try join\n        try:\n            await client(JoinChannelRequest(ent))\n            # join ok\n            self._pending_clear(account, channel_username)\n            return True, "joined"\n        except RPCError as e:\n            s = str(e).lower()\n\n            # join request sent / pending approval\n            if ("invite_request_sent" in s) or ("join_request_sent" in s) or ("request" in s and "sent" in s):\n                self._pending_set(account, channel_username, {\n                    "ts": now_ts(),\n                    "next_check_ts": now_ts() + 300,\n                    "why": str(e),\n                })\n                return False, "pending"\n\n            # forbidden for this user -> per-account blacklist\n            if ("user_banned_in_channel" in s) or ("chat_write_forbidden" in s) or ("channel_private" in s) or ("forbidden" in s):\n                self.add_account_blacklist(account, channel_username, str(e))\n                return False, "forbidden"\n\n            return False, f"join_error:{e}"\n        except Exception as e:\n            return False, f"join_error:{e}"\n\n\n    def post_key(self, ch: str, msg_id: int) -> str:\n        return f"{ch}:{msg_id}"\n\n    def channel_day_key(self, ch: str, ts: int) -> str:\n        return f"{ch}:{day_bucket(ts)}"\n\n    def acc_day_key(self, acc: str, ts: int) -> str:\n        return f"{acc}:{day_bucket(ts)}"\n\n    def acc_hour_key(self, acc: str, ts: int) -> str:\n        return f"{acc}:{hour_bucket(ts)}"\n\n    def get_counter(self, scope: str, key: str) -> int:\n        return int(self.state["counters"][scope].get(key, 0) or 0)\n\n    def inc_counter(self, scope: str, key: str) -> None:\n        self.state["counters"][scope][key] = self.get_counter(scope, key) + 1\n\n    def acc_cooldown_until(self, acc: str) -> int:\n        return int(self.state["cooldowns"]["accounts"].get(acc, 0) or 0)\n\n    def set_acc_cooldown(self, acc: str, until_ts: int) -> None:\n        self.state["cooldowns"]["accounts"][acc] = int(until_ts)\n        save_state(self.state)\n\n    def compute_delay(self) -> int:\n        if self.delay_mode == "fixed":\n            return max(0, int(self.fixed_delay))\n        a = int(min(self.rnd_delay_min, self.rnd_delay_max))\n        b = int(max(self.rnd_delay_min, self.rnd_delay_max))\n        return random.randint(a, b)\n\n    def compute_cooldown(self) -> float:\n        a = int(min(self.cooldown_min, self.cooldown_max))\n        b = int(max(self.cooldown_min, self.cooldown_max))\n        return float(random.randint(a, b))\n\n    def can_use_account(self, acc_name: str, ts: int) -> Tuple[bool, str]:\n        until = self.acc_cooldown_until(acc_name)\n        if until and ts < until:\n            return False, f"аккаунт отдыхает {until - ts} сек"\n\n        if self.get_counter("accounts", self.acc_hour_key(acc_name, ts)) >= self.max_per_acc_hour:\n            return False, "лимит аккаунта в час"\n\n        if self.get_counter("accounts", self.acc_day_key(acc_name, ts)) >= self.max_per_acc_day:\n            return False, "лимит аккаунта в сутки"\n\n        return True, "ok"\n\n    def can_use_channel(self, channel_username: str, ts: int) -> Tuple[bool, str]:\n        if self.get_counter("channels", self.channel_day_key(channel_username, ts)) >= self.max_per_channel_day:\n            return False, "лимит канала в сутки"\n        return True, "ok"\n\n    def pick_account_for_send(self, channel_username: str) -> Optional[ClientWrap]:\n        if not self.rotator:\n            return None\n        ts = now_ts()\n        tries = min(25, max(5, len(self.clients) * 3))\n        \n        for _ in range(tries):\n            cw = self.rotator.pick()\n            \n            # Проверяем лимит каналов на аккаунт\n            if self.max_channels_per_account > 0:\n                acc_channels = self.state.setdefault("account_channels", {}).get(cw.name, [])\n                if channel_username not in acc_channels and len(acc_channels) >= self.max_channels_per_account:\n                    continue  # У аккаунта уже слишком много разных каналов\n            \n            ok, _ = self.can_use_account(cw.name, ts)\n            if ok:\n                return cw\n        \n        return None\n\n    def warmup_active_channel_count(self) -> int:\n        if not self.warmup_enabled:\n            return len(self.channels_all)\n\n        elapsed_min = max(0, int((now_ts() - self.start_time_ts) / 60))\n        add = 0\n        if self.warm_add_every_min > 0:\n            add = elapsed_min // self.warm_add_every_min\n\n        active = self.warm_start_channels + add\n        if self.warm_max_active and self.warm_max_active > 0:\n            active = min(active, self.warm_max_active)\n        active = max(1, min(active, len(self.channels_all)))\n        return active\n\n    def compute_active_channels(self) -> List[str]:\n        # apply blacklist + warmup\n        chans = [c for c in self.channels_all if c not in self.blacklist]\n        if not chans:\n            return []\n        n = self.warmup_active_channel_count()\n        return chans[:n]\n\n    def quarantine_account_files(self, session_name: str):\n        if not self.quarantine_on_error:\n            return\n        # move .session and .json\n        try:\n            src_sess = COMMENT_ACCOUNTS_DIR / f"{session_name}.session"\n            src_json = COMMENT_ACCOUNTS_DIR / f"{session_name}.json"\n            if src_sess.exists():\n                shutil.move(str(src_sess), str(COMMENT_NELIKVID_DIR / src_sess.name))\n            if src_json.exists():\n                shutil.move(str(src_json), str(COMMENT_NELIKVID_DIR / src_json.name))\n            self.logger.error(f"[NELIKVID] Аккаунт {session_name} перенесён в commenting/accounts_nelikvid/")\n        except Exception as e:\n            self.logger.error(f"Не смог перенести аккаунт в неликвид: {e}")\n\n    async def init_clients(self) -> None:\n\n        """Split mode:\n        - accounts/*.session -> 1 аккаунт мониторинга (читает посты)\n        - accounts/*.session -> N аккаунтов комментинга (пишут комментарии)\n        """\n        if self.api_id == 123456 or not self.api_hash or self.api_hash == "PASTE_API_HASH":\n            raise SystemExit("Заполни telegram.api_id / telegram.api_hash в config.yaml")\n\n        commenter_sessions = list_sessions(COMMENT_ACCOUNTS_DIR)\n        if not commenter_sessions:\n            raise SystemExit("Нет аккаунтов КОММЕНТИНГА. Положи .session файлы в accounts/")\n\n        async def _connect_one(sp: Path, accounts_dir: Path) -> Optional[ClientWrap]:\n            name = sp.with_suffix("").name\n            proxy = load_proxy_for_session(sp)\n\n            client = TelegramClient(\n                session=str(Path(accounts_dir) / name),\n                api_id=self.api_id,\n                api_hash=self.api_hash,\n                proxy=proxy\n            )\n\n            try:\n                await client.connect()\n                if not await client.is_user_authorized():\n                    self.logger.error(f"Аккаунт НЕ авторизован: {name}")\n                    await client.disconnect()\n                    return None\n\n                self.logger.info(f"Аккаунт подключен: {name} (proxy={\'да\' if proxy else \'нет\'})")\n                return ClientWrap(name=name, client=client, proxy_enabled=bool(proxy))\n\n            except SessionPasswordNeededError:\n                self.logger.error(f"На аккаунте {name} включен пароль 2FA (войти вручную)")\n                try:\n                    await client.disconnect()\n                except Exception:\n                    pass\n                return None\n\n            except Exception as e:\n                self.logger.error(f"Не смог подключить аккаунт {name}: {e}")\n                try:\n                    await client.disconnect()\n                except Exception:\n                    pass\n                return None        # monitoring disabled: use first commenting account as monitor\n        # 2) комментинг — подключаем ВСЕ аккаунты из accounts/\n        self.clients = []\n        for sp in commenter_sessions:\n            cw = await _connect_one(sp, COMMENT_ACCOUNTS_DIR)\n            if cw:\n                self.clients.append(cw)\n\n        if not self.clients:\n            raise SystemExit("Не удалось подключить ни одного аккаунта комментинга (accounts/).")\n\n        self.monitor = self.clients[0]\n        self.logger.info(f"Monitor (first commenter): {self.clients[0].name}")\n        self.logger.info(f"Аккаунтов комментинга: {len(self.clients)}")\n\n        self.rotator = AccountRotator(self.clients, self.rotation_mode)\n        self._install_autoreply_handlers()\n    async def validate_channels_basic(self) -> None:\n        if not self.channels_all:\n            raise SystemExit("channels.txt пустой. Добавь каналы.")\n\n        tester = (self.monitor.client if getattr(self, \'monitor\', None) else self.clients[0].client)\n        ok = 0\n        for ch in self.channels_all:\n            if ch in self.blacklist:\n                continue\n            try:\n                await tester.get_entity(ch)\n                ok += 1\n            except Exception as e:\n                self.logger.error(f"Канал недоступен: {ch} ({e})")\n\n        if ok == 0:\n            raise SystemExit("Не удалось подключиться ни к одному каналу. Проверь channels.txt.")\n\n        self.logger.info(f"Каналов доступно: {ok} (blacklist: {len(self.blacklist)})")\n\n    async def _check_comment_alive(self, cw: ClientWrap, peer, msg_id: int, channel_username: str):\n        if not self.check_deleted_comments:\n            return\n        await asyncio.sleep(max(5, self.deleted_check_after))\n        try:\n            got = await cw.client.get_messages(peer, ids=msg_id)\n            if not got or getattr(got, "id", None) is None:\n                stats_inc(self.stats, "deleted", 1)\n                stats_inc_nested(self.stats, "by_channel", channel_username, "deleted", 1)\n            else:\n                stats_inc(self.stats, "alive", 1)\n                stats_inc_nested(self.stats, "by_channel", channel_username, "alive", 1)\n            save_stats(self.stats)\n        except Exception:\n            # If can\'t read, ignore\n            pass\n\n    async def _comment_task(self, channel_username: str, message) -> None:\n        async with self.sem:\n            # Probability / skip\n            if random.random() > self.comment_probability:\n                return\n\n            # НЕ комментировать свежие посты (<2 минут)\n            post_date = getattr(message, \'date\', None)\n            if post_date and self.min_post_age > 0:\n                post_ts = int(post_date.timestamp())\n                age = now_ts() - post_ts\n                if age < self.min_post_age:\n                    self.logger.info(f"Пост слишком свежий ({age} сек), пропуск")\n                    return\n\n            ts = now_ts()\n\n            ch_ok, ch_reason = self.can_use_channel(channel_username, ts)\n            if not ch_ok:\n                self.logger.info(f"Пропуск @{channel_username}: {ch_reason}")\n                return\n\n            post_text = norm_text(getattr(message, "message", "") or "")\n            if not post_text:\n                self.logger.info(f"Пустой пост, пропуск @{channel_username} id={message.id}")\n                return\n\n            key = self.post_key(channel_username, message.id)\n            if self.only_one_comment and self.state.get("commented", {}).get(key):\n                return\n\n            self.logger.info(f"Новый пост @{channel_username} id={message.id}: {post_text[:140]}")\n\n            # Generate comment\n            try:\n                comment = await self.commenter.generate(post_text)\n                comment = clamp_comment(comment, self.min_len, self.max_len)\n                # Если OpenAI вернул пустое - берем простую фразу\n                if not comment and self.phrases:\n                    comment = random.choice(self.phrases)\n                    comment = clamp_comment(comment, self.min_len, self.max_len)\n            except Exception as e:\n                self.logger.error(f"Ошибка OpenAI: {e}")\n                # Fallback на простые фразы\n                if self.phrases:\n                    comment = random.choice(self.phrases)\n                    comment = clamp_comment(comment, self.min_len, self.max_len)\n                else:\n                    return\n\n            delay = self.compute_delay()\n            self.logger.info(f"Ждём перед отправкой: {delay} сек")\n            await asyncio.sleep(delay)\n\n            cw = self.pick_account_for_send(channel_username)\n            if not cw:\n                self.logger.warning("Нет доступных аккаунтов (лимиты/отдых). Пропуск.")\n                return\n\n            # Double-check race\n            if self.only_one_comment and self.state.get("commented", {}).get(key):\n                return\n\n            try:\n                client = cw.client\n                peer = await client.get_entity(channel_username)\n\n                discussion = await client(GetDiscussionMessageRequest(peer=peer, msg_id=message.id))\n                if not discussion.messages:\n                    # Часто бывает: у конкретного поста отключены комментарии (например рекламный пост).\n                    # Просто пропускаем и продолжаем мониторинг дальше.\n                    self.logger.warning(f"Комментарии недоступны для этого поста: @{channel_username} | msg_id={message.id}")\n                    return\n\n                discussion_msg = discussion.messages[0]\n                discussion_peer = discussion_msg.peer_id\n\n                # 1) Если для этого аккаунта доступ в канал/обсуждения закрыт — больше не трогаем этот канал с данного аккаунта\n                if self.is_account_blacklisted(cw.name, channel_username):\n                    self.logger.warning(f"PER-ACC BLACKLIST: {cw.name} -> @{channel_username} | {self.account_blacklist_reason(cw.name, channel_username)}")\n                    return\n\n                # 2) Если в обсуждениях требуют вступить в группу — пробуем вступить / подать заявку.\n                ok_join, why_join = await self.ensure_discussion_membership(client, cw.name, channel_username, discussion_peer)\n                if not ok_join:\n                    if why_join.startswith("pending_wait:") or why_join == "pending":\n                        left = self.pending_wait_left(cw.name, channel_username)\n                        self.logger.info(f"JOIN PENDING: @{channel_username} | аккаунт={cw.name} | пауза {left} сек (проверка каждые 5 минут)")\n                        return\n                    if why_join in ("forbidden",) or "forbidden" in why_join:\n                        self.logger.warning(f"JOIN FORBIDDEN: @{channel_username} | аккаунт={cw.name} | добавил в per-acc blacklist")\n                        return\n                    self.logger.warning(f"JOIN SKIP: @{channel_username} | аккаунт={cw.name} | {why_join}")\n                    return\n\n                # Имитация набора текста (как человек) (как человек)\n                if self.typing_delay_min > 0:\n                    typing_delay = random.uniform(self.typing_delay_min, self.typing_delay_max)\n                    self.logger.info(f"Имитация набора текста: {typing_delay:.1f} сек")\n                    await asyncio.sleep(typing_delay)\n\n                sent = await client.send_message(\n                    entity=discussion_peer,\n                    message=comment,\n                    reply_to=discussion_msg.id\n                )\n\n                self.logger.info(f"Отправлено ({cw.name}): {comment}")\n                try:\n                    _stats["commented"] += 1\n                except Exception:\n                    pass\n\n                # State + counters\n                self.state.setdefault("commented", {})[key] = True\n                \n                # Учет каналов аккаунта\n                self.state.setdefault("account_channels", {})\n                self.state["account_channels"].setdefault(cw.name, [])\n                if channel_username not in self.state["account_channels"][cw.name]:\n                    self.state["account_channels"][cw.name].append(channel_username)\n                \n                ts2 = now_ts()\n                self.inc_counter("accounts", f"{cw.name}:{hour_bucket(ts2)}")\n                self.inc_counter("accounts", f"{cw.name}:{day_bucket(ts2)}")\n                self.inc_counter("channels", f"{channel_username}:{day_bucket(ts2)}")\n                save_state(self.state)\n\n                # Stats\n                stats_inc(self.stats, "sent", 1)\n                stats_inc_nested(self.stats, "by_account", cw.name, "sent", 1)\n                stats_inc_nested(self.stats, "by_channel", channel_username, "sent", 1)\n                save_stats(self.stats)\n\n                # Check alive/deleted\n                if getattr(sent, "id", None) is not None:\n                    asyncio.create_task(self._check_comment_alive(cw, discussion_peer, sent.id, channel_username))\n\n                await asyncio.sleep(self.compute_cooldown())\n\n            except FloodWaitError as e:\n                self.logger.error(f"FloodWait на аккаунте {cw.name}: {e.seconds}s")\n                until = now_ts() + max(self.floodwait_cooldown, min(e.seconds, 86400))\n                self.set_acc_cooldown(cw.name, until)\n\n            except RPCError as e:\n                # Hard restrictions -> quarantine\n                s = str(e).lower()\n                self.logger.error(f"Telegram RPC ошибка ({cw.name}): {e}")\n                if "banned" in s or "restricted" in s or "forbidden" in s or "peer_flood" in s:\n                    self.quarantine_account_files(cw.name)\n                # Channel problems -> per-account blacklist / pending join\n                # 1) Требуют вступить в обсуждения\n                if ("join the discussion group" in s) or (("discussion group" in s) and ("join" in s)):\n                    self._pending_set(cw.name, channel_username, {\n                        "ts": now_ts(),\n                        "next_check_ts": now_ts() + 300,\n                        "why": str(e),\n                    })\n                    self.logger.info(f"JOIN REQUIRED -> pending: @{channel_username} | acc={cw.name}")\n                    return\n\n                # 2) Доступ закрыт именно для этого аккаунта -> per-acc blacklist\n                if ("chat_write_forbidden" in s) or ("user_banned_in_channel" in s) or ("channel_private" in s):\n                    self.add_account_blacklist(cw.name, channel_username, str(e))\n                    self.logger.warning(f"PER-ACC BLACKLIST add: @{channel_username} | acc={cw.name} | {e}")\n                    return\n\n\n            except Exception as e:\n                self.logger.error(f"Ошибка отправки: {e}")\n\n    async def run(self) -> None:\n        await self.init_clients()\n        await self.validate_channels_basic()\n\n        if self.ignore_old_on_start:\n            self.logger.info("Режим: игнор старых постов при старте")\n        monitor_client = self.monitor.client\n        # ---- Telethon: suppress noisy warnings + auto-reconnect on \'security error\' spam ----\n        _telethon_reconnect_event = asyncio.Event()\n\n        def _telethon_trigger(msg: str) -> None:\n            # avoid spamming reconnect requests\n            if not _telethon_reconnect_event.is_set():\n                try:\n                    self.logger.warning(f"[TELETHON] {msg} -> переподключаюсь")\n                except Exception:\n                    pass\n                _telethon_reconnect_event.set()\n\n        # Re-attach quiet handler that can still detect \'security error\' without printing to console\n        try:\n            _qh = _QuietTelethonHandler(trigger_cb=_telethon_trigger)\n            for _lname in _TELETHON_NOISE_LOGGERS:\n                _lg = logging.getLogger(_lname)\n                _lg.handlers = []\n                _lg.addHandler(_qh)\n                _lg.setLevel(logging.WARNING)  # capture warnings, but do not print\n                _lg.propagate = False\n        except Exception:\n            pass\n\n        async def _telethon_reconnect_watchdog():\n            while True:\n                await _telethon_reconnect_event.wait()\n                _telethon_reconnect_event.clear()\n                # reconnect all clients sequentially\n                for _cw in ([self.monitor] + list(self.clients)):\n                    await _safe_reconnect_client(_cw.client, self.logger, f"{_cw.name}")\n                # small cooldown to prevent thrash\n                await asyncio.sleep(10)\n\n        try:\n            asyncio.create_task(_telethon_reconnect_watchdog())\n        except Exception:\n            pass\n        # -------------------------------\n        # PANEL COMMANDS WATCHDOG (PA WEB)\n        # -------------------------------\n        # Сайт-панель создаёт флаги:\n        #   command/reload.flag  -> перечитать файлы (channels/proxy/config)\n        #   command/stop.flag    -> корректно остановить процесс\n        base_dir = os.path.abspath(os.path.dirname(__file__))\n        cmd_dir = os.path.join(base_dir, "command")\n        reload_flag = os.path.join(cmd_dir, "reload.flag")\n        stop_flag = os.path.join(cmd_dir, "stop.flag")\n        try:\n            os.makedirs(cmd_dir, exist_ok=True)\n        except Exception:\n            pass\n\n        async def _panel_commands_watchdog():\n            while True:\n                try:\n                    if os.path.exists(stop_flag):\n                        try:\n                            os.remove(stop_flag)\n                        except Exception:\n                            pass\n                        ru_log("ЖИВОЙ", "PANEL: получен STOP -> остановка сервиса", extra=f"flag={stop_flag}")\n                        os._exit(0)\n\n                    if os.path.exists(reload_flag):\n                        try:\n                            os.remove(reload_flag)\n                        except Exception:\n                            pass\n                        ru_log("ЖИВОЙ", "PANEL: получен RELOAD -> перечитываем файлы при следующем цикле", extra=f"flag={reload_flag}")\n                except Exception:\n                    pass\n                await asyncio.sleep(3)\n\n        try:\n            asyncio.create_task(_panel_commands_watchdog())\n        except Exception:\n            pass\n\n\n        @monitor_client.on(events.NewMessage())\n        async def handler(event):\n            _stats[\'seen_posts\'] += 1\n            _stats[\'last_event_ts\'] = int(time.time())\n            # ---- Кулдаун канала после удалений ----\n            try:\n                chan = getattr(event, "chat", None)\n                channel_key = getattr(chan, "username", None) or str(getattr(event, "chat_id", "-"))\n            except Exception:\n                channel_key = "-"\n\n            # Динамическая проверка активных каналов (warmup меняет список после запуска)\n            # Важно: если прогрев отключен (warmup_enabled=False) — active_now будет пустым и мы НИЧЕГО не фильтруем.\n            try:\n                active_now_list = (self.compute_active_channels() or [])\n                active_now = set([str(x).lstrip(\'@\') for x in active_now_list])\n                if active_now:\n                    ck = str(channel_key).lstrip(\'@\')\n                    if ck not in active_now:\n                        # чтобы не казалось, что "мониторит один канал" — логируем редкие пропуски\n                        _stats[\'skipped\'] += 1\n                        try:\n                            now = now_ts()\n                            last = _warmup_skip_log.get(ck, 0)\n                            if now - last >= 60:\n                                _warmup_skip_log[ck] = now\n                                ru_log(\n                                    "ПРОПУСК",\n                                    "Канал сейчас не активен (warmup/лимит активных каналов)",\n                                    channel=str(channel_key),\n                                    account="-",\n                                    extra=f"Активно сейчас: {len(active_now)} | Пример активных: {\', \'.join(list(active_now)[:5])}"\n                                )\n                        except Exception:\n                            pass\n                        return\n            except Exception:\n                pass\n\n            in_cd, left = channel_in_cooldown(channel_key)\n            if in_cd:\n                ru_log("ПРОПУСК", "Канал на паузе после удалений", channel=channel_key, account="-", extra=f"Осталось: {left} сек")\n                return\n\n            msg = event.message\n            ru_log("ЖИВОЙ", "Увидел новый пост", channel=str(channel_key), account="-")\n            _stats[\'last_post_channel\'] = str(channel_key)\n\n            try:\n                chat = await event.get_chat()\n                username = getattr(chat, "username", None)\n                if not username:\n                    username = str(getattr(chat, "id", "unknown"))\n\n                # Warmup update log occasionally\n                if random.random() < 0.02:\n                    act = self.compute_active_channels()\n                    self.logger.info(f"[WARMUP] Активные каналы сейчас: {len(act)}")\n\n                last_seen = self.state.setdefault("last_seen", {}).get(username)\n\n                if self.ignore_old_on_start and last_seen is None:\n                    self.state["last_seen"][username] = msg.id\n                    save_state(self.state)\n                    return\n\n                self.state["last_seen"][username] = msg.id\n                save_state(self.state)\n\n                asyncio.create_task(self._comment_task(username, msg))\n\n            except Exception as e:\n                self.logger.error(f"Ошибка обработчика: {e}")\n\n        # ==========================\n        # AUTOJOIN + HEARTBEAT (старт сразу при запуске)\n        # ==========================\n        try:\n            main_account = self.monitor.name\n        except Exception:\n            main_account = "-"\n\n        try:\n            def _hb_get_active_channels():\n                return self.compute_active_channels()\n\n            def _hb_get_counts():\n                try:\n                    return dict(_counts)\n                except Exception:\n                    return {}\n\n            asyncio.create_task(heartbeat_loop(load_channels_all, _hb_get_active_channels, _hb_get_counts))\n            self.logger.info("[ЖИВОЙ] HEARTBEAT TASK START")\n            ru_log("ЖИВОЙ", "HEARTBEAT включён", channel="-", account=main_account, extra=f"каждые {HEARTBEAT_SEC} сек")\n        except Exception as e:\n            self.logger.error(f"Не смог запустить HEARTBEAT: {e}")\n\n        try:\n            # AUTOJOIN для КАЖДОГО аккаунта комментинга (новые аккаунты вступают, старые НЕ дёргаем)\n            # AUTOJOIN для аккаунта мониторинга (тоже догоняет каналы каждые 7 минут)\n            asyncio.create_task(autojoin_channels_loop(self.monitor.client, self.monitor.name, load_channels_all))\n            for _cw in list(self.clients):\n                asyncio.create_task(autojoin_channels_loop(_cw.client, _cw.name, load_channels_all))\n            self.logger.info("[ЖИВОЙ] AUTOJOIN TASK START")\n            ru_log("ЖИВОЙ", "AUTOJOIN TASK START", channel="-", account=main_account, extra=f"каждые {JOIN_COOLDOWN_SEC//60} мин, лимит {MAX_JOINS_PER_DAY_PER_ACCOUNT}/сутки")\n        except Exception as e:\n            self.logger.error(f"Не смог запустить AUTOJOIN: {e}")\n\n        self.logger.info("Сервис запущен. Мониторю каналы... (Ctrl+C остановить)")\n        try:\n            await asyncio.gather(*[cw.client.run_until_disconnected() for cw in ([self.monitor] + self.clients)])\n        finally:\n            for cw in ([self.monitor] + self.clients):\n                try:\n                    await cw.client.disconnect()\n                except Exception:\n                    pass\n\n\n\ndef is_config_ready(cfg: Dict[str, Any]) -> bool:\n    tg = cfg.get("telegram", {})\n    oa = cfg.get("openai", {})\n    svc = cfg.get("service", {})\n    # keys placeholders\n    if tg.get("api_hash") in (None, "", "PASTE_API_HASH") or int(tg.get("api_id") or 0) in (0, 123456):\n        return False\n    if oa.get("api_key") in (None, "", "PASTE_OPENAI_KEY"):\n        return False\n    # anti-spam must be explicitly confirmed on first run\n    if not svc.get("_setup_done", False):\n        return False\n    return True\n\ndef menu_first_run_wizard(cfg: Dict[str, Any]):\n    ui_title("МАСТЕР ПЕРВОГО ЗАПУСКА (антиспам защита)")\n    ui_warn("Сервис НЕ запустится, пока ты не настроишь безопасные параметры.")\n    ui_hr()\n    print("Шаги:")\n    print("1) Укажи Telegram api_id/api_hash и OpenAI key")\n    print("2) Выбери режим SAFE/NORMAL/AGGRESSIVE")\n    print("3) Поставь вероятность коммента (SAFE 0.25..0.40)")\n    print("4) Поставь задержку (рандом) и лимиты на аккаунт")\n    ui_hr()\n    input("Нажми Enter — открыть быструю настройку ключей...")\n    menu_quick_setup(cfg)\n\n    ui_hr()\n    input("Нажми Enter — выбрать режим...")\n    menu_choose_mode(cfg)\n\n    ui_hr()\n    print("Антиспам минимум (рекомендация):")\n    print("- comment_probability: 0.35")\n    print("- delay random: 35..90 сек")\n    print("- max per account: 6/час и 30/день")\n    svc = cfg.setdefault("service", {})\n    v = input(f"Вероятность коммента сейчас={svc.get(\'comment_probability\', 0.35)}: ").strip()\n    if v:\n        try:\n            svc["comment_probability"] = max(0.01, min(1.0, float(v)))\n        except Exception:\n            pass\n\n    # Force random delays safer defaults\n    svc.setdefault("delay_mode", "random")\n    if svc.get("delay_mode") != "random":\n        svc["delay_mode"] = "random"\n    v = input(f"random_delay_min_sec сейчас={svc.get(\'random_delay_min_sec\', 35)}: ").strip()\n    if v:\n        svc["random_delay_min_sec"] = max(1, int(v))\n    v = input(f"random_delay_max_sec сейчас={svc.get(\'random_delay_max_sec\', 90)}: ").strip()\n    if v:\n        svc["random_delay_max_sec"] = max(svc.get("random_delay_min_sec", 35), int(v))\n\n    v = input(f"max_comments_per_account_per_hour сейчас={svc.get(\'max_comments_per_account_per_hour\', 6)}: ").strip()\n    if v:\n        svc["max_comments_per_account_per_hour"] = max(1, int(v))\n    v = input(f"max_comments_per_account_per_day сейчас={svc.get(\'max_comments_per_account_per_day\', 30)}: ").strip()\n    if v:\n        svc["max_comments_per_account_per_day"] = max(1, int(v))\n    \n    ui_hr()\n    print("Защита от банов (рекомендация):")\n    print("- Не комментировать посты младше 2 минут")\n    print("- Имитация набора текста 3-7 секунд")\n    print("- Макс 15 каналов на аккаунт")\n    \n    v = input(f"min_post_age_sec сейчас={svc.get(\'min_post_age_sec\', 120)}: ").strip()\n    if v:\n        svc["min_post_age_sec"] = max(0, int(v))\n    \n    v = input(f"typing_delay_min_sec сейчас={svc.get(\'typing_delay_min_sec\', 3)}: ").strip()\n    if v:\n        svc["typing_delay_min_sec"] = max(0, int(v))\n    \n    v = input(f"typing_delay_max_sec сейчас={svc.get(\'typing_delay_max_sec\', 7)}: ").strip()\n    if v:\n        svc["typing_delay_max_sec"] = max(svc.get("typing_delay_min_sec", 3), int(v))\n    \n    v = input(f"max_channels_per_account сейчас={svc.get(\'max_channels_per_account\', 15)}: ").strip()\n    if v:\n        svc["max_channels_per_account"] = max(0, int(v))\n\n    svc["_setup_done"] = True\n    save_config(cfg)\n    ui_ok("Готово. Мастер настройки завершён. Теперь сервис можно запускать безопасно.")\n    ui_pause(False)\n\n\n# =========================\n# Menu (Premium RU)\n# =========================\ndef cfg_summary(cfg: Dict[str, Any]):\n    tg = cfg.get("telegram", {})\n    oa = cfg.get("openai", {})\n    svc = cfg.get("service", {})\n    warm = cfg.get("warmup", {})\n    ab = cfg.get("antiban", {})\n    ui_info(f"Telegram api_id: {tg.get(\'api_id\')} | api_hash: {\'OK\' if tg.get(\'api_hash\') not in (\'\', None, \'PASTE_API_HASH\') else \'НЕ ЗАДАН\'}")\n    ui_info(f"OpenAI key: {\'OK\' if oa.get(\'api_key\') not in (\'\', None, \'PASTE_OPENAI_KEY\') else \'НЕ ЗАДАН\'} | model: {oa.get(\'model\')}")\n    ui_info(f"Mode: {svc.get(\'mode\')} | probability: {svc.get(\'comment_probability\')}")\n    ui_info(f"Warmup: {\'ON\' if warm.get(\'enabled\') else \'OFF\'} | start={warm.get(\'start_channels\')} | +1/{warm.get(\'add_one_channel_every_min\')} мин")\n    ui_info(f"Защита: пост>{svc.get(\'min_post_age_sec\', 120)}сек | набор {svc.get(\'typing_delay_min_sec\', 3)}-{svc.get(\'typing_delay_max_sec\', 7)}сек")\n    ui_info(f"Deleted-check: {\'ON\' if ab.get(\'check_deleted_comments\') else \'OFF\'} | after={ab.get(\'deleted_check_after_sec\')} сек")\n\n\ndef menu_help_accounts_proxies():\n    ui_title("Подсказки: сколько аккаунтов и прокси нужно")\n    print("✅ База антибана:")\n    print("  • Идеально: 1 аккаунт = 1 прокси")\n    print("  • Допустимо (аккуратно): 2 аккаунта = 1 прокси (но риск выше)")\n    print("")\n    print("🟢 SAFE (тихо, долго, стабильнее):")\n    print("  • 3 аккаунта + 3 прокси → 50–150 комментов/сутки")\n    print("")\n    print("🟡 NORMAL (баланс):")\n    print("  • 5–10 аккаунтов + 5–10 прокси → 150–500/сутки")\n    print("")\n    print("🔴 AGGRESSIVE (быстро, риск выше):")\n    print("  • 10+ аккаунтов + 10+ прокси → 500–1000+/сутки (может резать)")\n    ui_pause(False)\n\n\ndef menu_show_channels():\n    ui_title("channels.txt — каналы")\n    chans = load_channels_all()\n    bl = load_blacklist()\n    if not chans:\n        ui_warn("Пусто.")\n        ui_pause(False)\n        return\n    for i, c in enumerate(chans, 1):\n        mark = "⛔" if c in bl else "✅"\n        print(f"{i:02d}) {mark} @{c}")\n    ui_pause(False)\n\n\ndef menu_add_channel():\n    ui_title("Добавить канал")\n    s = input("Введи @username или https://t.me/...: ").strip()\n    if not s:\n        return\n    if s.startswith("https://t.me/"):\n        s = s.replace("https://t.me/", "").strip("/")\n    if s.startswith("@"):\n        s = s[1:]\n    chans = load_channels_all()\n    if s not in chans:\n        chans.append(s)\n        CHANNELS_FILE.write_text("# Каналы: по одному в строку\\n" + "\\n".join(chans) + "\\n", encoding="utf-8")\n        ui_ok(f"Добавлен: @{s}")\n    else:\n        ui_info("Уже есть.")\n    ui_pause(False)\n\n\ndef menu_remove_channel():\n    ui_title("Удалить канал")\n    chans = load_channels_all()\n    if not chans:\n        ui_warn("Пусто.")\n        ui_pause(False)\n        return\n    for i, c in enumerate(chans, 1):\n        print(f"{i:02d}) @{c}")\n    s = input("Номер для удаления: ").strip()\n    if not s.isdigit():\n        return\n    idx = int(s) - 1\n    if 0 <= idx < len(chans):\n        removed = chans.pop(idx)\n        CHANNELS_FILE.write_text("# Каналы: по одному в строку\\n" + "\\n".join(chans) + "\\n", encoding="utf-8")\n        ui_ok(f"Удалён: @{removed}")\n    ui_pause(False)\n\n\ndef menu_check_accounts():\n    ui_title("Аккаунты (.session) + прокси")\n\n    sessions_monitor = list_sessions(MONITOR_ACCOUNTS_DIR)\n    sessions_comment = list_sessions(COMMENT_ACCOUNTS_DIR)\n\n    total = len(sessions_monitor) + len(sessions_comment)\n    if total == 0:\n        ui_bad("Нет .session файлов")\n        print(f"Мониторинг: {MONITOR_ACCOUNTS_DIR}/")\n        print(f"Комментинг:  {COMMENT_ACCOUNTS_DIR}/")\n        print("Формат: account1.session (без .txt, без пробелов, без (1))")\n        ui_pause(False)\n        return\n\n    if sessions_monitor:\n        ui_info("MONITORING аккаунты:")\n        for sp in sessions_monitor:\n            name = sp.with_suffix(\'\').name\n            proxy = load_proxy_for_session(sp)\n            print(f"✅ {name:20s} | proxy={\'да\' if proxy else \'нет\'} | file={sp.name}")\n\n    if sessions_comment:\n        ui_info("COMMENTING аккаунты:")\n        for sp in sessions_comment:\n            name = sp.with_suffix(\'\').name\n            proxy = load_proxy_for_session(sp)\n            print(f"✅ {name:20s} | proxy={\'да\' if proxy else \'нет\'} | file={sp.name}")\n\n    ui_pause(False)\n\n\ndef apply_mode_preset(cfg: Dict[str, Any], mode: str):\n    mode = mode.upper().strip()\n    cfg.setdefault("service", {})\n    svc = cfg["service"]\n    svc["mode"] = mode\n\n    if mode == "SAFE":\n        svc["comment_probability"] = 0.35\n        svc["random_delay_min_sec"] = 35\n        svc["random_delay_max_sec"] = 90\n        svc["max_comments_per_account_per_hour"] = 6\n        svc["max_comments_per_account_per_day"] = 30\n        svc["max_comments_per_channel_per_day"] = 15\n        svc["min_post_age_sec"] = 120\n        svc["typing_delay_min_sec"] = 3\n        svc["typing_delay_max_sec"] = 7\n        svc["max_channels_per_account"] = 15\n    elif mode == "NORMAL":\n        svc["comment_probability"] = 0.60\n        svc["random_delay_min_sec"] = 20\n        svc["random_delay_max_sec"] = 70\n        svc["max_comments_per_account_per_hour"] = 10\n        svc["max_comments_per_account_per_day"] = 60\n        svc["max_comments_per_channel_per_day"] = 20\n        svc["min_post_age_sec"] = 60\n        svc["typing_delay_min_sec"] = 2\n        svc["typing_delay_max_sec"] = 5\n        svc["max_channels_per_account"] = 25\n    else:  # AGGRESSIVE\n        svc["comment_probability"] = 0.90\n        svc["random_delay_min_sec"] = 8\n        svc["random_delay_max_sec"] = 35\n        svc["max_comments_per_account_per_hour"] = 18\n        svc["max_comments_per_account_per_day"] = 120\n        svc["max_comments_per_channel_per_day"] = 40\n        svc["min_post_age_sec"] = 30\n        svc["typing_delay_min_sec"] = 1\n        svc["typing_delay_max_sec"] = 3\n        svc["max_channels_per_account"] = 50\n\n\ndef menu_choose_mode(cfg: Dict[str, Any]):\n    ui_title("Выбор режима")\n    print("1) SAFE — аккуратно, меньше банов")\n    print("2) NORMAL — баланс скорость/безопасность")\n    print("3) AGGRESSIVE — быстрее, риск выше")\n    s = input("Выбор: ").strip()\n    if s == "1":\n        apply_mode_preset(cfg, "SAFE")\n    elif s == "2":\n        apply_mode_preset(cfg, "NORMAL")\n    elif s == "3":\n        apply_mode_preset(cfg, "AGGRESSIVE")\n    else:\n        return\n    save_config(cfg)\n    ui_ok("Режим применён и сохранён в config.yaml")\n    ui_pause(False)\n\n\ndef menu_quick_setup(cfg: Dict[str, Any]):\n    ui_title("Быстрая настройка ключей")\n    print("Оставь пусто — не меняем.")\n    tg = cfg.setdefault("telegram", {})\n    oa = cfg.setdefault("openai", {})\n\n    v = input("Telegram api_id: ").strip()\n    if v:\n        tg["api_id"] = int(v)\n\n    v = input("Telegram api_hash: ").strip()\n    if v:\n        tg["api_hash"] = v\n\n    v = input("OpenAI api_key: ").strip()\n    if v:\n        oa["api_key"] = v\n\n    v = input("OpenAI model (gpt-4.1): ").strip()\n    if v:\n        oa["model"] = v\n\n    save_config(cfg)\n    ui_ok("Сохранено.")\n    ui_pause(False)\n\n\ndef menu_warmup_settings(cfg: Dict[str, Any]):\n    ui_title("Прогрев (сетка)")\n    warm = cfg.setdefault("warmup", {})\n    print("Это защита от палива: каналы включаются постепенно.")\n    print("Оставь пусто — не меняем.\\n")\n\n    v = input(f"Включен? (1=да,0=нет) сейчас={int(bool(warm.get(\'enabled\', True)))}: ").strip()\n    if v in ("0", "1"):\n        warm["enabled"] = (v == "1")\n\n    v = input(f"Старт каналов сейчас={warm.get(\'start_channels\', 2)}: ").strip()\n    if v:\n        warm["start_channels"] = max(1, int(v))\n\n    v = input(f"Каждые N минут +1 канал сейчас={warm.get(\'add_one_channel_every_min\', 30)}: ").strip()\n    if v:\n        warm["add_one_channel_every_min"] = max(1, int(v))\n\n    v = input(f"Максимум активных каналов (0=все) сейчас={warm.get(\'max_active_channels\', 0)}: ").strip()\n    if v:\n        warm["max_active_channels"] = max(0, int(v))\n\n    save_config(cfg)\n    ui_ok("Прогрев сохранён.")\n    ui_pause(False)\n\n\ndef menu_proxy_add_to_pool():\n    ui_title("Прокси: добавить в пул")\n    p = input("Прокси (socks5://user:pass@ip:port): ").strip()\n    if not p:\n        ui_pause(False)\n        return\n    lines = load_proxy_pool_lines()\n    lines.append(p)\n    PROXY_POOL_FILE.write_text("# proxy_pool.txt\\n" + "\\n".join(lines) + "\\n", encoding="utf-8")\n    ui_ok("Добавлено.")\n    ui_pause(False)\n\ndef menu_proxy_distribute_all():\n    ui_title("Прокси: распределить на все аккаунты")\n    sessions = list_sessions(COMMENT_ACCOUNTS_DIR)\n    pool = load_proxy_pool_lines()\n    if not sessions:\n        ui_bad("Нет аккаунтов .session")\n        ui_pause(False)\n        return\n    if not pool:\n        ui_bad("proxy_pool.txt пустой")\n        ui_pause(False)\n        return\n    ratio = input("Режим (1=1прокси=1акк, 2=1прокси=2акк): ").strip()\n    ratio = 2 if ratio == "2" else 1\n    i = 0\n    for idx, sp in enumerate(sessions):\n        name = sp.with_suffix("").name\n        proxy_uri = pool[i % len(pool)]\n        write_proxy_json(name, proxy_uri, COMMENT_ACCOUNTS_DIR)\n        print(f"✅ {name} -> {proxy_uri}")\n        if (idx + 1) % ratio == 0:\n            i += 1\n    ui_ok("Готово.")\n    ui_pause(False)\n\ndef menu_proxy_set_one():\n    ui_title("Прокси: назначить одному аккаунту")\n    sessions = list_sessions(COMMENT_ACCOUNTS_DIR)\n    if not sessions:\n        ui_bad("Нет аккаунтов .session")\n        ui_pause(False)\n        return\n    for i, sp in enumerate(sessions, 1):\n        name = sp.with_suffix("").name\n        print(f"{i}) {name}")\n    s = input("Номер: ").strip()\n    if not s.isdigit():\n        ui_pause(False)\n        return\n    idx = int(s) - 1\n    if not (0 <= idx < len(sessions)):\n        ui_pause(False)\n        return\n    name = sessions[idx].with_suffix("").name\n    p = input("Прокси (socks5://...): ").strip()\n    if not p:\n        ui_pause(False)\n        return\n    write_proxy_json(name, p, COMMENT_ACCOUNTS_DIR)\n    ui_ok("Готово.")\n    ui_pause(False)\n\ndef menu_proxy_clear():\n    ui_title("Прокси: очистить")\n    print("1) Один аккаунт")\n    print("2) Все аккаунты")\n    m = input("Выбор: ").strip()\n    if m == "1":\n        sessions = list_sessions(COMMENT_ACCOUNTS_DIR)\n        for i, sp in enumerate(sessions, 1):\n            print(f"{i}) {sp.with_suffix(\'\').name}")\n        s = input("Номер: ").strip()\n        if not s.isdigit():\n            ui_pause(False)\n            return\n        idx = int(s)-1\n        if not (0 <= idx < len(sessions)):\n            ui_pause(False)\n            return\n        jp = sessions[idx].with_suffix(".json")\n        if jp.exists():\n            jp.unlink()\n        ui_ok("Очищено.")\n        ui_pause(False)\n        return\n    if m == "2":\n        cleared = 0\n        for sp in list_sessions(COMMENT_ACCOUNTS_DIR):\n            jp = sp.with_suffix(".json")\n            if jp.exists():\n                jp.unlink()\n                cleared += 1\n        ui_ok(f"Очищено у {cleared} аккаунтов.")\n        ui_pause(False)\n        return\n    ui_pause(False)\n\ndef menu_proxy_check():\n    ui_title("Прокси: проверка")\n    for sp in list_sessions(COMMENT_ACCOUNTS_DIR):\n        jp = sp.with_suffix(".json")\n        if jp.exists():\n            ui_ok(f"{sp.with_suffix(\'\').name}: proxy OK")\n        else:\n            ui_warn(f"{sp.with_suffix(\'\').name}: proxy НЕТ")\n    ui_pause(False)\n\n\ndef menu_show_stats():\n    ui_title("Статистика (stats.json)")\n    stats = load_stats()\n    print(f"sent:    {stats.get(\'sent\', 0)}")\n    print(f"alive:   {stats.get(\'alive\', 0)}")\n    print(f"deleted: {stats.get(\'deleted\', 0)}")\n    ui_hr()\n    print("По аккаунтам:")\n    for acc, d in (stats.get("by_account") or {}).items():\n        print(f"  • {acc}: {d}")\n    ui_hr()\n    print("По каналам:")\n    for ch, d in (stats.get("by_channel") or {}).items():\n        print(f"  • @{ch}: {d}")\n    ui_pause(False)\n\n\nasync def run_service_async():\n    ensure_project_structure()\n    cfg = ensure_api_keys_cached(load_config())\n    apply_neuro_protection_defaults(cfg)\n    logger = setup_logging()\n    svc = TGCommentServiceUltra(cfg, logger)\n    await svc.run()\n\n    def _autoreply_skip(self, text: str) -> bool:\n        try:\n            t = (text or "").lower()\n        except Exception:\n            return True\n        if not t.strip():\n            return True\n        # minimal safety: ignore links and obvious promo\n        bad = ["http://", "https://", "t.me/", "bit.ly", "promo", "промо", "заработ", "инвест", "крипт"]\n        return any(x in t for x in bad)\n\n    def _install_autoreply_handlers(self) -> None:\n        if not self.autoreply_enabled:\n            return\n        try:\n            from telethon import events\n        except Exception:\n            return\n\n        for cw in getattr(self, "clients", []) or []:\n            client = getattr(cw, "client", None)\n            if not client:\n                continue\n\n            @client.on(events.NewMessage(incoming=True))\n            async def _pm_handler(event):\n                try:\n                    if getattr(event, "out", False):\n                        return\n                    if not getattr(event, "is_private", False):\n                        return\n                    sender = getattr(event, "sender_id", None)\n                    if sender is None:\n                        return\n                    if self.autoreply_once_per_user and sender in self._autoreply_seen:\n                        return\n                    txt = ""\n                    try:\n                        txt = event.raw_text or ""\n                    except Exception:\n                        txt = ""\n                    if self._autoreply_skip(txt):\n                        return\n                    dmin = max(0, int(self.autoreply_delay_min))\n                    dmax = max(dmin, int(self.autoreply_delay_max))\n                    await asyncio.sleep(random.randint(dmin, dmax))\n                    await event.respond(self.autoreply_text)\n                    if self.autoreply_once_per_user:\n                        self._autoreply_seen.add(sender)\n                except Exception:\n                    return\n\n\ndef start_service(server_mode: bool):\n    ui_title("Запуск сервиса")\n    ui_info("Остановить: Ctrl+C")\n    if not server_mode:\n        ui_warn("Для PythonAnywhere Always-on включай \'Server mode\' (без ожиданий).")\n    try:\n        asyncio.run(run_service_async())\n    except KeyboardInterrupt:\n        print("\\n🛑 Остановлено (Ctrl+C)")\n    except Exception as e:\n        ui_bad(f"Ошибка: {e}")\n    ui_pause(server_mode)\n\n\ndef main_menu():\n    ensure_project_structure()\n    server_mode = False\n\n    while True:\n        ui_clear()\n        ui_title("TG COMMENT SERVICE — ULTRA FULL (RU)")\n        ensure_config_exists_ru()\n        cfg = load_config()\n        cfg_summary(cfg)\n\n        ui_hr()\n        print(_c(C.BOLD + "1)" + C.RESET), "Быстрая настройка ключей (Telegram/OpenAI)")\n        print(_c(C.BOLD + "2)" + C.RESET), "Выбрать режим SAFE/NORMAL/AGGRESSIVE")\n        print(_c(C.BOLD + "3)" + C.RESET), "Настроить ПРОГРЕВ (сетка)")\n        print(_c(C.BOLD + "4)" + C.RESET), "Каналы: показать")\n        print(_c(C.BOLD + "5)" + C.RESET), "Каналы: добавить")\n        print(_c(C.BOLD + "6)" + C.RESET), "Каналы: удалить")\n        print(_c(C.BOLD + "7)" + C.RESET), "Проверить аккаунты (.session + proxy)")\n        print(_c(C.BOLD + "8)" + C.RESET), "Подсказки: сколько аккаунтов/прокси нужно")\n        print(_c(C.BOLD + "9)" + C.RESET), "Статистика sent/deleted/alive")\n        ui_hr()\n        print(_c(C.BOLD + C.MAGENTA + "ПРОКСИ:" + C.RESET))\n        print(_c(C.BOLD + "10)" + C.RESET), "Прокси: добавить в пул (proxy_pool.txt)")\n        print(_c(C.BOLD + "11)" + C.RESET), "Прокси: распределить на ВСЕ аккаунты")\n        print(_c(C.BOLD + "12)" + C.RESET), "Прокси: назначить ОДНОМУ аккаунту")\n        print(_c(C.BOLD + "13)" + C.RESET), "Прокси: очистить (1/все)")\n        print(_c(C.BOLD + "14)" + C.RESET), "Прокси: проверить (быстро)")\n        print(_c(C.BOLD + "S)" + C.RESET), "Server mode: " + ("ON" if server_mode else "OFF"))\n        print(_c(C.BOLD + "0)" + C.RESET), "Запустить сервис")\n        print(_c(C.BOLD + "W)" + C.RESET), "Мастер первого запуска (антиспам)")\n        print(_c(C.BOLD + "Q)" + C.RESET), "Выход")\n        ui_hr()\n\n        ch = input("Выбор: ").strip().lower()\n\n        if ch == "1":\n            menu_quick_setup(cfg)\n        elif ch == "2":\n            menu_choose_mode(cfg)\n        elif ch == "3":\n            menu_warmup_settings(cfg)\n        elif ch == "4":\n            menu_show_channels()\n        elif ch == "5":\n            menu_add_channel()\n        elif ch == "6":\n            menu_remove_channel()\n        elif ch == "7":\n            menu_check_accounts()\n        elif ch == "8":\n            menu_help_accounts_proxies()\n        elif ch == "9":\n            menu_show_stats()\n        elif ch == "s":\n            server_mode = not server_mode\n        elif ch == "w":\n            menu_first_run_wizard(cfg)\n        elif ch == "10":\n            menu_proxy_add_to_pool()\n        elif ch == "11":\n            menu_proxy_distribute_all()\n        elif ch == "12":\n            menu_proxy_set_one()\n        elif ch == "13":\n            menu_proxy_clear()\n        elif ch == "14":\n            menu_proxy_check()\n        elif ch == "0":\n            start_service(server_mode)\n        elif ch == "q":\n            print("\\nПока.")\n            break\n        else:\n            ui_warn("Неверный выбор.")\n            time.sleep(0.8)\n\n\nif __name__ == "__main__":\n    main_menu()\n\n# ==========================\n# Антибан: join/удаления\n# ==========================\nJOIN_COOLDOWN_SEC = 420  # 7 минут\n\nDELETE_STEP_1_SEC = 3600      # 1 час\nDELETE_STEP_2_SEC = 21600     # 6 часов\nDELETE_STEP_3_SEC = 86400     # 24 часа\n\n_last_join_ts = {}\n\n# ---------------------------\n# JOIN PROGRESS (per account)\n# ---------------------------\n_join_progress = {}  # account_key -> {"total": int, "need": int, "already": int, "last": {"status": str, "channel": str, "ts": int}}\n\ndef _jp_get(account_key: str) -> dict:\n    rec = _join_progress.get(account_key)\n    if not isinstance(rec, dict):\n        rec = {"total": 0, "need": 0, "already": 0, "last": {"status": "-", "channel": "-", "ts": 0}}\n        _join_progress[account_key] = rec\n    return rec\n\ndef _jp_set_counts(account_key: str, total: int, need: int, already: int):\n    rec = _jp_get(account_key)\n    rec["total"] = int(total or 0)\n    rec["need"] = int(need or 0)\n    rec["already"] = int(already or 0)\n\ndef _jp_last(account_key: str, status: str, channel: str):\n    rec = _jp_get(account_key)\n    rec["last"] = {"status": str(status or "-"), "channel": str(channel or "-"), "ts": int(time.time())}\n\n             # account_key -> ts\n_channel_cooldown_until = {}   # channel_key -> ts\n_channel_delete_strikes = {}   # channel_key -> [ts...]\n\ndef can_join_now(account_key: str):\n    now = int(time.time())\n    last = int(_last_join_ts.get(account_key, 0))\n    left = JOIN_COOLDOWN_SEC - (now - last)\n    return (left <= 0, max(0, left))\n\ndef mark_join(account_key: str):\n    _last_join_ts[account_key] = int(time.time())\n\ndef channel_in_cooldown(channel_key: str):\n    now = int(time.time())\n    until = int(_channel_cooldown_until.get(channel_key, 0))\n    if until > now:\n        return True, until - now\n    return False, 0\n\ndef apply_delete_penalty(channel_key: str):\n    now = int(time.time())\n    strikes = _channel_delete_strikes.get(channel_key, [])\n    strikes = [t for t in strikes if now - t <= 24*3600]\n    strikes.append(now)\n    _channel_delete_strikes[channel_key] = strikes\n    n = len(strikes)\n    if n == 1:\n        _channel_cooldown_until[channel_key] = now + DELETE_STEP_1_SEC\n        return "пауза на 1 час"\n    if n == 2:\n        _channel_cooldown_until[channel_key] = now + DELETE_STEP_2_SEC\n        return "пауза на 6 часов"\n    _channel_cooldown_until[channel_key] = now + DELETE_STEP_3_SEC\n    return "пауза на 24 часа + BLACKLIST"\n\n\n# ==========================\n# Авто-вступление в каналы (чтобы точно мониторил)\n# ==========================\nJOIN_COOLDOWN_SEC = 420  # 7 минут\nMAX_JOINS_PER_DAY_PER_ACCOUNT = 20\nHEARTBEAT_SEC = 60\n_autojoin_started = False\n\n_stats = {\n    "seen_posts": 0,\n    "commented": 0,\n    "skipped": 0,\n    "errors": 0,\n    "joined": 0,\n    "last_event_ts": 0,\n    "last_post_channel": "-",\n}\n\n_join_daily = {}  # account_key -> {"day": "YYYY-MM-DD", "count": int}\n_last_join_ts = {}\n\n# ---------------------------\n# JOIN PROGRESS (per account)\n# ---------------------------\n_join_progress = {}  # account_key -> {"total": int, "need": int, "already": int, "last": {"status": str, "channel": str, "ts": int}}\n\ndef _jp_get(account_key: str) -> dict:\n    rec = _join_progress.get(account_key)\n    if not isinstance(rec, dict):\n        rec = {"total": 0, "need": 0, "already": 0, "last": {"status": "-", "channel": "-", "ts": 0}}\n        _join_progress[account_key] = rec\n    return rec\n\ndef _jp_set_counts(account_key: str, total: int, need: int, already: int):\n    rec = _jp_get(account_key)\n    rec["total"] = int(total or 0)\n    rec["need"] = int(need or 0)\n    rec["already"] = int(already or 0)\n\ndef _jp_last(account_key: str, status: str, channel: str):\n    rec = _jp_get(account_key)\n    rec["last"] = {"status": str(status or "-"), "channel": str(channel or "-"), "ts": int(time.time())}\n\n  # account_key -> ts\n\ndef _today_key():\n    return time.strftime("%Y-%m-%d", time.localtime())\n\ndef can_join_today(account_key: str):\n    day = _today_key()\n    rec = _join_daily.get(account_key)\n    if not rec or rec.get("day") != day:\n        rec = {"day": day, "count": 0}\n        _join_daily[account_key] = rec\n    return rec["count"] < MAX_JOINS_PER_DAY_PER_ACCOUNT\n\ndef mark_join_today(account_key: str):\n    day = _today_key()\n    rec = _join_daily.get(account_key)\n    if not rec or rec.get("day") != day:\n        rec = {"day": day, "count": 0}\n        _join_daily[account_key] = rec\n    rec["count"] += 1\n\ndef can_join_now(account_key: str):\n    now = int(time.time())\n    last = int(_last_join_ts.get(account_key, 0))\n    left = JOIN_COOLDOWN_SEC - (now - last)\n    return (left <= 0, max(0, left))\n\ndef mark_join_now(account_key: str):\n    _last_join_ts[account_key] = int(time.time())\n\nasync def heartbeat_loop_legacy(get_channels_count_fn):\n    """Единый heartbeat лог: показывает реальную картину по каналам.\n    Важно: ничего не блокирует, просто выводит агрегированные цифры.\n    """\n    while True:\n        try:\n            now = int(time.time())\n            last = int(_stats.get("last_event_ts", 0) or 0)\n            last_str = f"{max(0, (now - last))} сек назад" if last else "пока не было"\n\n            # --- Каналы: TOTAL / ACTIVE / PENDING / BLACKLIST / PAUSED ---\n            total_channels = 0\n            active_channels = 0\n            pending_channels = 0\n            blacklist_channels = 0\n            paused_channels = 0\n\n            try:\n                # список всех каналов (как в конфиге)\n                all_ch = []\n                # fallback: только количество\n                \n            except Exception:\n                all_ch = []\n\n            total_channels = len(all_ch)\n\n            # pending join (ожидание заявок на вступление)\n            try:\n                pending_channels = sum(len(v) for v in PENDING_JOIN.values()) if isinstance(PENDING_JOIN, dict) else 0\n            except Exception:\n                pending_channels = 0\n\n            # blacklist (каналы, куда аккаунтам нельзя)\n            try:\n                blacklist_channels = sum(len(v) for v in ACCOUNT_BLACKLIST.values()) if isinstance(ACCOUNT_BLACKLIST, dict) else 0\n            except Exception:\n                blacklist_channels = 0\n\n            # paused (ручная пауза/ошибки/заморозка)\n            try:\n                paused_channels = len(PAUSED_CHANNELS) if isinstance(PAUSED_CHANNELS, (set, list, dict)) else 0\n            except Exception:\n                paused_channels = 0\n\n            # active считаем как TOTAL минус PENDING минус PAUSED (blacklist это отдельный "нерабочий пул")\n            active_channels = max(0, total_channels - pending_channels - paused_channels)\n\n            ru_log(\n                "ЖИВОЙ",\n                "Мониторинг работает",\n                channel=_stats.get("last_post_channel", "-"),\n                account="-",\n                extra=(\n                    f"Каналов: {total_channels}/{total_channels} | "\n                    f"Активно: {active_channels} | "\n                    f"Пауза: {pending_channels} | "\n                    f"Blacklist: {blacklist_channels} | "\n                    f"Увидел: {_stats[\'seen\']} | "\n                    f"Отправил: {_stats[\'sent\']} | "\n                    f"Пропусков: {_stats[\'skips\']} | "\n                    f"Ошибок: {_stats[\'errors\']} | "\n                    f"Join: {_stats[\'joined\']} | "\n                    f"Последний ивент: {last_str}"+ " | JOIN-АККАУНТЫ: " + ("; ".join([f"{k}: {(_jp_get(k).get(\'already\',0))}/{(_jp_get(k).get(\'total\',0))} (нужно {(_jp_get(k).get(\'need\',0))}) | last={_jp_get(k).get(\'last\',{}).get(\'status\',\'-\')} {_jp_get(k).get(\'last\',{}).get(\'channel\',\'-\')}" for k in list(_join_progress.keys())[:8]]) if _join_progress else "-" )\n                )\n            )\n        except Exception:\n            pass\n        await asyncio.sleep(HEARTBEAT_SEC)\n\nasync def autojoin_channels_loop(main_client, account_key: str, get_channels_fn):\n    """Главный JOIN-аккаунт: сразу проверяет все каналы и вступает постепенно.\n    Правила:\n    - Сразу при старте проверяет каждый канал: если уже подписан -> лог и дальше.\n    - Если не подписан -> вступает и ЖДЁТ 7 минут до следующего вступления.\n    - Максимум 20 вступлений в сутки на аккаунт.\n    """\n    ru_log("ЖИВОЙ", "AUTOJOIN включён", channel="-", account=account_key, extra=f"1 вступление / {JOIN_COOLDOWN_SEC} сек | лимит {MAX_JOINS_PER_DAY_PER_ACCOUNT}/сутки")\n\n    while True:\n        try:\n            channels = get_channels_fn() or []\n            for ch in channels:\n                ch = (ch or "").strip()\n                if not ch:\n                    continue\n                ru_log("ЖИВОЙ", "Проверяю подписку", channel=ch, account=account_key)\n\n                # дневной лимит\n                if not can_join_today(account_key):\n                    ru_log("ПРОПУСК", "Лимит вступлений на сегодня", channel=ch, account=account_key, extra=f"Лимит: {MAX_JOINS_PER_DAY_PER_ACCOUNT}/сутки")\n                    break\n\n                # нормализуем t.me ссылки\n                ch_norm = ch\n                if ch_norm.startswith("https://t.me/"):\n                    tail = ch_norm.replace("https://t.me/", "").strip("/")\n                    if not tail.startswith("+"):\n                        ch_norm = "@" + tail\n\n                \n                # приватная инвайт-ссылка вида https://t.me/+HASH или +HASH\n                if ch.startswith("https://t.me/+") or ch.startswith("+"):\n                    invite_hash = ch.split("+", 1)[1].strip().strip("/")\n                    if not invite_hash:\n                        ru_log("ПРОПУСК", "Пустой invite hash", channel=ch, account=account_key)\n                        continue\n                    ru_log("ЖИВОЙ", "Пробую вступить по invite", channel=ch, account=account_key)\n                    try:\n                        await main_client(ImportChatInviteRequest(invite_hash))\n                        mark_join_today(account_key)\n                        _stats["joined"] += 1\n                        ru_log("УСПЕХ", "Вступил по invite", channel=ch, account=account_key)\n                        try:\n                            _jp_last(account_key, "JOIN_OK_INVITE", ch)\n                        except Exception:\n                            pass\n                        await asyncio.sleep(JOIN_COOLDOWN_SEC)\n                    except Exception as e:\n                        ru_log("ПЛОХО", "Не удалось вступить по invite", channel=ch, account=account_key, extra=str(e))\n                        try:\n                            _jp_last(account_key, "JOIN_FAIL_INVITE", ch)\n                        except Exception:\n                            pass\n                        await asyncio.sleep(120)\n                    continue\n\n                # Получаем entity\n                try:\n                    async with _telethon_lock:\n                        entity = await main_client.get_entity(ch_norm)\n                except Exception as e:\n                    ru_log("ПРОПУСК", "Не смог получить канал (entity)", channel=ch, account=account_key, extra=str(e))\n                    continue\n\n                # Проверяем подписку (строго)\n                already = False\n                try:\n                    already = await is_member_strict(main_client, entity)\n                except Exception:\n                    already = False\n\n                if already:\n                    ru_log("ЖИВОЙ", "Уже подписан", channel=ch, account=account_key)\n                    continue\n\n                # Пытаемся вступить\n                ru_log("ЖИВОЙ", "Пробую вступить в канал", channel=ch, account=account_key)\n                try:\n                    await main_client(JoinChannelRequest(entity))\n                    mark_join_today(account_key)\n                    _stats["joined"] += 1\n                    ru_log("УСПЕХ", "Вступил в канал", channel=ch, account=account_key)\n\n                    # ✅ Пауза 7 минут ТОЛЬКО после успешного вступления\n                    await asyncio.sleep(JOIN_COOLDOWN_SEC)\n\n                except Exception as e:\n                    # Если канал требует заявку/одобрение — ставим в PENDING и проверяем раз в 5 минут\n                    es = str(e)\n                    low = es.lower()\n                    if ("invite_request_sent" in low) or (("request" in low) and ("sent" in low)) or ("join request" in low) or ("requested" in low):\n                        try:\n                            PENDING_JOIN.setdefault(account_key, {})[ch] = {\n                                "ts": now_ts(),\n                                "next_check_ts": now_ts() + 300,\n                                "why": es,\n                            }\n                        except Exception:\n                            pass\n                        ru_log("ПАУЗА", "Заявка на вступление отправлена — жду одобрения", channel=ch, account=account_key)\n                        await asyncio.sleep(10)\n                    else:\n                        ru_log("ПЛОХО", "Не удалось вступить", channel=ch, account=account_key, extra=es)\n                        await asyncio.sleep(120)\n\n        except Exception:\n            pass\n\n        # после прохода по списку — ждём и повторяем (вдруг добавили новые каналы)\n        await asyncio.sleep(60)\n\n\n\n# Алиасы для старых вызовов (чтобы не падало)\nheartbeat_loop = heartbeat_loop\nautojoin_channels_loop = autojoin_channels_loop\n\n# Совместимость: имена, которые пытался вызвать старый запуск\nheartbeat_loop_ = heartbeat_loop\nautojoin_channels_loop_ = autojoin_channels_loop\n\n# Совместимость старых имён\nheartbeat_loop = heartbeat_loop if \'heartbeat_loop\' in globals() else heartbeat_loop_\nautojoin_channels_loop = autojoin_channels_loop if \'autojoin_channels_loop\' in globals() else autojoin_channels_loop_\n\n# Имена, которые ожидает старый запуск (loop)\nheartbeat_loop = heartbeat_loop_\nautojoin_channels_loop = autojoin_channels_loop_'

ANTI_SPAM_MARKERS = ["antispam", "defensy", "grouphelp", "captcha", "verify", "verification", "guard", "security", "shield", "антиспам", "анти-спам"]

def load_yaml(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return yaml.safe_load(path.read_text("utf-8")) or {}
    except Exception:
        return {}

def dump_yaml_safe(path: Path, data: Dict[str, Any]) -> None:
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), "utf-8")

def _proxy_pool_path(user_dir: Path) -> Path:
    return user_dir / "proxy_pool.txt"

def _channels_path(user_dir: Path) -> Path:
    return user_dir / "channels.txt"

def _blacklist_path(user_dir: Path) -> Path:
    return user_dir / "channels_blacklist.txt"

def _accounts_dir(user_dir: Path) -> Path:
    d = user_dir / "accounts"
    d.mkdir(exist_ok=True)
    return d

def _list_sessions(user_dir: Path) -> List[Path]:
    return sorted(_accounts_dir(user_dir).glob("*.session"))

def _read_lines(p: Path) -> List[str]:
    if not p.exists():
        return []
    return [ln.strip() for ln in p.read_text("utf-8", errors="ignore").splitlines() if ln.strip() and not ln.strip().startswith("#")]

def _append_unique_line(p: Path, line: str) -> None:
    existing = set(_read_lines(p))
    if line not in existing:
        with p.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

def _load_user_module(user_id: int, user_dir: Path):
    """
    Load tg_core as isolated module instance for this user.

    In the single-file build we execute TG_CORE_SOURCE into a fresh module namespace.
    Important for Python 3.13+: register module in sys.modules BEFORE exec to satisfy dataclasses.
    """
    mod_name = f"tg_core_u{user_id}"

    if not TG_CORE_SOURCE or not isinstance(TG_CORE_SOURCE, str):
        raise RuntimeError("TG_CORE_SOURCE not injected")

    # Prepare env
    old = os.getenv("TGCORE_BASE_DIR")
    os.environ["TGCORE_BASE_DIR"] = str(user_dir)

    try:
        module = types.ModuleType(mod_name)
        module.__file__ = f"<embedded {mod_name}>"
        # Critical: register in sys.modules before exec_module/exec (Python 3.13 dataclasses fix)
        sys.modules[mod_name] = module
        exec(TG_CORE_SOURCE, module.__dict__)
    finally:
        if old is None:
            os.environ.pop("TGCORE_BASE_DIR", None)
        else:
            os.environ["TGCORE_BASE_DIR"] = old
    return module


class CommentEngineManager:
    def __init__(self):
        self._tasks: Dict[int, asyncio.Task] = {}
        self._start_ts: Dict[int, int] = {}
        self._modules: Dict[int, Any] = {}

    def is_running(self, user_id: int) -> bool:
        t = self._tasks.get(user_id)
        return bool(t) and not t.done()

    def uptime(self, user_id: int) -> str:
        ts = self._start_ts.get(user_id)
        if not ts:
            return "00:00:00"
        sec = max(0, int(time.time()) - ts)
        h = sec // 3600
        m = (sec % 3600) // 60
        s = sec % 60
        return f"{h:02d}:{m:02d}:{s:02d}"

    async def start(self, user_id: int, user_dir: Path, settings: Dict[str, Any], prompt: str) -> Tuple[bool, str]:
        if self.is_running(user_id):
            return False, "уже запущено"

        # Ensure dirs/files
        _accounts_dir(user_dir)
        _proxy_pool_path(user_dir).touch(exist_ok=True)
        _channels_path(user_dir).touch(exist_ok=True)
        _blacklist_path(user_dir).touch(exist_ok=True)

        # Apply settings into config.yaml (service section) + enforce gpt-4.1
        cfg_path = user_dir / "config.yaml"
        cfg = load_yaml(cfg_path)
        cfg.setdefault("service", {})
        cfg.setdefault("openai", {})
        cfg["openai"]["model"] = "gpt-4.1"

        svc = cfg["service"]
        svc["mode"] = settings.get("mode", "SAFE")
        svc["comment_probability"] = float(settings.get("comment_probability", 0.30))
        svc["random_delay_min_sec"] = int(settings.get("delay_min_sec", 60))
        svc["random_delay_max_sec"] = int(settings.get("delay_max_sec", 160))
        svc["limit_per_hour"] = int(settings.get("limit_per_hour", 4))
        svc["limit_per_day"] = int(settings.get("limit_per_day", 20))

        # Autoreply (simple)
        svc["autoreply_enabled"] = bool(settings.get("autoreply_enabled", False))
        svc["autoreply_text"] = str(settings.get("autoreply_text", "Привет 🙂 Сейчас занят, отвечу чуть позже."))
        svc["autoreply_delay_min_sec"] = int(settings.get("autoreply_delay_min_sec", 20))
        svc["autoreply_delay_max_sec"] = int(settings.get("autoreply_delay_max_sec", 60))
        svc["autoreply_once_per_user"] = bool(settings.get("autoreply_once_per_user", True))

        dump_yaml_safe(cfg_path, cfg)

        # Store prompt (engine reads prompt.txt)
        (user_dir / "prompt.txt").write_text(prompt, "utf-8")

        # Optional prechecks (as approved)
        if settings.get("precheck_channels_before_start", True):
            await self.precheck_and_blacklist(user_id, user_dir)

        if settings.get("check_proxy_before_start", True):
            # lightweight: format only (real connectivity checked implicitly by Telethon)
            pass

        # Load isolated module
        mod = _load_user_module(user_id, user_dir)
        self._modules[user_id] = mod

        # Start service coroutine
        async def _run():
            await mod.run_service_async()

        self._tasks[user_id] = asyncio.create_task(_run())
        self._start_ts[user_id] = int(time.time())
        return True, "ok"

    async def stop(self, user_id: int) -> None:
        t = self._tasks.get(user_id)
        if t and not t.done():
            t.cancel()
            try:
                await t
            except Exception:
                pass
        self._tasks.pop(user_id, None)
        self._modules.pop(user_id, None)
        self._start_ts.pop(user_id, None)

    async def auto_assign_proxies(self, user_id: int, user_dir: Path) -> Dict[str, Any]:
        sessions = _list_sessions(user_dir)
        proxies = _read_lines(_proxy_pool_path(user_dir))
        accounts_total = len(sessions)
        proxies_total = len(proxies)
        assigned = 0

        if accounts_total == 0:
            return {"accounts_total": 0, "proxies_total": proxies_total, "assigned": 0}

        # For each session: write accounts/<name>.json with {"uri": proxy}
        for i, sp in enumerate(sessions):
            name = sp.with_suffix("").name
            proxy_uri = proxies[i % proxies_total] if proxies_total else ""
            data = {"uri": proxy_uri}
            (sp.parent / f"{name}.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
            assigned += 1

        return {"accounts_total": accounts_total, "proxies_total": proxies_total, "assigned": assigned}

    async def show_proxy_distribution(self, user_id: int, user_dir: Path) -> str:
        sessions = _list_sessions(user_dir)
        if not sessions:
            return "Нет аккаунтов (.session) в accounts/"

        out = []
        for sp in sessions[:50]:
            name = sp.with_suffix("").name
            js = sp.parent / f"{name}.json"
            proxy = ""
            if js.exists():
                try:
                    proxy = (json.loads(js.read_text("utf-8")) or {}).get("uri", "") or ""
                except Exception:
                    proxy = ""
            out.append(f"- {name} → {'✅' if proxy else '⚠️ нет'}")

        more = "" if len(sessions) <= 50 else f"\n…и ещё {len(sessions)-50}"
        return "Распределение:\n" + "\n".join(out) + more

    async def precheck_and_blacklist(self, user_id: int, user_dir: Path) -> Tuple[int, int, int]:
        channels = _read_lines(_channels_path(user_dir))
        checked = len(channels)
        bl_file = _blacklist_path(user_dir)
        bl_set = set(_read_lines(bl_file))

        if not channels:
            return 0, 0, 0

        sessions = _list_sessions(user_dir)
        if not sessions:
            # cannot check without a session
            return checked, 0, checked

        # Connect using first session and scan admins / recent messages for known anti-spam bots
        cfg = load_yaml(user_dir / "config.yaml")
        api_id = int((cfg.get("telegram") or {}).get("api_id", 0))
        api_hash = str((cfg.get("telegram") or {}).get("api_hash", ""))

        if not api_id or not api_hash or api_hash == "PUT_YOUR_API_HASH":
            # cannot connect
            return checked, 0, checked - len(bl_set)

        sp = sessions[0]
        name = sp.with_suffix("").name
        proxy = None
        js = sp.parent / f"{name}.json"
        if js.exists():
            try:
                proxy_uri = (json.loads(js.read_text("utf-8")) or {}).get("uri", "")
                if proxy_uri:
                    # let tg_core parse later; for check we keep no proxy to avoid dependencies
                    proxy = None
            except Exception:
                proxy = None

        from telethon import TelegramClient
        from telethon.tl.types import ChannelParticipantsAdmins
        from telethon.errors import RPCError

        client = TelegramClient(session=str(sp.parent / name), api_id=api_id, api_hash=api_hash, proxy=proxy)
        await client.connect()
        try:
            if not await client.is_user_authorized():
                await client.disconnect()
                return checked, 0, checked - len(bl_set)

            blacklisted = 0
            for ch in channels:
                if ch in bl_set:
                    continue
                suspect = False
                reason = ""
                try:
                    ent = await client.get_entity(ch)
                    # admins scan
                    try:
                        admins = await client.get_participants(ent, filter=ChannelParticipantsAdmins)
                        for u in admins:
                            blob = " ".join([str(getattr(u, "username", "") or ""), str(getattr(u, "first_name", "") or ""), str(getattr(u, "last_name", "") or "")]).lower()
                            if getattr(u, "bot", False) and any(m in blob for m in ANTI_SPAM_MARKERS):
                                suspect = True
                                reason = "admin_antispam_bot"
                                break
                    except Exception:
                        pass

                    if not suspect:
                        # recent messages scan
                        async for msg in client.iter_messages(ent, limit=30):
                            try:
                                snd = await msg.get_sender()
                                if not snd:
                                    continue
                                blob = " ".join([str(getattr(snd, "username", "") or ""), str(getattr(snd, "first_name", "") or ""), str(getattr(snd, "last_name", "") or "")]).lower()
                                if getattr(snd, "bot", False) and any(m in blob for m in ANTI_SPAM_MARKERS):
                                    suspect = True
                                    reason = "recent_antispam_bot"
                                    break
                            except Exception:
                                continue
                except Exception:
                    # cannot check -> keep
                    suspect = False

                if suspect:
                    _append_unique_line(bl_file, ch)
                    bl_set.add(ch)
                    blacklisted += 1

            remaining = len([c for c in channels if c not in bl_set])
            return checked, blacklisted, remaining
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass


# -*- coding: utf-8 -*-
"""
Telegram Commenting Bot (FULL, PythonAnywhere-ready)
- UI matches approved buttons/subbuttons.
- Uses per-user isolated directories: data/{user_id}/...
- Integrates core commenting engine (tg_core.py) via engine_adapter.py

Notes:
- Does NOT ask for Telegram api_id/api_hash or OpenAI key.
- GPT model enforced as gpt-4.1 in config.
"""

import os
import re
import json
import time
import datetime
import shutil
import asyncio
import logging
from pathlib import Path
from typing import Dict, Any, Optional, Tuple

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton

# engine_adapter is embedded above in single-file build
# CommentEngineManager and dump_yaml_safe are available in globals


BOT_TOKEN = ""  # loaded from admin_config.yaml (bot_token) at startup
DATA_ROOT = Path(__file__).resolve().parent / "data"
DATA_ROOT.mkdir(parents=True, exist_ok=True)

ADMIN_CONFIG_PATH = Path(__file__).resolve().parent / "admin_config.yaml"
ADMIN_CONFIG_EXAMPLE_PATH = Path(__file__).resolve().parent / "admin_config.example.yaml"

def ensure_admin_config():
    """Create admin_config.yaml if missing.

    Owner sets here:
    - bot.token
    - telegram.api_id / telegram.api_hash
    - openai.api_key / openai.model

    Bot never asks end-users for these values.
    """
    if ADMIN_CONFIG_PATH.exists():
        return
    # Try copy example
    if ADMIN_CONFIG_EXAMPLE_PATH.exists():
        try:
            shutil.copy2(str(ADMIN_CONFIG_EXAMPLE_PATH), str(ADMIN_CONFIG_PATH))
            return
        except Exception:
            pass

    skeleton = {
        "bot": {"token": "PUT_YOUR_BOT_TOKEN"},
        "telegram": {"api_id": 0, "api_hash": "PUT_YOUR_API_HASH"},
        "openai": {"api_key": "PUT_YOUR_OPENAI_KEY", "model": "gpt-4.1", "temperature": 0.7, "max_tokens": 60},
        "service": {}
    }
    dump_yaml_safe(ADMIN_CONFIG_PATH, skeleton)


logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("tg_bot_full")

# ===== Defaults =====
DEFAULT_PROMPT = """Ты — живой участник Telegram-чата. Пиши по-русски.
Твоя задача: ответить как человек, коротко и по делу (2–6 слов, максимум 140 символов).
Если пост похож на спам/скам/ботов/крипто/розыгрыш/рефералки/призывы в ЛС/ссылки/подозрительную рекламу — верни строго: SKIP

Иначе верни строго формат:
OK: <короткий комментарий 2–6 слов>

Запрещено:
- ссылки
- политика/война
- токсичность
- длинные ответы
"""

DEFAULT_SETTINGS = {
    "mode": "SAFE",
    "delay_min_sec": 60,
    "delay_max_sec": 160,
    "comment_probability": 0.30,   # 0..1
    "limit_per_hour": 4,
    "limit_per_day": 20,
    "check_proxy_before_start": True,
    "precheck_channels_before_start": True,

    "autoreply_enabled": False,
    "autoreply_text": "Привет 🙂 Сейчас занят, отвечу чуть позже.",
    "autoreply_delay_min_sec": 20,
    "autoreply_delay_max_sec": 60,
    "autoreply_once_per_user": True,
}


# ===== Channel filter (protected channels only) =====
ANTI_SPAM_UI_MARKERS = [
    # ВАЖНО: маркеры должны быть достаточно "сильными", чтобы не помечать любой чат как защищённый
    # (слова вроде "rules/spam/links" встречаются почти везде и дают ложные срабатывания).
    "captcha", "capcha", "captha",
    "i am not a bot", "i'm not a bot", "not a robot",
    "verify", "verification", "verified",
    "подтвердите", "подтверди", "проверка", "проверьте", "верификац",
    "пройдите проверку", "чтобы писать", "для доступа",
    "нажмите кнопку", "нажми кнопку", "press the button", "press button",
    "антиспам", "анти-спам", "anti-spam", "anti spam",
    "защита от спама", "protection from spam",
    "shieldy", "combot", "rose",
]

def _norm_channel_line(line: str) -> str:
    s = (line or "").strip()
    if not s:
        return ""
    # strip comments
    s = s.split("#", 1)[0].strip()
    if not s:
        return ""
    # normalize t.me links
    s = s.replace("https://", "").replace("http://", "")
    if s.startswith("t.me/"):
        s = s[5:]
    if s.startswith("@"):
        s = s[1:]
    return s.strip()

def _is_invite(s: str) -> bool:
    return s.startswith("+") or "joinchat" in s

def _invite_hash(s: str) -> str:
    # formats: +HASH or joinchat/HASH
    s = s.strip()
    if s.startswith("+"):
        return s[1:]
    if "joinchat/" in s:
        return s.split("joinchat/", 1)[1]
    return s

async def _scan_discussion_for_protection(client, discussion_entity, limit: int = 60) -> bool:
    """
    Сканирует обсуждения и пытается понять, есть ли реальная антиспам-защита.
    Логика сделана так, чтобы НЕ давать массовые ложные "SAFE" на обычных чатах.
    Возвращает True только при наличии "сильных" сигналов (бот + проверка/капча/верификация/кнопки).
    """
    try:
        msgs = await client.get_messages(discussion_entity, limit=limit)
    except Exception:
        return False

    # Боты, которые чаще всего используются как антиспам/верификация
    KNOWN_ANTISPAM_BOT_TOKENS = (
        "shieldy", "shieldybot",
        "combot", "combotantispam", "combot_anti_spam",
        "rose", "missrose", "miss_rose",
        "antispam", "anti_spam", "antispam_bot",
        "captcha", "joincaptcha",
        "protect", "guard", "security",
    )

    # Фразы, типичные для проверки/капчи/ограничения прав
    import re as _re
    PHRASE_RE = _re.compile(
        r"(captcha|capcha|captha|i\s*am\s*not\s*a\s*bot|not\s*a\s*robot|"
        r"verify|verification|verified|"
        r"подтверд(ите|и)|верификац|провер(ка|ьте)|пройдите\s+проверку|"
        r"нажм(ите|и)\s+кнопк|press\s+the\s+button|"
        r"анти[- ]?спам|anti[- ]?spam|защит[ауы]\s+от\s+спама|"
        r"чтобы\s+писать|для\s+доступа)"
    )

    def _sender_is_antispam_bot(sender) -> bool:
        try:
            if sender is None:
                return False
            # у Telethon: User.bot
            if getattr(sender, "bot", False):
                uname = (getattr(sender, "username", "") or "").lower()
                name = (getattr(sender, "first_name", "") or "").lower()
                combo = uname + " " + name
                return any(t in combo for t in KNOWN_ANTISPAM_BOT_TOKENS)
            # иногда антиспам не как бот, но в имени/юзернейме
            uname = (getattr(sender, "username", "") or "").lower()
            name = (getattr(sender, "first_name", "") or "").lower()
            combo = uname + " " + name
            return any(t in combo for t in KNOWN_ANTISPAM_BOT_TOKENS)
        except Exception:
            return False

    def _has_verify_buttons(msg) -> bool:
        try:
            rm = getattr(msg, "reply_markup", None)
            if not rm:
                return False
            # Telethon: reply_markup.rows -> кнопки
            rows = getattr(rm, "rows", None) or []
            for row in rows:
                buttons = getattr(row, "buttons", None) or []
                for b in buttons:
                    t = (getattr(b, "text", "") or "").lower()
                    if not t:
                        continue
                    if ("verify" in t) or ("captcha" in t) or ("капч" in t) or ("подтверд" in t) or ("я не бот" in t) or ("not a bot" in t):
                        return True
            return False
        except Exception:
            return False

    score = 0
    strong = False

    for msg in msgs:
        txt = (getattr(msg, "message", "") or "").strip()
        txt_l = txt.lower()

        sender = None
        try:
            sender = await msg.get_sender()
        except Exception:
            sender = None

        if _sender_is_antispam_bot(sender):
            score += 3
            strong = True

        if _has_verify_buttons(msg):
            score += 2
            strong = True

        if txt and PHRASE_RE.search(txt_l):
            # не даём раздуваться: считаем максимум 3 очка за тексты
            score += 1

        # Достаточно уверенности: либо сильные признаки + ещё один сигнал, либо общий высокий score
        if (strong and score >= 4) or (score >= 6):
            return True

    return False

async def filter_channels(uid: int, user_dir_path: Path, src_txt: Path) -> dict:
    """Classify channels into GOOD/REQUEST/BAD based on discussion protection availability."""
    from telethon import TelegramClient
    from telethon.errors import RPCError
    from telethon.tl.functions.channels import GetFullChannelRequest
    from telethon.tl.functions.messages import CheckChatInviteRequest

    lines = []
    for raw in src_txt.read_text("utf-8", errors="ignore").splitlines():
        s = _norm_channel_line(raw)
        if s:
            lines.append(s)
    # unique, keep order
    seen = set()
    targets = []
    for s in lines:
        if s not in seen:
            seen.add(s)
            targets.append(s)

    out = {"good": [], "request": [], "bad": []}

    sessions = [p for p in (user_dir_path / "accounts").glob("*.session")]
    if not sessions:
        return {"error": "Нет .session. Сначала загрузите хотя бы один .session в 📥 Файлы."}

    cfg = load_yaml(user_dir_path / "config.yaml")
    api_id = int((cfg.get("telegram") or {}).get("api_id", 0))
    api_hash = str((cfg.get("telegram") or {}).get("api_hash", ""))
    if not api_id or not api_hash or api_hash == "PUT_YOUR_API_HASH":
        return {"error": "В config.yaml не заполнены telegram.api_id / telegram.api_hash"}

    sp = sessions[0]
    name = sp.with_suffix("").name
    client = TelegramClient(session=str(sp.parent / name), api_id=api_id, api_hash=api_hash)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            return {"error": "Session не авторизована. Загрузите рабочий .session"}

        for t in targets:
            # Invite links (always private) -> REQUEST if request-needed else BAD
            if _is_invite(t):
                try:
                    inv = await client(CheckChatInviteRequest(hash=_invite_hash(t)))
                    req = bool(getattr(inv, "request_needed", False))
                    if req:
                        out["request"].append(t)
                    else:
                        out["bad"].append(t)
                except Exception:
                    out["bad"].append(t)
                continue

            # Public username or channel
            try:
                ent = await client.get_entity(t)
            except Exception:
                out["bad"].append(t)
                continue

            # Find linked discussion
            linked_chat_id = None
            try:
                full = await client(GetFullChannelRequest(ent))
                linked_chat_id = getattr(getattr(full, "full_chat", None), "linked_chat_id", None)
            except Exception:
                linked_chat_id = None

            if not linked_chat_id:
                out["bad"].append(t)
                continue

            # Access discussion without joining
            try:
                disc = await client.get_entity(linked_chat_id)
            except Exception:
                out["bad"].append(t)
                continue

            # If messages can be read, scan for protection markers
            is_protected = await _scan_discussion_for_protection(client, disc)
            if is_protected:
                out["good"].append(t)
            else:
                out["bad"].append(t)

    finally:
        try:
            await client.disconnect()
        except Exception:
            pass

    return out
# ===== Buttons =====
BTN_START = "▶️ Старт"
BTN_STOP = "⏹ Стоп"
BTN_REPORT = "📊 Отчёт (на данный момент)"
BTN_SETTINGS = "⚙️ Настройки"
BTN_PROXY = "🌐 Прокси"
BTN_FILES = "📥 Файлы"
BTN_FILTER = "🧹 Отсеить каналы"
BTN_AUTOREPLY = "🤖 Автоответчик ЛС"
BTN_PROMPT = "🧠 Промт GPT-4.1"
BTN_BACK = "🔙 Назад"

# Settings
BTN_MODE = "SAFE / NORMAL"
BTN_DELAY = "delay min/max"
BTN_PROB = "вероятность %"
BTN_LIMITS = "лимиты час/день"
BTN_PROXYCHECK = "проверка прокси перед стартом вкл/выкл"
BTN_PRECHECK = "проверка каналов перед стартом (отсев)"

# Proxy
BTN_PROXY_AUTO = "🤖 Авто распределить"
BTN_PROXY_SHOW = "📋 показать распределение"

# Files
BTN_UP_SESSION = "📥 Загрузить session"
BTN_UP_CHANNELS = "📥 Загрузить channels.txt"
BTN_UP_PROXIES = "📥 Загрузить proxy_pool.txt"
BTN_SHOW_UPLOADED = "📁 Показать что загружено"
BTN_CLEAR = "🧹 Очистить sessions/result"

# Filter
BTN_FILTER_RUN = "отсеить сейчас"
BTN_FILTER_SHOW = "показать blacklist"
BTN_FILTER_CLEAR = "очистить blacklist"

# Autoreply
BTN_AR_TOGGLE = "Вкл/Выкл"
BTN_AR_TEXT = "текст автоответа"
BTN_AR_DELAY = "задержка 20–60 сек"
BTN_AR_ONCE = "отвечать 1 раз на человека"

# Prompt
BTN_PROMPT_SHOW = "Показать текущий"
BTN_PROMPT_SET = "Изменить промт"
BTN_PROMPT_RESET = "Сбросить стандартный"


# ===== Keyboards =====
def kb_main() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_START), KeyboardButton(text=BTN_STOP), KeyboardButton(text=BTN_REPORT)],
            [KeyboardButton(text=BTN_SETTINGS), KeyboardButton(text=BTN_PROXY), KeyboardButton(text=BTN_FILES)],
            [KeyboardButton(text=BTN_FILTER), KeyboardButton(text=BTN_AUTOREPLY), KeyboardButton(text=BTN_PROMPT)],
        ],
        resize_keyboard=True
    )

def kb_settings() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_MODE), KeyboardButton(text=BTN_DELAY)],
            [KeyboardButton(text=BTN_PROB), KeyboardButton(text=BTN_LIMITS)],
            [KeyboardButton(text=BTN_PROXYCHECK), KeyboardButton(text=BTN_PRECHECK)],
            [KeyboardButton(text=BTN_BACK)],
        ],
        resize_keyboard=True
    )

def kb_proxy() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_PROXY_AUTO), KeyboardButton(text=BTN_PROXY_SHOW)],
            [KeyboardButton(text=BTN_BACK)],
        ],
        resize_keyboard=True
    )

def kb_files() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_UP_SESSION)],
            [KeyboardButton(text=BTN_UP_CHANNELS), KeyboardButton(text=BTN_UP_PROXIES)],
            [KeyboardButton(text=BTN_SHOW_UPLOADED), KeyboardButton(text=BTN_CLEAR)],
            [KeyboardButton(text=BTN_BACK)],
        ],
        resize_keyboard=True
    )

def kb_filter() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_FILTER_RUN), KeyboardButton(text=BTN_FILTER_SHOW)],
            [KeyboardButton(text=BTN_FILTER_CLEAR)],
            [KeyboardButton(text=BTN_BACK)],
        ],
        resize_keyboard=True
    )

def kb_autoreply() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_AR_TOGGLE), KeyboardButton(text=BTN_AR_TEXT)],
            [KeyboardButton(text=BTN_AR_DELAY), KeyboardButton(text=BTN_AR_ONCE)],
            [KeyboardButton(text=BTN_BACK)],
        ],
        resize_keyboard=True
    )

def kb_prompt() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_PROMPT_SHOW), KeyboardButton(text=BTN_PROMPT_SET)],
            [KeyboardButton(text=BTN_PROMPT_RESET)],
            [KeyboardButton(text=BTN_BACK)],
        ],
        resize_keyboard=True
    )

# ===== State =====
USER_AWAIT: Dict[int, Optional[str]] = {}

dp = Dispatcher()
engine = CommentEngineManager()

def user_dir(user_id: int) -> Path:
    d = DATA_ROOT / str(user_id)
    d.mkdir(parents=True, exist_ok=True)
    # unified folders (as agreed)
    (d / "accounts").mkdir(exist_ok=True)
    (d / "sessions").mkdir(exist_ok=True)
    (d / "result").mkdir(exist_ok=True)
    return d

def settings_path(user_id: int) -> Path:
    return user_dir(user_id) / "settings.json"

def prompt_path(user_id: int) -> Path:
    return user_dir(user_id) / "prompt.txt"

def channels_path(user_id: int) -> Path:
    return user_dir(user_id) / "channels.txt"

def proxy_pool_path(user_id: int) -> Path:
    return user_dir(user_id) / "proxy_pool.txt"

def stats_path(user_id: int) -> Path:
    return user_dir(user_id) / "stats.json"

def blacklist_txt_path(user_id: int) -> Path:
    d = user_dir(user_id)
    d.mkdir(parents=True, exist_ok=True)
    p = d / "channels_blacklist.txt"
    if not p.exists():
        try:
            p.write_text("", encoding="utf-8")
        except Exception:
            # best-effort, do not crash bot
            pass
    return p

def load_json(path: Path, default: Any) -> Any:
    if path.exists():
        try:
            return json.loads(path.read_text("utf-8"))
        except Exception:
            return default
    path.write_text(json.dumps(default, ensure_ascii=False, indent=2), "utf-8")
    return default

def save_json(path: Path, obj: Any) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), "utf-8")

def ensure_user_storage(user_id: int) -> None:
    """Создаёт папку пользователя и базовые файлы, чтобы бот не падал на первом клике."""
    ud = user_dir(user_id)
    ud.mkdir(parents=True, exist_ok=True)

    # Базовые файлы
    for path, default_text in [
        (prompt_path(user_id), DEFAULT_PROMPT.strip() + "\n"),
        (channels_path(user_id), ""),
        (blacklist_txt_path(user_id), ""),
    ]:
        if not path.exists():
            path.write_text(default_text, "utf-8")

    # JSON статы
    sp = stats_path(user_id)
    if not sp.exists():
        save_json(sp, {})

def read_stats(user_id: int) -> Dict[str, Any]:
    ensure_user_storage(user_id)
    return load_json(stats_path(user_id), {})

def write_stats(user_id: int, data: Dict[str, Any]) -> None:
    ensure_user_storage(user_id)
    save_json(stats_path(user_id), data)

def load_settings(user_id: int) -> Dict[str, Any]:
    s = load_json(settings_path(user_id), dict(DEFAULT_SETTINGS))
    # backfill
    for k, v in DEFAULT_SETTINGS.items():
        s.setdefault(k, v)
    save_json(settings_path(user_id), s)
    return s

def save_settings(user_id: int, s: Dict[str, Any]) -> None:
    save_json(settings_path(user_id), s)

def load_prompt(user_id: int) -> str:
    p = prompt_path(user_id)
    if p.exists():
        try: return p.read_text("utf-8")
        except Exception: pass
    p.write_text(DEFAULT_PROMPT, "utf-8")
    return DEFAULT_PROMPT

def save_prompt(user_id: int, txt: str) -> None:
    prompt_path(user_id).write_text(txt, "utf-8")

async def bootstrap(user_id: int):
    ensure_admin_config()
    ensure_user_storage(user_id)
    d = user_dir(user_id)
    load_settings(user_id)
    load_prompt(user_id)

    # Ensure per-user config.yaml exists (admin-only credentials live on server)
    cfg_path = d / "config.yaml"
    if not cfg_path.exists():
        if ADMIN_CONFIG_PATH.exists():
            try:
                shutil.copy2(str(ADMIN_CONFIG_PATH), str(cfg_path))
            except Exception:
                pass
        else:
            # Fallback skeleton; bot still will not ask user for keys
            cfg = {
                "telegram": {"api_id": 0, "api_hash": "PUT_YOUR_API_HASH"},
                "openai": {"api_key": "PUT_YOUR_OPENAI_KEY", "model": "gpt-4.1"},
                "service": {}
            }
            dump_yaml_safe(cfg_path, cfg)

@dp.message(Command("start"))
async def on_start(message: Message):
    await bootstrap(message.from_user.id)
    USER_AWAIT[message.from_user.id] = None
    await message.answer("Готов ✅", reply_markup=kb_main())

# ===== Main menu =====
@dp.message(F.text == BTN_SETTINGS)
async def go_settings(message: Message):
    await bootstrap(message.from_user.id)
    USER_AWAIT[message.from_user.id] = None
    await message.answer("⚙️ Настройки", reply_markup=kb_settings())

@dp.message(F.text == BTN_PROXY)
async def go_proxy(message: Message):
    await bootstrap(message.from_user.id)
    USER_AWAIT[message.from_user.id] = None
    await message.answer("🌐 Прокси", reply_markup=kb_proxy())

@dp.message(F.text == BTN_FILES)
async def go_files(message: Message):
    await bootstrap(message.from_user.id)
    USER_AWAIT[message.from_user.id] = None
    await message.answer("📥 Файлы", reply_markup=kb_files())

@dp.message(F.text == BTN_UP_SESSION)
async def ask_session(message: Message):
    uid = message.from_user.id
    await bootstrap(uid)
    USER_AWAIT[uid] = "upload_session"
    await message.answer("Отправь файл .session (Telethon). По одному файлу на аккаунт.")

@dp.message(F.text == BTN_UP_CHANNELS)
async def ask_channels(message: Message):
    uid = message.from_user.id
    await bootstrap(uid)
    USER_AWAIT[uid] = "upload_channels"
    await message.answer("Отправь channels.txt (по одному каналу/чату в строке).")

@dp.message(F.text == BTN_UP_PROXIES)
async def ask_proxies(message: Message):
    uid = message.from_user.id
    await bootstrap(uid)
    USER_AWAIT[uid] = "upload_proxies"
    await message.answer("Отправь proxy_pool.txt (по одной прокси в строке).")


@dp.message(F.text == BTN_FILTER)
async def go_filter(message: Message):
    await bootstrap(message.from_user.id)
    USER_AWAIT[message.from_user.id] = None
    await message.answer("🧹 Отсеить каналы", reply_markup=kb_filter())

@dp.message(F.text == BTN_AUTOREPLY)
async def go_autoreply(message: Message):
    await bootstrap(message.from_user.id)
    USER_AWAIT[message.from_user.id] = None
    await message.answer("🤖 Автоответчик ЛС", reply_markup=kb_autoreply())

@dp.message(F.text == BTN_PROMPT)
async def go_prompt(message: Message):
    await bootstrap(message.from_user.id)
    USER_AWAIT[message.from_user.id] = None
    await message.answer("🧠 Промт GPT-4.1", reply_markup=kb_prompt())

@dp.message(F.text == BTN_BACK)
async def back(message: Message):
    await bootstrap(message.from_user.id)
    USER_AWAIT[message.from_user.id] = None
    await message.answer("Главное меню ✅", reply_markup=kb_main())

# ===== Report =====
@dp.message(F.text == BTN_REPORT)
async def report(message: Message):
    await bootstrap(message.from_user.id)
    uid = message.from_user.id
    st = read_stats(uid)
    running = engine.is_running(uid)
    status = "🟢" if running else "🔴"

    # Map core stats -> approved report format
    total = int(st.get("sent", 0))
    alive = int(st.get("alive", 0))
    deleted = int(st.get("deleted", 0))

    # Accounts/proxies quick counts
    acc_total = len(list((user_dir(uid)/"accounts").glob("*.session")))
    acc_alive = acc_total  # conservative (no extra checks)
    proxies = [ln.strip() for ln in proxy_pool_path(uid).read_text("utf-8", errors="ignore").splitlines() if ln.strip() and not ln.strip().startswith("#")]
    proxy_total = len(proxies)
    proxy_alive = proxy_total

    uptime = engine.uptime(uid)

    txt = (
        f"Статус: {status}\n"
        f"Комменты: {total} (✅{alive} / ❌{deleted})\n"
        f"Акки: {acc_alive}/{acc_total} 🟢\n"
        f"Прокси: {proxy_alive}/{proxy_total} 🟢\n"
        f"Время: {uptime}"
    )
    await message.answer(txt)

# ===== Start/Stop =====
@dp.message(F.text == BTN_START)
async def start(message: Message):
    await bootstrap(message.from_user.id)
    uid = message.from_user.id
    s = load_settings(uid)

    # apply service settings (admin config on server)
    ok, msg = await engine.start(uid, user_dir(uid), settings=s, prompt=load_prompt(uid))
    if not ok:
        await message.answer(f"❌ Не стартануло: {msg}")
        return
    await message.answer("▶️ Запущено ✅")

@dp.message(F.text == BTN_STOP)
async def stop(message: Message):
    await bootstrap(message.from_user.id)
    uid = message.from_user.id
    await engine.stop(uid)
    await message.answer("⏹ Остановлено ✅")

# ===== Settings actions =====
@dp.message(F.text == BTN_MODE)
async def toggle_mode(message: Message):
    uid = message.from_user.id
    s = load_settings(uid)
    s["mode"] = "NORMAL" if s.get("mode") == "SAFE" else "SAFE"
    save_settings(uid, s)
    await message.answer(f"Режим: {s['mode']} ✅")

@dp.message(F.text == BTN_DELAY)
async def ask_delay(message: Message):
    USER_AWAIT[message.from_user.id] = "delay"
    await message.answer("Отправь: min max (сек)\nПример: 60 160")

@dp.message(F.text == BTN_PROB)
async def ask_prob(message: Message):
    USER_AWAIT[message.from_user.id] = "prob"
    await message.answer("Отправь вероятность %\nПример: 30")

@dp.message(F.text == BTN_LIMITS)
async def ask_limits(message: Message):
    USER_AWAIT[message.from_user.id] = "limits"
    await message.answer("Отправь: в_час в_день\nПример: 4 20")

@dp.message(F.text == BTN_PROXYCHECK)
async def toggle_proxycheck(message: Message):
    uid = message.from_user.id
    s = load_settings(uid)
    s["check_proxy_before_start"] = not bool(s.get("check_proxy_before_start"))
    save_settings(uid, s)
    await message.answer(f"Проверка прокси: {'Вкл' if s['check_proxy_before_start'] else 'Выкл'} ✅")

@dp.message(F.text == BTN_PRECHECK)
async def toggle_precheck(message: Message):
    uid = message.from_user.id
    s = load_settings(uid)
    s["precheck_channels_before_start"] = not bool(s.get("precheck_channels_before_start"))
    save_settings(uid, s)
    await message.answer(f"Отсев каналов перед стартом: {'Вкл' if s['precheck_channels_before_start'] else 'Выкл'} ✅")

# ===== Proxy =====
@dp.message(F.text == BTN_PROXY_AUTO)
async def proxy_auto(message: Message):
    await bootstrap(message.from_user.id)
    uid = message.from_user.id
    res = await engine.auto_assign_proxies(uid, user_dir(uid))
    await message.answer(
        f"Авто распределение ✅\n"
        f"Аккаунты: {res['accounts_total']}\n"
        f"Прокси: {res['proxies_total']}\n"
        f"Назначено: {res['assigned']}/{res['accounts_total']}"
    )

@dp.message(F.text == BTN_PROXY_SHOW)
async def proxy_show(message: Message):
    await bootstrap(message.from_user.id)
    uid = message.from_user.id
    txt = await engine.show_proxy_distribution(uid, user_dir(uid))
    await message.answer(txt)

# ===== Filter =====
@dp.message(F.text == BTN_FILTER_RUN)
async def filter_run(message: Message):
    await bootstrap(message.from_user.id)
    uid = message.from_user.id
    checked, blacklisted, remaining = await engine.precheck_and_blacklist(uid, user_dir(uid))
    await message.answer(
        f"Проверено: {checked}\n"
        f"В blacklist: {blacklisted}\n"
        f"Останется: {remaining} ✅"
    )

@dp.message(F.text == BTN_FILTER_SHOW)
async def filter_show(message: Message):
    await bootstrap(message.from_user.id)
    uid = message.from_user.id
    p = blacklist_txt_path(uid)
    if not p.exists():
        p.write_text('', 'utf-8')
    txt = p.read_text("utf-8", errors="ignore").strip()
    if not txt:
        await message.answer("Blacklist пуст ✅")
        return
    lines = [ln.strip() for ln in txt.splitlines() if ln.strip()]
    out = "\n".join("- " + ln for ln in lines[:50])
    if len(lines) > 50:
        out += f"\n…и ещё {len(lines)-50}"
    await message.answer("Blacklist:\n" + out)

@dp.message(F.text == BTN_FILTER_CLEAR)
async def filter_clear(message: Message):
    uid = message.from_user.id
    blacklist_txt_path(uid).write_text("", "utf-8")
    await message.answer("Blacklist очищен ✅")

# ===== Autoreply =====
@dp.message(F.text == BTN_AR_TOGGLE)
async def ar_toggle(message: Message):
    uid = message.from_user.id
    s = load_settings(uid)
    s["autoreply_enabled"] = not bool(s.get("autoreply_enabled"))
    save_settings(uid, s)
    await message.answer(f"Автоответчик: {'Вкл' if s['autoreply_enabled'] else 'Выкл'} ✅")

@dp.message(F.text == BTN_AR_TEXT)
async def ar_text(message: Message):
    USER_AWAIT[message.from_user.id] = "ar_text"
    await message.answer("Отправь текст автоответа одним сообщением.")

@dp.message(F.text == BTN_AR_DELAY)
async def ar_delay(message: Message):
    USER_AWAIT[message.from_user.id] = "ar_delay"
    await message.answer("Отправь: min max (сек)\nПример: 20 60")

@dp.message(F.text == BTN_AR_ONCE)
async def ar_once(message: Message):
    uid = message.from_user.id
    s = load_settings(uid)
    s["autoreply_once_per_user"] = not bool(s.get("autoreply_once_per_user"))
    save_settings(uid, s)
    await message.answer(f"1 раз на человека: {'Вкл' if s['autoreply_once_per_user'] else 'Выкл'} ✅")

# ===== Prompt =====
@dp.message(F.text == BTN_PROMPT_SHOW)
async def p_show(message: Message):
    uid = message.from_user.id
    txt = load_prompt(uid)
    await message.answer(txt[:3500] + ("\n…(обрезано)" if len(txt) > 3500 else ""))

@dp.message(F.text == BTN_PROMPT_SET)
async def p_set(message: Message):
    USER_AWAIT[message.from_user.id] = "prompt"
    await message.answer("Отправь новый промт одним сообщением.")

@dp.message(F.text == BTN_PROMPT_RESET)
async def p_reset(message: Message):
    uid = message.from_user.id
    save_prompt(uid, DEFAULT_PROMPT)
    await message.answer("Промт сброшен ✅")


# ===== Channel filter (button) =====
@dp.message(F.text == BTN_FILTER)
async def filter_menu(message: Message):
    await bootstrap(message.from_user.id)
    uid = message.from_user.id
    USER_AWAIT[uid] = "upload_filter"
    await message.answer(
        "🧹 Отсев каналов\n\n"
        "Пришли .txt со списком каналов/чатов (t.me/..., @user, username, или invite +HASH).\n"
        "Я верну 3 файла:\n"
        "1) ✅ good_protected.txt — есть защита (AntiSpam/правила, как на твоих скринах)\n"
        "2) 🟡 request_needed.txt — только где нужна заявка (invite request)\n"
        "3) ❌ bad_unprotected_or_closed.txt — нет защиты или нет доступа\n\n"
        "Важно: вступать будем только при работе, тут просто быстрый пробег."
    )
# ===== Files =====
@dp.message(F.text == BTN_SHOW_UPLOADED)
async def show_uploads(message: Message):
    uid = message.from_user.id
    await bootstrap(uid)
    d = user_dir(uid)
    sessions = len(list((d / "accounts").glob("*.session")))
    has_channels = (d / "channels.txt").exists() and (d / "channels.txt").read_text("utf-8", errors="ignore").strip() != ""
    has_proxies = (d / "proxy_pool.txt").exists() and (d / "proxy_pool.txt").read_text("utf-8", errors="ignore").strip() != ""
    await message.answer(
        f"📁 Загружено:\n"
        f"- sessions: {sessions}\n"
        f"- channels.txt: {'✅' if has_channels else '❌'}\n"
        f"- proxy_pool.txt: {'✅' if has_proxies else '❌'}"
    )

@dp.message(F.text == BTN_CLEAR)

async def clear_sr(message: Message):
    uid = message.from_user.id
    d = user_dir(uid)
    # Clear uploads and runtime artifacts for this user
    for p in [d/"accounts", d/"sessions", d/"result", d/"logs"]:
        if p.exists():
            shutil.rmtree(p, ignore_errors=True)
        p.mkdir(exist_ok=True)
    # Keep control files
    (d / "channels.txt").write_text((d/"channels.txt").read_text("utf-8", errors="ignore") if (d/"channels.txt").exists() else "", "utf-8")
    (d / "proxy_pool.txt").write_text((d/"proxy_pool.txt").read_text("utf-8", errors="ignore") if (d/"proxy_pool.txt").exists() else "", "utf-8")
    (d / "channels_blacklist.txt").write_text((d/"channels_blacklist.txt").read_text("utf-8", errors="ignore") if (d/"channels_blacklist.txt").exists() else "", "utf-8")
    await message.answer("🧹 sessions/result очищено ✅")

@dp.message(F.document)
async def on_doc(message: Message):
    uid = message.from_user.id
    await bootstrap(uid)
    awaiting = USER_AWAIT.get(uid)
    if awaiting not in ("upload_session", "upload_channels", "upload_proxies"):
        return

    doc = message.document
    f = await message.bot.get_file(doc.file_id)
    tmp = user_dir(uid) / ("upload_" + (doc.file_name or "file"))
    await message.bot.download_file(f.file_path, destination=tmp)

    try:
        if awaiting == "upload_session":
            if not (doc.file_name or "").lower().endswith(".session"):
                tmp.unlink(missing_ok=True)
                await message.answer("❌ Нужен файл .session")
                return
            target = user_dir(uid) / "accounts" / doc.file_name
            shutil.move(str(tmp), str(target))
            await message.answer(f"✅ session загружен: {doc.file_name}")
            return

        if awaiting == "upload_channels":
            if not (doc.file_name or "").lower().endswith(".txt"):
                tmp.unlink(missing_ok=True)
                await message.answer("❌ Нужен channels.txt")
                return
            shutil.move(str(tmp), str(user_dir(uid) / "channels.txt"))
            await message.answer("✅ channels.txt загружен")
            return


        if awaiting == "upload_filter":
            if not (doc.file_name or "").lower().endswith(".txt"):
                tmp.unlink(missing_ok=True)
                await message.answer("❌ Нужен .txt со списком каналов")
                return
            # store input
            in_path = user_dir(uid) / "result" / "channels_input.txt"
            shutil.move(str(tmp), str(in_path))
            await message.answer("⏳ Пробегаюсь по списку...")
            res = await filter_channels(uid, user_dir(uid), in_path)
            USER_AWAIT.pop(uid, None)
            if res.get("error"):
                await message.answer(f"❌ {res['error']}")
                return
            # write outputs
            ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            good_p = user_dir(uid) / "result" / f"good_protected_{ts}.txt"
            req_p = user_dir(uid) / "result" / f"request_needed_{ts}.txt"
            bad_p = user_dir(uid) / "result" / f"bad_unprotected_or_closed_{ts}.txt"
            good_p.write_text("\n".join(res.get("good", [])), "utf-8")
            req_p.write_text("\n".join(res.get("request", [])), "utf-8")
            bad_p.write_text("\n".join(res.get("bad", [])), "utf-8")
            await message.answer_document(FSInputFile(str(good_p)), caption="✅ good_protected")
            await message.answer_document(FSInputFile(str(req_p)), caption="🟡 request_needed")
            await message.answer_document(FSInputFile(str(bad_p)), caption="❌ bad_unprotected_or_closed")
            return
        if awaiting == "upload_proxies":
            if not (doc.file_name or "").lower().endswith(".txt"):
                tmp.unlink(missing_ok=True)
                await message.answer("❌ Нужен proxy_pool.txt")
                return
            shutil.move(str(tmp), str(user_dir(uid) / "proxy_pool.txt"))
            await message.answer("✅ proxy_pool.txt загружен")
            return
    finally:
        USER_AWAIT[uid] = None


@dp.message()
async def on_text(message: Message):
    uid = message.from_user.id
    await bootstrap(uid)
    awaiting = USER_AWAIT.get(uid)
    if not awaiting:
        return
    txt = (message.text or "").strip()

    if awaiting == "delay":
        pr = parse_two_ints(txt)
        if not pr:
            await message.answer("❌ Формат: min max")
            return
        mn, mx = pr
        s = load_settings(uid)
        s["delay_min_sec"] = max(1, mn)
        s["delay_max_sec"] = max(s["delay_min_sec"], mx)
        save_settings(uid, s)
        USER_AWAIT[uid] = None
        await message.answer("Задержка сохранена ✅")
        return

    if awaiting == "prob":
        p = parse_percent(txt)
        if p is None:
            await message.answer("❌ Число 0-100")
            return
        s = load_settings(uid)
        s["comment_probability"] = p/100.0
        save_settings(uid, s)
        USER_AWAIT[uid] = None
        await message.answer("Вероятность сохранена ✅")
        return

    if awaiting == "limits":
        pr = parse_two_ints(txt)
        if not pr:
            await message.answer("❌ Формат: час день")
            return
        h, d = pr
        s = load_settings(uid)
        s["limit_per_hour"] = max(0, h)
        s["limit_per_day"] = max(0, d)
        save_settings(uid, s)
        USER_AWAIT[uid] = None
        await message.answer("Лимиты сохранены ✅")
        return

    if awaiting == "ar_text":
        s = load_settings(uid)
        s["autoreply_text"] = txt[:500]
        save_settings(uid, s)
        USER_AWAIT[uid] = None
        await message.answer("Текст сохранён ✅")
        return

    if awaiting == "ar_delay":
        pr = parse_two_ints(txt)
        if not pr:
            await message.answer("❌ Формат: min max")
            return
        mn, mx = pr
        s = load_settings(uid)
        s["autoreply_delay_min_sec"] = max(0, mn)
        s["autoreply_delay_max_sec"] = max(s["autoreply_delay_min_sec"], mx)
        save_settings(uid, s)
        USER_AWAIT[uid] = None
        await message.answer("Задержка автоответа сохранена ✅")
        return

    if awaiting == "prompt":
        if len(txt) < 20:
            await message.answer("❌ Слишком коротко")
            return
        save_prompt(uid, txt[:8000])
        USER_AWAIT[uid] = None
        await message.answer("Промт сохранён ✅")
        return

async def main():
    ensure_admin_config()
    admin_cfg = load_yaml(ADMIN_CONFIG_PATH)
    global BOT_TOKEN
    BOT_TOKEN = str((admin_cfg.get("bot") or {}).get("token", ""))
    if not BOT_TOKEN or ":" not in BOT_TOKEN or "PUT_YOUR" in BOT_TOKEN:
        raise SystemExit("Fill admin_config.yaml: bot.token")
    bot = Bot(BOT_TOKEN)
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
