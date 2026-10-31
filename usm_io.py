"""CRI USM 的流式读取、VP9 解密、封装及结构校验"""

from fractions import Fraction
import hashlib
from pathlib import Path

from wannacri import usm
from wannacri.usm.chunk import UsmChunk
from wannacri.usm.media.video import GenericVideo
from wannacri.usm.tools import decrypt_video_packet, generate_keys
from wannacri.usm.types import ChunkType, PayloadType


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def chunks(path: Path, encoding: str = "cp932", prefix_only: bool = False):
    """逐块读取"""
    size = path.stat().st_size
    with path.open("rb") as stream:
        while stream.tell() < size:
            offset = stream.tell()
            header = stream.read(32)
            if len(header) != 32:
                raise ValueError(f"USM 块头截断：{path.name} @ {offset}")
            length = int.from_bytes(header[4:8], "big") + 8
            if length < 32 or offset + length > size:
                raise ValueError(f"USM 块越界：{path.name} @ {offset}")
            if prefix_only and header[15] & 3 == PayloadType.STREAM.value:
                return
            raw = header + stream.read(length - 32)
            yield offset, raw, UsmChunk.from_bytes(raw, encoding=encoding)


def inspect(path: Path, encoding: str = "cp932") -> dict:
    """读取容器头，这个不需要解码的密钥"""
    with path.open("rb") as stream:
        if stream.read(4) != b"CRID":
            raise ValueError(f"不是 CRI USM：{path.name}")
    videos, others = [], set()
    for _, _, chunk in chunks(path, encoding, prefix_only=True):
        if chunk.chunk_type not in (ChunkType.INFO, ChunkType.VIDEO):
            others.add(chunk.chunk_type.name)
        if chunk.chunk_type == ChunkType.VIDEO and chunk.payload_type == PayloadType.HEADER:
            page = chunk.payload[0]
            fps = Fraction(page["framerate_n"].val, page["framerate_d"].val)
            frames = int(page["total_frames"].val)
            if fps <= 0 or frames <= 0:
                raise ValueError(f"无效视频时长：{path.name}")
            videos.append({"channel": chunk.channel_number, "width": page["width"].val,
                           "height": page["height"].val, "fps": str(fps), "frames": frames,
                           "duration": frames / float(fps), "codec_id": page["mpeg_codec"].val})
    if not videos:
        raise ValueError(f"没有视频轨：{path.name}")
    return {"videos": videos, "other_tracks": sorted(others)}


def decrypt(path: Path, destination: Path, key: int, encoding: str = "cp932") -> dict:
    """只接受单轨 VP9；其他轨道不能被静默丢弃。"""
    info = inspect(path, encoding)
    if len(info["videos"]) != 1 or info["other_tracks"] or info["videos"][0]["codec_id"] != 9:
        raise ValueError(f"需要无附加轨道的单轨 VP9：{path.name}")
    video_key, _ = generate_keys(key)
    channel = info["videos"][0]["channel"]
    frames = 0
    with destination.open("wb") as stream:
        for _, _, chunk in chunks(path, encoding):
            if chunk.chunk_type == ChunkType.VIDEO and chunk.payload_type == PayloadType.STREAM:
                if chunk.channel_number != channel:
                    raise ValueError("发现未声明的视频轨")
                stream.write(decrypt_video_packet(chunk.payload, video_key))
                frames += 1
            elif chunk.chunk_type not in (ChunkType.INFO, ChunkType.VIDEO):
                raise ValueError("发现附加轨道，拒绝丢弃")
    if frames != info["videos"][0]["frames"]:
        raise ValueError("USM 实际帧数与容器头不符")
    return info["videos"][0]


def normalize(path: Path) -> None:
    """修正 WannaCRI 0.3.3 的总文件大小及元数据长度"""
    prefix = list(chunks(path, "utf-8", prefix_only=True))
    metadata = {}
    for _, raw, chunk in prefix:
        if chunk.chunk_type == ChunkType.VIDEO and chunk.payload_type == PayloadType.METADATA:
            metadata[chunk.channel_number] = metadata.get(chunk.channel_number, 0) + len(raw)
    patches = []
    for offset, raw, chunk in prefix:
        changed = False
        if chunk.chunk_type == ChunkType.INFO:
            for page in chunk.payload:
                if page["chno"].val == -1:
                    page.update("filesize", page["filesize"].type, path.stat().st_size)
                    changed = True
        elif chunk.chunk_type == ChunkType.VIDEO and chunk.payload_type == PayloadType.HEADER:
            page = chunk.payload[0]
            page.update("metadata_size", page["metadata_size"].type, metadata[chunk.channel_number])
            changed = True
        if changed:
            packed = chunk.pack()
            if len(packed) != len(raw):
                raise ValueError("修正容器头将改变 seek 偏移，拒绝写入")
            patches.append((offset, packed))
    with path.open("r+b") as stream:
        for offset, raw in patches:
            stream.seek(offset)
            stream.write(raw)


def mux(ivf: Path, destination: Path, key: int) -> None:
    """将 IVF 加密封装；显式统一 CRID fmtver，兼容当前 WannaCRI"""
    video = usm.Vp9(str(ivf))
    if len(video) == 1:
        # WannaCRI 0.3.3 的单帧分支漏算 IVF 的 32 字节文件头，导致尾部截断
        # 用公开的 GenericVideo 提供完整载荷
        def single_frame():
            with ivf.open("rb") as stream:
                yield stream.read(), True
        size = ivf.stat().st_size
        page, header = video.crid_page, video.header_page
        page.update("minbuf", page["minbuf"].type, size)
        header.update("ixsize", header["ixsize"].type, 0x18 + size + (-size % 0x20))
        video = GenericVideo(stream_factory=single_frame, crid_page=page, header_page=header,
                             length=1, channel_number=0)
    movie = usm.Usm(videos=[video], key=key, version=video.crid_page["fmtver"].val)
    with destination.open("wb") as stream:
        for packet in movie.stream(usm.OpMode.ENCRYPT):
            stream.write(packet)
    normalize(destination)


def validate_structure(path: Path, encoding: str = "cp932") -> dict:
    frames, headers, metadata, seeks = {}, {}, {}, {}
    found_root = False
    for offset, raw, chunk in chunks(path, encoding):
        if chunk.chunk_type == ChunkType.INFO:
            for page in chunk.payload:
                if page["chno"].val == -1:
                    found_root = True
                    if page["filesize"].val != path.stat().st_size:
                        raise ValueError("CRID 总大小不符")
        elif chunk.chunk_type == ChunkType.VIDEO:
            channel = chunk.channel_number
            if chunk.payload_type == PayloadType.HEADER:
                headers[channel] = chunk.payload[0]
            elif chunk.payload_type == PayloadType.STREAM:
                frames.setdefault(channel, []).append(offset)
            elif chunk.payload_type == PayloadType.METADATA:
                count, size = metadata.get(channel, (0, 0))
                metadata[channel] = (count + 1, size + len(raw))
                seeks.setdefault(channel, []).extend(chunk.payload)
    if not found_root or not headers or set(frames) != set(headers):
        raise ValueError("USM 缺少必要的头或视频块")
    for channel, header in headers.items():
        positions = frames[channel]
        count, size = metadata.get(channel, (0, 0))
        if (header["total_frames"].val != len(positions)
                or header["metadata_size"].val != size or header["metadata_count"].val != count):
            raise ValueError("USM 帧数或元数据长度不符")
        for page in seeks.get(channel, []):
            index = page["ofs_frmid"].val
            if not 0 <= index < len(positions) or page["ofs_byte"].val != positions[index]:
                raise ValueError("USM seek 未指向正确的视频帧")
    return {"valid": True, "frames": sum(len(v) for v in frames.values())}
