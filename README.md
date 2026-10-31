# Maimai MovieData Compressor

Compress BGA data (MovieData) for maimai DX custom server's StreamingAssets data.

压缩 maimai DX 数据包中 StreamingAssets 的BGA数据，减小体积占用。

## 运行

需要 Python 3.11+，以及 PATH 中支持 `libvpx-vp9` 的 `ffmpeg`、`ffprobe`

**macOS / Linux：**

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

**Windows（PowerShell）：**

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
```

编辑 `.env` 中的 `DEFAULT_A000` 和 `DEFAULT_KEY`，也可修改 `DEFAULT_BITRATE`、`DEFAULT_FPS`。 Windows 路径可写为如 `D:/maimai/StreamingAssets/A000`


```bash
python compressor.py scan
python compressor.py compress
python compressor.py check
```

Windows 可以将命令中的 `python` 替换为 `.\.venv\Scripts\python.exe`，无需激活环境

## 指令

- `scan`：读取 MovieData 的容器头，生成 `output/inventory.json`，无需密钥
- `compress`：压缩、加密封装并验证，输出到 `output/MovieData`，报告为 `output/compress_report.json`
- `check`：重新检查全部生成物的摘要、容器、解密解码和整体平均码率，报告为 `output/check_report.json`

```bash
# 自定义资源目录；密钥默认读取 .env，也可传 --key
python compressor.py compress --a000 /path/to/A000

# 先压缩指定视频，或使用另一组码率
python compressor.py compress --ids 2059
python compressor.py compress --bitrate 360 --output output-360
python compressor.py check --bitrate 360 --output output-360

# 保持原帧率
python compressor.py compress --fps 0 --output output-original-fps

# 指定目标码率，使用新的输出目录
python compressor.py compress --bitrate 400 --fps 30 --output output-400

# 如需保持尺寸（可能无法达到码率目标）
python compressor.py compress --no-auto-scale --output output-original-size
```

自动缩放参数：`--min-width` 为宽度下限（默认 400）；`--min-scale` 可额外限制保留比例（默认 0，不限制）；`--max-attempts` 为最多编码轮次（默认 24）；`--max-quantizer` 默认为 63，降低此值会限制压缩强度、增大体积

确认效果后，将生成的 `.dat` 复制到目标数据包的 MovieData 文件夹即可

## Reference
【适用于轻量包】将曲绘制作为静态的MoviaData BGA [Maimai Static BGA](https://github.com/star-reader/Maimai-static-bga-convertor)
