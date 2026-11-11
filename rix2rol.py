#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
rix2rol.py —— Softstar RIX → AdLib ROL 转换器（独立版）

把大宇/软星的 .RIX（"Rol + Ins miXed"：20 字节头 + 内嵌乐器库 + 2 字节一条的指令流）
反解成 AdLib Visual Composer 能打开的 .ROL 工程：

  * 内嵌乐器库  → 每个音色一个 80 字节的 .ins（复刻原文件的参数与名字）
  * 指令流      → 45 轨 ROL：音符、音色切换、音量、音高 全部保留
  * 时间轴      → 默认按「Composer 能打开」的标准控制（≤ 7500 tick）；
                  超了就整体换到更粗的网格，事件一个不丢，只损失时间精度
  * 结构        → 事件超出轨道长度的声部用休止补到曲末，
                  杜绝「0 长度轨道里却有 tick>0 事件」这种会被拒绝的写法

用法
----
  python rix2rol.py 曲子.RIX                    # 输出到 ./曲子_ROL/
  python rix2rol.py 曲子.RIX -o out
  python rix2rol.py 曲子.RIX --max-ticks 60000  # 精确网格（不受 7500 限制）
  python rix2rol.py 曲子.RIX --tick-ms 25       # 自己指定网格
  python rix2rol.py 曲子.RIX --bpm 120 --tick-beat 4

参数
----
  -o, --out DIR        输出目录（默认 <源文件名>_ROL）
  --max-ticks N        时间轴上限，默认 7500（AdLib 自带曲子最大值）
  --tick-ms MS         强制每 tick 的毫秒数
  --bpm / --tick-beat  指定 BPM 或每拍 tick 数（另一个自动配平）
  --beat-measure N     每小节拍数（默认 4）
  --scale-x / --scale-y  ROL 里的编辑缩放（默认 56 / 48）

独立性
------
只依赖 Python 3 标准库，自带 RIX 解析、时间网格选择、ROL 写入与结构校验，
不引用同目录其它文件。功能更全的套件（含反向转换、往返验证、分轨、合并）见
rixrol_tool.py；按声部拆分的程序见 split_rol.py。

产物
----
  <stem>.ROL        45 轨工程（可直接用 Composer 打开，或用 ROLRIX.EXE 转回 RIX）
  <stem>_ROL/*.ins  乐器库（打开 ROL 前把它作为 .INS 路径，或打包成 .BNK 载入）
"""

import argparse
import math
import os
import struct
import sys

MAX_U16_TICKS = 60000      # ROL 的 tick 字段是 u16，先保证不溢出
DEFAULT_MAX_TICKS = 7500   # 再按「Composer 打得开」收一档（AdLib 自带曲子的最大值）


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------
def u16(d, o):
    return struct.unpack_from('<H', d, o)[0]


def f32(d, o):
    return struct.unpack_from('<f', d, o)[0]


def cstr(raw):
    return raw.split(b'\0')[0].decode('latin1')


def data_to_pitch(byte):
    """RIX 0xAx 数据字节 → ROL 音高倍率（0x80 = 原调）。"""
    return 2.0 ** ((((byte & 0xFF) - 128) / 128.0) / 12.0)


# --------------------------------------------------------------------------
# 读 RIX
# --------------------------------------------------------------------------
class Rix:
    """55 AA 头 + 64 字节/条乐器库 + 2 字节/条指令流。"""

    def __init__(self, path):
        self.path = path
        self.data = d = open(path, 'rb').read()
        self.size = len(d)
        if len(d) < 0x14 or u16(d, 0) != 0x55AA:
            raise ValueError('不是 RIX 文件（缺少 55 AA 魔数）')
        self.rhythm = d[2]
        self.ins_block = u16(d, 0x08)
        self.mus_block = u16(d, 0x0C)

        self.insts = []
        pos = self.ins_block
        while pos + 62 <= self.mus_block:
            self.insts.append({'index': len(self.insts), 'pos': pos,
                               'name': cstr(d[pos + 56:pos + 62]).strip()})
            pos += 64

        self.ops, self.end_pos = self._decode()

    def _decode(self):
        d = self.data
        I = self.mus_block + 1
        ops = []
        while I < len(d) - 1 and d[I] != 0x80:
            low, ctrl = d[I - 1], d[I]
            kind, ch = ctrl & 0xF0, ctrl & 0x0F
            if kind == 0x90:
                ops.append(('ins', ch, low, None))
            elif kind == 0xA0:
                ops.append(('pitch', ch, low, None))
            elif kind == 0xB0:
                ops.append(('vol', ch, low, None))
            elif kind == 0xC0:
                ops.append(('note', ch, low, None))
            else:
                ops.append(('delay', None, (ctrl << 8) + low, None))
            I += 2
        # 把延时折成绝对毫秒
        t = 0
        for i, (k, ch, v, _) in enumerate(ops):
            if k == 'delay':
                t += v
            ops[i] = (k, ch, v, t)
        return ops, I

    @property
    def total_ms(self):
        return sum(o[2] for o in self.ops if o[0] == 'delay')

    def timeline(self):
        return [(o[3], o) for o in self.ops if o[0] != 'delay']

    def bank_order(self):
        return [inst_bank_name(i) for i in self.insts]

    def channel_order(self):
        """初始化声道顺序：ROLRIX 是降序(10→0)，大宇原版是升序(0→10)。"""
        first = [o[1] for t, o in self.timeline() if t == 0]
        return 'asc' if len(first) >= 2 and first[0] < first[-1] else 'desc'


def inst_bank_name(inst):
    nm = (inst['name'] or '').strip().lower()
    return nm or f"ins{inst['index']}"


def inst_to_ins_blob(data, inst):
    """64 字节内嵌记录 → 80 字节 .ins（补 2 B 前缀 + 16 B 补零）。"""
    p = inst['pos']
    return b'\x00\x00' + data[p:p + 56] + data[p + 56:p + 62] + b'\x00' * 16


# --------------------------------------------------------------------------
# 时间网格
# --------------------------------------------------------------------------
def choose_timing(times, total_ms, bpm=None, tick_beat=None, max_ticks=MAX_U16_TICKS):
    """优先精确（tick_ms = 所有时刻的 gcd），其次保证 tick 数不超上限。"""
    if bpm and tick_beat:
        return 60000.0 / (bpm * tick_beat), float(bpm), int(tick_beat), False

    g = 0
    for t in times:
        g = math.gcd(g, int(t))
    tick_ms = g if g > 0 else 1.0
    exact = True
    if total_ms / tick_ms > max_ticks or tick_ms < 1:
        tick_ms = max(1.0, math.ceil(total_ms / max_ticks))
        exact = False
    return _fit_bpm(tick_ms) + (exact,)


def _fit_bpm(tick_ms):
    """给定每 tick 毫秒数，反推一对好看的 BPM × tickBeat。"""
    base = 60000.0 / tick_ms
    best = None
    for tb in range(1, 1025):
        b = base / tb
        if not (20.0 <= b <= 400.0):
            continue
        score = (0 if abs(b - round(b)) < 1e-9 else 1,
                 0 if 70.0 <= b <= 180.0 else 1,
                 abs(tb - 4))
        if best is None or score < best[0]:
            best = (score, tb, b)
    if best is None:
        tb, b = 4, base / 4
    else:
        tb, b = best[1], best[2]
    return 60000.0 / (b * tb), float(b), int(tb)


# --------------------------------------------------------------------------
# RIX → 声部模型（时刻全部用 tick 表示，长度用 tick）
# --------------------------------------------------------------------------
def empty_voice(i):
    return {'idx': i, 'name': f"Voix {i:2d}", 'nticks': 0, 'notes': [],
            'timbre_name': f"Timbre {i:2d}", 'timbre': [],
            'volume_name': f"Volume {i:2d}", 'volume': [],
            'pitch_name': f"Pitch {i:2d}", 'pitch': []}


def convert(rix, bpm=None, tick_beat=None, beat_measure=4,
            scale_x=56, scale_y=48):
    tl = rix.timeline()
    total_ms = rix.total_ms
    times = sorted({t for t, _ in tl} | {total_ms})
    mspt, bpm, tb, exact = choose_timing(times, total_ms, bpm, tick_beat)

    def tick(ms):
        return int(round(ms / mspt))

    per = {c: {'note': [], 'ins': [], 'vol': [], 'pitch': []} for c in range(11)}
    for t, o in tl:
        kind, ch, val = o[0], o[1], o[2]
        per[ch][kind].append((t, val))

    voices = []
    for c in range(11):
        p = per[c]
        v = empty_voice(c)
        notes = p['note']
        # 轨道终点：最后一个事件是关音就用它，否则用曲长
        t_end = 0
        if notes:
            t_end = notes[-1][0] if notes[-1][1] == 0 else total_ms
        segs = []
        for i, (t0, val) in enumerate(notes):
            t_next = notes[i + 1][0] if i + 1 < len(notes) else t_end
            d = tick(t_next) - tick(t0)
            if d <= 0:
                if i == len(notes) - 1:
                    break
                d = 1
            segs.append((val, d))
        v['notes'] = segs
        v['nticks'] = sum(d for _, d in segs)

        for t0, idx in p['ins']:
            inst = rix.insts[idx] if idx < len(rix.insts) else None
            nm = inst_bank_name(inst) if inst else f'ins{idx}'
            v['timbre'].append({'at': tick(t0), 'name': nm, 'index': idx})
        v['volume'] = [(tick(t0), val / 128.0) for t0, val in p['vol']]
        v['pitch'] = [(tick(t0), data_to_pitch(val)) for t0, val in p['pitch']]
        voices.append(v)

    # 曲尾静音：最长的声部补一段休止，让 ROL 总长与 RIX 的延时合计一致
    song = tick(total_ms)
    mx = max((v['nticks'] for v in voices), default=0)
    if 0 < mx < song:
        longest = max(range(11), key=lambda i: voices[i]['nticks'])
        voices[longest]['notes'].append((0, song - mx))
        voices[longest]['nticks'] = song

    return {'voices': voices, 'tick_ms': mspt, 'bpm': bpm, 'tick_beat': tb,
            'beat_measure': beat_measure, 'total_ms': total_ms,
            'scale_x': scale_x, 'scale_y': scale_y,
            'is_melodic': 1 - rix.rhythm, 'exact': exact}


# --------------------------------------------------------------------------
# 重定标 / 补边界
# --------------------------------------------------------------------------
def collect_times(voices, mspt):
    times = [0]
    for v in voices:
        t = 0.0
        for note, dur in v['notes']:
            times.append(int(round(t)))
            t += dur * mspt
        times.append(int(round(t)))
        for e in v['timbre']:
            times.append(int(round(e['at'] * mspt)))
        for at, _ in v['volume']:
            times.append(int(round(at * mspt)))
        for at, _ in v['pitch']:
            times.append(int(round(at * mspt)))
    return times


def rescale_voice(v, src_mspt, dst_mspt):
    """把声部整体换到新网格：音符/音色/音量/音高一个都不丢。"""
    def tick(t):
        return int(round(t / dst_mspt))

    out = dict(v)
    out['notes'] = []
    segs = []
    t_ms = 0.0
    for note, dur in v['notes']:
        t0, t1 = tick(t_ms), tick(t_ms + dur * src_mspt)
        d = t1 - t0
        if d <= 0:
            if segs and segs[-1][0] == note:
                t_ms += dur * src_mspt
                continue
            d = 1
        segs.append((note, d))
        t_ms += dur * src_mspt
    out['notes'] = segs
    out['nticks'] = sum(d for _, d in segs)
    n = out['nticks']

    tim, seen = [], set()
    for e in v['timbre']:
        at = min(max(tick(e['at'] * src_mspt), 0), n) if n else 0
        if at in seen:
            continue
        seen.add(at)
        tim.append({'at': at, 'name': e['name'], 'index': e['index']})
    out['timbre'] = tim

    for key in ('volume', 'pitch'):
        last = {}
        for at, val in v[key]:
            a = min(max(tick(at * src_mspt), 0), n) if n else 0
            last[a] = val
        out[key] = sorted(last.items())
    return out


def fit_event_bounds(voices):
    """事件超出轨道长度的声部用休止补到曲末（Composer 不接受越界事件）。"""
    song = max((v['nticks'] for v in voices), default=0)
    fixed = 0
    for v in voices:
        last = max([e['at'] for e in v['timbre']]
                   + [x[0] for x in v['volume']]
                   + [x[0] for x in v['pitch']], default=0)
        if last > v['nticks']:
            target = max(last, song)
            v['notes'].append((0, target - v['nticks']))
            v['nticks'] = target
            fixed += 1
    return fixed


# --------------------------------------------------------------------------
# 写 ROL
# --------------------------------------------------------------------------
def name15(text):
    b = text.encode('latin1', 'replace')[:14]
    return b + b'\x00' * (15 - len(b))


def build_rol(conv, tempo=((0, 1.0),)):
    voices = conv['voices']
    out = bytearray()
    out += struct.pack('<HH', 0, 4)                       # 版本 0.4
    sig = b'\\roll\\default'
    out += sig + b'\x00' * (40 - len(sig))
    out += struct.pack('<HHHH', conv['tick_beat'], conv['beat_measure'],
                       conv['scale_y'], conv['scale_x'])
    out += bytes([0, conv['is_melodic'] & 1])
    out += struct.pack('<11H', *[v['nticks'] for v in voices])
    out += struct.pack('<11H', *[len(v['timbre']) for v in voices])
    out += struct.pack('<11H', *[len(v['volume']) for v in voices])
    out += struct.pack('<11H', *[len(v['pitch']) for v in voices])
    out += struct.pack('<H', len(tempo))
    out += b'\x00' * 38                                   # filler → 轨道从 0xB6 开始

    out += name15('Tempo')
    out += struct.pack('<f', conv['bpm'])
    out += struct.pack('<H', len(tempo))
    for at, mult in tempo:
        out += struct.pack('<Hf', at, mult)

    for v in voices:
        out += name15(v['name'])
        out += struct.pack('<H', v['nticks'])
        for n, d in v['notes']:
            out += struct.pack('<HH', n, d)
        out += name15(v['timbre_name'])
        out += struct.pack('<H', len(v['timbre']))
        for e in v['timbre']:
            out += struct.pack('<H', e['at'])
            out += e['name'].encode('latin1', 'replace')[:8].ljust(9, b'\x00')
            out += b'\x00'
            out += struct.pack('<H', e['index'])
        out += name15(v['volume_name'])
        out += struct.pack('<H', len(v['volume']))
        for at, val in v['volume']:
            out += struct.pack('<Hf', at, val)
        out += name15(v['pitch_name'])
        out += struct.pack('<H', len(v['pitch']))
        for at, val in v['pitch']:
            out += struct.pack('<Hf', at, val)
    return bytes(out)


# --------------------------------------------------------------------------
# 校验（把「Composer 会拒绝的结构」提前抓出来）
# --------------------------------------------------------------------------
def scan_rol(data):
    """轻量解析：只为校验，返回 (头信息, 声部列表)。"""
    o = 0xB6
    n = u16(data, o + 19)
    tempo = [(u16(data, o + 21 + 6 * k), f32(data, o + 23 + 6 * k)) for k in range(n)]
    o += 21 + 6 * n
    voices = []
    for i in range(11):
        o += 15
        nticks = u16(data, o); o += 2
        notes = []
        tot = 0
        while tot < nticks:
            nn, dd = u16(data, o), u16(data, o + 2); o += 4
            notes.append((nn, dd)); tot += dd
        o += 15
        n = u16(data, o); o += 2
        tim = [(u16(data, o + 14 * k), cstr(data[o + 14 * k + 2:o + 14 * k + 11])) for k in range(n)]
        o += 14 * n
        o += 15
        n = u16(data, o); o += 2
        vol = [(u16(data, o + 6 * k), f32(data, o + 6 * k + 2)) for k in range(n)]
        o += 6 * n
        o += 15
        n = u16(data, o); o += 2
        pit = [(u16(data, o + 6 * k), f32(data, o + 6 * k + 2)) for k in range(n)]
        o += 6 * n
        voices.append({'idx': i, 'nticks': nticks, 'notes': notes,
                       'sum': sum(d for _, d in notes),
                       'timbre': tim, 'volume': vol, 'pitch': pit})
    return {'tempo': tempo, 'voices': voices, 'end': o,
            'cTicks': [u16(data, 0x36 + 2 * i) for i in range(11)],
            'cTimbre': [u16(data, 0x4C + 2 * i) for i in range(11)],
            'cVolume': [u16(data, 0x62 + 2 * i) for i in range(11)],
            'cPitch': [u16(data, 0x78 + 2 * i) for i in range(11)],
            'cTempo': u16(data, 0x8E)}


def validate(data, max_ticks=DEFAULT_MAX_TICKS):
    errs, warns = [], []
    if len(data) < 0xB6:
        return ['文件过短'], []
    if u16(data, 0x2C) == 0 or u16(data, 0x2E) == 0:
        errs.append('tickBeat / beatMeasure 不能为 0')
    try:
        r = scan_rol(data)
    except Exception as e:
        return [f'解析失败：{e}'], []
    if r['end'] != len(data):
        errs.append(f'解析未闭环 {r["end"]}/{len(data)}')
    for name, cnt, act in (('cTicks', r['cTicks'], [v['nticks'] for v in r['voices']]),
                           ('cTimbre', r['cTimbre'], [len(v['timbre']) for v in r['voices']]),
                           ('cVolume', r['cVolume'], [len(v['volume']) for v in r['voices']]),
                           ('cPitch', r['cPitch'], [len(v['pitch']) for v in r['voices']])):
        if list(cnt) != list(act):
            errs.append(f'{name} 与轨道不符：{cnt} vs {act}')
    if r['cTempo'] != len(r['tempo']):
        errs.append(f'cTempo {r["cTempo"]} != {len(r["tempo"])}')
    for v in r['voices']:
        if v['sum'] != v['nticks']:
            errs.append(f"ch{v['idx']} 时值和 {v['sum']} != nticks {v['nticks']}")
        for kind, evs in (('音色', [(e[0], 0) for e in v['timbre']]),
                          ('音量', v['volume']), ('音高', v['pitch'])):
            ts = [e[0] for e in evs]
            if ts != sorted(ts):
                errs.append(f"ch{v['idx']} {kind}事件未升序")
            for at in ts:
                if v['nticks'] == 0 and at != 0:
                    errs.append(f"ch{v['idx']} nticks=0 却有 tick={at} 的{kind}事件")
                elif at > v['nticks']:
                    errs.append(f"ch{v['idx']} {kind}事件 tick={at} > nticks={v['nticks']}")
    top = max((v['nticks'] for v in r['voices']), default=0)
    if top > max_ticks:
        warns.append(f'最长声部 {top} tick，超过上限 {max_ticks}')
    return errs, warns


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(
        description='Softstar RIX → AdLib ROL 转换（默认输出 Composer 能打开的时间轴）')
    ap.add_argument('input', help='.rix 文件')
    ap.add_argument('-o', '--out', help='输出目录（默认 <源文件名>_ROL）')
    ap.add_argument('--max-ticks', type=int, default=DEFAULT_MAX_TICKS,
                    help=f'时间轴上限（默认 {DEFAULT_MAX_TICKS}）')
    ap.add_argument('--tick-ms', type=float, help='强制每 tick 的毫秒数')
    ap.add_argument('--bpm', type=float, help='指定 BPM')
    ap.add_argument('--tick-beat', type=int, help='指定每拍 tick 数')
    ap.add_argument('--beat-measure', type=int, default=4)
    ap.add_argument('--scale-x', type=int, default=56)
    ap.add_argument('--scale-y', type=int, default=48)
    a = ap.parse_args(argv)

    src = a.input
    if not src.lower().endswith('.rix'):
        print('输入必须是 .rix 文件', file=sys.stderr)
        return 2
    try:
        rix = Rix(src)
    except Exception as e:
        print(f'读取失败：{e}', file=sys.stderr)
        return 2

    conv = convert(rix, bpm=a.bpm, tick_beat=a.tick_beat,
                   beat_measure=a.beat_measure, scale_x=a.scale_x, scale_y=a.scale_y)
    voices = conv['voices']
    src_mspt = conv['tick_ms']
    total_ms = conv['total_ms']
    scaled = padded = 0

    if max(v['nticks'] for v in voices) > a.max_ticks or a.tick_ms:
        if a.tick_ms:
            mspt = float(a.tick_ms)
            _, bpm, tb = _fit_bpm(mspt)
            conv['exact'] = total_ms / mspt <= a.max_ticks
        else:
            mspt, bpm, tb, exact = choose_timing(
                collect_times(voices, src_mspt), total_ms,
                a.bpm, a.tick_beat, max_ticks=a.max_ticks)
            conv['exact'] = exact
        if abs(mspt - src_mspt) > 1e-9:
            conv['voices'] = voices = [rescale_voice(v, src_mspt, mspt) for v in voices]
            conv['tick_ms'], conv['bpm'], conv['tick_beat'] = mspt, bpm, tb
            scaled = 1

    padded = fit_event_bounds(voices)
    data = build_rol(conv)

    stem = os.path.splitext(os.path.basename(src))[0]
    outdir = a.out or os.path.join(os.path.dirname(os.path.abspath(src)), stem + '_ROL')
    os.makedirs(outdir, exist_ok=True)
    rol_path = os.path.join(outdir, stem + '.ROL')
    open(rol_path, 'wb').write(data)

    ins_files = {}
    for inst in rix.insts:
        fn = inst_bank_name(inst) + '.ins'
        blob = inst_to_ins_blob(rix.data, inst)
        open(os.path.join(outdir, fn), 'wb').write(blob)
        ins_files[fn] = blob

    total_ticks = max(v['nticks'] for v in voices)
    print(f"输入 RIX : {src}  ({rix.size} 字节, {len(rix.insts)} 个音色)")
    print(f"输出 ROL : {rol_path}  ({len(data)} 字节)")
    print(f"输出 INS : {len(ins_files)} 个 → {outdir}")
    print(f"\n时间基准 : {conv['tick_ms']:g} ms/tick = {conv['bpm']:g} BPM × "
          f"{conv['tick_beat']} tick/拍，每小节 {a.beat_measure} 拍"
          + ("" if not scaled else f"（原 {src_mspt:g} ms/tick，已重定标）"))
    print(f"总 tick  : {total_ticks} = {total_ms} ms = "
          f"{total_ticks / conv['tick_beat'] / a.beat_measure:.2f} 小节"
          f"（上限 {a.max_ticks}）")
    if conv['exact'] and not scaled:
        print("量化误差 : 无（tick 网格整除全部时刻）")
    else:
        print(f"量化误差 : 最大 {conv['tick_ms'] / 2:.2f} ms（重定标到更粗的网格）")
    if padded:
        print(f"结构修正 : {padded} 个声部的事件超出轨道长度，已用休止补齐")
    print(f"模式     : {'节奏/打击乐 (11 声道)' if conv['is_melodic'] == 0 else '旋律 (9 声道)'}"
          f"   isMelodic={conv['is_melodic']}  rhythm={rix.rhythm}")
    print("\n ch  nticks 音符事件 音色切点 音量 音高   乐器")
    for v in voices:
        insts = ' → '.join(f"{e['name']}#{e['index']}" for e in v['timbre']) or '—'
        print(f" {v['idx']:2d}  {v['nticks']:6d} {len(v['notes']):7d} {len(v['timbre']):7d} "
              f"{len(v['volume']):4d} {len(v['pitch']):4d}   {insts}")

    errs, warns = validate(data, a.max_ticks)
    print(f"\n结构校验 : {'PASS（0 长度轨道无越界事件、计数器与时值和自洽）' if not errs else 'FAIL'}")
    for e in errs[:6]:
        print('   ✘', e)
    for w in warns[:2]:
        print('   !', w)

    print(f"\n用 ROLRIX.EXE 再转回 RIX 时的参数：")
    print(f"  Input .ROL Filename: {os.path.basename(rol_path)}")
    print(f"  Input .INS Path    : {outdir}")
    print(f"\n想在 Composer 里打开：File → Open 选 {os.path.basename(rol_path)}；")
    print( "  若提示找不到乐器，用 File → Bank File 载入包含这些乐器的 .BNK。")
    return 0 if not errs else 3


if __name__ == '__main__':
    sys.exit(main())
