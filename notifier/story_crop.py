"""Conservatively remove a centered, baked-in border from TikTok story videos."""

import io
import json
import logging
import os
import shutil
import subprocess
import tempfile

from PIL import Image

log = logging.getLogger("notifier.story_crop")


def _hidden_process_flags():
    return subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def video_dimensions(data):
    """Read the actual encoded dimensions without trusting a source's metadata."""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None
    with tempfile.TemporaryDirectory(prefix="tiktok_probe_") as temp_dir:
        source = os.path.join(temp_dir, "source.mp4")
        with open(source, "wb") as file:
            file.write(data)
        try:
            result = subprocess.run(
                [ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries",
                 "stream=width,height", "-of", "json", source], capture_output=True,
                check=True, timeout=20, creationflags=_hidden_process_flags(),
            )
            stream = json.loads(result.stdout)["streams"][0]
            return int(stream["width"]), int(stream["height"])
        except (OSError, ValueError, KeyError, IndexError, subprocess.SubprocessError):
            return None


def _inset_box(frame):
    """Find a centered 9:16 picture against a mostly uniform side border."""
    width, height = frame.size
    if abs(width / height - 9 / 16) > 0.04:
        return None

    sample_width = 288
    sample_height = round(height * sample_width / width)
    image = frame.convert("RGB").resize((sample_width, sample_height))
    pixels = image.load()
    row_counts = [0] * sample_height
    column_counts = [0] * sample_width

    for y in range(sample_height):
        background = pixels[0, y]
        for x in range(sample_width):
            color = pixels[x, y]
            if sum(abs(color[i] - background[i]) for i in range(3)) > 30:
                row_counts[y] += 1
                column_counts[x] += 1

    active_rows = [y for y, count in enumerate(row_counts) if count > sample_width * 0.1]
    active_columns = [x for x, count in enumerate(column_counts) if count > sample_height * 0.1]
    if not active_rows or not active_columns:
        return None

    left, right = active_columns[0], active_columns[-1] + 1
    top, bottom = active_rows[0], active_rows[-1] + 1
    margins = (left, sample_width - right, top, sample_height - bottom)
    if min(margins[0:2]) < sample_width * 0.08 or min(margins[2:4]) < sample_height * 0.08:
        return None
    if abs(margins[0] - margins[1]) > sample_width * 0.05:
        return None
    if abs(margins[2] - margins[3]) > sample_height * 0.05:
        return None

    inset_width, inset_height = right - left, bottom - top
    if abs(inset_width / inset_height - width / height) > 0.04:
        return None

    scale_x, scale_y = width / sample_width, height / sample_height
    x0 = round(left * scale_x)
    y0 = round(top * scale_y)
    x1 = round(right * scale_x)
    y1 = round(bottom * scale_y)
    center_x, center_y = (x0 + x1) / 2, (y0 + y1) / 2
    crop_width = min(x1 - x0, round((y1 - y0) * width / height))
    crop_height = min(y1 - y0, round(crop_width * height / width))
    crop_width -= crop_width % 2
    crop_height -= crop_height % 2
    if crop_width < 2 or crop_height < 2:
        return None
    x = round(center_x - crop_width / 2)
    y = round(center_y - crop_height / 2)
    return x, y, crop_width, crop_height


def fill_story_video(data, duration=None, upload_limit=None):
    """Return (video_bytes, dimensions) or (original_bytes, None)."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        log.warning("ffmpeg not found; story video keeps its original frame")
        return data, None

    with tempfile.TemporaryDirectory(prefix="tiktok_story_") as temp_dir:
        source = os.path.join(temp_dir, "source.mp4")
        output = os.path.join(temp_dir, "filled.mp4")
        with open(source, "wb") as file:
            file.write(data)

        sample_at = min(3.0, max(0.2, float(duration or 3) / 3))
        try:
            frame_result = subprocess.run(
                [ffmpeg, "-hide_banner", "-loglevel", "error", "-ss", str(sample_at),
                 "-i", source, "-frames:v", "1", "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1"],
                capture_output=True, check=True, timeout=30, creationflags=_hidden_process_flags(),
            )
            frame = Image.open(io.BytesIO(frame_result.stdout))
            box = _inset_box(frame)
            if box is None:
                return data, None

            width, height = frame.size
            x, y, crop_width, crop_height = box
            output_width, output_height = 1080, 1920
            subprocess.run(
                [ffmpeg, "-hide_banner", "-loglevel", "error", "-i", source,
                 "-vf", f"crop={crop_width}:{crop_height}:{x}:{y},scale={output_width}:{output_height}:flags=lanczos,setsar=1",
                 "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
                 "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", "-y", output],
                capture_output=True, check=True, timeout=180, creationflags=_hidden_process_flags(),
            )
            with open(output, "rb") as file:
                filled = file.read()
            if upload_limit and len(filled) > upload_limit:
                log.warning("cropped story exceeds Telegram upload limit; using original video")
                return data, None
            log.info("cropped story inset %dx%d at (%d,%d) to fill %dx%d frame",
                     crop_width, crop_height, x, y, output_width, output_height)
            return filled, (output_width, output_height)
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            log.warning("story crop failed (%s); using original video", type(error).__name__)
            return data, None
