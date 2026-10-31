#!/usr/bin/env python3

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from fractions import Fraction
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time

from dotenv import dotenv_values
import usm_io

HERE = Path(__file__).resolve().parent
STOP = threading.Event()
REVISION = 3


def atomic_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                     delete=False, suffix=".tmp") as stream:
        temporary = Path(stream.name)
        try:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def config() -> dict:
    values = dotenv_values(HERE / ".env", interpolate=False)
    return {name: (os.environ.get(name, values.get(name)) or "").strip()
            for name in ("DEFAULT_A000", "DEFAULT_KEY", "DEFAULT_BITRATE", "DEFAULT_FPS")}


def parse_key(value: str) -> int:
    try:
        text = value.strip()
        key = int(text, 16 if text.lower().startswith("0x") else 10)
        if not 0 <= key < 2**64:
            raise ValueError()
        return key
    except ValueError as exc:
        raise ValueError("密钥必须是十进制或 0x 开头的 uint64 整数") from exc


def run(command: list[str]) -> str:
    """不用 shell；中断批处理时同时终止 FFmpeg，不等待整个编码结束。"""
    if STOP.is_set():
        raise RuntimeError("任务已取消")
    with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, encoding="utf-8", errors="replace") as process:
        try:
            while True:
                if STOP.is_set():
                    raise RuntimeError("任务已取消")
                try:
                    stdout, stderr = process.communicate(timeout=0.5)
                    break
                except subprocess.TimeoutExpired:
                    continue
        except BaseException:
            process.terminate()
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
            raise
        if process.returncode:
            raise RuntimeError(f"{command[0]} 退出 {process.returncode}：{stderr[-4000:]}")
        return stdout


def require_tools() -> None:
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            raise RuntimeError(f"Cant't find {tool} in PATH")
    if "libvpx-vp9" not in run(["ffmpeg", "-hide_banner", "-encoders"]):
        raise RuntimeError("libvpx-vp9 is missing!!!")


def probe(path: Path) -> dict:
    data = json.loads(run(["ffprobe", "-v", "error", "-count_packets", "-select_streams", "v:0",
                           "-show_streams", "-of", "json", str(path)]))
    if not data.get("streams"):
        raise ValueError("Wrong key")
    stream = data["streams"][0]
    fps = Fraction(stream["r_frame_rate"])
    count = int(stream.get("nb_read_packets", 0))
    if stream["codec_name"] != "vp9" or fps <= 0 or count <= 0:
        raise ValueError("需要有效的 VP9 IVF 视频")
    return {"width": stream["width"], "height": stream["height"], "fps": str(fps),
            "frames": count, "duration": count / float(fps),
            "color_space": stream.get("color_space", "unknown"),
            "color_range": stream.get("color_range", "unknown")}


def decode_check(path: Path) -> None:
    run(["ffmpeg", "-v", "error", "-xerror", "-err_detect", "explode", "-threads", "1",
         "-i", str(path), "-f", "null", "-"])


def verify(path: Path, key: int, scratch: Path, encoding: str,
           expected_ivf: str | None = None) -> dict:
    usm_io.validate_structure(path, encoding)
    decrypted = scratch / "verified.ivf"
    source = usm_io.decrypt(path, decrypted, key, encoding)
    if expected_ivf is not None and usm_io.sha256(decrypted) != expected_ivf:
        raise ValueError("USM 加解密往返摘要不符")
    video = probe(decrypted)
    if (video["frames"] != source["frames"] or video["fps"] != source["fps"]
            or video["width"] != source["width"] or video["height"] != source["height"]):
        raise ValueError("USM 与 IVF 视频参数不符")
    decode_check(decrypted)
    return {**video, "bytes": path.stat().st_size,
            "kbps": kbps(path.stat().st_size, video["duration"]), "valid": True}


def kbps(size: int, duration: float) -> float:
    return size * 8 / duration / 1000 if duration > 0 else 0.0


def next_budget(current: float, actual: float, target: float) -> float:
    return min(current * 0.9, current * target / actual * 0.95)


def scale_widths(source_width: int, settings: dict) -> list[int]:
    floor = min(source_width, max(math.ceil(source_width * settings["min_scale"] / 2) * 2,
                                  settings["min_width"]))
    start = min(source_width, settings["width"] or source_width)
    if start < floor:
        raise ValueError("--width 低于自动缩放下限；请增大宽度或使用 --no-auto-scale")
    widths = [start]
    for ratio in (0.9, 0.8, 0.7, 0.6, 0.5, 0.4, 0.3, 0.2, 0.1, 0):
        width = max(floor, math.ceil(source_width * ratio / 2) * 2)
        if width < widths[-1]:
            widths.append(width)
    return widths


def encode_ladder(original: Path, scratch: Path, settings: dict, info: dict,
                  target: float, tolerance: float, key: int) -> tuple:
    """优先逼近封装后码率，调低预算无效时缩小；始终保留最低实际码率版本。"""
    attempts = []
    best = None
    widths = (scale_widths(info["width"], settings) if settings["auto_scale"]
              else [min(info["width"], settings["width"] or info["width"])])
    level, at_width, budget = 0, 0, target
    stop_reason = "attempt_limit"
    for attempt in range(1, settings["max_attempts"] + 1):
        width = widths[level]
        active = {**settings, "width": width}
        active["height"] = (math.ceil(info["height"] * width / info["width"] / 2) * 2
                            if width < info["width"] else info["height"])
        encoded = scratch / f"attempt-{attempt}.ivf"
        candidate = scratch / f"attempt-{attempt}.dat"
        # 每一档都从原片编码，要不然会重复压缩
        encode(original, encoded, scratch, active, info, budget)
        encoded_info = probe(encoded)
        verify_encoded(encoded_info, info, active)
        usm_io.mux(encoded, candidate, key)
        size = candidate.stat().st_size
        actual = kbps(size, encoded_info["duration"])
        row = {"attempt": attempt, "encoder_kbps": budget, "actual_kbps": actual,
               "bytes": size, "width": encoded_info["width"], "height": encoded_info["height"],
               "fps": encoded_info["fps"], "scale": width / info["width"]}
        previous_best = best
        if best is None or actual < best[0]["actual_kbps"]:
            best = (row, candidate, encoded, active)
        # 只保留最佳候选与当前统计，防止多轮编码积累大量临时文件
        if previous_best is not None and best is not previous_best:
            previous_best[1].unlink()
            previous_best[2].unlink()
        if best[1] != candidate:
            candidate.unlink()
            encoded.unlink()
        attempts.append(row)
        at_width += 1
        print(f"{width}×{encoded_info['height']} 第 {attempt} 轮：预算 {budget:.1f}，实际 {actual:.1f} kbps，"
              f"{size / 1e6:.2f} MB", flush=True)
        if actual <= target * (1 + tolerance):
            stop_reason = "target_met"
            break
        plateau = (at_width >= 2 and actual >= attempts[-2]["actual_kbps"] * 0.98)
        if at_width >= 2 and level + 1 < len(widths):
            level += 1
            at_width, budget = 0, target
        elif plateau:
            stop_reason = "minimum_size_plateau" if settings["auto_scale"] else "scaling_disabled_plateau"
            break
        else:
            budget = max(1.0, next_budget(budget, actual, target))
    row, candidate, encoded, active = best
    selection = {"attempt": row["attempt"], "reason": "lowest_actual_bitrate", "stop_reason": stop_reason,
                 "budget_policy": "best_effort", "budget_met": row["actual_kbps"] <= target * (1 + tolerance),
                 "target_ratio": row["actual_kbps"] / target}
    return candidate, encoded, active, attempts, selection


def scan(a000: Path, encoding: str, ids: list[int] | None = None) -> dict:
    folder = a000 / "MovieData"
    if not folder.is_dir():
        raise ValueError(f"资源目录缺少 MovieData：{a000}")
    files = sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() == ".dat")
    if not files:
        raise ValueError(f"没有找到 .dat：{folder}")
    if ids:
        available = {int(p.stem) for p in files if p.stem.isdecimal()}
        if set(ids) - available:
            raise ValueError(f"视频 ID 不存在：{sorted(set(ids) - available)}")
        files = [p for p in files if p.stem.isdecimal() and int(p.stem) in ids]
    movies = []
    for path in files:
        record = {"name": path.name, "source": str(path), "bytes": path.stat().st_size,
                  "system": not path.stem.isdecimal()}
        try:
            info = usm_io.inspect(path, encoding)
            record.update(info)
            if len(info["videos"]) == 1:
                video = info["videos"][0]
                record["duration"] = video["duration"]
                record["kbps"] = kbps(record["bytes"], record["duration"])
        except (ValueError, KeyError, UnicodeError, OSError, ZeroDivisionError) as exc:
            record["error"] = str(exc)
        movies.append(record)
    return {"a000": str(a000), "files": len(movies),
            "source_bytes": sum(m["bytes"] for m in movies), "movies": movies}


def encode(source: Path, output: Path, scratch: Path, settings: dict,
           info: dict, bitrate: float) -> None:
    filters = []
    if settings["fps"] and Fraction(info["fps"]) > settings["fps"]:
        filters.append(f"fps={settings['fps']}")
    if settings["width"] and info["width"] > settings["width"]:
        filters.extend([f"scale={settings['width']}:{settings.get('height', -2)}:flags=lanczos", "setsar=1"])
    fps = min(float(Fraction(info["fps"])), settings["fps"]) if settings["fps"] else float(Fraction(info["fps"]))
    base = ["ffmpeg", "-hide_banner", "-v", "error", "-nostdin", "-y", "-threads", "1",
            "-i", str(source), "-an"]
    if filters:
        base.extend(["-vf", ",".join(filters)])
    base.extend(["-c:v", "libvpx-vp9", "-b:v", str(round(bitrate * 1000)),
                 "-qmin", "0", "-qmax", str(settings["max_quantizer"]),
                 "-deadline", "good", "-cpu-used", str(settings["speed"]),
                 "-threads", str(settings["threads"]), "-row-mt", "1", "-pix_fmt", "yuv420p",
                 "-g", str(max(1, round(fps * settings["gop"]))),
                 "-passlogfile", str(scratch / "vp9-pass")])
    # 保留源视频的颜色标记，避免把不同矩阵的 PV 一律误标为 BT.601
    if info["color_space"] not in ("unknown", "reserved", None):
        base.extend(["-colorspace", info["color_space"]])
    if info["color_range"] in ("tv", "pc"):
        base.extend(["-color_range", info["color_range"]])
    run(base + ["-pass", "1", "-f", "null", "-"])
    run(base + ["-pass", "2", "-f", "ivf", str(output)])


def verify_encoded(info: dict, source: dict, settings: dict) -> None:
    expected_fps = min(Fraction(source["fps"]), Fraction(settings["fps"])) if settings["fps"] else Fraction(source["fps"])
    expected_width = min(source["width"], settings["width"]) if settings["width"] else source["width"]
    if info["width"] != expected_width or Fraction(info["fps"]) != expected_fps:
        raise ValueError("输出分辨率或帧率不符")
    if not settings["width"] and info["height"] != source["height"]:
        raise ValueError("输出高度被意外修改")
    if settings.get("height") and info["height"] != settings["height"]:
        raise ValueError("输出高度不符")
    if abs(info["duration"] - source["duration"]) > 1 / float(expected_fps) + 0.002:
        raise ValueError("输出时长与原视频相差超过一帧")


def process_one(movie: dict, args, settings: dict) -> dict:
    source = Path(movie["source"])
    digest = usm_io.sha256(source)
    fingerprint = hashlib.sha256(json.dumps({"source": digest, "settings": settings}, sort_keys=True).encode()).hexdigest()
    destination = args.output / "MovieData" / source.name
    record_path = args.output / "records" / (source.name + ".json")
    if destination.exists() and not args.force:
        if record_path.is_file():
            old = json.loads(record_path.read_text(encoding="utf-8"))
            if old.get("fingerprint") == fingerprint and old.get("sha256") == usm_io.sha256(destination):
                return {**old, "resumed": True}
        raise ValueError("输出已存在，但来源/参数/摘要不符；请另选输出目录或使用 --force")
    if movie.get("error"):
        raise ValueError(movie["error"])
    scratch_root = args.output / ".tmp"
    scratch_root.mkdir(parents=True, exist_ok=True)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=source.stem + "-", dir=scratch_root) as temporary:
        scratch = Path(temporary)
        candidate = scratch / "candidate.dat"
        attempts = []
        selection = None
        video = movie["videos"][0] if len(movie["videos"]) == 1 else None
        reason = None
        if movie["system"] and not args.include_system:
            reason = "system_preserved"
        elif video is None or movie["other_tracks"] or video["codec_id"] != 9:
            if args.unsupported == "error":
                raise ValueError("附加轨道或非单轨 VP9，不支持压缩")
            reason = "unsupported_preserved"
        elif video["duration"] < settings["min_duration"]:
            reason = "short_clip_preserved"
        controlled = reason is None
        verification = None
        source_info = None
        if video is not None and not movie["other_tracks"] and video["codec_id"] == 9:
            original = scratch / "original.ivf"
            usm_io.decrypt(source, original, args.key, args.encoding)
            source_info = probe(original)
            if (source_info["frames"] != video["frames"] or source_info["fps"] != video["fps"]
                    or source_info["width"] != video["width"] or source_info["height"] != video["height"]):
                raise ValueError("源视频 USM/IVF 参数不符")
            decode_check(original)
        if (controlled and movie["kbps"] <= args.bitrate * (1 + args.tolerance)
                and (not settings["fps"] or Fraction(source_info["fps"]) <= settings["fps"])
                and (not settings["width"] or source_info["width"] <= settings["width"])):
            reason = "already_within_budget"
        if reason:
            if usm_io.sha256(source) != digest:
                raise ValueError("处理期间源文件发生变化")
            shutil.copyfile(source, candidate)
            if source_info is not None:
                usm_io.validate_structure(candidate, args.encoding)
                verification = {**source_info, "valid": True, "copied_original": True}
            # 不认识的轨道原样保留，明确只声明逐字节校验，不能声称完成了解码验证。
            validation = "full_decode" if verification else "byte_copy_only"
        else:
            candidate, encoded, active_settings, attempts, selection = encode_ladder(
                original, scratch, settings, source_info, args.bitrate, args.tolerance, args.key)
            verification = verify(candidate, args.key, scratch, "utf-8", usm_io.sha256(encoded))
            verify_encoded(verification, source_info, active_settings)
            reason, validation = "compressed", "roundtrip_and_full_decode"
            if not selection["budget_met"]:
                print(f"{source.name} 已尽力压缩（{selection['stop_reason']}），保留第 {selection['attempt']} 档，"
                      f"接受 {verification['kbps']:.1f} kbps（高于目标）", flush=True)
        if usm_io.sha256(candidate) != digest and reason != "compressed":
            raise ValueError("原样复制摘要不符")
        # 读取来源摘要后再次确认，避免把正在被外部修改的 PV 标记为可续跑。
        if usm_io.sha256(source) != digest:
            raise ValueError("处理期间源文件发生变化")
        duration = verification["duration"] if verification else movie.get("duration", 0)
        record = {"name": source.name, "source_sha256": digest, "fingerprint": fingerprint,
                  "sha256": usm_io.sha256(candidate), "source_bytes": movie["bytes"],
                  "bytes": candidate.stat().st_size, "duration": duration,
                  "kbps": kbps(candidate.stat().st_size, duration), "controlled": controlled,
                  "status": reason, "validation": validation, "verification": verification,
                  "attempts": attempts, "selection": selection,
                  "settings": settings, "resumed": False}
        if args.force:
            os.replace(candidate, destination)
        else:
            os.link(candidate, destination)
        atomic_json(record_path, record)
        return record


def summarize(records: list[dict], target: float, tolerance: float) -> dict:
    """以总字节/总时长计算整体码率，不能对每首码率做算术平均。"""
    controlled = [r for r in records if r["controlled"]]
    video = [r for r in records if r["duration"] > 0]
    seconds = sum(r["duration"] for r in controlled)
    size = sum(r["bytes"] for r in controlled)
    actual = kbps(size, seconds)
    all_actual = kbps(sum(r["bytes"] for r in video), sum(r["duration"] for r in video))
    unmeasured = [r["name"] for r in records if r["duration"] <= 0]
    over = [r["name"] for r in controlled if r["kbps"] > target * (1 + tolerance)]
    accepted_over = [r["name"] for r in controlled if r["name"] in over
                     and (r.get("selection") or {}).get("budget_policy") in ("soft", "best_effort")]
    return {"files": len(records), "source_bytes": sum(r["source_bytes"] for r in records),
            "output_bytes": sum(r["bytes"] for r in records), "controlled_files": len(controlled),
            "controlled_seconds": seconds, "controlled_kbps": actual,
            "all_video_kbps": all_actual,
            "all_video_budget_met": (all_actual <= target * (1 + tolerance)) if not unmeasured else None,
            "unmeasured_files": unmeasured,
            "preserved_files": [r["name"] for r in records if not r["controlled"]],
            "target_kbps": target, "tolerance": tolerance, "over_budget_files": over,
            "far_over_budget_files": [r["name"] for r in controlled if r["kbps"] > target * 1.25],
            "accepted_over_budget_files": accepted_over,
            "strict_over_budget_files": [name for name in over if name not in accepted_over],
            "budget_met": bool(controlled) and not over and actual <= target * (1 + tolerance)}


def make_settings(args) -> dict:
    from importlib.metadata import version
    return {"revision": REVISION, "bitrate": args.bitrate, "fps": args.fps, "width": args.width,
            "tolerance": args.tolerance, "max_attempts": args.max_attempts,
            "min_duration": args.min_duration, "gop": args.gop, "threads": args.threads,
            "auto_scale": args.auto_scale, "min_width": args.min_width,
            "min_scale": args.min_scale,
            "max_quantizer": args.max_quantizer,
            "speed": args.speed, "encoding": args.encoding, "include_system": args.include_system,
            "unsupported": args.unsupported, "wannacri": version("WannaCRI"),
            "key_id": hashlib.sha256(str(args.key).encode()).hexdigest()}


def compress(args) -> int:
    require_tools()
    inventory = scan(args.a000, args.encoding, args.ids)
    movies = inventory["movies"][:args.limit] if args.limit else inventory["movies"]
    atomic_json(args.output / "inventory.json", inventory)
    settings = make_settings(args)
    results, failures = [], []
    start, cancelled = time.monotonic(), False
    print(f"处理 {len(movies)} 个文件，目标 {args.bitrate:g} kbps，输出 {args.output / 'MovieData'}", flush=True)
    pool = ThreadPoolExecutor(max_workers=args.workers)
    try:
        futures = {pool.submit(process_one, m, args, settings): m for m in movies}
        for future in as_completed(futures):
            movie = futures[future]
            try:
                result = future.result()
                results.append(result)
                print(f"[{len(results) + len(failures)}/{len(movies)}] {movie['name']} "
                      f"{result['status']} {result['bytes'] / 1e6:.2f} MB "
                      f"{result['kbps']:.1f} kbps" + ("（复用）" if result["resumed"] else ""), flush=True)
            except Exception as exc:
                failures.append({"name": movie["name"], "error": str(exc),
                                 "attempts": getattr(exc, "attempts", [])})
                print(f"失败 {movie['name']}：{exc}", file=sys.stderr, flush=True)
    except KeyboardInterrupt:
        cancelled = True
        STOP.set()
        print("正在停止编码；已完成文件可用相同命令续跑", flush=True)
    finally:
        pool.shutdown(wait=True, cancel_futures=cancelled)
    summary = summarize(results, args.bitrate, args.tolerance)
    report = {"complete": not cancelled and not failures and len(results) == len(movies),
              "cancelled": cancelled, "requested": len(movies), "failed": len(failures),
              "requested_files": [m["name"] for m in movies],
              "elapsed_seconds": time.monotonic() - start, "summary": summary,
              "errors": failures, "movies": sorted(results, key=lambda r: r["name"])}
    atomic_json(args.output / "compress_report.json", report)
    print(f"完成 {len(results)}/{len(movies)}，输出 {summary['output_bytes'] / 1e9:.3f} GB；"
          f"视频整体 {summary['controlled_kbps']:.1f} kbps，"
          f"含保留文件 {summary['all_video_kbps']:.1f} kbps，失败 {len(failures)}", flush=True)
    print(f"码率超标但已接受 {len(summary['accepted_over_budget_files'])} 个", flush=True)
    if summary["far_over_budget_files"]:
        print(f"警告：{len(summary['far_over_budget_files'])} 个文件仍超过目标 25%，"
              "请查看报告中的实际码率和停止原因，可增加 --max-attempts 或降低 --min-width", flush=True)
    return 130 if cancelled else (1 if failures else 0)


def check(args) -> int:
    require_tools()
    folder = args.output / "MovieData"
    files = sorted(p for p in folder.glob("*") if p.is_file() and p.suffix.lower() == ".dat")
    if not files:
        raise ValueError("输出目录中没有 .dat")
    results, failures = [], []
    expected_names = None
    report_path = args.output / "compress_report.json"
    if report_path.is_file():
        previous = json.loads(report_path.read_text(encoding="utf-8"))
        expected_names = set(previous.get("requested_files", [])) or (
            {r["name"] for r in previous["movies"]} | {r["name"] for r in previous["errors"]})
    for path in files:
        try:
            record = json.loads((args.output / "records" / (path.name + ".json")).read_text(encoding="utf-8"))
            if record["sha256"] != usm_io.sha256(path):
                raise ValueError("摘要不符")
            if record["validation"] != "byte_copy_only":
                with tempfile.TemporaryDirectory(prefix="mai-pv-check-") as temporary:
                    info = verify(path, args.key, Path(temporary), args.encoding)
                record.update(bytes=info["bytes"], duration=info["duration"], kbps=info["kbps"])
            results.append(record)
            print(f"通过 {path.name}：{record['kbps']:.1f} kbps（{record['validation']}）", flush=True)
        except Exception as exc:
            failures.append({"name": path.name, "error": str(exc)})
    if expected_names:
        for name in sorted(expected_names - {p.name for p in files}):
            failures.append({"name": name, "error": "生成物缺失"})
    summary = summarize(results, args.bitrate, args.tolerance)
    atomic_json(args.output / "check_report.json", {"summary": summary, "errors": failures,
                                                  "valid": not failures})
    print(f"整体 {summary['controlled_kbps']:.1f} kbps，超标 {len(summary['over_budget_files'])}，错误 {len(failures)}")
    if summary["accepted_over_budget_files"]:
        print(f"其中 {len(summary['accepted_over_budget_files'])} 个已保留最佳结果；完整性检查通过不代表码率达标")
    return 1 if failures else 0


def main(argv=None) -> int:
    STOP.clear()
    defaults = config()
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("scan", "compress", "check"):
        p = sub.add_parser(command)
        p.add_argument("--output", type=Path, default=HERE / "output")
        p.add_argument("--encoding", default="cp932", help="原容器字符串编码")
        if command != "check":
            p.add_argument("--a000", type=Path)
            p.add_argument("--ids", nargs="+", type=int)
        if command != "scan":
            p.add_argument("--key", type=parse_key)
            p.add_argument("--bitrate", type=float, default=defaults["DEFAULT_BITRATE"] or "400", help="封装后目标平均码率，kbps")
            p.add_argument("--tolerance", type=float, default=0.02, help="允许超出目标的比例，默认 0.02")
        if command == "compress":
            p.add_argument("--fps", type=int, default=defaults["DEFAULT_FPS"] or "30", help="帧率上限，0 保持原帧率")
            p.add_argument("--width", type=int, default=0, help="首轮最大宽度，0 从原尺寸开始")
            p.add_argument("--auto-scale", action=argparse.BooleanOptionalAction, default=True,
                           help="优先调低码率，仍超标则逐级缩小；--no-auto-scale 禁止缩小")
            p.add_argument("--min-width", type=int, default=400, help="自动缩放的最小宽度，默认 400")
            p.add_argument("--min-scale", type=float, default=0.0, help="额外的尺寸保留比例下限，默认 0，仅受 min-width 限制")
            p.add_argument("--max-quantizer", type=int, default=63, help="量化上限，默认 63，允许更强压缩；降低此值会增大体积")
            p.add_argument("--workers", type=int, default=1)
            p.add_argument("--threads", type=int, default=4, help="每个编码器的线程数")
            p.add_argument("--speed", type=int, default=4, help="VP9 编码速度 0..5，低值更慢")
            p.add_argument("--gop", type=float, default=4, help="关键帧间隔秒数")
            p.add_argument("--max-attempts", type=int, default=24, help="最多编码轮次，默认 24，达标即停")
            p.add_argument("--min-duration", type=float, default=2, help="更短的视频原样保留")
            p.add_argument("--limit", type=int)
            p.add_argument("--include-system", action="store_true", help="也尝试压缩非数字命名的系统视频")
            p.add_argument("--unsupported", choices=("copy", "error"), default="copy")
            p.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.ERROR)
    try:
        args.output = args.output.expanduser().resolve()
        if args.command != "scan":
            if args.key is None:
                if not defaults["DEFAULT_KEY"]:
                    raise ValueError("请设置 DEFAULT_KEY 或 --key")
                args.key = parse_key(defaults["DEFAULT_KEY"])
            if not (math.isfinite(args.bitrate) and 16 <= args.bitrate <= 100000
                    and math.isfinite(args.tolerance) and 0 <= args.tolerance <= 0.5):
                raise ValueError("bitrate 应为 16..100000，tolerance 应为 0..0.5")
        if args.command != "check":
            if args.a000 is None:
                if not defaults["DEFAULT_A000"]:
                    raise ValueError("请设置 DEFAULT_A000 或 --a000")
                args.a000 = Path(defaults["DEFAULT_A000"]).expanduser()
                if not args.a000.is_absolute():
                    args.a000 = HERE / args.a000
            args.a000 = args.a000.expanduser().resolve()
            if (args.output == args.a000 or args.a000 in args.output.parents
                    or args.output in args.a000.parents):
                raise ValueError("输出目录必须与源资源目录分离")
        if args.command == "scan":
            report = scan(args.a000, args.encoding, args.ids)
            atomic_json(args.output / "inventory.json", report)
            errors = sum("error" in m for m in report["movies"])
            print(f"文件 {report['files']}，源体积 {report['source_bytes'] / 1e9:.3f} GB，头信息错误 {errors}")
            return 1 if errors else 0
        if args.command == "check":
            return check(args)
        if not (0 <= args.fps <= 120 and (args.width == 0 or 64 <= args.width <= 4096 and args.width % 2 == 0)
                and 64 <= args.min_width <= 4096 and args.min_width % 2 == 0
                and math.isfinite(args.min_scale) and 0 <= args.min_scale <= 1
                and 0 <= args.max_quantizer <= 63
                and 1 <= args.workers <= 16 and 1 <= args.threads <= 64 and 0 <= args.speed <= 5
                and 1 <= args.max_attempts <= 40 and math.isfinite(args.gop) and 0.1 <= args.gop <= 60
                and math.isfinite(args.min_duration) and 0 <= args.min_duration <= 60
                and (args.limit is None or args.limit > 0)):
            raise ValueError("编码参数超出允许范围，请查看 compress --help")
        return compress(args)
    except (ValueError, OSError, RuntimeError, KeyError, UnicodeError, ZeroDivisionError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
