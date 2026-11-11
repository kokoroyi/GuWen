# -*- coding: utf-8 -*-
"""古籍整理工作台 · 离线查字数据层（charlib）

职责
----
1. 解析 Unicode Unihan 数据库（`data/unihan/` 下的 Unihan_IRGSources.txt、
   Unihan_Readings.txt、Unihan_OtherMappings.txt、Unihan_Variants.txt），生成紧凑索引：
   * `data/chardata/index.json`  汉字 -> {py, rs, ts}（约 10 万汉字，4 MB 左右）
   * `data/chardata/freq.txt`    常用度分层（4 行，由 kHanyuPinlu / kTGHZ2013 /
                                 kGB0 / kXHC1983 / kIICore 推导，只用于候选排序）
   * `data/chardata/variants.txt` 异体/繁简/新旧字形关系（只用于候选排序）
2. 提供查字信息、同音/形近候选、拼音/部首/笔画/文本检索；
3. 提供繁体 -> 简体 / 简体 -> 繁体转换（opencc-python-reimplemented，懒加载）。

设计约束
--------
* 完全离线，本模块不做任何网络请求；
* **导入阶段不做文件 IO**：索引只在 `ensure_data()` 第一次被调用时构建或加载，
  之后进程内缓存复用（首次构建约 2~4 s，纯加载约 0.2~0.5 s）；
* 索引缺失但 Unihan 原始 txt 存在时自动重建；两者都缺失时抛
  `DataUnavailable`，由 API 层转成 503 JSON。

对外主要接口（char_api.py 使用）
--------------------------------
    ensure_data()                      -> dict   数据层（首次触发构建/加载）
    data_stats()                       -> dict   构建/加载统计（测试与说明文档用）
    char_info(ch)                      -> dict
    candidates(ch, n=24)               -> list[dict]
    candidates_batch(chs, n=8)         -> list[dict]
    lookup(mode, q, strokes=None, n=60)-> dict
    radicals()                         -> list[dict]
    convert_text(text, direction)      -> str
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import unicodedata

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE, "data")
UNIHAN_DIR = os.path.join(DATA_DIR, "unihan")
CHAR_DIR = os.path.join(DATA_DIR, "chardata")
INDEX_PATH = os.path.join(CHAR_DIR, "index.json")
FREQ_PATH = os.path.join(CHAR_DIR, "freq.txt")
VARIANTS_PATH = os.path.join(CHAR_DIR, "variants.txt")
UNIHAN_IRG = os.path.join(UNIHAN_DIR, "Unihan_IRGSources.txt")
UNIHAN_READ = os.path.join(UNIHAN_DIR, "Unihan_Readings.txt")
UNIHAN_MAP = os.path.join(UNIHAN_DIR, "Unihan_OtherMappings.txt")
UNIHAN_VAR = os.path.join(UNIHAN_DIR, "Unihan_Variants.txt")

INDEX_VERSION = 2
# 单次请求候选数上限（防御异常参数）
MAX_CANDIDATES = 100


class DataUnavailable(RuntimeError):
    """索引与 Unihan 原始数据都不可用。"""


# ---------------------------------------------------------------------------
# 214 个康熙部首（Unicode 顺序，U+2F00 起 214 个康熙部首字符的 NFKC 单字形式）
# ---------------------------------------------------------------------------
RADICALS = (
    "一丨丶丿乙亅二亠人儿入八冂冖冫几凵刀力勹匕匚匸十卜卩厂厶又"
    "口囗土士夂夊夕大女子宀寸小尢尸屮山巛工己巾干幺广廴廾弋弓彐彡彳"
    "心戈戶手支攴文斗斤方无日曰月木欠止歹殳毋比毛氏气水火爪父爻爿片牙牛犬"
    "玄玉瓜瓦甘生用田疋疒癶白皮皿目矛矢石示禸禾穴立竹米糸缶网羊羽老而耒耳聿"
    "肉臣自至臼舌舛舟艮色艸虍虫血行衣襾見角言谷豆豕豸貝赤走足身車辛辰辵邑酉釆里"
    "金長門阜隶隹雨靑非面革韋韭音頁風飛食首香馬骨高髟鬥鬯鬲鬼"
    "魚鳥鹵鹿麥麻黃黍黑黹黽鼎鼓鼠鼻齊齒龍龜龠"
)
if len(RADICALS) != 214 or len(set(RADICALS)) != 214:  # pragma: no cover - 防御
    raise RuntimeError("RADICALS 常量损坏：期望 214 个互异部首，实际 %d" % len(RADICALS))

_RADICAL_INDEX = {ch: i + 1 for i, ch in enumerate(RADICALS)}

# 常见「偏旁变体 / 简体部首字」到康熙部首号的别名，便于 mode=radical 查询
_RADICAL_ALIASES = {
    "亻": 9, "冫": 15, "刂": 18, "囗": 31, "忄": 61, "㣺": 61, "户": 63, "戸": 63,
    "扌": 64, "龵": 64, "攵": 66, "氵": 85, "氺": 85, "灬": 86, "爫": 87,
    "牜": 93, "犭": 94, "王": 96, "疋": 103, "礻": 113, "罒": 122, "罓": 122,
    "艹": 140, "衤": 145, "訁": 149, "讠": 149, "见": 147, "贝": 154, "车": 159,
    "辶": 162, "钅": 167, "釒": 167, "长": 168, "门": 169, "青": 174,
    "韦": 178, "页": 181, "风": 182, "飞": 183, "飠": 184, "饣": 184,
    "纟": 120, "糹": 120,
    "马": 187, "鱼": 195, "鸟": 196, "卤": 197, "麦": 199, "黄": 201,
    "黾": 205, "齐": 210, "齿": 211, "龙": 212, "龟": 213,
}

# 康熙部首标准笔画数（按部首编号分组；Unihan 里个别部首字自身笔画与传统表不同，
# 例如 114 禸 在 Unihan 记 4 画、传统部首表记 5 画，这里以传统部首表为准）
_RADICAL_STROKE_GROUPS = (
    (6, 1), (29, 2), (60, 3), (94, 4), (117, 5), (146, 6), (166, 7),
    (175, 8), (186, 9), (194, 10), (200, 11), (204, 12), (208, 13),
    (210, 14), (213, 15), (214, 16),
)


def radical_strokes(num: int) -> int:
    """康熙部首号 -> 该部首自身的笔画数（1~16）。"""
    for upper, strokes in _RADICAL_STROKE_GROUPS:
        if num <= upper:
            return strokes
    return 16


# ---------------------------------------------------------------------------
# 拼音处理
# ---------------------------------------------------------------------------
_TONE_MARKS = {"\u0304": 1, "\u0301": 2, "\u030c": 3, "\u0300": 4}


def reading_base(reading: str) -> str:
    """带调拼音 -> 无声调基础音节（ü、ê 等一律折成 ASCII，便于前缀检索）。

    'lǜ' -> 'lu'，'lüè' -> 'lue'，'nǚ' -> 'nu'，'ma' -> 'ma'。
    """
    decomposed = unicodedata.normalize("NFD", reading.lower())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def reading_tone(reading: str) -> int:
    """带调拼音 -> 声调号（1~4，轻声返回 5）。"""
    for c in unicodedata.normalize("NFD", reading):
        tone = _TONE_MARKS.get(c)
        if tone:
            return tone
    return 5


def _query_fold(q: str) -> str:
    """查询串归一：小写、去声调符号、ü/v 折成 u、去空白。"""
    q = (q or "").strip().lower()
    decomposed = unicodedata.normalize("NFD", q)
    folded = "".join(c for c in decomposed if not unicodedata.combining(c))
    return folded.replace("v", "u").replace(" ", "")


def _has_tone_marks(s: str) -> bool:
    if any(c in s for c in "üêńňǹḿ"):
        return True
    return any(unicodedata.combining(c) for c in unicodedata.normalize("NFD", s))


# ---------------------------------------------------------------------------
# 首次构建 / 加载
# ---------------------------------------------------------------------------
_lock = threading.Lock()
_db = None  # type: dict | None
_stats = {"mode": None, "seconds": 0.0, "chars": 0}


class _Parser:
    """Unihan 原始 txt 的流式解析（只在 build_index 中使用）。"""

    @staticmethod
    def _codepoint(raw: str) -> int:
        return int(raw[2:], 16)

    @staticmethod
    def parse(irg_path, readings_path, mappings_path, variants_path):
        rsv = {}      # codepoint -> (radical_num, residual_strokes)
        tsv = {}      # codepoint -> total strokes
        mand = {}     # codepoint -> kMandarin 原始字符串
        core = set()  # IICore 成员（只用于常用度分层）
        pinlu = set()   # kHanyuPinlu：现代汉语频率词典用字（常用度第 0 层）
        tghz = set()    # kTGHZ2013：通用规范汉字表 8105 字（第 1 层）
        gb0 = set()     # kGB0：GB2312 常用字（第 2 层）
        xhc = set()     # kXHC1983：现代汉语词典用字（第 2 层）
        variants = {}   # codepoint -> set(codepoint)：传统/简化/异体/新旧字形关系

        with open(irg_path, encoding="utf-8") as f:
            for line in f:
                if not line or line[0] == "#":
                    continue
                parts = line.rstrip("\n").split("\t")
                if len(parts) < 3:
                    continue
                field = parts[1]
                if field == "kRSUnicode":
                    m = re.match(r"(\d+)'*\.(-?\d+)", parts[2])
                    if m:
                        rsv[_Parser._codepoint(parts[0])] = (int(m.group(1)), int(m.group(2)))
                elif field == "kTotalStrokes":
                    try:
                        tsv[_Parser._codepoint(parts[0])] = int(parts[2].split()[0])
                    except (ValueError, IndexError):
                        pass
                elif field == "kIICore":
                    core.add(_Parser._codepoint(parts[0]))

        for path, wanted, bucket in (
            (readings_path, ("kMandarin", "kHanyuPinlu", "kTGHZ2013", "kXHC1983"), None),
            (mappings_path, ("kGB0",), None),
        ):
            with open(path, encoding="utf-8") as f:
                for line in f:
                    if not line or line[0] == "#":
                        continue
                    parts = line.rstrip("\n").split("\t")
                    if len(parts) < 3 or parts[1] not in wanted:
                        continue
                    field = parts[1]
                    cp = _Parser._codepoint(parts[0])
                    if field == "kMandarin":
                        mand[cp] = parts[2].strip()
                    elif field == "kHanyuPinlu":
                        pinlu.add(cp)
                    elif field == "kTGHZ2013":
                        tghz.add(cp)
                    elif field == "kXHC1983":
                        xhc.add(cp)
                    elif field == "kGB0":
                        gb0.add(cp)

        # 异体关系：Unihan_Variants.txt 的 8 类变体字段（只作候选排序加权）
        variant_fields = {
            "kTraditionalVariant", "kSimplifiedVariant", "kSemanticVariant",
            "kSpecializedSemanticVariant", "kZVariant", "kSpoofingVariant",
            "kJapaneseOldVariant", "kJapaneseNewVariant",
        }
        with open(variants_path, encoding="utf-8") as f:
            for line in f:
                if not line or line[0] == "#":
                    continue
                parts = line.rstrip("\n").split("\t")
                if len(parts) < 3 or parts[1] not in variant_fields:
                    continue
                cp = _Parser._codepoint(parts[0])
                for token in parts[2].split():
                    raw = token.split("<", 1)[0].strip()
                    if not raw.startswith("U+"):
                        continue
                    try:
                        other = _Parser._codepoint(raw)
                    except ValueError:
                        continue
                    if other == cp:
                        continue
                    variants.setdefault(cp, set()).add(other)
                    variants.setdefault(other, set()).add(cp)

        return {
            "rs": rsv, "ts": tsv, "mand": mand, "core": core,
            "pinlu": pinlu, "tghz": tghz, "gb0": gb0, "xhc": xhc,
            "variants": variants,
        }


def _write_atomic(path: str, text: str) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def build_index(verbose: bool = False) -> dict:
    """从 Unihan 原始 txt 重建 index.json / freq.txt / variants.txt（原子写入）。"""
    for p in (UNIHAN_IRG, UNIHAN_READ, UNIHAN_MAP, UNIHAN_VAR):
        if not os.path.exists(p):
            raise DataUnavailable(
                "找不到 Unihan 原始数据（缺 %s），且索引不存在。请把 Unihan.zip 解压到 "
                "app/data/unihan/（恢复步骤见 spec/查字数据说明.md）。" % os.path.basename(p)
            )
    started = time.perf_counter()
    parsed = _Parser.parse(UNIHAN_IRG, UNIHAN_READ, UNIHAN_MAP, UNIHAN_VAR)
    rsv, tsv, mand = parsed["rs"], parsed["ts"], parsed["mand"]

    chars = {}
    for cp in sorted(set(rsv) | set(tsv) | set(mand)):
        rad, resid = rsv.get(cp, (0, 0))
        ts = tsv.get(cp)
        if ts is None:  # 理论上不会发生（Unihan 两个字段覆盖同一批字），兜底估算
            ts = (radical_strokes(rad) + max(resid, 0)) if rad else 0
        chars[chr(cp)] = {
            "py": mand.get(cp, ""),
            "rs": ("%d.%d" % (rad, resid)) if rad else "",
            "ts": ts,
        }

    # 常用度分层（只影响候选/检索排序，不影响是否收录）：
    #   0 kHanyuPinlu 高频字；1 通用规范汉字表；2 GB2312 / 现代汉语词典；3 IICore；4 其他
    tier_lines = []
    tiers = [
        ({cp for cp in parsed["pinlu"] if cp in rsv},),
        ({cp for cp in parsed["tghz"] if cp in rsv} - parsed["pinlu"],),
        ({cp for cp in (parsed["gb0"] | parsed["xhc"]) if cp in rsv}
         - parsed["pinlu"] - parsed["tghz"],),
        ({cp for cp in parsed["core"] if cp in rsv}
         - parsed["pinlu"] - parsed["tghz"] - parsed["gb0"] - parsed["xhc"],),
    ]
    for (group,) in tiers:
        tier_lines.append("".join(chr(cp) for cp in sorted(group)))

    variants = {}
    for cp, others in parsed["variants"].items():
        ch = chr(cp)
        if ch not in chars:
            continue
        keep = "".join(sorted({chr(o) for o in others if chr(o) in chars}))
        if keep:
            variants[ch] = keep

    os.makedirs(CHAR_DIR, exist_ok=True)
    payload = {"_v": INDEX_VERSION, "_count": len(chars), "chars": chars}
    tmp_index = INDEX_PATH + ".tmp"
    with open(tmp_index, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp_index, INDEX_PATH)
    _write_atomic(FREQ_PATH, "\n".join(tier_lines) + "\n")
    _write_atomic(VARIANTS_PATH,
                  "".join("%s\t%s\n" % (ch, vs) for ch, vs in sorted(variants.items())))

    elapsed = time.perf_counter() - started
    if verbose:
        print("[charlib] index built: %d chars, %.2fs, index %.2f MB, variants %d"
              % (len(chars), elapsed, os.path.getsize(INDEX_PATH) / 1048576.0, len(variants)))
    return {"chars": len(chars), "seconds": elapsed, "variants": len(variants)}


def _load_index() -> dict:
    """加载索引与辅助数据，构建内存检索结构（进程内仅一次）。"""
    started = time.perf_counter()
    with open(INDEX_PATH, encoding="utf-8") as f:
        raw = json.load(f)
    raw_chars = raw.get("chars") if isinstance(raw, dict) else None
    if not isinstance(raw_chars, dict) or not raw_chars:
        raise DataUnavailable("索引文件损坏或为空：%s" % INDEX_PATH)

    chars = {}
    by_radical = {}
    by_stroke = {}
    for ch, rec in raw_chars.items():
        py = rec.get("py") or ""
        rs = rec.get("rs") or ""
        rad = 0
        resid = 0
        if rs:
            try:
                num, _, res = rs.partition(".")
                rad = int(num)
                resid = int(res) if res else 0
            except ValueError:
                rad, resid = 0, 0
        try:
            ts = int(rec.get("ts") or 0)
        except (TypeError, ValueError):
            ts = 0
        chars[ch] = (py, rad, resid, ts)
        if 1 <= rad <= 214:
            by_radical.setdefault(rad, []).append((ts, resid, ch))
        if ts > 0:
            by_stroke.setdefault(ts, []).append(ch)

    # 常用度分层：0 最高……3，未收录为 4
    tier = {}
    if os.path.exists(FREQ_PATH):
        try:
            with open(FREQ_PATH, encoding="utf-8") as f:
                for level, line in enumerate(f.read().split("\n")[:4]):
                    for ch in line:
                        tier[ch] = level
        except OSError:
            tier = {}

    # 异体/繁简关系（只用于候选排序）
    variants = {}
    if os.path.exists(VARIANTS_PATH):
        try:
            with open(VARIANTS_PATH, encoding="utf-8") as f:
                for line in f:
                    line = line.rstrip("\n")
                    if not line or "\t" not in line:
                        continue
                    ch, vs = line.split("\t", 1)
                    variants[ch] = set(vs)
        except OSError:
            variants = {}

    for bucket in by_radical.values():
        bucket.sort(key=lambda x: (x[0], tier.get(x[2], 4), x[2]))
    for bucket in by_stroke.values():
        bucket.sort(key=lambda c: (tier.get(c, 4), c))

    # 同音检索索引：基础音节 -> [(带调读音, 总笔画, 常用层, 字)]
    by_base = {}
    # 带调读音 -> [(总笔画, 常用层, 字)]，用于「带声调」查询
    by_reading = {}
    for ch, (py, _rad, _resid, ts) in chars.items():
        if not py:
            continue
        level = tier.get(ch, 4)
        seen_reading = set()
        for reading in py.split():
            if not reading or reading in seen_reading:
                continue
            seen_reading.add(reading)
            by_base.setdefault(reading_base(reading), []).append(
                (reading, ts, level, ch))
            by_reading.setdefault(unicodedata.normalize("NFC", reading), []).append(
                (ts, level, ch))
    for bucket in by_base.values():
        bucket.sort(key=lambda x: (x[2], x[1], x[3]))
    for bucket in by_reading.values():
        bucket.sort(key=lambda x: (x[1], x[0], x[2]))

    elapsed = time.perf_counter() - started
    return {
        "chars": chars,
        "by_radical": by_radical,
        "by_stroke": by_stroke,
        "by_base": by_base,
        "by_reading": by_reading,
        "tier": tier,
        "variants": variants,
        "index_version": raw.get("_v"),
    }


def _index_version_ok() -> bool:
    """索引头部的 INDEX_VERSION 是否与当前代码匹配（不匹配则重建）。"""
    try:
        with open(INDEX_PATH, "rb") as f:
            head = f.read(64).decode("utf-8", "ignore")
    except OSError:
        return False
    m = re.search(r'"_v"\s*:\s*(\d+)', head)
    return bool(m) and int(m.group(1)) == INDEX_VERSION


def ensure_data() -> dict:
    """返回进程内缓存的数据层；第一次调用时构建或加载索引（阻塞等待）。"""
    global _db
    if _db is not None:
        return _db
    with _lock:
        if _db is not None:
            return _db
        needed = (INDEX_PATH, FREQ_PATH, VARIANTS_PATH)
        if not all(os.path.exists(p) for p in needed) or not _index_version_ok():
            result = build_index(verbose=True)
            _stats.update(mode="build", seconds=result["seconds"], chars=result["chars"])
        else:
            _stats["mode"] = "load"
        started = time.perf_counter()
        db = _load_index()
        _stats["seconds"] = (round(time.perf_counter() - started, 4)
                             if _stats["mode"] == "load"
                             else round(_stats["seconds"] + time.perf_counter() - started, 4))
        _stats["chars"] = len(db["chars"])
        _db = db
        return _db


def data_stats() -> dict:
    """最近一次 ensure_data 的模式 / 耗时 / 收录字数，以及索引文件大小。"""
    out = dict(_stats)
    try:
        out["index_bytes"] = os.path.getsize(INDEX_PATH)
        out["index_mb"] = round(out["index_bytes"] / 1048576.0, 2)
    except OSError:
        out["index_bytes"] = 0
        out["index_mb"] = 0.0
    out["ready"] = _db is not None
    return out


# ---------------------------------------------------------------------------
# 单字信息
# ---------------------------------------------------------------------------
def char_info(ch: str) -> dict:
    """单字信息；未收录返回 {"ch", "known": False}。"""
    ch = (ch or "").strip()
    if len(ch) != 1:
        raise ValueError("ch 必须是单个汉字")
    db = ensure_data()
    rec = db["chars"].get(ch)
    if rec is None:
        return {"ch": ch, "known": False}
    py, rad, resid, ts = rec
    return {
        "ch": ch,
        "known": True,
        "pinyin": py.split() if py else [],
        "radical": RADICALS[rad - 1] if 1 <= rad <= 214 else "",
        "radical_strokes": max(0, resid),
        "strokes": ts,
    }


# ---------------------------------------------------------------------------
# 候选字（形近 / 同音）
# ---------------------------------------------------------------------------
def _shape_candidates(db: dict, ch: str, rec: tuple) -> list:
    """同部首且总笔画差 <= 2 的候选。

    排序：常用字优先（常用层 <= 2 的一组排前，生僻扩展字排后），组内
    「异体关系 -> 笔画差 -> 常用层 -> 总笔画 -> 码位」。
    """
    _py, rad, _resid, ts = rec
    if not rad:
        return []
    tier = db["tier"]
    variants = db["variants"].get(ch, ())
    familiar = []
    rare = []
    for ts2, resid2, ch2 in db["by_radical"].get(rad, ()):
        if ch2 == ch:
            continue
        diff = abs(ts2 - ts)
        if diff > 2:
            continue
        level = tier.get(ch2, 4)
        item = (0 if ch2 in variants else 1, diff, level, ts2, resid2, ch2)
        (familiar if level <= 2 else rare).append(item)
    familiar.sort()
    rare.sort()
    return familiar + rare


def _base_set(py: str) -> set:
    return {reading_base(r) for r in py.split() if r}


def _sound_candidates(db: dict, ch: str, rec: tuple, src_bases: set) -> list:
    """同音（kMandarin 去声调后基础音节相同）候选。

    排序：异体关系 -> 同调（带调读音完全一致）-> 常用层 -> 笔画接近 -> 码位。
    """
    py, _rad, _resid, ts = rec
    if not py or not src_bases:
        return []
    variants = db["variants"].get(ch, ())
    tier = db["tier"]
    src_readings = {unicodedata.normalize("NFC", r) for r in py.split() if r}
    best = {}
    for base in src_bases:
        for reading, ts2, level2, ch2 in db["by_base"].get(base, ()):
            if ch2 == ch:
                continue
            exact = 0 if unicodedata.normalize("NFC", reading) in src_readings else 1
            rank = (0 if ch2 in variants else 1, exact, level2, abs(ts2 - ts), ts2, ch2)
            prev = best.get(ch2)
            if prev is None or rank < prev:
                best[ch2] = rank
    return sorted(best.values())


def candidates(ch: str, n: int = 24) -> list:
    """同音 / 形近候选字。排序：先形近（同部首、笔画差 <= 2），后同音。

    为避免同部首字过多挤掉同音候选，形近组最多占约 3/4 名额，其余留给同音；
    若同音候选不足，剩余名额回填形近。
    """
    ch = (ch or "").strip()
    if len(ch) != 1:
        raise ValueError("ch 必须是单个汉字")
    n = max(1, min(int(n or 24), MAX_CANDIDATES))
    db = ensure_data()
    rec = db["chars"].get(ch)
    if rec is None:
        return []

    src_bases = _base_set(rec[0])
    shape = _shape_candidates(db, ch, rec)
    sound = _sound_candidates(db, ch, rec, src_bases)

    # 同音字集合（用于给形近候选加 "·同音" 标签）
    sound_chs = {item[5] for item in sound}

    sound_reserve = min(len(sound), max(3, n // 4)) if shape else 0
    shape_take = min(len(shape), n - sound_reserve) if shape else 0

    out = []
    shape_label = {}
    for item in shape[:shape_take]:
        ch2 = item[5]
        label = "形近·同音" if ch2 in sound_chs else "形近"
        shape_label[ch2] = label
        out.append({"text": ch2, "why": label})

    for item in sound:
        if len(out) >= n:
            break
        ch2 = item[5]
        if ch2 in shape_label:
            continue
        out.append({"text": ch2, "why": "同音"})

    if len(out) < n:
        for item in shape[shape_take:]:
            if len(out) >= n:
                break
            ch2 = item[5]
            out.append({"text": ch2, "why": shape_label.get(ch2, "形近")})
    return out


def candidates_batch(chs: str, n: int = 8) -> list:
    """批量候选：chs 为连续汉字串，每个字最多 n（默认 8）个候选。"""
    text = (chs or "").strip()
    n = max(1, min(int(n or 8), MAX_CANDIDATES))
    ensure_data()
    out = []
    seen = set()
    for ch in text:
        if ch in seen or ch.isspace():
            continue
        seen.add(ch)
        out.append({"ch": ch, "cands": candidates(ch, n)})
    return out


# ---------------------------------------------------------------------------
# 检索
# ---------------------------------------------------------------------------
def _resolve_radical(db: dict, q: str) -> int:
    q = (q or "").strip()
    if not q:
        raise ValueError("q（部首字）不能为空")
    if q.isdigit():
        num = int(q)
        if 1 <= num <= 214:
            return num
        raise ValueError("部首编号超出 1~214")
    ch = q[0]
    if ch in _RADICAL_INDEX:
        return _RADICAL_INDEX[ch]
    if ch in _RADICAL_ALIASES:
        return _RADICAL_ALIASES[ch]
    rec = db["chars"].get(ch)
    if rec is not None and 1 <= rec[1] <= 214:
        return rec[1]
    raise ValueError("无法识别的部首：%s" % ch)


def _lookup_item(db: dict, ch: str) -> dict:
    rec = db["chars"].get(ch)
    if rec is None:
        return {"text": ch, "pinyin": [], "radical": None, "strokes": None}
    py, rad, _resid, ts = rec
    return {
        "text": ch,
        "pinyin": py.split() if py else [],
        "radical": RADICALS[rad - 1] if 1 <= rad <= 214 else None,
        "strokes": ts or None,
    }


def lookup(mode: str, q: str = "", strokes=None, n: int = 60) -> dict:
    """查字检索。

    mode=text     按字符逐个查询（含未收录字）
    mode=pinyin   无声调前缀（li -> li/lí/lǐ/lì/liang/lin...）或带声调（lǐ / li3）
    mode=radical  q 为部首字（如 口），可选 strokes 过滤「部首外笔画数」
    mode=strokes  q 为总笔画数
    """
    db = ensure_data()
    n = max(1, min(int(n or 60), 200))
    mode = (mode or "").strip().lower()

    if mode == "text":
        chars = [c for c in (q or "").strip() if not c.isspace()]
        if len(chars) > 1000:
            raise ValueError("q 过长（最多 1000 字）")
        items = [_lookup_item(db, ch) for ch in chars]
        return {"mode": mode, "total": len(items), "items": items}

    if mode == "pinyin":
        return _lookup_pinyin(db, q, n)

    if mode == "radical":
        num = _resolve_radical(db, q)
        bucket = db["by_radical"].get(num, ())
        tier = db["tier"]
        want = None
        if strokes is not None and str(strokes).strip() != "":
            try:
                want = int(strokes)
            except (TypeError, ValueError):
                raise ValueError("strokes 必须是整数（部首外笔画数）")
            if want < 0:
                raise ValueError("strokes 不能为负数")
        matched = [x for x in bucket if (want is None or x[1] == want)]
        matched.sort(key=lambda x: (x[1], tier.get(x[2], 4), x[0], x[2]))
        total = len(matched)
        items = [_lookup_item(db, x[2]) for x in matched[:n]]
        return {"mode": mode, "total": total, "items": items,
                "radical": RADICALS[num - 1], "stroke_filter": want}

    if mode == "strokes":
        try:
            want = int(str(q).strip())
        except (TypeError, ValueError):
            raise ValueError("mode=strokes 时 q 必须是整数总笔画数")
        bucket = db["by_stroke"].get(want, ())
        total = len(bucket)
        return {"mode": mode, "total": total,
                "items": [_lookup_item(db, ch) for ch in bucket[:n]]}

    raise ValueError("mode 必须是 text / pinyin / radical / strokes 之一")


def _lookup_pinyin(db: dict, q: str, n: int) -> dict:
    q = (q or "").strip()
    if not q:
        raise ValueError("q（拼音）不能为空")
    tier = db["tier"]
    matched = {}  # ch -> rank

    toned_query = unicodedata.normalize("NFC", q.lower())
    if _has_tone_marks(toned_query):
        # 带声调（含调号）：按带调读音前缀匹配
        for reading, bucket in db["by_reading"].items():
            if not reading.startswith(toned_query):
                continue
            exact = 0 if reading == toned_query else 1
            for ts, level, ch in bucket:
                rank = (exact, level, reading, ts, ch)
                prev = matched.get(ch)
                if prev is None or rank < prev:
                    matched[ch] = rank
        if matched:
            ordered = sorted(matched.items(), key=lambda kv: kv[1])[:n]
            return {"mode": "pinyin", "total": len(matched),
                    "items": [_lookup_item(db, ch) for ch, _r in ordered]}

    folded = _query_fold(q)
    if not folded:
        raise ValueError("q（拼音）不能为空")

    # 「音节+声调数字」形式：li4 / lu:4 / lv4
    m = re.fullmatch(r"([a-z]+)([1-5])", folded)
    if m and not _has_tone_marks(q):
        base, tone_digit = m.group(1), int(m.group(2))
        for reading, bucket in db["by_reading"].items():
            if reading_base(reading) != base or reading_tone(reading) != tone_digit:
                continue
            for ts, level, ch in bucket:
                rank = (0, level, reading, ts, ch)
                prev = matched.get(ch)
                if prev is None or rank < prev:
                    matched[ch] = rank
        if matched:
            ordered = sorted(matched.items(), key=lambda kv: kv[1])[:n]
            return {"mode": "pinyin", "total": len(matched),
                    "items": [_lookup_item(db, ch) for ch, _r in ordered]}
        folded = base  # 精确声调无结果时退回无声调前缀

    # 无声调前缀：基础音节 startswith
    for base, bucket in db["by_base"].items():
        if not base.startswith(folded):
            continue
        exact = 0 if base == folded else 1
        for reading, ts, level, ch in bucket:
            rank = (exact, level, base, ts, ch)
            prev = matched.get(ch)
            if prev is None or rank < prev:
                matched[ch] = rank
    ordered = sorted(matched.items(), key=lambda kv: kv[1])[:n]
    return {"mode": "pinyin", "total": len(matched),
            "items": [_lookup_item(db, ch) for ch, _r in ordered]}


def radicals() -> list:
    """214 个康熙部首（按编号/笔画排序），count 为索引中该部首汉字数。"""
    db = ensure_data()
    out = []
    for i, ch in enumerate(RADICALS, 1):
        out.append({
            "radical": ch,
            "num": i,
            "strokes": radical_strokes(i),
            "count": len(db["by_radical"].get(i, ())),
        })
    return out


# ---------------------------------------------------------------------------
# 繁简转换
# ---------------------------------------------------------------------------
_opencc_instances = {}
_opencc_lock = threading.Lock()


def convert_text(text: str, direction: str = "t2s") -> str:
    """繁体 -> 简体（t2s）/ 简体 -> 繁体（s2t）。OpenCC 实例懒加载并缓存。"""
    direction = (direction or "t2s").strip().lower()
    if direction not in ("t2s", "s2t"):
        raise ValueError("dir 必须是 t2s 或 s2t")
    if text is None:
        text = ""
    if not isinstance(text, str):
        raise ValueError("text 必须是字符串")
    inst = _opencc_instances.get(direction)
    if inst is None:
        with _opencc_lock:
            inst = _opencc_instances.get(direction)
            if inst is None:
                from opencc import OpenCC  # 懒加载：导入 char_api / server 不触发
                inst = OpenCC(direction)
                _opencc_instances[direction] = inst
    return inst.convert(text)
