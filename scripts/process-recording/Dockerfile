FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py .
# Vendored, cloud-free shared segmentation core (``screencap.segmentation``),
# assembled into the build context by the deploy step (see main.py docstring's
# "vendor" line). Only the ``segmentation`` subpackage plus a MINIMAL
# ``screencap/__init__.py`` are vendored — NOT the whole ``screencap`` package
# (its real __init__ calls importlib.metadata.version("screencap"), which is not
# pip-installed here, and it would drag in recorder/daemon/engine + heavy deps).
COPY screencap ./screencap
CMD ["functions-framework", "--target=process_recording", "--signature-type=cloudevent"]
