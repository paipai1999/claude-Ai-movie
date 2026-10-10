# ==============================================================================
# Pai AI Movie Studio - Production Multi-Platform Dockerfile
# Supports: Linux (x86_64, aarch64), Windows Docker Desktop, macOS Apple Silicon
# ==============================================================================

FROM python:3.11-slim-bookworm

# Prevent Python from writing .pyc files and enable unbuffered logging
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive \
    PORT=5000

# Install required system packages: FFmpeg, OpenGL, SoundFile libs, FontConfig
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    git \
    curl \
    fontconfig \
    fonts-noto-core \
    libgl1 \
    libglib2.0-0 \
    libsndfile1 \
    procps \
    && rm -rf /var/lib/apt/lists/*

# Set workspace directory
WORKDIR /app

# Install Python dependencies first for caching layers
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy application source code
COPY . .

# Install Myanmar Padauk font into system fonts directory for libass subtitle engine
RUN mkdir -p /usr/share/fonts/truetype/padauk && \
    if [ -f "assets/fonts/Padauk.ttf" ]; then cp assets/fonts/Padauk.ttf /usr/share/fonts/truetype/padauk/; fi && \
    fc-cache -f -v > /dev/null 2>&1 || true

# Ensure runtime directories exist
RUN mkdir -p movies outputs temp assets/fonts

# Expose Web UI default port
EXPOSE 5000

# Liveness probe (the endpoint is public by design and reveals nothing but "auth required?")
HEALTHCHECK --interval=60s --timeout=5s --start-period=60s --retries=3 \
    CMD curl -fsS http://127.0.0.1:5000/api/auth/status || exit 1

# Start Pai AI Movie Studio Web Dashboard
CMD ["python", "web_ui.py", "--host", "0.0.0.0", "--port", "5000"]
