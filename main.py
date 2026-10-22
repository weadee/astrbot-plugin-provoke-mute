"""AstrBot 挑衅禁言插件

群友出言挑衅？自动送上一份禁言套餐。

特性：
- 内置挑衅词库，支持"只盯针对机器人的挑衅"与"全群扫描"两种模式
- 支持自定义关键词 / 正则词条
- 累犯阶梯升级，冷却保护，避免一条消息被反复禁言
- 戳一戳连击检测（戳机器人 N 次也算挑衅）
- 群主 / 群管 / 指定用户 / 指定群豁免
- 一整套管理指令，数据落 KV 存储

平台：仅 aiocqhttp（OneBot v11，如 NapCat / Lagrange），因为要用 set_group_ban。
"""

from __future__ import annotations

import difflib
import random
import re
import time
import unicodedata
from collections import deque
from typing import Any, Iterable

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At, Plain, Poke
from astrbot.api.star import Context, Star

PLUGIN_NAME = "astrbot_plugin_provoke_mute"

# 指令组名与别名
GROUP_NAMES: tuple[str, ...] = ("挑衅禁言", "provoke")

# 交互式指令的根指令名，用于把管理指令排除在挑衅检测之外
COMMAND_ROOTS: tuple[str, ...] = tuple(
    {p for name in GROUP_NAMES for p in (name, f"/{name}")}
) + ("/", "help")

# ---------------------------------------------------------------------------
# 默认挑衅词库
# 以 "re:" 开头表示正则（不区分大小写），否则按子串包含匹配。
# ---------------------------------------------------------------------------

# 主动叫板求禁言：这类话本身就是在跟机器人（群管）叫号，
# 因而除了当挑衅词用，也用来判定"这条消息是不是针对机器人"。
CHALLENGE_PATTERNS: tuple[str, ...] = (
    r"re:(?:有本事|你敢|你倒是|来|试试)?\s*(?:禁言|封禁)\s*(?:我|老子|你爹)",
    # 单字「封」必须带叫板前缀，否则「信封我」「查封我的」会被误判
    r"re:(?:有本事|你敢|你倒是|来|试试|给我)\s*封\s*(?:我|老子|你爹)",
)

# target_mode = "bot" 时使用：只有"冲着机器人来"的挑衅才算数
DEFAULT_BOT_PATTERNS: tuple[str, ...] = (
    # 骂机器人
    r"re:(?:机器人|bot|小布|智障ai|你个ai)\s*(?:你|它|他)?\s*(?:是|真|就|个|太|好|怎么)?\s*(?:垃圾|废物|弱智|智障|蠢|傻|呆|笨|菜|没用|饭桶|废|low)",
    r"re:(?:垃圾|废物|弱智|智障|蠢货|傻子|笨蛋|菜鸡|没用|饭桶|废物点心)\s*(?:机器人|bot|小布)",
    r"re:(?:你|尼|泥)\s*(?:是|真|就|个|这个|个破)?\s*(?:傻[逼比批]|傻子|垃圾|废物|弱智|智障|蠢货|饭桶)",
    # 直接开喷
    r"re:^(?:滚|爬|滚蛋|滚开|爬开|闭嘴|一边去|别说话|闭嘴吧)(?:吧|啊|啦|了)?[!！。]*$",
    # 实打实的人身攻击
    r"re:你(?:算|是)什么(?:东西|玩意)|你懂个(?:屁|锤|毛|啥)",
    # 命令口吻使唤机器人
    r"re:(?:给我|赶紧|快点|立刻)\s*(?:滚|爬|闭嘴|住口)",
) + CHALLENGE_PATTERNS

# 玩笑/抬杠高频句式：默认关闭。群里「你行你上」「不服来」太常见，
# 开了就是误伤制造机，想要的话把配置 casual_patterns 打开。
CASUAL_PATTERNS: tuple[str, ...] = (
    r"re:你行你上|有本事你(?:来|上)|不服(?:来|单挑|solo)|不服你(?:来|上)|有种你来|没本事别(?:叫|bb)",
    r"re:(?:你|尼|泥)\s*(?:是|真|就|个|这个|个破)?\s*(?:二货|二百五|250)",
)

# 重复刷屏检测的豁免短句（归一化后完整匹配即跳过）：
# 群里 "哈哈" "收到" "666" 连发太常见，不该算违规
REPEAT_IGNORE_WORDS: tuple[str, ...] = (
    "哈哈", "呵呵", "嘿嘿", "嘻嘻", "呜呜", "嘤嘤",
    "嗯嗯", "哦哦", "啊啊", "哇哦", "哎哎",
    "好好", "对对", "是是", "行行", "走走",
    "收到", "好的", "好嘞", "明白", "了解", "知道", "谢谢", "辛苦",
    "ok", "okok", "yes", "yeah", "no", "hi", "hello", "hey",
    "666", "6", "赞", "顶", "牛", "强", "绝", "冲", "啊这",
)

# target_mode = "any" 时使用：全群范围内的挑衅 / 辱骂
DEFAULT_GENERAL_PATTERNS: tuple[str, ...] = (
    # 英文缩写按单词边界匹配，避免命中随机字母组合
    r"re:\b(?:nmsl|nmdsl|cnmb|cnm|tmd|mdzz)\b|(?:傻[逼比批]|沙[雕比]|煞笔|傻屌|智障|弱智|脑残|狗东西|你妈|尼玛|草泥马|去死|死全家|全家死)",
    r"re:(?:你算什么东西|你懂个[屁锤毛])",
    r"re:^(?:滚|爬|滚蛋|滚开|爬开|闭嘴|一边去|别说话)(?:吧|啊|啦|了)?[!！。]*$",
)

# 默认回怼文案，{user} {duration} {count} {reason} 会被替换
DEFAULT_REPLY_TEMPLATES: tuple[str, ...] = (
    "{user} 挑衅值 +1，禁言 {duration}。这里不是擂台。",
    "{user} 嘴挺快，先安静 {duration} 吧。",
    "检测到挑衅，{user} 领走 {duration} 的禁言套餐。",
    "{user} 第 {count} 次了，{duration}，慢慢想。",
    "挑衅不是本事，{user} 去冷静 {duration}。",
)


def _fmt_duration(seconds: int) -> str:
    """把秒数格式化成人类可读的时长。"""
    seconds = int(seconds)
    if seconds <= 0:
        return "0 秒"
    if seconds < 60:
        return f"{seconds} 秒"
    if seconds < 3600:
        minutes, sec = divmod(seconds, 60)
        return f"{minutes} 分钟" + (f" {sec} 秒" if sec else "")
    hours, rem = divmod(seconds, 3600)
    minutes = rem // 60
    return f"{hours} 小时" + (f" {minutes} 分钟" if minutes else "")


def _shorten(text: str, limit: int = 24) -> str:
    text = str(text).replace("\n", " ").strip()
    return text if len(text) <= limit else text[:limit] + "…"


class ProvokeMutePlugin(Star):
    """群友挑衅自动禁言插件。

    发送 /挑衅禁言 查看全部指令。
    """

    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context, config)
        self.conf: dict = dict(config) if isinstance(config, dict) else {}
        self._loaded = False

        # 可在运行时通过指令修改，并持久化到 KV
        self._enabled: bool = True
        self._ban_duration: int | None = None
        self._target_mode: str | None = None
        self._repeat_limit: int | None = None
        self._repeat_scope: str | None = None
        self._dry_run: bool | None = None
        self._custom_words: list[str] = []
        self._exempt_users: list[str] = []
        self._exempt_groups: list[str] = []
        self._offenders: dict[str, dict] = {}
        self._records: list[dict] = []

        # 纯内存状态（重启即清空）
        self._poke_hits: dict[str, deque[float]] = {}
        self._repeat_hits: dict[str, deque[tuple[float, str]]] = {}
        self._last_mute: dict[str, float] = {}

    # ------------------------------------------------------------------
    # 生命周期 / 持久化
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        """插件激活时从 KV 恢复运行时状态。"""
        try:
            enabled = await self.get_kv_data("enabled", None)
            self._enabled = bool(
                self._conf("enabled", True) if enabled is None else enabled
            )

            duration = await self.get_kv_data("ban_duration", None)
            if duration:
                try:
                    self._ban_duration = int(duration)
                except (TypeError, ValueError):
                    self._ban_duration = None

            mode = await self.get_kv_data("target_mode", None)
            if isinstance(mode, str) and mode.strip().lower() in ("bot", "any"):
                self._target_mode = mode.strip().lower()

            repeat_limit = await self.get_kv_data("repeat_limit", None)
            if repeat_limit is not None:
                try:
                    self._repeat_limit = max(0, int(repeat_limit))
                except (TypeError, ValueError):
                    self._repeat_limit = None

            scope = await self.get_kv_data("repeat_scope", None)
            if isinstance(scope, str) and scope.strip().lower() in (
                "provocation",
                "all",
            ):
                self._repeat_scope = scope.strip().lower()

            dry_run = await self.get_kv_data("dry_run", None)
            if dry_run is not None:
                self._dry_run = bool(dry_run)

            self._custom_words = list(await self.get_kv_data("custom_words", []) or [])
            self._exempt_users = list(await self.get_kv_data("exempt_users", []) or [])
            self._exempt_groups = list(await self.get_kv_data("exempt_groups", []) or [])
            self._offenders = dict(await self.get_kv_data("offenders", {}) or {})
            self._records = list(await self.get_kv_data("records", []) or [])
        except Exception as e:  # KV 不可用不应导致插件不可用
            logger.warning(f"[{PLUGIN_NAME}] 读取本地数据失败，使用默认值: {e}")
        finally:
            self._loaded = True

        repeat_limit = self._eff_repeat_limit()
        logger.info(
            f"[{PLUGIN_NAME}] 已就绪：启用={self._enabled}，模式={self._eff_mode()}，"
            f"基础时长={self._eff_duration()}s，"
            f"刷屏阈值={repeat_limit if repeat_limit > 1 else '关'}，"
            f"自定义词条={len(self._custom_words)}",
        )
        if self._eff_dry_run():
            logger.warning(
                f"[{PLUGIN_NAME}] 当前是观察模式：命中只提示、不真禁言。"
                "确认没有误伤后在群里发「挑衅禁言 观察 关」转真实禁言。"
            )

    async def terminate(self) -> None:
        """插件卸载 / 重载时落盘。"""
        await self._save()

    async def _ensure_loaded(self) -> None:
        if not self._loaded:
            await self.initialize()

    async def _save(self, *keys: str) -> None:
        """把运行时状态写回 KV。"""
        snapshot: dict[str, Any] = {
            "enabled": self._enabled,
            "ban_duration": self._ban_duration,
            "target_mode": self._target_mode,
            "repeat_limit": self._repeat_limit,
            "repeat_scope": self._repeat_scope,
            "dry_run": self._dry_run,
            "custom_words": self._custom_words,
            "exempt_users": self._exempt_users,
            "exempt_groups": self._exempt_groups,
            "offenders": self._offenders,
            "records": self._records[-100:],
        }
        for key in keys or tuple(snapshot):
            try:
                await self.put_kv_data(key, snapshot[key])
            except Exception as e:
                logger.warning(f"[{PLUGIN_NAME}] 保存 {key} 失败: {e}")

    # ------------------------------------------------------------------
    # 配置读取
    # ------------------------------------------------------------------

    def _conf(self, key: str, default: Any = None) -> Any:
        try:
            value = self.conf.get(key, default)
        except Exception:
            return default
        return default if value is None else value

    def _eff_duration(self) -> int:
        """生效的基础禁言时长。"""
        if self._ban_duration:
            return int(self._ban_duration)
        try:
            return max(1, int(self._conf("ban_duration", 180)))
        except (TypeError, ValueError):
            return 180

    def _eff_mode(self) -> str:
        """生效的判定模式：bot（只盯针对机器人的）或 any（全群扫描）。"""
        if self._target_mode in ("bot", "any"):
            return self._target_mode
        return "any" if str(self._conf("target_mode", "bot")).strip().lower() == "any" else "bot"

    def _eff_repeat_limit(self) -> int:
        """生效的重复刷屏阈值，<= 1 表示关闭该检测。

        运行时指令（挑衅禁言 刷屏 <n>）优先于配置文件。
        """
        if self._repeat_limit is not None:
            return int(self._repeat_limit)
        if not self._conf("detect_repeat", True):
            return 0
        try:
            return int(self._conf("repeat_limit", 2))
        except (TypeError, ValueError):
            return 2

    def _eff_repeat_scope(self) -> str:
        """生效的刷屏检测范围。

        - provocation（默认）：只有命中挑衅词的消息重复才算，正常话连发不管
        - all：任何内容重复都算
        """
        if self._repeat_scope in ("provocation", "all"):
            return self._repeat_scope
        value = str(self._conf("repeat_scope", "all")).strip().lower()
        return value if value in ("provocation", "all") else "all"

    def _eff_dry_run(self) -> bool:
        """观察模式：命中只提示，不真禁言。"""
        if self._dry_run is not None:
            return bool(self._dry_run)
        return bool(self._conf("dry_run", False))

    def _repeat_state_text(self) -> str:
        """重复刷屏检测的可读状态。"""
        limit = self._eff_repeat_limit()
        if limit <= 1:
            return "关"
        try:
            window = _fmt_duration(int(self._conf("repeat_window_seconds", 20)))
        except (TypeError, ValueError):
            window = "20 秒"
        scope = "任何内容" if self._eff_repeat_scope() == "all" else "仅挑衅内容"
        return f"开（{window} 内连发 {limit} 遍即禁言，范围：{scope}）"

    def _all_patterns(self) -> list[str]:
        mode = self._eff_mode()
        patterns: list[str] = []
        if self._conf("use_default_patterns", True):
            if mode == "any":
                patterns.extend(DEFAULT_GENERAL_PATTERNS)
            else:
                patterns.extend(DEFAULT_BOT_PATTERNS)
        if self._conf("casual_patterns", False):
            patterns.extend(CASUAL_PATTERNS)
        for word in self._conf("extra_patterns", []) or []:
            if isinstance(word, str) and word.strip():
                patterns.append(word.strip())
        patterns.extend(w for w in self._custom_words if str(w).strip())
        return patterns

    # ------------------------------------------------------------------
    # 主检测流程
    # ------------------------------------------------------------------

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    async def on_group_message(self, event: AstrMessageEvent):
        """群消息挑衅检测。"""
        await self._ensure_loaded()
        if not self._enabled:
            return

        group_id = str(event.get_group_id() or "")
        sender_id = str(event.get_sender_id() or "")
        self_id = str(event.get_self_id() or "")
        if not group_id or not sender_id or sender_id == self_id:
            return
        if not self._group_allowed(group_id):
            return
        if self._is_own_command(event):
            return
        if self._is_exempt(event, sender_id):
            return

        # 1) 戳一戳连击
        if self._conf("detect_poke", True):
            poke_reason = self._check_poke(event, group_id, sender_id)
            if poke_reason:
                async for result in self._mute(
                    event, group_id, sender_id, poke_reason
                ):
                    yield result
                return

        # 2) 重复刷屏（同一个人短时间内连发相同消息）
        repeat_reason = self._check_repeat(event, group_id, sender_id)
        if repeat_reason:
            async for result in self._mute(event, group_id, sender_id, repeat_reason):
                yield result
            return

        # 3) 文本挑衅
        text = (event.get_message_str() or "").strip()
        if not text:
            return
        mode = self._eff_mode()
        if mode != "any" and not self._is_targeting_bot(event):
            return

        hit = self._match_patterns(text)
        if not hit:
            return

        reason = f"命中挑衅词「{_shorten(hit)}」"
        async for result in self._mute(event, group_id, sender_id, reason):
            yield result

    @staticmethod
    def _match_list(text: str, patterns: Iterable[str]) -> str | None:
        """在给定词条列表里匹配，返回命中的词条，未命中返回 None。"""
        lowered = text.lower()
        for pattern in patterns:
            if not isinstance(pattern, str) or not pattern.strip():
                continue
            pattern = pattern.strip()
            if pattern.startswith("re:"):
                expr = pattern[3:].strip()
                if not expr:
                    continue
                try:
                    if re.search(expr, text, re.IGNORECASE):
                        return expr
                except re.error:
                    logger.warning(f"[{PLUGIN_NAME}] 非法正则已跳过: {expr}")
            elif pattern.lower() in lowered:
                return pattern
        return None

    def _match_patterns(self, text: str) -> str | None:
        """返回命中的词条，未命中返回 None。"""
        return self._match_list(text, self._all_patterns())

    @staticmethod
    def _alias_hit(text: str, alias: str) -> bool:
        """别名命中判定。

        纯英文 / 数字的别名按**单词边界**匹配 —— 否则 "bot" 会命中
        "robot" / "bottle" / "bottom" 这类无关词，是误伤的大头之一。
        """
        if re.fullmatch(r"[a-z0-9_]+", alias):
            pattern = rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])"
            return re.search(pattern, text) is not None
        return alias in text

    def _is_targeting_bot(self, event: AstrMessageEvent) -> bool:
        """判断这条消息是不是冲着机器人来的。"""
        self_id = str(event.get_self_id() or "")
        for message in event.get_messages():
            if isinstance(message, At) and str(getattr(message, "qq", "")) == self_id:
                return True
            if (
                type(message).__name__ == "Reply"
                and str(getattr(message, "sender_id", "")) == self_id
            ):
                return True
        text = (event.get_message_str() or "").lower()
        aliases = self._conf("bot_aliases", ["机器人", "bot", "小布"])
        for alias in aliases or []:
            alias = str(alias).strip().lower()
            if alias and self._alias_hit(text, alias):
                return True
        # 「来禁言我」这类叫板本身就是在跟机器人（群管）叫号
        if self._conf("use_default_patterns", True) and self._match_list(
            text, CHALLENGE_PATTERNS
        ):
            return True
        return False

    def _check_poke(
        self, event: AstrMessageEvent, group_id: str, sender_id: str
    ) -> str | None:
        """戳一戳连击检测，命中返回原因文本。"""
        target: str | None = None
        poked = False
        for message in event.get_messages():
            if isinstance(message, Poke):
                poked = True
                getter = getattr(message, "target_id", None)
                target = getter() if callable(getter) else getattr(message, "id", None)
        if not poked:
            return None
        if target and str(target) != str(event.get_self_id() or ""):
            return None  # 戳别人不管

        try:
            limit = int(self._conf("poke_limit", 3))
        except (TypeError, ValueError):
            limit = 3
        if limit <= 0:
            return None
        try:
            window = float(self._conf("poke_window_seconds", 60))
        except (TypeError, ValueError):
            window = 60.0

        key = f"{group_id}:{sender_id}"
        now = time.time()
        hits = self._poke_hits.setdefault(key, deque())
        hits.append(now)
        while hits and now - hits[0] > window:
            hits.popleft()
        if len(hits) < limit:
            return None
        hits.clear()
        return f"{_fmt_duration(int(window))} 内戳了 {limit} 次"

    # ------------------------------------------------------------------
    # 重复刷屏检测
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_text(text: str) -> str:
        """归一化文本用于重复判定：全角转半角、去空白与标点、转小写。"""
        normalized = unicodedata.normalize("NFKC", str(text))
        return re.sub(r"[\s\W_]+", "", normalized).lower()

    def _text_similar(self, a: str, b: str) -> bool:
        """两段归一化文本是否算「同一条消息」。

        默认要求完全相同（repeat_similarity = 1.0）；
        调低阈值则按相似度判定。
        """
        if not a or not b:
            return False
        if a == b:
            return True
        try:
            threshold = float(self._conf("repeat_similarity", 1.0))
        except (TypeError, ValueError):
            threshold = 1.0
        if threshold >= 1.0:
            return False
        return difflib.SequenceMatcher(None, a, b).ratio() >= max(0.5, threshold)

    @staticmethod
    def _collapse_repeats(text: str) -> str:
        """把连续重复字符折叠成一个："哈哈哈哈" -> "哈"。"""
        return re.sub(r"(.)\1+", r"\1", text)

    def _is_trivial_repeat(self, normalized: str) -> bool:
        """判断是不是"哈哈""666""收到"这类没信息量的短句。"""
        extra = {
            str(w).strip().lower()
            for w in (self._conf("repeat_ignore_words", []) or [])
            if str(w).strip()
        }
        if normalized in (extra | set(REPEAT_IGNORE_WORDS)):
            return True
        # 折叠后只剩一个字符 → 纯粹是重复字符，没有语义
        return len(self._collapse_repeats(normalized)) <= 1

    def _check_repeat(
        self, event: AstrMessageEvent, group_id: str, sender_id: str
    ) -> str | None:
        """同一人短时间内连发相同消息的检测，命中返回原因文本。

        repeat_scope = "provocation"（默认）时，只有命中挑衅词的消息重复
        才算违规 —— 否则 "哈哈" "收到" "666" 连发会被大量误判。
        """
        limit = self._eff_repeat_limit()
        if limit <= 1:
            return None

        raw = (event.get_message_str() or "").strip()
        normalized = self._normalize_text(raw)
        try:
            min_length = max(1, int(self._conf("repeat_min_length", 2)))
        except (TypeError, ValueError):
            min_length = 2
        if len(normalized) < min_length:
            return None

        if self._is_trivial_repeat(normalized):
            return None

        if self._eff_repeat_scope() == "provocation" and not self._match_list(
            raw, self._all_patterns()
        ):
            return None

        try:
            window = max(1.0, float(self._conf("repeat_window_seconds", 20)))
        except (TypeError, ValueError):
            window = 20.0

        key = f"{group_id}:{sender_id}"
        now = time.time()
        hits = self._repeat_hits.setdefault(key, deque())
        hits.append((now, normalized))
        while hits and now - hits[0][0] > window:
            hits.popleft()

        same = sum(1 for _, prev in hits if self._text_similar(prev, normalized))
        if same < limit:
            if not hits:
                self._repeat_hits.pop(key, None)
            return None

        # 触发后重新计数，避免同一批消息被反复判罚
        self._repeat_hits.pop(key, None)
        return f"{_fmt_duration(int(window))} 内连发 {same} 遍相同消息「{_shorten(raw, 12)}」"

    # ------------------------------------------------------------------
    # 禁言执行
    # ------------------------------------------------------------------

    async def _mute(self, event: AstrMessageEvent, group_id: str, sender_id: str, reason: str):
        """执行禁言并回怼。"""
        now = time.time()
        cooldown_key = f"{group_id}:{sender_id}"
        dry = self._eff_dry_run()
        try:
            cooldown = float(self._conf("cooldown_seconds", 10))
        except (TypeError, ValueError):
            cooldown = 10.0
        # 观察模式下不套冷却，否则会掩盖真实的误伤频率
        if not dry:
            if cooldown > 0 and now - self._last_mute.get(cooldown_key, 0.0) < cooldown:
                logger.debug(f"[{PLUGIN_NAME}] {cooldown_key} 处于冷却期，跳过")
                return
            self._last_mute[cooldown_key] = now

        if dry:
            count = 1  # 观察模式不计入累犯
        else:
            count, _total = self._bump_offender(group_id, sender_id, now)
        duration = self._calc_duration(self._eff_duration(), count)

        if dry:
            name = event.get_sender_name() or sender_id
            self._push_record(group_id, sender_id, name, f"[观察] {reason}", duration)
            await self._save("records")
            logger.info(
                f"[{PLUGIN_NAME}] [观察模式] 群 {group_id} 本应禁言 "
                f"{name}({sender_id}) {duration}s，原因：{reason}",
            )
            yield event.plain_result(
                f"[观察模式] 这条本会被禁言 {_fmt_duration(duration)}（{reason}），实际没执行。\n"
                "觉得判错了就调词库/阈值，确认准了再 挑衅禁言 观察 关。",
            )
            if self._conf("stop_after_mute", True):
                event.stop_event()
            return

        ok, error = await self._set_group_ban(event, group_id, sender_id, duration)
        if not ok:
            logger.warning(
                f"[{PLUGIN_NAME}] 禁言 {sender_id} 失败（群 {group_id}）: {error}",
            )
            yield event.plain_result(f"想给 {sender_id} 上禁言，但没成功：{error}")
            return

        name = event.get_sender_name() or sender_id
        self._push_record(group_id, sender_id, name, reason, duration)
        await self._save("offenders", "records")

        logger.info(
            f"[{PLUGIN_NAME}] 群 {group_id} 禁言 {name}({sender_id}) {duration}s，原因：{reason}",
        )

        text = self._render_reply(name, duration, count, reason)
        chain = [At(qq=sender_id), Plain(f" {text}")]
        if self._conf("show_reason", True):
            chain.append(Plain(f"\n（{reason}）"))

        # 先把回怼消息交出去（RespondStage 会发送），再终止事件传播，
        # 否则消息可能因为事件被 stop 而送不出去。
        yield event.chain_result(chain)
        if self._conf("stop_after_mute", True):
            event.stop_event()

    async def _set_group_ban(
        self, event: AstrMessageEvent, group_id: str, user_id: str, duration: int
    ) -> tuple[bool, str]:
        """调用 OneBot 的 set_group_ban。"""
        bot = getattr(event, "bot", None)
        if bot is None:
            return False, "当前平台适配器不支持群禁言（仅 aiocqhttp / OneBot v11）"
        if not (group_id.isdigit() and user_id.isdigit()):
            return False, f"群号或 QQ 号不是纯数字：{group_id} / {user_id}"

        params: dict[str, Any] = {
            "group_id": int(group_id),
            "user_id": int(user_id),
            "duration": int(duration),
        }
        self_id = str(event.get_self_id() or "")
        if self_id.isdigit():
            params["self_id"] = int(self_id)

        call_action = getattr(bot, "call_action", None)
        if not callable(call_action):
            return False, "bot 实例缺少 call_action 方法"
        try:
            await call_action("set_group_ban", **params)
        except Exception as e:
            return False, self._explain_error(e)
        return True, ""

    @staticmethod
    def _explain_error(error: Exception) -> str:
        detail = str(error)
        lowered = detail.lower()
        if "permission" in lowered or "权限" in detail or "admin" in lowered:
            return "权限不足，请先把机器人设成群管理员"
        if "role" in lowered:
            return "目标身份不允许被禁言（可能是群主或同级管理员）"
        return detail or error.__class__.__name__

    def _render_reply(self, name: str, duration: int, count: int, reason: str) -> str:
        templates = [
            t
            for t in (self._conf("reply_templates", []) or [])
            if isinstance(t, str) and t.strip()
        ] or list(DEFAULT_REPLY_TEMPLATES)
        template = random.choice(templates)
        try:
            return template.format(
                user=name,
                duration=_fmt_duration(duration),
                count=count,
                reason=reason,
            )
        except (KeyError, IndexError, ValueError):
            return f"{name} 挑衅值 +1，禁言 {_fmt_duration(duration)}。"

    # ------------------------------------------------------------------
    # 违规记录
    # ------------------------------------------------------------------

    def _bump_offender(self, group_id: str, sender_id: str, now: float) -> tuple[int, int]:
        """累加违规次数，返回 (窗口内次数, 累计次数)。"""
        key = f"{group_id}:{sender_id}"
        item = dict(self._offenders.get(key) or {"count": 0, "last": 0.0, "total": 0})
        try:
            decay = float(self._conf("decay_minutes", 30)) * 60
        except (TypeError, ValueError):
            decay = 1800.0
        if decay > 0 and now - float(item.get("last", 0.0)) > decay:
            item["count"] = 0
        item["count"] = int(item.get("count", 0)) + 1
        item["total"] = int(item.get("total", 0)) + 1
        item["last"] = now
        self._offenders[key] = item
        return item["count"], item["total"]

    def _calc_duration(self, base: int, count: int) -> int:
        """按累犯次数升级禁言时长。"""
        if not self._conf("escalate", True) or count <= 1:
            duration = base
        else:
            try:
                step = float(self._conf("escalate_step", 0.5))
            except (TypeError, ValueError):
                step = 0.5
            duration = int(base * (1 + step * (count - 1)))
        try:
            max_duration = int(self._conf("max_duration", 3600))
        except (TypeError, ValueError):
            max_duration = 3600
        if max_duration > 0:
            duration = min(duration, max_duration)
        return max(1, duration)

    def _push_record(
        self, group_id: str, sender_id: str, name: str, reason: str, duration: int
    ) -> None:
        self._records.append(
            {
                "ts": time.time(),
                "group": group_id,
                "user": sender_id,
                "name": name,
                "reason": reason,
                "duration": duration,
            }
        )
        self._records = self._records[-100:]

    # ------------------------------------------------------------------
    # 豁免 / 群范围判断
    # ------------------------------------------------------------------

    def _static_ids(self, key: str) -> set[str]:
        values = self._conf(key, []) or []
        if isinstance(values, (str, int)):
            values = [values]
        return {str(v).strip() for v in values if str(v).strip()}

    def _group_allowed(self, group_id: str) -> bool:
        exempt = set(self._exempt_groups) | self._static_ids("exempt_groups")
        if group_id in exempt:
            return False
        whitelist = self._static_ids("whitelist_groups")
        if whitelist and group_id not in whitelist:
            return False
        return True

    def _is_exempt(self, event: AstrMessageEvent, sender_id: str) -> bool:
        exempt = set(self._exempt_users) | self._static_ids("exempt_users")
        if str(sender_id) in exempt:
            return True
        if self._conf("exempt_group_staff", True):
            if self._sender_group_role(event) in ("owner", "admin", "administrator"):
                return True
        return False

    @staticmethod
    def _sender_group_role(event: AstrMessageEvent) -> str:
        """从原始事件里读发送者在群内的身份（owner / admin / member）。"""
        raw = getattr(event.message_obj, "raw_message", None)
        if raw is None:
            return ""
        try:
            sender = raw.get("sender") if hasattr(raw, "get") else None
            if sender is None:
                sender = getattr(raw, "sender", None)
            if sender is None:
                return ""
            role = sender.get("role") if hasattr(sender, "get") else None
            if role is None:
                role = getattr(sender, "role", None)
            return str(role or "")
        except Exception:
            return ""

    def _is_own_command(self, event: AstrMessageEvent) -> bool:
        text = re.sub(r"\s+", " ", (event.get_message_str() or "").strip())
        return any(text.startswith(root) for root in COMMAND_ROOTS)

    def _can_manage(self, event: AstrMessageEvent) -> bool:
        if event.is_admin():
            return True
        return (
            self._conf("manage_allow_group_staff", True)
            and self._sender_group_role(event) in ("owner", "admin", "administrator")
        )

    # ------------------------------------------------------------------
    # 管理指令
    # ------------------------------------------------------------------

    def _rest(self, event: AstrMessageEvent, sub_command: str) -> str:
        """取子指令后面的参数部分。"""
        text = re.sub(r"\s+", " ", (event.get_message_str() or "").strip())
        for group_name in GROUP_NAMES:
            prefix = f"{group_name} {sub_command}"
            if text == prefix:
                return ""
            if text.startswith(prefix + " "):
                return text[len(prefix) + 1 :].strip()
        return ""

    def _extract_target(self, event: AstrMessageEvent) -> str:
        """从 At 组件或文本里提取目标 QQ 号。"""
        for message in event.get_messages():
            if isinstance(message, At):
                qq = str(getattr(message, "qq", "") or "").strip()
                if qq and qq != "all":
                    return qq
        digits = re.findall(r"\d{5,12}", event.get_message_str() or "")
        return digits[-1] if digits else ""

    @filter.command_group("挑衅禁言", alias={"provoke"})
    def provoke(self):
        """群友挑衅自动禁言。"""

    @provoke.command("状态")
    async def cmd_status(self, event: AstrMessageEvent):
        """查看当前配置与统计"""
        await self._ensure_loaded()
        lines = [
            "挑衅禁言 · 运行状态",
            f"开关：{'开' if self._enabled else '关'}",
            f"判定模式：{'全群扫描' if self._eff_mode() == 'any' else '仅针对机器人'}",
            f"基础禁言：{_fmt_duration(self._eff_duration())}",
            f"累犯升级：{'开' if self._conf('escalate', True) else '关'}（每多一次 +{self._conf('escalate_step', 0.5)}，上限 {_fmt_duration(int(self._conf('max_duration', 3600)))}）",
            f"冷却间隔：{_fmt_duration(int(self._conf('cooldown_seconds', 10)))}",
            f"戳一戳检测：{'开' if self._conf('detect_poke', True) else '关'}（{self._conf('poke_limit', 3)} 次 / {_fmt_duration(int(self._conf('poke_window_seconds', 60)))}）",
            f"重复刷屏：{self._repeat_state_text()}",
            f"观察模式：{'开（命中只提示，不真禁言）' if self._eff_dry_run() else '关'}",
            f"生效词条：{len(self._all_patterns())} 条（其中自定义 {len(self._custom_words)} 条）",
            f"豁免用户：{len(set(self._exempt_users) | self._static_ids('exempt_users'))} 个",
            f"豁免群：{len(set(self._exempt_groups) | self._static_ids('exempt_groups'))} 个",
            f"累计记录：{len(self._records)} 条",
        ]
        yield event.plain_result("\n".join(lines))

    @provoke.command("帮助")
    async def cmd_help(self, event: AstrMessageEvent):
        """查看全部指令"""
        yield event.plain_result(self._help_text())

    def _help_text(self) -> str:
        return "\n".join(
            [
                "挑衅禁言 · 指令一览",
                "挑衅禁言 状态",
                "挑衅禁言 开 / 关",
                "挑衅禁言 时长 <秒>",
                "挑衅禁言 模式 机器人 / 全群",
                "挑衅禁言 词列表",
                "挑衅禁言 词添加 <内容|re:正则>",
                "挑衅禁言 词删除 <序号|内容>",
                "挑衅禁言 词重置",
                "挑衅禁言 豁免列表",
                "挑衅禁言 豁免添加 <@某人|QQ>",
                "挑衅禁言 豁免删除 <@某人|QQ>",
                "挑衅禁言 忽略本群 / 监控本群",
                "挑衅禁言 禁言 <@某人|QQ> [秒]",
                "挑衅禁言 解禁 <@某人|QQ>",
                "挑衅禁言 记录 [条数]",
                "挑衅禁言 刷屏 <条数|挑衅|全量|开|关>",
                "挑衅禁言 观察 开 / 关（只提示不真禁言）",
                "挑衅禁言 测试 <文本>",
                "挑衅禁言 清空记录",
            ]
        )

    @provoke.command("开")
    async def cmd_enable(self, event: AstrMessageEvent):
        """启用自动禁言"""
        if not self._can_manage(event):
            yield event.plain_result("这事你说了不算。")
            return
        await self._ensure_loaded()
        self._enabled = True
        await self._save("enabled")
        yield event.plain_result("挑衅禁言已开启，管好你的嘴。")

    @provoke.command("关")
    async def cmd_disable(self, event: AstrMessageEvent):
        """关闭自动禁言"""
        if not self._can_manage(event):
            yield event.plain_result("这事你说了不算。")
            return
        await self._ensure_loaded()
        self._enabled = False
        await self._save("enabled")
        yield event.plain_result("挑衅禁言已关闭，江湖再见。")

    @provoke.command("时长")
    async def cmd_duration(self, event: AstrMessageEvent):
        """设置基础禁言时长（秒）"""
        if not self._can_manage(event):
            yield event.plain_result("这事你说了不算。")
            return
        await self._ensure_loaded()
        raw = self._rest(event, "时长")
        digits = re.findall(r"\d+", raw)
        if not digits:
            yield event.plain_result(f"当前基础禁言时长：{_fmt_duration(self._eff_duration())}\n用法：挑衅禁言 时长 300")
            return
        seconds = int(digits[0])
        if seconds < 1 or seconds > 2592000:
            yield event.plain_result("时长请给 1 ~ 2592000 秒（30 天）。")
            return
        self._ban_duration = seconds
        await self._save("ban_duration")
        yield event.plain_result(f"基础禁言时长已设为 {_fmt_duration(seconds)}。")

    @provoke.command("模式")
    async def cmd_mode(self, event: AstrMessageEvent):
        """切换判定模式：机器人 / 全群"""
        if not self._can_manage(event):
            yield event.plain_result("这事你说了不算。")
            return
        await self._ensure_loaded()
        raw = self._rest(event, "模式").lower()
        if raw in ("机器人", "bot", "0"):
            self._target_mode = "bot"
        elif raw in ("全群", "any", "all", "1"):
            self._target_mode = "any"
        else:
            current = "全群扫描" if self._eff_mode() == "any" else "仅针对机器人"
            yield event.plain_result(
                f"当前模式：{current}\n用法：挑衅禁言 模式 机器人 / 挑衅禁言 模式 全群",
            )
            return
        await self._save("target_mode")
        label = "全群扫描" if self._target_mode == "any" else "仅针对机器人"
        tip = "（全群模式下群友互怼也会被管，注意配好豁免名单）" if self._target_mode == "any" else ""
        yield event.plain_result(f"判定模式已切换为「{label}」并已保存。{tip}")

    @provoke.command("刷屏")
    async def cmd_repeat(self, event: AstrMessageEvent):
        """设置重复刷屏阈值：条数 / 开 / 关"""
        if not self._can_manage(event):
            yield event.plain_result("这事你说了不算。")
            return
        await self._ensure_loaded()
        raw = self._rest(event, "刷屏").strip().lower()
        if not raw:
            yield event.plain_result(
                f"当前重复刷屏：{self._repeat_state_text()}\n"
                "用法：挑衅禁言 刷屏 2（连发几遍算违规）\n"
                "　　　挑衅禁言 刷屏 挑衅 / 全量（检测范围）\n"
                "　　　挑衅禁言 刷屏 关 / 开",
            )
            return
        if raw in ("全量", "all", "任意", "全部"):
            self._repeat_scope = "all"
            await self._save("repeat_scope")
            yield event.plain_result(
                "刷屏检测范围已切换为「任何内容」——"
                "任何消息连发都会判违规，正常话容易被误伤，慎用。",
            )
            return
        if raw in ("挑衅", "provocation", "仅挑衅", "保守"):
            self._repeat_scope = "provocation"
            await self._save("repeat_scope")
            yield event.plain_result(
                "刷屏检测范围已切换为「仅挑衅内容」，正常话连发不再管。",
            )
            return
        if raw in ("关", "off", "0"):
            self._repeat_limit = 0
        elif raw in ("开", "on", "默认", "default"):
            self._repeat_limit = None
        else:
            digits = re.findall(r"\d+", raw)
            if not digits:
                yield event.plain_result(
                    "用法：挑衅禁言 刷屏 2 / 挑衅禁言 刷屏 挑衅 / 挑衅禁言 刷屏 关",
                )
                return
            self._repeat_limit = max(0, int(digits[0]))
        await self._save("repeat_limit")
        yield event.plain_result(f"重复刷屏检测已更新：{self._repeat_state_text()}（已保存）")

    @provoke.command("观察")
    async def cmd_dry_run(self, event: AstrMessageEvent):
        """观察模式：命中只提示不真禁言，用来调词库 / 验误伤"""
        if not self._can_manage(event):
            yield event.plain_result("这事你说了不算。")
            return
        await self._ensure_loaded()
        raw = self._rest(event, "观察").strip().lower()
        if raw in ("开", "on", "1", "true"):
            self._dry_run = True
        elif raw in ("关", "off", "0", "false"):
            self._dry_run = False
        else:
            yield event.plain_result(
                f"观察模式：{'开（命中只提示，不真禁言）' if self._eff_dry_run() else '关（真禁言）'}\n"
                "用法：挑衅禁言 观察 开 / 挑衅禁言 观察 关",
            )
            return
        await self._save("dry_run")
        yield event.plain_result(
            "观察模式已开启：命中只发提示，不真禁言，也不会计入累犯。"
            "用 挑衅禁言 记录 看误伤情况，确认准了再 挑衅禁言 观察 关。"
            if self._dry_run
            else "观察模式已关闭，恢复真实禁言。"
        )

    @provoke.command("词列表")
    async def cmd_word_list(self, event: AstrMessageEvent):
        """查看词库"""
        await self._ensure_loaded()
        lines = ["挑衅禁言 · 词库", f"内置：{len(self._all_patterns()) - len(self._custom_words)} 条（随模式变化）"]
        if not self._custom_words:
            lines.append("自定义：暂无。用 挑衅禁言 词添加 <内容> 添加。")
        else:
            lines.append("自定义：")
            for idx, word in enumerate(self._custom_words, 1):
                lines.append(f"{idx}. {_shorten(word, 40)}")
        yield event.plain_result("\n".join(lines))

    @provoke.command("词添加")
    async def cmd_word_add(self, event: AstrMessageEvent):
        """添加自定义挑衅词（re: 开头为正则）"""
        if not self._can_manage(event):
            yield event.plain_result("这事你说了不算。")
            return
        await self._ensure_loaded()
        word = self._rest(event, "词添加")
        if not word:
            yield event.plain_result("用法：挑衅禁言 词添加 <内容>，正则请写 re:你的正则")
            return
        if word.startswith("re:"):
            try:
                re.compile(word[3:].strip())
            except re.error as e:
                yield event.plain_result(f"正则不合法：{e}")
                return
        if word in self._custom_words:
            yield event.plain_result("这条已经在词库里了。")
            return
        self._custom_words.append(word)
        await self._save("custom_words")
        yield event.plain_result(f"已添加：{_shorten(word, 40)}（当前自定义 {len(self._custom_words)} 条）")

    @provoke.command("词删除")
    async def cmd_word_del(self, event: AstrMessageEvent):
        """删除自定义挑衅词（可给序号）"""
        if not self._can_manage(event):
            yield event.plain_result("这事你说了不算。")
            return
        await self._ensure_loaded()
        raw = self._rest(event, "词删除")
        if not raw:
            yield event.plain_result("用法：挑衅禁言 词删除 <序号|内容>")
            return
        if raw.isdigit() and 1 <= int(raw) <= len(self._custom_words):
            removed = self._custom_words.pop(int(raw) - 1)
        elif raw in self._custom_words:
            self._custom_words.remove(raw)
            removed = raw
        else:
            yield event.plain_result("没找到这条词。用 挑衅禁言 词列表 看序号。")
            return
        await self._save("custom_words")
        yield event.plain_result(f"已删除：{_shorten(removed, 40)}")

    @provoke.command("词重置")
    async def cmd_word_reset(self, event: AstrMessageEvent):
        """清空自定义词库"""
        if not self._can_manage(event):
            yield event.plain_result("这事你说了不算。")
            return
        await self._ensure_loaded()
        count = len(self._custom_words)
        self._custom_words = []
        await self._save("custom_words")
        yield event.plain_result(f"自定义词库已清空（删除 {count} 条），内置词库不动。")

    @provoke.command("豁免列表")
    async def cmd_exempt_list(self, event: AstrMessageEvent):
        """查看豁免名单"""
        await self._ensure_loaded()
        users = sorted(set(self._exempt_users) | self._static_ids("exempt_users"))
        groups = sorted(set(self._exempt_groups) | self._static_ids("exempt_groups"))
        lines = ["挑衅禁言 · 豁免名单"]
        lines.append("用户：" + ("、".join(users) if users else "无"))
        lines.append("群：" + ("、".join(groups) if groups else "无"))
        lines.append(f"群主/管理员豁免：{'开' if self._conf('exempt_group_staff', True) else '关'}")
        yield event.plain_result("\n".join(lines))

    @provoke.command("豁免添加")
    async def cmd_exempt_add(self, event: AstrMessageEvent):
        """把某人加入豁免名单"""
        if not self._can_manage(event):
            yield event.plain_result("这事你说了不算。")
            return
        await self._ensure_loaded()
        target = self._extract_target(event)
        if not target or not target.isdigit():
            yield event.plain_result("用法：挑衅禁言 豁免添加 @某人 或 挑衅禁言 豁免添加 10001")
            return
        if target in self._exempt_users:
            yield event.plain_result("这人已经在豁免名单里了。")
            return
        self._exempt_users.append(target)
        await self._save("exempt_users")
        yield event.plain_result(f"已豁免 {target}（当前 {len(self._exempt_users)} 人）")

    @provoke.command("豁免删除")
    async def cmd_exempt_del(self, event: AstrMessageEvent):
        """把某人移出豁免名单"""
        if not self._can_manage(event):
            yield event.plain_result("这事你说了不算。")
            return
        await self._ensure_loaded()
        target = self._extract_target(event)
        if target in self._exempt_users:
            self._exempt_users.remove(target)
            await self._save("exempt_users")
            yield event.plain_result(f"已取消豁免 {target}")
            return
        yield event.plain_result("这人不在豁免名单里（若来自配置文件，请改插件配置）。")

    @provoke.command("忽略本群")
    async def cmd_group_ignore(self, event: AstrMessageEvent):
        """本群不参与挑衅检测"""
        if not self._can_manage(event):
            yield event.plain_result("这事你说了不算。")
            return
        await self._ensure_loaded()
        group_id = str(event.get_group_id() or "")
        if not group_id:
            yield event.plain_result("这个命令得在群里发。")
            return
        if group_id in self._exempt_groups:
            yield event.plain_result("本群已经在豁免名单里了。")
            return
        self._exempt_groups.append(group_id)
        await self._save("exempt_groups")
        yield event.plain_result("本群已忽略，这里随便骂。")

    @provoke.command("监控本群")
    async def cmd_group_watch(self, event: AstrMessageEvent):
        """恢复本群的挑衅检测"""
        if not self._can_manage(event):
            yield event.plain_result("这事你说了不算。")
            return
        await self._ensure_loaded()
        group_id = str(event.get_group_id() or "")
        if group_id in self._exempt_groups:
            self._exempt_groups.remove(group_id)
            await self._save("exempt_groups")
            yield event.plain_result("本群恢复监控，注意言辞。")
            return
        yield event.plain_result("本群本来就在监控中（若在配置文件的豁免列表里，请改插件配置）。")

    @provoke.command("禁言")
    async def cmd_manual_mute(self, event: AstrMessageEvent):
        """手动禁言某人，可指定秒数"""
        if not self._can_manage(event):
            yield event.plain_result("这事你说了不算。")
            return
        target = self._extract_target(event)
        if not target or not target.isdigit():
            yield event.plain_result("用法：挑衅禁言 禁言 @某人 [秒数]")
            return
        raw = self._rest(event, "禁言")
        digits = [d for d in re.findall(r"\d+", raw) if d != target]
        duration = int(digits[0]) if digits else self._eff_duration()
        group_id = str(event.get_group_id() or "")
        if not group_id:
            yield event.plain_result("这个命令得在群里发。")
            return
        ok, error = await self._set_group_ban(event, group_id, target, duration)
        if not ok:
            yield event.plain_result(f"禁言失败：{error}")
            return
        name = event.get_sender_name() or target
        self._push_record(group_id, target, name, "管理员手动禁言", duration)
        await self._save("records")
        yield event.plain_result(f"{target} 已被禁言 {_fmt_duration(duration)}。")

    @provoke.command("解禁")
    async def cmd_unmute(self, event: AstrMessageEvent):
        """解除某人的禁言"""
        if not self._can_manage(event):
            yield event.plain_result("这事你说了不算。")
            return
        target = self._extract_target(event)
        if not target or not target.isdigit():
            yield event.plain_result("用法：挑衅禁言 解禁 @某人")
            return
        group_id = str(event.get_group_id() or "")
        if not group_id:
            yield event.plain_result("这个命令得在群里发。")
            return
        ok, error = await self._set_group_ban(event, group_id, target, 0)
        if not ok:
            yield event.plain_result(f"解禁失败：{error}")
            return
        yield event.plain_result(f"{target} 已解除禁言。")

    @provoke.command("记录")
    async def cmd_records(self, event: AstrMessageEvent):
        """查看最近的禁言记录"""
        await self._ensure_loaded()
        raw = self._rest(event, "记录")
        digits = re.findall(r"\d+", raw)
        limit = min(int(digits[0]) if digits else 10, 30)
        if not self._records:
            yield event.plain_result("还没有禁言记录，群友都很文明。")
            return
        group_id = str(event.get_group_id() or "")
        current = [r for r in self._records if r.get("group") == group_id]
        pool = current or self._records
        lines = [f"挑衅禁言 · 最近 {min(limit, len(pool))} 条记录"]
        for record in pool[-limit:][::-1]:
            stamp = time.strftime("%m-%d %H:%M", time.localtime(record.get("ts", 0)))
            lines.append(
                f"{stamp} {record.get('name') or record.get('user')} "
                f"被禁 {_fmt_duration(record.get('duration', 0))}｜{_shorten(record.get('reason', ''), 20)}",
            )
        yield event.plain_result("\n".join(lines))

    @provoke.command("测试")
    async def cmd_test(self, event: AstrMessageEvent):
        """测试一段文本会不会被判定为挑衅"""
        await self._ensure_loaded()
        text = self._rest(event, "测试")
        if not text:
            yield event.plain_result("用法：挑衅禁言 测试 你这个废物机器人")
            return
        hit = self._match_patterns(text)
        if hit:
            yield event.plain_result(f"会命中：{_shorten(hit, 60)}")
            return
        yield event.plain_result("没命中任何词条，这条消息是安全的。")

    @provoke.command("清空记录")
    async def cmd_clear_records(self, event: AstrMessageEvent):
        """清空禁言记录与累计次数"""
        if not self._can_manage(event):
            yield event.plain_result("这事你说了不算。")
            return
        await self._ensure_loaded()
        self._records = []
        self._offenders = {}
        await self._save("records", "offenders")
        yield event.plain_result("记录已清空。")
