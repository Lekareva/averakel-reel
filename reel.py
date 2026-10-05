#!/usr/bin/env python3
"""averakel-reel — монтаж рилсов блога @averakel (ffmpeg + Python).

Команды:
  sheet   <папка|файлы...> [out.jpg]     — лист-превью клипов (имя, время съёмки, длина), чтобы Ольга выбрала
  montage plan.json                      — «чистый» рилс: стабилизация, цвет «природа», длинные куски, мягкие переходы
  voice   clean.mp4 voice.m4a phrases.txt out.mp4 [ambient]
                                         — закадровый голос (шумодав + выравнивание громкости) и субтитры по фразам

plan.json:
{
  "out": "reel_clean.mp4",
  "order": "plan",          # "plan" — как в списке, "time" — по времени съёмки
  "xfade": 0.5,             # растворение между кусками, сек (0 = встык)
  "ambient": 0.0,           # громкость родного звука 0..1
  "stabilize": true,        # стабилизация (vidstab, 2 прохода)
  "color": "nature",        # "nature" | "soft" | "none"
  "strength": 1.0,          # сила цвета «nature», 0.5–1.5
  "fit": "crop",            # горизонтальные клипы: "crop" (обрезать до 9:16) | "blur" (размытый фон)
  "clips": [
    {"file": "VID_1.mp4", "start": 1.0, "dur": 5.0},
    {"file": "VID_2.mp4", "start": 0, "dur": 4, "speed": 0.5},   # замедление (лучше из 60 fps)
    {"file": "IMG_3.jpg", "dur": 3.5}                             # фото = медленный наезд
  ]
}
Требуется: ffmpeg с libvidstab, Python 3 + Pillow. Шрифт: assets/fonts/Unbounded.ttf (OFL).
"""
import json, os, re, subprocess, sys, tempfile
from datetime import datetime
from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
FONT = os.path.join(HERE, "assets", "fonts", "Unbounded.ttf")
W, H, FPS = 1080, 1920, 30
PINE = (47, 74, 58); CREAM = (245, 239, 226)
SDR = ["-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709"]
PHOTO_EXT = (".jpg", ".jpeg", ".png", ".heic", ".webp")


def run(c): subprocess.run(c, check=True)


def ffprobe(path, entries):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", entries, "-of", "json", path],
                       capture_output=True, text=True)
    try: return json.loads(r.stdout)
    except Exception: return {}


def info(path):
    j = ffprobe(path, "format=duration,format_name:format_tags=creation_time:stream=codec_type,color_transfer,width,height,r_frame_rate:stream_tags=creation_time:stream_side_data=rotation")
    fmt = j.get("format", {}); st = j.get("streams", [])
    v = next((s for s in st if s.get("codec_type") == "video"), {})
    photo = path.lower().endswith(PHOTO_EXT) or "pipe" in fmt.get("format_name", "") or "image2" in fmt.get("format_name", "")
    rot = 0
    for sd in v.get("side_data_list", []) or []:
        if "rotation" in sd: rot = int(sd["rotation"])
    w, h = v.get("width", 0), v.get("height", 0)
    if abs(rot) == 90: w, h = h, w
    ct = (fmt.get("tags", {}) or {}).get("creation_time") or (v.get("tags", {}) or {}).get("creation_time")
    if not ct and photo:
        try:
            ex = Image.open(path).getexif(); ct = ex.get(36867) or ex.get(306)
            if ct: ct = datetime.strptime(ct, "%Y:%m:%d %H:%M:%S").isoformat()
        except Exception: pass
    num, den = (v.get("r_frame_rate", "30/1").split("/") + ["1"])[:2]
    return {"photo": photo, "dur": float(fmt.get("duration", 0) or 0), "w": w, "h": h,
            "hdr": v.get("color_transfer") in ("arib-std-b67", "smpte2084"),
            "audio": any(s.get("codec_type") == "audio" for s in st),
            "time": ct or datetime.fromtimestamp(os.path.getmtime(path)).isoformat(),
            "fps": float(num) / float(den or 1) if float(den or 1) else 30}


# ---------- цвет ----------

def color_chain(mode, k=1.0):
    if mode == "none": return "null"
    if mode == "soft":
        return "eq=contrast=0.97:saturation=1.06:gamma=1.03,colorbalance=rs=0.03:bs=-0.03:rm=0.02:bm=-0.02"
    # «nature»: небо голубее, листва желтее/краснее, зелень зеленее; кожу не трогаем сильно
    s = lambda x: round(x * k, 3)
    return ",".join([
        f"huesaturation=saturation={s(0.30)}:intensity={s(0.02)}:colors=b+c:strength=1",   # небо, вода
        f"huesaturation=saturation={s(0.28)}:colors=y:strength=1",                          # жёлтая листва
        f"huesaturation=saturation={s(0.22)}:colors=r:strength=1",                          # красная листва, рябина
        f"huesaturation=saturation={s(0.20)}:colors=g:strength=1",                          # хвоя, трава
        f"vibrance=intensity={s(0.12)}",                                                    # мягкая общая сочность
        "eq=contrast=1.04:gamma=1.01",
        "colorbalance=rh=0.02:bh=-0.02",                                                    # чуть тёплые света
    ])


def geom(fit):
    if fit == "blur":
        return (f"split[a][b];[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},boxblur=30:3,eq=brightness=-0.08[bg];"
                f"[b]scale={W}:{H}:force_original_aspect_ratio=decrease[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2")
    return f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H}"


TONEMAP = "zscale=t=linear:npl=100,format=gbrpf32le,tonemap=hable,zscale=t=bt709:m=bt709:r=tv,format=yuv420p,"


def render_piece(c, i, tmp, plan):
    o = os.path.join(tmp, f"p{i:02d}.mp4"); f = c["file"]; inf = info(f)
    dur = float(c.get("dur", 5)); speed = float(c.get("speed", 1.0))
    col = color_chain(plan.get("color", "nature"), float(plan.get("strength", 1.0)))
    silent = ["-f", "lavfi", "-t", str(dur), "-i", "anullsrc=r=48000:cl=stereo"]
    if inf["photo"]:
        n = int(dur * FPS)
        vf = (f"scale={W*2}:{H*2}:force_original_aspect_ratio=increase,crop={W*2}:{H*2},"
              f"zoompan=z='1+0.0008*on':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d={n}:s={W}x{H}:fps={FPS},{col},format=yuv420p")
        run(["ffmpeg", "-v", "error", "-y", "-loop", "1", "-t", str(dur), "-i", f, *silent,
             "-filter_complex", f"[0:v]{vf}[v]", "-map", "[v]", "-map", "1:a", "-t", str(dur), "-r", str(FPS),
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "17", "-c:a", "aac", *SDR, o])
        return o, dur
    src_dur = dur * speed                       # сколько исходника нужно
    start = float(c.get("start", 0))
    pre = TONEMAP if inf["hdr"] else ""
    stab = ""
    if plan.get("stabilize", True):
        trf = os.path.join(tmp, f"t{i}.trf")
        run(["ffmpeg", "-v", "error", "-y", "-ss", str(start), "-t", str(src_dur), "-i", f,
             "-vf", f"{pre}vidstabdetect=shakiness=6:accuracy=12:result={trf}", "-f", "null", "-"])
        stab = f"vidstabtransform=input={trf}:smoothing=20:optzoom=1:zoomspeed=0.2:interpol=bicubic,unsharp=5:5:0.6:3:3:0.3,"
    slow = f"setpts={1/speed:.4f}*PTS," if speed != 1.0 else ""
    vf = f"{pre}{stab}{slow}{geom(plan.get('fit', 'crop'))},fps={FPS},{col},format=yuv420p"
    amb = float(plan.get("ambient", 0))
    if inf["audio"] and amb > 0:
        atempo = f"atempo={max(speed,0.5)}," if speed != 1.0 else ""
        ain, amap = [], ["-map", "0:a:0"]
        af = ["-af", f"{atempo}volume={amb},aresample=48000,aformat=channel_layouts=stereo"]
    else:
        ain, amap, af = silent, ["-map", "1:a"], []
    run(["ffmpeg", "-v", "error", "-y", "-ss", str(start), "-t", str(src_dur), "-i", f, *ain,
         "-filter_complex", f"[0:v]{vf}[v]", "-map", "[v]", *amap, *af, "-t", str(dur), "-r", str(FPS),
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "17", "-c:a", "aac", *SDR, o])
    return o, dur


def montage(plan_path):
    plan = json.load(open(plan_path)); base = os.path.dirname(os.path.abspath(plan_path))
    clips = plan["clips"]
    for c in clips:
        if not os.path.isabs(c["file"]): c["file"] = os.path.join(base, c["file"])
    if plan.get("order") == "time":
        clips = sorted(clips, key=lambda c: info(c["file"])["time"])
    xf = float(plan.get("xfade", 0.5)); tmp = tempfile.mkdtemp()
    pieces = []
    for i, c in enumerate(clips):
        print(f"[{i+1}/{len(clips)}] {os.path.basename(c['file'])}", flush=True)
        pieces.append(render_piece(c, i, tmp, plan))
    out = plan["out"] if os.path.isabs(plan["out"]) else os.path.join(base, plan["out"])
    enc = ["-c:v", "libx264", "-preset", "medium", "-b:v", "7M", "-maxrate", "8M", "-bufsize", "16M",
           "-pix_fmt", "yuv420p", "-c:a", "aac", "-movflags", "+faststart", *SDR, out]
    if xf <= 0 or len(pieces) == 1:
        lst = os.path.join(tmp, "l.txt"); open(lst, "w").write("".join(f"file '{p}'\n" for p, _ in pieces))
        run(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", lst, *enc])
    else:
        ins, fc, off, vl, al = [], [], 0.0, "[0:v]", "[0:a]"
        for p, _ in pieces: ins += ["-i", p]
        for k in range(1, len(pieces)):
            off += pieces[k - 1][1] - xf
            fc += [f"{vl}[{k}:v]xfade=transition=fade:duration={xf}:offset={off:.3f}[v{k}]",
                   f"{al}[{k}:a]acrossfade=d={xf}[a{k}]"]
            vl, al = f"[v{k}]", f"[a{k}]"
        run(["ffmpeg", "-v", "error", "-y", *ins, "-filter_complex", ";".join(fc), "-map", vl, "-map", al, *enc])
    print("OK", out, round(info(out)["dur"], 1), "сек")


# ---------- лист-превью ----------

def sheet(paths, out="sheet.jpg"):
    files = []
    for p in paths:
        if os.path.isdir(p): files += [os.path.join(p, x) for x in sorted(os.listdir(p))]
        else: files.append(p)
    rows = []
    for f in files:
        try: inf = info(f)
        except Exception: continue
        if inf["dur"] == 0 and not inf["photo"]: continue
        rows.append((inf["time"], f, inf))
    rows.sort()
    tw, th = 216, 384; cols = 6; tmp = tempfile.mkdtemp()
    S = Image.new("RGB", (cols * tw, ((len(rows) + cols - 1) // cols) * (th + 40)), "white")
    fnt = ImageFont.truetype(FONT, 16) if os.path.exists(FONT) else ImageFont.load_default()
    for i, (t, f, inf) in enumerate(rows):
        j = os.path.join(tmp, f"{i}.jpg")
        ss = [] if inf["photo"] else ["-ss", str(inf["dur"] * 0.4)]
        subprocess.run(["ffmpeg", "-v", "error", "-y", *ss, "-i", f, "-frames:v", "1", "-vf", f"scale={tw}:{th}:force_original_aspect_ratio=decrease", j])
        x, y = (i % cols) * tw, (i // cols) * (th + 40)
        try: S.paste(Image.open(j), (x, y))
        except Exception: pass
        d = ImageDraw.Draw(S)
        lab = f"#{i+1} {t[11:19]} " + ("фото" if inf["photo"] else f"{inf['dur']:.0f}с {inf['fps']:.0f}fps")
        d.text((x + 4, y + th + 4), lab, font=fnt, fill=(0, 0, 0))
        d.text((x + 4, y + th + 21), os.path.basename(f)[:26], font=fnt, fill=(90, 90, 90))
    S.save(out, quality=85); print("OK", out, len(rows), "файлов")


# ---------- голос и субтитры ----------

def speech_segments(audio, noise="-32dB", min_sil=0.35):
    err = subprocess.run(["ffmpeg", "-hide_banner", "-i", audio, "-af", f"silencedetect=n={noise}:d={min_sil}", "-f", "null", "-"],
                         capture_output=True, text=True).stderr
    total = info(audio)["dur"]
    starts = [float(x) for x in re.findall(r"silence_start: ([\d.]+)", err)]
    ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", err)]
    segs, cur = [], 0.0
    for s, e in zip(starts, ends + [total] * (len(starts) - len(ends))):
        if s - cur > 0.15: segs.append([cur, s])
        cur = e
    if total - cur > 0.15: segs.append([cur, total])
    return segs, total


def fit(segs, phrases):
    if len(segs) == len(phrases): return segs
    a, b = segs[0][0], segs[-1][1]; L = sum(len(p) for p in phrases); t, out = a, []
    for p in phrases:
        d = (b - a) * len(p) / L; out.append([t, t + d]); t += d
    print(f"! фраз {len(phrases)}, речевых отрезков {len(segs)} — тайминг по длине текста, проверить")
    return out


def sub_png(text, path):
    im = Image.new("RGBA", (W, H), (0, 0, 0, 0)); d = ImageDraw.Draw(im)
    f = ImageFont.truetype(FONT, 50); lines, cur = [], ""
    for w in text.split():
        t = (cur + " " + w).strip()
        if d.textbbox((0, 0), t, font=f)[2] > W - 220 and cur: lines.append(cur); cur = w
        else: cur = t
    lines.append(cur); lh = 74; bh = lh * len(lines) + 44
    bw = max(d.textbbox((0, 0), l, font=f)[2] for l in lines) + 80
    x0, y0 = (W - bw) // 2, int(H * 0.72)
    d.rounded_rectangle((x0, y0, x0 + bw, y0 + bh), 28, fill=PINE + (210,))
    for i, l in enumerate(lines):
        b = d.textbbox((0, 0), l, font=f)
        d.text(((W - b[2]) // 2, y0 + 22 + i * lh - b[1] + 6), l, font=f, fill=CREAM)
    im.save(path)


def voice(clean, audio, phrases_txt, out, ambient=0.0):
    tmp = tempfile.mkdtemp()
    vo = os.path.join(tmp, "vo.wav")   # шумодав + выравнивание громкости голоса
    run(["ffmpeg", "-v", "error", "-y", "-i", audio, "-vn", "-af",
         "highpass=f=80,afftdn=nf=-25,loudnorm=I=-16:TP=-1.5:LRA=11", "-ar", "48000", vo])
    phrases = [l.strip() for l in open(phrases_txt, encoding="utf-8") if l.strip()]
    segs, total = speech_segments(vo); times = fit(segs, phrases)
    vdur = info(clean)["dur"]
    if vdur < total: print(f"! видео ({vdur:.1f}с) короче голоса ({total:.1f}с) — добавить клипов")
    ins, chain, last = [], [], "[0:v]"
    for i, (p, (a, b)) in enumerate(zip(phrases, times)):
        png = os.path.join(tmp, f"s{i}.png"); sub_png(p, png); ins += ["-i", png]
        chain.append(f"{last}[{i+2}:v]overlay=0:0:enable='between(t,{a:.2f},{b+0.15:.2f})'[o{i}]"); last = f"[o{i}]"
    amix = f"[0:a]volume={ambient}[bg];[1:a]volume=1.0[vo];[bg][vo]amix=inputs=2:duration=longest:normalize=0[a]"
    end = min(vdur, total + 0.8)
    run(["ffmpeg", "-v", "error", "-y", "-i", clean, "-i", vo, *ins,
         "-filter_complex", ";".join(chain + [amix]), "-map", last, "-map", "[a]", "-t", f"{end:.2f}",
         "-c:v", "libx264", "-preset", "medium", "-b:v", "7M", "-maxrate", "8M", "-bufsize", "16M",
         "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", *SDR, out])
    for p, (a, b) in zip(phrases, times): print(f"{a:6.2f}–{b:6.2f}  {p}")
    print("OK", out)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "montage": montage(sys.argv[2])
    elif cmd == "sheet": sheet(sys.argv[2:-1] or sys.argv[2:], sys.argv[-1] if sys.argv[-1].endswith(".jpg") and len(sys.argv) > 3 else "sheet.jpg")
    elif cmd == "voice": voice(*sys.argv[2:6], float(sys.argv[6]) if len(sys.argv) > 6 else 0.0)
    else: print(__doc__)
