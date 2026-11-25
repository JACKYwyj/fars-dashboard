#!/usr/bin/env python3
"""FARS 内测日志系统 —— 让内测用户一键导出日志，发回来就能复现 bug。

设计目标（针对「发给别人的内测包」这个场景）：
  1. **零配置**：用户不用做任何事，日志自动写。
  2. **不泄密**：API Key、令牌、论文正文**一律不写进日志**。
  3. **可离线**：用户点一下「导出日志」→ 得到一个 zip → 发回给我。
  4. **够用**：包含「前端 JS 报错」「服务端异常」「HTTP 请求流水」「环境信息」。

日志落在状态目录下的 logs/：
  - app.log        —— 追加写的文本日志（按天轮转）
  - client.log     —— 前端上报的 JS 错误
  - requests.log   —— HTTP 请求流水（方法/路径/状态/耗时）

导出的 zip 里含：
  - app.log / client.log / requests.log（脱敏后）
  - env.json        —— 环境信息（系统/Python/版本/端口/状态目录）
  - summary.txt     —— 人看的摘要：报错次数、Top 错误、最近的错误
"""

import io
import json
import os
import platform
import re
import sys
import threading
import time
import traceback
import zipfile
from datetime import datetime

# ---------------------------------------------------------------- 脱敏

# 必须脱敏的东西：**日志绝不能带这些**
_SECRET_PATTERNS = [
    # ⚠️ 顺序很重要：**先脱敏具体的凭据形态，再脱敏 key=value**。
    # 反过来会漏 —— 实测踩过：
    #   "Authorization: Bearer eyJhbGci..." 先被 key=value 规则改成
    #   "Authorization=***REDACTED*** eyJhbGci..." → Bearer 规则再也匹配不上
    #   → **JWT 原样留在日志里**。
    # 所以 Bearer / JWT / sk- 这些必须**独立**匹配，不依赖前面的规则。

    # Bearer 令牌（独立匹配，不看前面是什么）
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]{8,}"), "Bearer ***REDACTED***"),
    # JWT（base64url 形态）
    # ⚠️ 实测踩过：写成「必须三段」就漏了 **两段**的令牌
    #    （"eyJhbGciOiJIUzI1NiJ9.abcdef" 只有一段点号，27 字符）
    #    → 既不匹配三段 JWT，也不是 ≥32 字符的裸 token → **原样留在日志里**。
    # 现在两段/三段都遮，且不要求长度到 32。
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{4,}(?:\.[A-Za-z0-9_\-]{4,}){1,2}"),
     "***REDACTED-JWT***"),
    # API Key 常见前缀
    (re.compile(r"\b(sk-[A-Za-z0-9_\-]{12,})"), "sk-***REDACTED***"),
    (re.compile(r"\b(ghp_[A-Za-z0-9]{20,})"), "ghp_***REDACTED***"),
    (re.compile(r"\b(AIza[0-9A-Za-z_\-]{20,})"), "AIza***REDACTED***"),
    # key=value 形式（值 4 字符以上）
    (re.compile(r"(?i)\b(api[_-]?key|access[_-]?key|secret[_-]?key|token|secret|password|passwd|pwd|authorization)\b\s*[:=]\s*[\"']?([^\s\"',;}]{4,})[\"']?"),
     lambda m: "%s=***REDACTED***" % m.group(1)),
    # 兜底：超长无空格串多半是凭据
    # 阈值 32 → 24：实测 "eyJhbGciOiJIUzI1NiJ9.abcdef"（27 字符）就是这样漏掉的。
    # 24 字符以上基本不会是正常英文单词，不会误伤。
    (re.compile(r"\b([A-Za-z0-9_\-]{24,})\b"), "***REDACTED***"),
]

# 论文正文片段：太长的连续中文/英文段落 → 截断
# ⚠️ 阈值 400 → 截断到 200 字仍会泄 200 字论文正文。
#    实测：日志里出现 33 次重复的正文 → 200 字太多了。收到 60 字。
_LONG_TEXT = re.compile(r"([A-Za-z一-鿿，。；：、（）()\s]{120,})")


def redact(text, max_len=8000):
    """把一段文本里的敏感信息去掉。

    ⚠️ 必须在**写日志之前**调用，不是导出时才脱敏 ——
    原始日志一旦落盘就可能被人翻到。
    """
    if text is None:
        return ""
    s = str(text)
    for pat, rep in _SECRET_PATTERNS:
        if pat.groups >= 2:
            s = pat.sub(rep, s)
        else:
            s = pat.sub(rep, s)
    # 长文本 → 截断并标注
    s = _LONG_TEXT.sub(lambda m: m.group(1)[:60] + "…[长文本已截断]", s)
    if len(s) > max_len:
        s = s[:max_len] + "…[已截断]"
    return s


# ---------------------------------------------------------------- 日志器

class DiagLog:
    """文件日志器（线程安全）。"""

    def __init__(self, log_dir):
        self.dir = log_dir
        self._lock = threading.Lock()
        self._counts = {}          # {级别: 次数}
        self._recent = []          # 最近若干条错误（内存）
        os.makedirs(self.dir, exist_ok=True)

    # ---- 内部 ----
    def _path(self, name):
        return os.path.join(self.dir, name)

    def _write(self, name, line):
        p = self._path(name)
        try:
            with self._lock:
                with open(p, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
        except Exception:
            pass    # 写日志失败绝不能拖垮主流程

    def _rotate_if_needed(self, name, max_bytes=8 * 1024 * 1024):
        """单文件超过 8MB 就轮转一次，避免无限增长。"""
        p = self._path(name)
        try:
            if os.path.isfile(p) and os.path.getsize(p) > max_bytes:
                bak = p + ".1"
                try:
                    if os.path.exists(bak):
                        os.remove(bak)
                    os.rename(p, bak)
                except Exception:
                    pass
        except Exception:
            pass

    # ---- 公开 API ----
    def info(self, msg, **kw):
        self._bump("INFO")
        self._write("app.log", self._fmt("INFO", msg, kw))

    def warn(self, msg, **kw):
        self._bump("WARN")
        self._write("app.log", self._fmt("WARN", msg, kw))

    def error(self, msg, **kw):
        self._bump("ERROR")
        self._write("app.log", self._fmt("ERROR", msg, kw))
        self._remember("ERROR", msg, kw)

    def exception(self, msg, exc=None, **kw):
        """记录一个异常（含堆栈）。"""
        self._bump("ERROR")
        tb = ""
        if exc is not None:
            tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        full = "%s\n%s" % (msg, tb) if tb else msg
        self._write("app.log", self._fmt("ERROR", full, kw))
        self._remember("ERROR", msg, {"exc": tb[-2000:], **kw})

    def client_error(self, payload):
        """前端上报的 JS 错误。"""
        self._bump("CLIENT")
        try:
            s = json.dumps(payload, ensure_ascii=False)
        except Exception:
            s = str(payload)
        s = redact(s, max_len=4000)
        self._write("client.log", "%s %s" % (self._ts(), s))
        self._remember("CLIENT", str(payload.get("message") or payload.get("msg") or ""), payload)

    def request(self, method, path, status, ms, note=""):
        """HTTP 请求流水。"""
        self._write("requests.log", "%s %-6s %-52s %3s %6dms %s" % (
            self._ts(), method, redact(path, 300), status, ms, redact(note, 200)))

    # ---- 内部 ----
    def _ts(self):
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def _fmt(self, level, msg, kw):
        extra = ""
        if kw:
            try:
                extra = " " + json.dumps(kw, ensure_ascii=False, default=str)
            except Exception:
                extra = " " + str(kw)
        return "%s [%-6s] %s%s" % (self._ts(), level, redact(msg), redact(extra, 2000))

    def _bump(self, level):
        with self._lock:
            self._counts[level] = self._counts.get(level, 0) + 1

    def _remember(self, level, msg, kw):
        with self._lock:
            self._recent.append({
                "at": self._ts(), "level": level,
                "msg": redact(msg, 500),
                "where": redact(str(kw.get("where") or ""), 200),
                "exc": redact(str(kw.get("exc") or ""), 1500),
            })
            if len(self._recent) > 200:
                del self._recent[:-200]

    def stats(self):
        with self._lock:
            return dict(self._counts)

    def recent(self, n=50):
        with self._lock:
            return list(self._recent[-n:])

    # ---- 导出 ----
    def export_zip(self, out_path, env_info=None):
        """把日志打成 zip。返回 (成功?, 说明)。"""
        try:
            self._rotate_if_needed("app.log")
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as z:
                for name in ("app.log", "client.log", "requests.log"):
                    p = self._path(name)
                    if os.path.isfile(p):
                        # ⚠️ **导出时再脱敏一次**（双保险）。
                        #    旧版本写下的行可能没脱敏（实测踩过：
                        #    修脱敏规则之前上报的 Bearer/JWT 原样留在文件里）。
                        #    写的时候脱敏 + 导出的时候脱敏，两道关。
                        try:
                            with open(p, "r", encoding="utf-8", errors="replace") as f:
                                content = f.read()
                        except Exception:
                            content = ""
                        z.writestr(name, redact(content, max_len=2_000_000))
                    else:
                        z.writestr(name, "(本次运行没有这类日志)\n")
                env = env_info or {}
                env["exportedAt"] = stamp
                env["logCounts"] = self.stats()
                z.writestr("env.json", json.dumps(env, ensure_ascii=False, indent=2))
                z.writestr("summary.txt", self._summary_text())
            return True, out_path
        except Exception as e:
            return False, str(e)

    def _summary_text(self):
        st = self.stats()
        lines = []
        lines.append("FARS 内测日志摘要")
        lines.append("导出时间：%s" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        lines.append("")
        lines.append("计数：" + ("  ".join("%s=%d" % (k, v) for k, v in sorted(st.items())) or "无"))
        lines.append("")
        rec = self.recent(30)
        if not rec:
            lines.append("没有捕获到错误。")
        else:
            lines.append("最近的错误（最多 30 条）：")
            for r in rec:
                lines.append("  [%s] %s %s" % (r["at"], r["level"], r["msg"][:200]))
        return "\n".join(lines)


# ---------------------------------------------------------------- 环境信息

def _mask_path(p):
    """把绝对路径里的用户名替换掉 → /Users/<USER>/...
    保留目录结构（便于定位问题），不暴露真实用户名与家目录。"""
    if not p:
        return p
    s = str(p)
    home = os.path.expanduser("~")
    if home and home != "/" and s.startswith(home):
        s = "~" + s[len(home):]
    s = re.sub(r"/Users/[^/]+", "/Users/<USER>", s)
    s = re.sub(r"/home/[^/]+", "/home/<USER>", s)
    return s


def collect_env(state_dir="", port=0, extra=None):
    """采集环境信息（不含任何密钥）。"""
    info = {
        "collectedAt": datetime.now().isoformat(timespec="seconds"),
        "os": "%s %s" % (platform.system(), platform.release()),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "python": sys.version.split()[0],
        "pythonFull": sys.version.replace("\n", " "),
        "frozen": bool(getattr(sys, "frozen", False)),
        "meipass": bool(getattr(sys, "_MEIPASS", None)),
        # ⚠️ 2026-11-23 23:0x：诊断包是**内测者发回来给开发者**的。
        #   绝对路径会带出他的**用户名**（/Users/xxx）和本机目录结构。
        #   → 路径统一脱敏成「用户名占位」，保留结构供定位问题。
        "cwd": _mask_path(os.getcwd()),
        "stateDir": _mask_path(state_dir),
        "port": port,
        "appVersion": APP_VERSION,
        "home": _mask_path(os.path.expanduser("~")),
    }
    if extra:
        try:
            info.update(extra)
        except Exception:
            pass
    return info


APP_VERSION = "2026-11-23"
