# Clip archiver — bounded-context service that listens for TP-label
# NOTIFYs and pulls the corresponding RTSP playback clip to local disk
# before the NVR's FIFO rotation eats the source footage.
#
# Runtime dependencies:
#   - ffmpeg (RTSP demux + mp4 mux, -c copy so no libx264 needed)
#   - psycopg (LISTEN/NOTIFY + DB reads for alert ts/camera lookup)
#
# NO YOLO / OpenCV / detector runtime — this service never touches a
# live stream, it only pulls the NVR's own recorded footage on demand.
#
# Image size: ~250 MB (Debian slim + ffmpeg + psycopg).

FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY docker/archiver/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Only the modules the archiver actually imports — clip_archiver +
# listener + the cv2-free playback_url helper. We do NOT copy the
# rest of src/stream (rtsp_handler imports cv2) or src/detector — the
# archiver never touches a live camera.
COPY src/__init__.py ./src/__init__.py
COPY src/archiver ./src/archiver
RUN mkdir -p src/stream
COPY src/stream/__init__.py ./src/stream/__init__.py
COPY src/stream/playback_url.py ./src/stream/playback_url.py

RUN mkdir -p clips

ENV PYTHONUNBUFFERED=1

CMD ["python", "-m", "src.archiver.listener"]
