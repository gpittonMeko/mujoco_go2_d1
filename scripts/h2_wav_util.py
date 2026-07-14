"""WAV read + PCM stream playback for H2 AudioClient (from unitree g1 audio example)."""
import struct
import time

BYTES_PER_SEC_16K_MONO = 16000 * 2  # 16 kHz, 16-bit mono


def read_wav(filename):
    try:
        with open(filename, "rb") as f:
            def read(fmt):
                return struct.unpack(fmt, f.read(struct.calcsize(fmt)))

            chunk_id, = read("<I")
            if chunk_id != 0x46464952:
                return [], -1, -1, False
            _chunk_size, = read("<I")
            format_tag, = read("<I")
            if format_tag != 0x45564157:
                return [], -1, -1, False

            subchunk1_id, = read("<I")
            subchunk1_size, = read("<I")
            if subchunk1_id == 0x4B4E554A:
                f.seek(subchunk1_size, 1)
                subchunk1_id, = read("<I")
                subchunk1_size, = read("<I")
            if subchunk1_id != 0x20746D66 or subchunk1_size not in (16, 18):
                return [], -1, -1, False

            audio_format, = read("<H")
            if audio_format != 1:
                return [], -1, -1, False
            num_channels, = read("<H")
            sample_rate, = read("<I")
            _byte_rate, = read("<I")
            _block_align, = read("<H")
            bits_per_sample, = read("<H")
            if bits_per_sample != 16:
                return [], -1, -1, False
            if subchunk1_size == 18:
                _extra, = read("<H")

            while True:
                subchunk2_id, subchunk2_size = read("<II")
                if subchunk2_id == 0x61746164:
                    break
                f.seek(subchunk2_size, 1)
            raw = f.read(subchunk2_size)
            return list(raw), sample_rate, num_channels, True
    except Exception as exc:
        print(f"[wav] read error: {exc}")
        return [], -1, -1, False


def play_pcm_stream(
    client,
    pcm_list,
    stream_name="h2_demo",
    sample_rate=16000,
    num_channels=1,
    chunk_size=96000,
    sleep_time=1.0,
):
    """Stream PCM to robot; wait for full playback before returning (caller may PlayStop)."""
    pcm_data = bytes(pcm_list)
    if not pcm_data:
        return 0.0

    bytes_per_sec = sample_rate * num_channels * 2
    duration_s = len(pcm_data) / bytes_per_sec
    stream_id = str(int(time.time() * 1000))
    offset = 0
    idx = 0
    t0 = time.time()

    while offset < len(pcm_data):
        chunk = pcm_data[offset : offset + min(chunk_size, len(pcm_data) - offset)]
        code, _ = client.PlayStream(stream_name, stream_id, chunk)
        if code != 0:
            print(f"[wav] chunk {idx} failed code={code}")
            break
        offset += len(chunk)
        idx += 1
        time.sleep(sleep_time)

    # Do not PlayStop until audio has had time to play (official example uses sleep_time=1.0).
    elapsed = time.time() - t0
    tail_wait = max(0.0, duration_s - elapsed + 0.75)
    if tail_wait > 0:
        print(f"[wav] waiting {tail_wait:.1f}s for playback ({duration_s:.1f}s total)")
        time.sleep(tail_wait)
    return duration_s
