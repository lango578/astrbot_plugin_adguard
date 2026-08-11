"""
astrbot_plugin_adguard —— AstrBot QQ 群广告检测插件

功能:
  1. 文字广告检测: 关键词 + 正则特征(QQ群号/微信号/手机号/网址等) 计分判定
  2. 图片广告检测: 本地 OCR(RapidOCR) 识别图片内文字 / 解码二维码 / 或使用 AI 视觉模型
  3. 视频广告检测: 抽帧后对每一帧进行 OCR / AI 识别
  4. 处置动作: 撤回消息 + 禁言 / 踢人(多级递进, 按违规次数升级)

要求:
  - AstrBot >= 4.5.7
  - 消息平台: aiocqhttp (OneBot v11 / NapCat / Lagrange)

可选依赖(缺失时自动降级, 文字广告检测仍然可用):
  - rapidocr_onnxruntime : 本地图片/视频帧 OCR
  - opencv-python-headless: 视频抽帧 + 二维码解码
"""

import asyncio
import base64
import hashlib
import json
import os
import re
import time
from collections import deque
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import At, Image, Plain, Reply, Video
from astrbot.api.star import Context, Star

# 兼容不同 AstrBot 版本的 AstrBotConfig 导入路径
try:
    from astrbot.api import AstrBotConfig
except ImportError:
    try:
        from astrbot.core.config import AstrBotConfig
    except ImportError:
        from astrbot.core.config.astrbot_config import AstrBotConfig

# ---- 可选依赖 ----
try:
    import cv2
except ImportError:  # pragma: no cover
    cv2 = None

try:
    import numpy as np
except ImportError:  # pragma: no cover
    np = None

try:
    from rapidocr_onnxruntime import RapidOCR
except ImportError:  # pragma: no cover
    RapidOCR = None

try:
    import httpx
except ImportError:  # pragma: no cover
    httpx = None


# ============================================================
# 默认关键词与特征
# ============================================================

DEFAULT_KEYWORDS = [
    "加群", "拉群", "进群", "群号", "裙号", "扫码", "扫一扫", "二维码",
    "加v", "加V", "加wx", "加微信", "微信号", "薇信", "威信号",
    "vx", "VX", "wx", "WX", "微我", "私我", "私聊我", "滴滴我", "找我",
    "看我主页", "主页有",
    "低价", "优惠", "福利", "免费", "免费领取", "领取福利", "红包群", "福利群",
    "返利", "刷单", "兼职", "日结", "代充", "代练", "代购", "代刷", "代肝",
    "代打", "陪玩", "接单", "派单", "出售", "出号", "收号", "回收",
    "担保", "信誉", "白菜价", "拼单", "外挂", "脚本", "辅助", "卡密", "激活码",
    "内部渠道", "稳赚", "回本", "理财", "投资", "股票", "荐股", "币圈",
    "USDT", "火币", "欧易", "币安", "挖矿", "一级市场", "私募", "众筹",
    "推广", "广告", "加我好友", "加我QQ", "QQ群", "拉你进群",
]

DEFAULT_STRONG_KEYWORDS = [
    "扫码进群", "扫码加群", "扫码添加", "扫码领取", "进群领取", "加群领取",
    "加群领", "群号：", "群号:", "裙号：", "QQ群号", "QQ群：", "QQ群:",
    "加v", "加V", "加VX", "加vx", "vx:", "vx：", "wx:", "wx：",
    "微信号：", "微信号:", "加我微信", "加微信领取", "免费领取",
    "领取福利", "福利群", "返利群", "刷单", "兼职日结", "日结兼职",
    "代充", "代练", "出号", "回收游戏", "外挂", "脚本", "卡密", "激活码",
    "稳赚", "回本", "内幕", "内部消息", "带你赚钱", "轻松日入", "躺赚",
]

# 内置特征正则: (正则, 权重类型, 描述)
_BUILTIN_PATTERNS = [
    (r"(?:[qQ]{2}群|群号|群号码|裙号)\s*[:：]?\s*[0-9５-９]{5,11}", "pattern", "群号+数字"),
    (
        r"(?:微信|微心|威信|vx|VX|wx|WX|V信|薇)"
        r"\s*[:：]?\s*[a-zA-Z][a-zA-Z0-9_\-]{4,19}",
        "pattern",
        "微信号",
    ),
    (r"(?:加|➕|＋)\s*[vVxX]\s*[:：]?\s*[a-zA-Z0-9_\-]{4,20}", "pattern", "加联系方式"),
    (r"手机号\s*[:：]?\s*1[3-9]\d{9}", "pattern", "手机号"),
    (r"1[3-9]\d{9}", "pattern", "手机号"),
    (r"(?:https?|ftp)://\S+|www\.\S+", "link", "网址链接"),
]

DEFAULT_AI_PROMPT = (
    "你是一个QQ群广告内容识别助手。请判断这张图片是否为广告图片。\n"
    "广告图片的特征包括：营销推广、出售/代充/刷单/兼职等信息、引导加群/加微信/加QQ等联系方式、"
    "二维码引流、低价优惠促销、外挂脚本卡密出售等内容。\n"
    "请只输出一个 JSON 对象，格式为：{\"is_ad\": true或false, \"reason\": \"简短原因\"}，不要输出其他内容。"
)

HELP_TEXT = (
    "📛 AstrBot 广告检测插件\n"
    "自动检测群内文字/图片/视频广告，支持撤回、禁言、踢人，以及二次人工审核。\n"
    "管理指令(仅管理员/群主/已配置管理可用):\n"
    "/adguard help - 本帮助\n"
    "/adguard status - 插件状态\n"
    "/adguard on / off - 开启/关闭检测\n"
    "/adguard addkw <关键词> - 添加普通关键词\n"
    "/adguard delkw <关键词> - 删除普通关键词\n"
    "/adguard kw - 查看当前关键词\n"
    "/adguard scan <文本> - 测试文本广告得分\n"
    "/adguard forgive <QQ号> - 清零某用户在本群的违规次数\n"
    "二次审核:\n"
    "/adguard audit <编号> yes|no - 确认广告/误判（学习记录）\n"
    "/adguard pending - 查看待审核列表\n"
    "/adguard forget <编号> - 移除待审核记录\n"
    "/adguard selftest - 插件自检(依赖/配置/文本检测)"
)

class AdGuardPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        # AstrBot 框架总会传入插件配置对象（AstrBotConfig），此处仅作兜底
        self.config: AstrBotConfig = (
            config if config is not None else {}
        )  # type: ignore[assignment]
        self._ocr_engine: Any = None          # RapidOCR 引擎(懒加载)
        self._ocr_lock = asyncio.Lock()  # 串行化 OCR 调用
        self._ocr_available = RapidOCR is not None
        self._cv2_available = cv2 is not None
        self._seen_ids: deque = deque(maxlen=800)    # 消息去重
        self._last_punish: dict[str, float] = {}     # 处罚冷却
        self._zhipu_fail_until: float = 0.0  # 智谱失败冷却截止时间戳

    async def terminate(self):
        """插件卸载/停用时调用。"""
        self._ocr_engine = None

    # ============================================================
    # 配置读取
    # ============================================================
    def _cfg(self, key: str, default=None):
        try:
            return self.config.get(key, default)
        except Exception:
            return default

    def _enabled(self) -> bool:
        return bool(self._cfg("enable", True))

    def _should_check_group(self, group_id: str) -> bool:
        only = [str(x) for x in (self._cfg("groups_only", []) or [])]
        if only and group_id not in only:
            return False
        whitelist = [str(x) for x in (self._cfg("groups_whitelist", []) or [])]
        return group_id not in whitelist

    def _is_user_whitelisted(self, user_id: str) -> bool:
        whitelist = [str(x) for x in (self._cfg("users_whitelist", []) or [])]
        return user_id in whitelist

    def _is_configured_admin(self, user_id: str) -> bool:
        admins = [str(x) for x in (self._cfg("admin_qq", []) or [])]
        return user_id in admins

    # ============================================================
    # 文本广告计分
    # ============================================================
    def _score_text(self, text: str) -> tuple[int, list[str]]:
        """对一段文本进行广告特征计分。返回 (总分, 命中说明列表)。"""
        if not text:
            return 0, []
        score = 0
        hits: list[str] = []
        lowered = text.lower()

        for kw in (self._cfg("keywords", DEFAULT_KEYWORDS) or []):
            kw = str(kw)
            if kw and kw.lower() in lowered:
                score += 2
                hits.append(f"关键词「{kw}」")

        for kw in (self._cfg("strong_keywords", DEFAULT_STRONG_KEYWORDS) or []):
            kw = str(kw)
            if kw and kw.lower() in lowered:
                score += 4
                hits.append(f"高危词「{kw}」")

        pattern_w = int(self._cfg("pattern_weight", 3) or 3)
        link_w = int(self._cfg("link_weight", 2) or 2)
        for pat, kind, label in _BUILTIN_PATTERNS:
            weight = pattern_w if kind == "pattern" else link_w
            try:
                if re.search(pat, text):
                    score += weight
                    hits.append(f"特征({label})")
            except re.error:
                continue

        for pat in (self._cfg("regex_patterns", []) or []):
            pat = str(pat)
            if not pat:
                continue
            try:
                re.compile(pat)
            except re.error:
                logger.warning(f"adguard: 非法正则已忽略: {pat}")
                continue
            if re.search(pat, text):
                score += pattern_w
                hits.append(f"自定义特征「{pat}」")

        return score, hits

    # ============================================================
    # 消息链媒体收集
    # ============================================================
    def _collect_media(
        self, event: AstrMessageEvent
    ) -> tuple[list[Image], list[Video]]:
        images: list[Image] = []
        videos: list[Video] = []
        for comp in event.get_messages():
            if isinstance(comp, Image):
                images.append(comp)
            elif isinstance(comp, Video):
                videos.append(comp)
            elif isinstance(comp, Reply):
                # 引用消息中的图片/视频也纳入检查
                for sub in (getattr(comp, "chain", None) or []):
                    if isinstance(sub, Image):
                        images.append(sub)
                    elif isinstance(sub, Video):
                        videos.append(sub)
        return images, videos

    # ============================================================
    # 媒体数据获取
    # ============================================================
    async def _image_to_bytes(self, img: Image) -> bytes:
        try:
            path = await img.convert_to_file_path()
            if path and os.path.exists(path):
                with open(path, "rb") as f:
                    return f.read()
        except Exception:
            pass
        try:
            b64 = await img.convert_to_base64()
            if b64:
                return base64.b64decode(b64)
        except Exception as e:
            logger.warning(f"adguard: 获取图片数据失败: {e}")
        return b""


    # ============================================================
    # OCR / 二维码 / AI 视觉
    # ============================================================
    def _decode_qr(self, img_bytes: bytes) -> str:
        """尝试解码图片中的二维码，返回解码出的文本。"""
        if cv2 is None or np is None:
            return ""
        try:
            arr = np.frombuffer(img_bytes, np.uint8)
            img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            if img is None:
                return ""
            detector = cv2.QRCodeDetector()
            data, _, _ = detector.detectAndDecode(img)
            return data or ""
        except Exception as e:
            logger.debug(f"adguard: 二维码解码失败: {e}")
            return ""

    def _ocr_bytes(self, img_bytes: bytes) -> str:
        """对图片字节做 OCR + 二维码解码，返回识别出的文本。"""
        texts: list[str] = []
        if RapidOCR is not None:
            if self._ocr_engine is None:
                try:
                    self._ocr_engine = RapidOCR()
                except Exception as e:
                    logger.warning(f"adguard: 初始化 OCR 引擎失败: {e}")
                    self._ocr_engine = False
            if self._ocr_engine:
                try:
                    result, _ = self._ocr_engine(img_bytes)
                    if result:
                        for line in result:
                            if len(line) > 1 and line[1]:
                                texts.append(str(line[1]))
                except Exception as e:
                    logger.warning(f"adguard: OCR 识别失败: {e}")
        qr_text = self._decode_qr(img_bytes)
        if qr_text:
            texts.append(f"[二维码]{qr_text}")
        return "\n".join(texts)

    async def _ocr_media(self, data: bytes) -> str:
        async with self._ocr_lock:
            return await asyncio.to_thread(self._ocr_bytes, data)

    def _parse_ai_answer(self, text: str) -> tuple[str, str]:
        """解析 AI 视觉模型的 JSON 返回。返回 (判定, 原因)。

        verdict: "ad" 判定为广告 / "ok" 判定非广告 / "unknown" 无法判定。
        """
        if not text:
            return "unknown", "AI无返回"
        m = re.search(r"\{[^{}]*\"is_ad\"[^{}]*\}", text, re.S)
        if m:
            try:
                data = json.loads(m.group())
                is_ad = bool(data.get("is_ad"))
                reason = str(data.get("reason", ""))
                return ("ad" if is_ad else "ok"), reason
            except Exception:
                pass
        t = text.lower()
        if "是广告" in t or "属于广告" in t or '"is_ad":true' in t:
            return "ad", text[:100]
        if "不是广告" in t or "非广告" in t or '"is_ad":false' in t:
            return "ok", ""
        return "unknown", "AI无法判定"

    # ============================================================
    # 智谱 GLM-4V-Flash 视觉检测
    # ============================================================
    def _zhipu_api_key(self) -> str:
        """返回智谱 API Key。

        支持两种填写方式：
        1) 分别在 zhipu_api_key_id / zhipu_api_key_secret 填两部分；
        2) 直接把完整 Key（含点号）填在 zhipu_api_key_id。
        """
        key_id = str(self._cfg("zhipu_api_key_id", "") or "").strip()
        secret = str(self._cfg("zhipu_api_key_secret", "") or "").strip()
        if not key_id:
            return ""
        if "." in key_id and not secret:
            return key_id
        return f"{key_id}.{secret}"

    def _zhipu_configured(self) -> bool:
        return bool(self._zhipu_api_key())

    def _zhipu_ready(self) -> bool:
        """智谱是否可用：已配置 key 且不在失败冷却期。"""
        return self._zhipu_configured() and time.time() >= self._zhipu_fail_until

    async def _zhipu_ai_check(
        self, img_bytes: bytes, kind: str
    ) -> tuple[str, str]:
        """调用智谱 GLM-4V-Flash 判断图片是否为广告。返回 (verdict, reason)。"""
        if httpx is None:
            logger.warning("adguard: 未安装 httpx，无法调用智谱 AI")
            return "unknown", "缺少httpx依赖"
        api_key = self._zhipu_api_key()
        if not api_key:
            return "unknown", "未配置智谱APIKey"
        model = str(self._cfg("zhipu_model", "glm-4v-flash") or "glm-4v-flash")
        base_url = str(
            self._cfg(
                "zhipu_base_url",
                "https://open.bigmodel.cn/api/paas/v4/chat/completions",
            )
            or "https://open.bigmodel.cn/api/paas/v4/chat/completions"
        )
        prompt = str(self._cfg("ai_prompt", DEFAULT_AI_PROMPT))
        b64 = base64.b64encode(img_bytes).decode()
        payload = {
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{b64}"
                            },
                        },
                    ],
                }
            ],
        }
        headers = {"Authorization": f"Bearer {api_key}"}
        timeout = httpx.Timeout(60)
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(base_url, json=payload, headers=headers)
                resp.raise_for_status()
                data = resp.json()
            choices = data.get("choices") or []
            if not choices:
                logger.warning(f"adguard: 智谱AI无返回({kind})，响应: {str(data)[:200]}")
                return "unknown", "智谱AI无返回"
            content = choices[0].get("message", {}).get("content", "") or ""
            verdict, reason = self._parse_ai_answer(str(content).strip())
            logger.info(
                f"adguard: 智谱AI判定({kind}) -> {verdict}"
                f"{' - ' + reason if reason else ''}"
            )
            return verdict, reason
        except httpx.HTTPStatusError as e:
            detail = ""
            try:
                err = e.response.json()
                detail = str(err.get("error") or err.get("message") or err)
            except Exception:
                pass
            logger.warning(
                f"adguard: 智谱AI请求失败({kind}) HTTP {e.response.status_code}: {detail}"
            )
            self._zhipu_fail_until = time.time() + 300  # 冷却 5 分钟
            return "unknown", "智谱AI请求失败"
        except Exception as e:
            logger.warning(f"adguard: 智谱AI识别失败({kind}): {e}")
            self._zhipu_fail_until = time.time() + 300  # 冷却 5 分钟
            return "unknown", "智谱AI识别出错"


    async def _ai_check(
        self, event: AstrMessageEvent, img_bytes: bytes, kind: str
    ) -> tuple[str, str]:
        """判断图片是否为广告。优先使用智谱 GLM-4V-Flash（若已配置 API Key），
        智谱失败（余额不足/网络等）时回退到 AstrBot 配置的 LLM。返回 (verdict, reason)。"""
        # 优先智谱 GLM-4V-Flash（已配置且不在失败冷却期）
        if self._zhipu_ready():
            logger.debug(f"adguard: 使用智谱 GLM-4V-Flash 检测{kind}")
            verdict, reason = await self._zhipu_ai_check(img_bytes, kind)
            if verdict != "unknown":
                return verdict, reason
            # 智谱失败（余额不足/限流等）→ 回退 AstrBot LLM
            logger.warning(f"adguard: 智谱检测失败({reason})，回退 AstrBot LLM")
        else:
            logger.debug(f"adguard: 未配置智谱或处于失败冷却期，回退 AstrBot LLM 检测{kind}")
        try:
            provider_id = str(self._cfg("ai_provider_id", "") or "")
            if not provider_id:
                try:
                    provider_id = await self.context.get_current_chat_provider_id(
                        umo=event.unified_msg_origin
                    )
                except Exception as e:
                    logger.debug(f"adguard: 获取当前会话模型失败: {e}")
                    provider_id = ""
            if not provider_id:
                # 当前会话模型不可用（如配置了不存在的 ID）→ 尝试任意可用提供商
                provider_id = await self._find_any_provider()
            if not provider_id:
                logger.warning("adguard: 未找到可用的 AI 模型提供商，跳过 AI 检测")
                return "unknown", "无可用AI提供商"
            b64 = base64.b64encode(img_bytes).decode()
            prompt = str(self._cfg("ai_prompt", DEFAULT_AI_PROMPT))
            resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
                image_urls=[f"base64://{b64}"],
            )
            answer = (getattr(resp, "completion_text", "") or "").strip()
            return self._parse_ai_answer(answer)
        except Exception as e:
            logger.warning(f"adguard: AI 识别失败({kind}): {e}")
            return "unknown", f"AI识别出错"

    async def _find_any_provider(self) -> str:
        """当前会话模型 ID 无效时，尝试返回任意一个已加载的提供商 ID。"""
        try:
            pm = self.context.provider_manager
            for inst in pm.get_insts():
                pid = getattr(inst, "id", "") or ""
                if pid:
                    logger.debug(f"adguard: 使用可用提供商兜底: {pid}")
                    return str(pid)
        except Exception as e:
            logger.debug(f"adguard: 查找可用提供商失败: {e}")
        return ""

    async def _check_media_bytes(
        self, event: AstrMessageEvent, data: bytes, mode: str, kind: str
    ) -> tuple[int, list[str]]:
        """对一段图片/视频帧数据进行广告检测。返回 (分数, 命中说明)。

        mode:
          - "ai":   仅 AI 视觉（智谱 GLM-4V-Flash 优先，回退 AstrBot LLM）
          - "ocr":  仅本地 OCR
          - "auto": OCR 优先，未命中再由 AI（智谱）兜底
        """
        if not data:
            return 0, []
        mode = str(mode)

        # ---------- auto 模式：OCR 优先，AI 兜底 ----------
        if mode == "auto":
            if self._ocr_available:
                ocr_text = await self._ocr_media(data)
                if ocr_text:
                    score, hits = self._score_text(ocr_text)
                    if hits:
                        return score, [
                            f"{kind}识别到广告文字: " + "、".join(hits[:5])
                        ]
            # OCR 未命中 → AI（智谱优先）兜底
            return await self._ai_check_media(event, data, kind)

        # ---------- 纯 AI 模式 ----------
        if mode == "ai":
            return await self._ai_check_media(event, data, kind)

        # ---------- 纯 OCR 模式 ----------
        if mode == "ocr":
            if not self._ocr_available:
                return 0, []
            ocr_text = await self._ocr_media(data)
            if not ocr_text:
                return 0, []
            score, hits = self._score_text(ocr_text)
            if not hits:
                return 0, []
            return score, [f"{kind}识别到广告文字: " + "、".join(hits[:5])]
        return 0, []

    async def _ai_check_media(
        self, event: AstrMessageEvent, data: bytes, kind: str
    ) -> tuple[int, list[str]]:
        """调用 AI 视觉（智谱 GLM-4V-Flash 优先）检查媒体。返回 (分数, 命中说明)。"""
        verdict, reason = await self._ai_check(event, data, kind)
        if verdict == "ad":
            return 999, [
                f"{kind}AI识别为广告: {reason}"
                if reason
                else f"{kind}AI识别为广告"
            ]
        if verdict == "unknown":
            # AI 不可用（未配置智谱且无 AstrBot 提供商 / 缺 httpx）→ 跳过
            if reason in ("无可用AI提供商", "缺少httpx依赖"):
                return 0, []
            # AI 无法判定 → 标记待人工审核，由审核分支接管
            return 0, [f"{kind}AI无法判定，待人工审核"]
        # verdict == "ok"：AI 判定非广告
        return 0, []

    # ============================================================
    # 视频抽帧
    # ============================================================
    def _extract_video_frames(
        self, video_path: str, max_frames: int, interval_sec: float
    ) -> list[bytes]:
        """使用 OpenCV 从视频中抽取若干帧，返回 JPEG 字节列表。"""
        if cv2 is None:
            return []
        try:
            cap = cv2.VideoCapture(video_path)
            if not cap.isOpened():
                cap.release()
                return []
            fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
            if fps <= 0:
                fps = 30.0
            total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
            step = max(1, int(fps * max(interval_sec, 0.1)))
            frames: list[bytes] = []
            target_indices = set(range(0, total, step)) if total > 0 else None
            idx = 0
            while len(frames) < max_frames:
                ret, frame = cap.read()
                if not ret:
                    break
                if target_indices is None or idx in target_indices:
                    ok, buf = cv2.imencode(
                        ".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 85]
                    )
                    if ok:
                        frames.append(buf.tobytes())
                idx += 1
            cap.release()
            return frames
        except Exception as e:
            logger.warning(f"adguard: 视频抽帧失败: {e}")
            return []

    async def _check_image(
        self, event: AstrMessageEvent, img: Image, mode: str
    ) -> tuple[int, list[str]]:
        data = await self._image_to_bytes(img)
        return await self._check_media_bytes(event, data, mode, "图片")

    async def _check_video(
        self, event: AstrMessageEvent, video: Video, mode: str
    ) -> tuple[int, list[str]]:
        path = ""
        try:
            path = await video.convert_to_file_path()
        except Exception as e:
            logger.debug(f"adguard: convert_to_file_path 失败: {e}")
        if not path or not os.path.exists(path):
            path = await self._resolve_video_path(event, video)
        if not path:
            logger.warning("adguard: 获取视频文件失败，跳过视频检测")
            return 0, []
        max_frames = int(self._cfg("video_max_frames", 3) or 3)
        interval = float(self._cfg("video_frame_interval_sec", 5.0) or 5.0)
        frames = await asyncio.to_thread(
            self._extract_video_frames, path, max_frames, interval
        )
        if not frames:
            return 0, []
        total_score = 0
        hits: list[str] = []
        for i, frame in enumerate(frames[:max_frames]):
            score, fhits = await self._check_media_bytes(
                event, frame, mode, f"视频第{i + 1}帧"
            )
            total_score += score
            hits.extend(fhits)
            if score >= 900:  # AI 命中，无需继续抽帧检测
                break
        return total_score, hits

    async def _resolve_video_path(
        self, event: AstrMessageEvent, video: Video
    ) -> str:
        """视频组件解析失败时的兜底：尝试 URL/path 字段，或通过协议端 get_file 获取。"""
        # 1) url / path / file 为 http(s) 或本地存在的路径
        for cand in (
            getattr(video, "url", "") or "",
            getattr(video, "path", "") or "",
            getattr(video, "file", "") or "",
        ):
            if cand.startswith(("http://", "https://")):
                return cand
            try:
                if os.path.exists(cand):
                    return cand
            except OSError:
                pass
        # 2) 通过协议端 get_file API 获取真实路径
        bot = getattr(event, "bot", None)
        file_val = getattr(video, "file", "") or getattr(video, "url", "")
        if bot and file_val:
            try:
                info = await bot.call_action("get_file", file_id=file_val)
                fpath = str(info.get("file") or "")
                if fpath.startswith(("http://", "https://")):
                    return fpath
                if fpath and os.path.exists(fpath):
                    return fpath
                if fpath:
                    logger.debug(f"adguard: get_file 返回路径不存在: {fpath}")
            except Exception as e:
                logger.debug(f"adguard: get_file 获取视频失败: {e}")
        return ""


    # ============================================================
    # 处罚逻辑
    # ============================================================
    async def _is_group_admin(self, bot, group_id: str, user_id: str) -> bool:
        """通过 OneBot API 判断用户是否为群主/管理员。"""
        try:
            info = await bot.call_action(
                "get_group_member_info", group_id=int(group_id), user_id=int(user_id)
            )
            return info.get("role") in ("owner", "admin")
        except Exception:
            return False

    async def _send(self, event: AstrMessageEvent, text: str) -> None:
        try:
            await event.send(MessageChain([Plain(text)]))
        except Exception as e:
            logger.warning(f"adguard: 发送通知失败: {e}")

    async def _punish(self, event: AstrMessageEvent, reasons: list[str]) -> None:
        """检测命中后的处置：撤回 + 禁言/踢人（多级递进）。"""
        bot = getattr(event, "bot", None)
        group_id = event.get_group_id()
        sender_id = event.get_sender_id()
        nickname = event.get_sender_name() or sender_id
        message_id = getattr(event.message_obj, "message_id", "")
        if not bot or not group_id or not sender_id:
            return
        await self._apply_punishment(
            bot, group_id, sender_id, nickname, message_id, reasons, event
        )

    async def _apply_punishment(
        self,
        bot,
        group_id: str,
        user_id: str,
        nickname: str,
        message_id,
        reasons: list[str],
        event: AstrMessageEvent | None = None,
        stop: bool = True,
    ) -> None:
        """具体执行处罚（撤回/禁言/踢人）。检测命中与管理员审核确认共用。"""
        sender_id = user_id
        if not bot or not group_id or not sender_id:
            return

        # 安全保护：不处置群主/管理员
        if self._cfg("skip_admins", True):
            try:
                if await self._is_group_admin(bot, group_id, sender_id):
                    logger.info(f"adguard: 跳过处置群管理员 {sender_id}")
                    return
            except Exception:
                pass

        # 处罚冷却，防止同一用户刷屏重复处罚
        now = time.time()
        dedup_sec = float(self._cfg("dedup_seconds", 20) or 0)
        dedup_key = f"{group_id}:{sender_id}"
        if dedup_sec > 0 and now - self._last_punish.get(dedup_key, 0) < dedup_sec:
            return
        self._last_punish[dedup_key] = now

        # 读取并累加违规次数（超过 forgive_hours 自动清零）
        kv_key = f"offense:{group_id}:{sender_id}"
        count, last_ts = 0, 0.0
        data = await self.get_kv_data(kv_key, None)
        if isinstance(data, dict):
            try:
                count = int(data.get("count", 0) or 0)
                last_ts = float(data.get("ts", 0) or 0)
            except Exception:
                pass
        forgive = int(self._cfg("forgive_hours", 24) or 0) * 3600
        if forgive > 0 and now - last_ts > forgive:
            count = 0
        count += 1
        await self.put_kv_data(kv_key, {"count": count, "ts": now})

        action = str(self._cfg("action", "escalate"))
        kick_after = int(self._cfg("kick_after", 3) or 3)
        durations = [
            int(x)
            for x in (self._cfg("mute_durations", [300, 1800]) or [300, 1800])
        ]
        if not durations:
            durations = [300]

        result_msgs: list[str] = []

        # 1) 撤回消息
        if self._cfg("recall_message", True) and message_id:
            try:
                await bot.call_action("delete_msg", message_id=message_id)
                result_msgs.append("已撤回消息")
            except Exception as e:
                logger.warning(f"adguard: 撤回消息失败: {e}")

        # 2) 禁言 / 踢人
        should_kick = action == "kick" or (action == "escalate" and count >= kick_after)
        if should_kick:
            try:
                await bot.call_action(
                    "set_group_kick", group_id=int(group_id), user_id=int(sender_id)
                )
                result_msgs.append("已移出群聊")
            except Exception as e:
                logger.warning(f"adguard: 移出群聊失败: {e}")
                result_msgs.append("移出群聊失败(可能无权限)")
        elif action in ("mute", "escalate"):
            duration = durations[min(count - 1, len(durations) - 1)]
            duration = max(1, min(int(duration), 2592000))
            try:
                await bot.call_action(
                    "set_group_ban",
                    group_id=int(group_id),
                    user_id=int(sender_id),
                    duration=duration,
                )
                if duration % 60 == 0:
                    result_msgs.append(f"已禁言 {duration // 60} 分钟")
                else:
                    result_msgs.append(f"已禁言 {duration} 秒")
            except Exception as e:
                logger.warning(f"adguard: 禁言失败: {e}")
                result_msgs.append("禁言失败(可能无权限)")

        # 3) 群内通知
        if self._cfg("notify_group", True) and event is not None:
            parts = [f"🚫 检测到广告内容，已处理：{nickname}（第 {count} 次）"]
            for r in reasons[:6]:
                parts.append(f"· {r}")
            parts.append("；".join(result_msgs) if result_msgs else "已处理")
            await self._send(event, "\n".join(parts))

        # 4) 终止事件传播，阻止机器人继续回复该广告消息
        if stop and self._cfg("stop_event", True) and event is not None:
            event.stop_event()

    # ============================================================
    # 二次审核与学习
    # ============================================================
    def _audit_enabled(self) -> bool:
        return bool(self._cfg("audit_enable", True))

    def _text_fingerprint(self, text: str) -> str:
        """规范化文本并生成指纹（用于学习记录与去重）。"""
        norm = re.sub(r"\s+", "", text or "").lower()
        if not norm:
            return ""
        return hashlib.md5(norm.encode("utf-8")).hexdigest()

    async def _get_learned(self, group_id: str, fp: str):
        """查询内容指纹的学习结果。返回 "ad"/"ok"/None。"""
        if not fp:
            return None
        data = await self.get_kv_data(f"learned_text:{group_id}", {}) or {}
        item = data.get(fp)
        return item.get("verdict") if isinstance(item, dict) else None

    async def _learn_text(self, group_id: str, text: str, verdict: str) -> None:
        """记录内容指纹学习结果（verdict: ad=确认广告 / ok=确认误判）。"""
        fp = self._text_fingerprint(text)
        if not fp:
            return
        key = f"learned_text:{group_id}"
        data = await self.get_kv_data(key, {}) or {}
        if not isinstance(data, dict):
            data = {}
        limit = int(self._cfg("learn_max_entries", 500) or 500)
        if len(data) >= limit:
            # 淘汰最旧的记录
            for old in sorted(
                data, key=lambda k: data[k].get("ts", 0)
            )[: max(0, len(data) - limit + 1)]:
                data.pop(old, None)
        data[fp] = {"verdict": verdict, "ts": time.time()}
        await self.put_kv_data(key, data)

    async def _record_user_ad(self, group_id: str, user_id: str) -> None:
        """记录某用户被管理员确认发过广告。"""
        key = f"user_ad:{group_id}"
        data = await self.get_kv_data(key, {}) or {}
        if not isinstance(data, dict):
            data = {}
        old_item = data.get(str(user_id))
        item = dict(old_item) if isinstance(old_item, dict) else {}
        item["count"] = int(item.get("count", 0) or 0) + 1
        item["ts"] = time.time()
        data[str(user_id)] = item
        await self.put_kv_data(key, data)

    async def _has_user_ad_record(self, group_id: str, user_id: str) -> bool:
        """判断某用户是否曾被确认发过广告。"""
        data = await self.get_kv_data(f"user_ad:{group_id}", {}) or {}
        item = data.get(str(user_id))
        return bool(isinstance(item, dict) and int(item.get("count", 0) or 0) > 0)

    async def _get_group_admins(self, event: AstrMessageEvent) -> list[str]:
        """获取群管理员（群主+管理+配置的 admin_qq）。"""
        bot = getattr(event, "bot", None)
        group_id = event.get_group_id()
        admins: list[str] = []
        if bot and group_id:
            try:
                members = await bot.call_action(
                    "get_group_member_list", group_id=int(group_id)
                )
                for m in members:
                    if m.get("role") in ("owner", "admin"):
                        admins.append(str(m.get("user_id")))
            except Exception as e:
                logger.warning(f"adguard: 获取群成员列表失败: {e}")
        for a in (self._cfg("admin_qq", []) or []):
            a = str(a)
            if a and a not in admins:
                admins.append(a)
        return admins


    # ============================================================
    # 主监听器：群消息广告检测
    # ============================================================
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    async def on_group_message(self, event: AstrMessageEvent):
        try:
            await self._handle_group_message(event)
        except Exception as e:
            logger.warning(f"adguard: 处理群消息异常: {e}")

    async def _handle_group_message(self, event: AstrMessageEvent) -> None:
        group_id = event.get_group_id()
        sender_id = event.get_sender_id()
        logger.debug(
            f"adguard: 收到群消息 group={group_id} sender={sender_id} "
            f"text={(event.message_str or '')[:30]!r}"
        )
        if not self._enabled():
            logger.debug("adguard: 插件未启用，跳过")
            return
        if not group_id or not self._should_check_group(group_id):
            logger.debug(f"adguard: 群 {group_id} 不在检测范围，跳过")
            return
        if not sender_id:
            return
        if sender_id == event.get_self_id():
            return
        if self._cfg("skip_admins", True) and (
            event.is_admin() or self._is_configured_admin(sender_id)
        ):
            logger.debug(f"adguard: 跳过管理员/管理 {sender_id}")
            return
        if self._is_user_whitelisted(sender_id):
            logger.debug(f"adguard: 白名单用户 {sender_id}，跳过")
            return
        # 不检测本插件自身的管理指令，避免误伤管理员
        if re.match(r"^[/!．]?\s*adguard\b", (event.message_str or "").strip(), re.I):
            logger.debug("adguard: 本插件指令，跳过检测")
            return

        # 同一消息 id 去重
        msg_id = str(getattr(event.message_obj, "message_id", "") or "")
        if msg_id:
            if msg_id in self._seen_ids:
                return
            self._seen_ids.append(msg_id)

        # 1) 收集媒体 + 文本计分
        text = event.message_str or ""
        images, videos = self._collect_media(event)
        total_score, reasons = self._score_text(text)

        # 2) 学习库快速判定：相同内容之前已被管理员确认
        fp = self._text_fingerprint(text)
        if fp:
            learned = await self._get_learned(group_id, fp)
            if learned == "ad":
                logger.info(
                    f"adguard: 群 {group_id} 命中已确认广告记录，直接处罚 {sender_id}"
                )
                await self._punish(event, ["命中已确认广告记录（二次审核学习）"])
                return
            if learned == "ok" and not images and not videos:
                # 已确认误判的纯文本内容直接放行
                return

        if not images and not videos and total_score == 0:
            logger.debug(f"adguard: 消息无媒体且文本无命中，放行")
            return

        # 3) 图片检测
        image_mode = str(self._cfg("image_check", "auto"))
        if images and image_mode != "off":
            limit = int(self._cfg("max_media_per_message", 3) or 3)
            for img in images[:limit]:
                score, hits = await self._check_image(event, img, image_mode)
                total_score += score
                reasons.extend(hits)
                if score >= 900:  # AI 已判定命中
                    break

        # 4) 视频检测
        video_mode = str(self._cfg("video_check", "auto"))
        if videos and video_mode != "off":
            limit = int(self._cfg("max_media_per_message", 3) or 3)
            for vid in videos[:limit]:
                score, hits = await self._check_video(event, vid, video_mode)
                total_score += score
                reasons.extend(hits)
                if score >= 900:
                    break

        # 4.5) 检测结果日志
        logger.debug(
            f"adguard: 检测完成 group={group_id} sender={sender_id} "
            f"score={total_score} reasons={reasons[:5]}"
        )

        # 5) 判定：直接处罚 / 二次审核 / 放行
        threshold = (
            int(self._cfg("media_score_threshold", 3) or 3)
            if (images or videos)
            else int(self._cfg("score_threshold", 4) or 4)
        )
        if total_score >= threshold:
            logger.info(
                f"adguard: 群 {group_id} 用户 {sender_id} 广告命中，得分 {total_score}，"
                f"原因: {'; '.join(reasons[:6])}"
            )
            await self._punish(event, reasons)
            return

        # 6) 二次审核：疑似内容（未达阈值但命中特征 / AI 无法判定）交给群管理员审核
        need_audit = any(("无法判定" in r or "待人工审核" in r) for r in reasons)
        audit_thr = int(self._cfg("audit_threshold", 2) or 0)
        if self._audit_enabled() and (
            need_audit or (audit_thr > 0 and total_score >= audit_thr)
        ):
            if await self._has_user_ad_record(group_id, sender_id):
                logger.info(
                    f"adguard: 群 {group_id} 用户 {sender_id} 有广告前科，疑似内容直接处罚"
                )
                await self._punish(event, reasons)
                return
            logger.info(
                f"adguard: 群 {group_id} 用户 {sender_id} 疑似广告，进入人工审核，"
                f"得分 {total_score}"
            )
            await self._create_audit(event, total_score, reasons, images, videos)

    # ============================================================
    # 管理指令
    # ============================================================
    @filter.command_group("adguard")
    def adguard(self):
        """广告检测插件指令组"""

    def _strip_cmd(self, message_str: str, sub: str) -> str:
        """从消息文本中剥离指令前缀，返回剩余内容。"""
        s = message_str.strip()
        m = re.match(r"^[/!．]?\s*adguard\s+" + sub + r"[\s　]*(.*)$", s, re.S)
        if m:
            return (m.group(1) or "").strip()
        m2 = re.match(r"^[/!．]?\s*adguard\s+" + sub + r"$", s)
        if m2:
            return ""
        return ""

    async def _is_operator(self, event: AstrMessageEvent) -> bool:
        """判断用户是否拥有插件管理权限（群主/管理员/配置的管理员/机器人管理员）。"""
        if event.is_admin():
            return True
        if self._is_configured_admin(event.get_sender_id()):
            return True
        bot = getattr(event, "bot", None)
        group_id = event.get_group_id()
        if bot and group_id:
            try:
                info = await bot.call_action(
                    "get_group_member_info",
                    group_id=int(group_id),
                    user_id=int(event.get_sender_id()),
                )
                return info.get("role") in ("owner", "admin")
            except Exception:
                return False
        return False


    @adguard.command("help")
    async def adguard_help(self, event: AstrMessageEvent):
        yield event.plain_result(HELP_TEXT)

    @adguard.command("status")
    async def adguard_status(self, event: AstrMessageEvent):
        ocr_state = (
            "✅ 可用" if self._ocr_available
            else "❌ 不可用(未安装 rapidocr_onnxruntime)"
        )
        cv_state = (
            "✅ 可用" if self._cv2_available
            else "❌ 不可用(未安装 opencv-python-headless)"
        )
        kw_count = len(self._cfg("keywords", DEFAULT_KEYWORDS) or [])
        strong_count = len(self._cfg("strong_keywords", DEFAULT_STRONG_KEYWORDS) or [])
        audit_state = "✅ 开启" if self._audit_enabled() else "❌ 关闭"
        pending_count = 0
        group_id = event.get_group_id()
        if group_id:
            try:
                pending_count = len(await self._get_pending(group_id))
            except Exception:
                pass
        lines = [
            f"📊 广告检测状态：{'🟢 已开启' if self._enabled() else '🔴 已关闭'}",
            f"文本阈值: {self._cfg('score_threshold', 4)}",
            f"媒体阈值: {self._cfg('media_score_threshold', 3)}",
            f"图片检测: {self._cfg('image_check', 'auto')}",
            f"视频检测: {self._cfg('video_check', 'auto')}",
            f"OCR: {ocr_state}",
            f"OpenCV: {cv_state}",
            f"处置方式: {self._cfg('action', 'escalate')}",
            f"二次审核: {audit_state}（待审核 {pending_count} 条）",
            f"普通关键词: {kw_count} 个",
            f"高危关键词: {strong_count} 个",
        ]
        yield event.plain_result("\n".join(lines))

    @adguard.command("on")
    async def adguard_on(self, event: AstrMessageEvent):
        if not await self._is_operator(event):
            yield event.plain_result("❌ 仅管理员/群主可执行此操作")
            return
        self.config["enable"] = True
        try:
            self.config.save_config()
            yield event.plain_result("✅ 广告检测已开启")
        except Exception as e:
            logger.warning(f"adguard: 保存配置失败: {e}")
            yield event.plain_result("✅ 广告检测已开启（配置保存失败，请到 WebUI 确认）")

    @adguard.command("off")
    async def adguard_off(self, event: AstrMessageEvent):
        if not await self._is_operator(event):
            yield event.plain_result("❌ 仅管理员/群主可执行此操作")
            return
        self.config["enable"] = False
        try:
            self.config.save_config()
            yield event.plain_result("✅ 广告检测已关闭")
        except Exception as e:
            logger.warning(f"adguard: 保存配置失败: {e}")
            yield event.plain_result("✅ 广告检测已关闭（配置保存失败，请到 WebUI 确认）")


    @adguard.command("addkw")
    async def adguard_addkw(self, event: AstrMessageEvent):
        if not await self._is_operator(event):
            yield event.plain_result("❌ 仅管理员/群主可执行此操作")
            return
        kw = self._strip_cmd(event.message_str, "addkw")
        if not kw:
            yield event.plain_result("用法：/adguard addkw <关键词>")
            return
        kws = list(self._cfg("keywords", DEFAULT_KEYWORDS) or [])
        if kw in kws:
            yield event.plain_result(f"⚠️ 关键词已存在：{kw}")
            return
        kws.append(kw)
        self.config["keywords"] = kws
        try:
            self.config.save_config()
            yield event.plain_result(f"✅ 已添加普通关键词：{kw}")
        except Exception as e:
            logger.warning(f"adguard: 保存配置失败: {e}")
            yield event.plain_result(f"⚠️ 已添加(未持久化)：{kw}")

    @adguard.command("delkw")
    async def adguard_delkw(self, event: AstrMessageEvent):
        if not await self._is_operator(event):
            yield event.plain_result("❌ 仅管理员/群主可执行此操作")
            return
        kw = self._strip_cmd(event.message_str, "delkw")
        if not kw:
            yield event.plain_result("用法：/adguard delkw <关键词>")
            return
        kws = list(self._cfg("keywords", DEFAULT_KEYWORDS) or [])
        if kw not in kws:
            yield event.plain_result(f"⚠️ 关键词不存在：{kw}")
            return
        kws.remove(kw)
        self.config["keywords"] = kws
        try:
            self.config.save_config()
            yield event.plain_result(f"✅ 已删除关键词：{kw}")
        except Exception as e:
            logger.warning(f"adguard: 保存配置失败: {e}")
            yield event.plain_result(f"⚠️ 已删除(未持久化)：{kw}")

    @adguard.command("kw")
    async def adguard_kw(self, event: AstrMessageEvent):
        kws = self._cfg("keywords", DEFAULT_KEYWORDS) or []
        strong = self._cfg("strong_keywords", DEFAULT_STRONG_KEYWORDS) or []
        lines = [f"普通关键词({len(kws)}个)："]
        lines.append("、".join(str(x) for x in kws) if kws else "(无)")
        lines.append(f"高危关键词({len(strong)}个)：")
        lines.append("、".join(str(x) for x in strong) if strong else "(无)")
        yield event.plain_result("\n".join(lines))

    @adguard.command("scan")
    async def adguard_scan(self, event: AstrMessageEvent):
        text = self._strip_cmd(event.message_str, "scan")
        if not text:
            yield event.plain_result("用法：/adguard scan <文本>")
            return
        score, hits = self._score_text(text)
        threshold = int(self._cfg("score_threshold", 4) or 4)
        verdict = "⚠️ 判定为广告" if score >= threshold else "✅ 未达阈值"
        lines = [f"检测文本：{text[:80]}", f"得分：{score} / 阈值：{threshold} → {verdict}"]
        if hits:
            lines.append("命中：" + "、".join(hits[:10]))
        yield event.plain_result("\n".join(lines))

    async def _get_pending(self, group_id: str) -> dict:
        """读取待审核队列并清理过期记录。"""
        key = f"audit_pending:{group_id}"
        data = await self.get_kv_data(key, {}) or {}
        if not isinstance(data, dict):
            data = {}
        ttl = float(self._cfg("audit_ttl_minutes", 120) or 120) * 60
        now = time.time()
        changed = False
        for aid in list(data.keys()):
            item = data[aid]
            if not isinstance(item, dict) or now - item.get("ts", 0) > ttl:
                data.pop(aid, None)
                changed = True
        if changed:
            await self.put_kv_data(key, data)
        return data

    async def _add_pending(self, entry: dict) -> str:
        """加入待审核队列，返回审核编号；重复（同人同内容 60s 内）返回空串。"""
        group_id = entry.get("group_id", "")
        key = f"audit_pending:{group_id}"
        data = await self.get_kv_data(key, {}) or {}
        if not isinstance(data, dict):
            data = {}
        now = time.time()
        fp = self._text_fingerprint(entry.get("text", ""))
        for item in data.values():
            if (
                isinstance(item, dict)
                and str(item.get("sender_id", "")) == str(entry.get("sender_id", ""))
                and self._text_fingerprint(item.get("text", "")) == fp
                and now - item.get("ts", 0) < 60
            ):
                return ""
        maxp = int(self._cfg("audit_max_pending", 20) or 20)
        if len(data) >= maxp:
            oldest = min(data, key=lambda k: data[k].get("ts", 0))
            data.pop(oldest, None)
        aid = str(int(now * 1000))[-8:]
        entry["id"] = aid
        data[aid] = entry
        await self.put_kv_data(key, data)
        return aid

    async def _drop_pending(self, group_id: str, aid: str) -> None:
        key = f"audit_pending:{group_id}"
        data = await self.get_kv_data(key, {}) or {}
        if isinstance(data, dict):
            data.pop(aid, None)
            await self.put_kv_data(key, data)

    async def _create_audit(
        self,
        event: AstrMessageEvent,
        score: int,
        reasons: list[str],
        images: list[Image],
        videos: list[Video],
    ) -> None:
        """创建二次审核请求并通知群管理员。"""
        group_id = event.get_group_id()
        media_paths: list[str] = []
        for img in images[:2]:
            try:
                path = await img.convert_to_file_path()
                if path:
                    media_paths.append(path)
            except Exception:
                pass
        entry = {
            "group_id": group_id,
            "sender_id": event.get_sender_id(),
            "sender_name": event.get_sender_name() or event.get_sender_id(),
            "text": event.message_str or "",
            "reasons": reasons[:8],
            "score": int(score),
            "message_id": getattr(event.message_obj, "message_id", ""),
            "ts": time.time(),
            "media_paths": media_paths,
        }
        aid = await self._add_pending(entry)
        if not aid:
            return  # 已有重复待审核，无需重复通知
        await self._notify_admins(event, entry)
        if self._cfg("audit_stop_event", True):
            event.stop_event()

    async def _notify_admins(self, event: AstrMessageEvent, entry: dict) -> None:
        """把待审核信息发送给群管理员（可带图片预览并 @ 管理员）。"""
        admins = await self._get_group_admins(event)
        if not admins:
            logger.info("adguard: 群内无管理员，跳过审核通知")
            return
        text = (entry.get("text") or "")[:60]
        lines = [
            f"🔍 [广告审核] #{entry.get('id')}",
            f"发送者: {entry.get('sender_name')} ({entry.get('sender_id')})",
            f"得分: {entry.get('score')}",
            f"内容: {text if text else '(纯图片/视频)'}",
        ]
        if entry.get("reasons"):
            lines.append("命中: " + "、".join(str(x) for x in entry["reasons"][:5]))
        lines.append(
            "处理: /adguard audit " + str(entry.get("id")) + " yes 确认广告  |  no 误判放行"
        )
        chain: list = [Plain("\n".join(lines))]
        if self._cfg("audit_attach_image", True):
            for p in (entry.get("media_paths") or [])[:1]:
                try:
                    chain.append(Image.fromFileSystem(p))
                except Exception:
                    pass
        if self._cfg("audit_mention_admin", True):
            for uid in admins[:6]:
                chain.append(At(qq=str(uid)))
        try:
            await event.send(MessageChain(chain))
        except Exception as e:
            logger.warning(f"adguard: 发送审核通知失败: {e}")


    @adguard.command("audit")
    async def adguard_audit(self, event: AstrMessageEvent):
        """二次审核：管理员确认是广告还是误判。"""
        if not await self._is_operator(event):
            yield event.plain_result("❌ 仅管理员/群主可执行此操作")
            return
        group_id = event.get_group_id()
        if not group_id:
            yield event.plain_result("请在本群执行此命令")
            return
        args = self._strip_cmd(event.message_str, "audit").split()
        if len(args) < 2:
            yield event.plain_result(
                "用法：/adguard audit <编号> yes|no\nyes=确认广告并处罚 / no=误判放行"
            )
            return
        aid = args[0]
        verdict = args[1].lower()
        pending = await self._get_pending(group_id)
        entry = pending.get(aid)
        if not entry:
            yield event.plain_result(f"❓ 未找到待审核编号 #{aid}（可能已处理或过期）")
            return
        if verdict in ("yes", "y", "是", "1", "ad"):
            await self._learn_text(group_id, entry.get("text", ""), "ad")
            await self._record_user_ad(group_id, str(entry.get("sender_id", "")))
            bot = getattr(event, "bot", None)
            nickname = entry.get("sender_name", "") or str(entry.get("sender_id", ""))
            reasons = ["管理员确认广告（二次审核）", *entry.get("reasons", [])[:4]]
            await self._apply_punishment(
                bot,
                group_id,
                str(entry.get("sender_id", "")),
                nickname,
                entry.get("message_id", ""),
                reasons,
                event,
                stop=False,
            )
            await self._drop_pending(group_id, aid)
            yield event.plain_result(f"✅ 已确认 #{aid} 为广告并处罚，已记录学习")
        elif verdict in ("no", "n", "否", "0", "ok"):
            await self._learn_text(group_id, entry.get("text", ""), "ok")
            await self._drop_pending(group_id, aid)
            yield event.plain_result(f"✅ 已记录 #{aid} 为误判，后续相同内容将自动放行")
        else:
            yield event.plain_result("参数错误：yes=确认广告 / no=误判放行")

    @adguard.command("pending")
    async def adguard_pending(self, event: AstrMessageEvent):
        """查看本群待审核记录。"""
        group_id = event.get_group_id()
        pending = await self._get_pending(group_id) if group_id else {}
        if not pending:
            yield event.plain_result("📋 当前无待审核记录")
            return
        lines = ["📋 待审核记录："]
        for aid, e in sorted(pending.items(), key=lambda x: x[1].get("ts", 0)):
            name = e.get("sender_name", "") or e.get("sender_id", "")
            text = (e.get("text") or "")[:24]
            lines.append(f"#{aid} {name}: {text if text else '(媒体)'}")
        yield event.plain_result("\n".join(lines))

    @adguard.command("forget")
    async def adguard_forget(self, event: AstrMessageEvent):
        """移除某条待审核记录（不做学习记录）。"""
        if not await self._is_operator(event):
            yield event.plain_result("❌ 仅管理员/群主可执行此操作")
            return
        group_id = event.get_group_id()
        if not group_id:
            yield event.plain_result("请在本群执行此命令")
            return
        arg = self._strip_cmd(event.message_str, "forget")

    @adguard.command("selftest")
    async def adguard_selftest(self, event: AstrMessageEvent):
        """插件自检：输出配置、依赖、智谱 Key 与文本检测测试结果。"""
        lines = ["🔧 广告检测插件自检"]
        try:
            from astrbot import VERSION as _v
            lines.append(f"插件版本: v1.1.1 | AstrBot: {_v}")
        except Exception:
            lines.append("插件版本: v1.1.1")
        lines.append(f"启用: {'✅' if self._enabled() else '❌'} | "
                     f"图片={self._cfg('image_check', 'auto')} | "
                     f"视频={self._cfg('video_check', 'auto')}")
        lines.append(
            f"依赖: OCR={'✅' if self._ocr_available else '❌'} | "
            f"OpenCV={'✅' if self._cv2_available else '❌'} | "
            f"httpx={'✅' if httpx is not None else '❌'}"
        )
        zkey = self._zhipu_api_key()
        if zkey:
            lines.append(f"智谱: ✅ 已配置 (key 前 {min(len(zkey), 8)} 位: {zkey[:8]}... | "
                         f"模型: {self._cfg('zhipu_model', 'glm-4v-flash')})")
        else:
            lines.append("智谱: ❌ 未配置（zhipu_api_key_id / zhipu_api_key_secret 为空）")
        thr = int(self._cfg("score_threshold", 4) or 4)
        media_thr = int(self._cfg("media_score_threshold", 3) or 3)
        audit_thr = int(self._cfg("audit_threshold", 2) or 2)
        lines.append(f"阈值: 文本={thr} 媒体={media_thr} 审核={audit_thr}")
        # 文本检测自检
        ad = "加群领福利 微信xxx 群号：12345678"
        ok = "今天天气不错，大家晚上好"
        s_ad, hits_ad = self._score_text(ad)
        s_ok, _ = self._score_text(ok)
        lines.append(
            f"文本自检[广告]: 得分={s_ad} (阈值{thr}) "
            f"{'✅ 命中' if s_ad >= thr else '❌ 未达阈值'} 命中={hits_ad[:3]}"
        )
        lines.append(
            f"文本自检[正常]: 得分={s_ok} {'' if s_ok < thr else '⚠️ 疑似误判'}"
        )
        yield event.plain_result("\n".join(lines))

        aid = arg.split()[0] if arg else ""
        if not aid:
            yield event.plain_result("用法：/adguard forget <编号>")
            return
        pending = await self._get_pending(group_id)
        if aid in pending:
            await self._drop_pending(group_id, aid)
            yield event.plain_result(f"✅ 已移除待审核 #{aid}")
        else:
            yield event.plain_result(f"❓ 未找到待审核 #{aid}")

    @adguard.command("forgive")
    async def adguard_forgive(self, event: AstrMessageEvent):
        if not await self._is_operator(event):
            yield event.plain_result("❌ 仅管理员/群主可执行此操作")
            return
        arg = self._strip_cmd(event.message_str, "forgive")
        user_id = arg.split()[0] if arg else ""
        group_id = event.get_group_id()
        if not user_id:
            yield event.plain_result("用法：/adguard forgive <QQ号>")
            return
        if not group_id:
            yield event.plain_result("请在本群执行此命令")
            return
        kv_key = f"offense:{group_id}:{user_id}"
        await self.delete_kv_data(kv_key)
        yield event.plain_result(f"✅ 已清零 {user_id} 在本群的违规次数")

