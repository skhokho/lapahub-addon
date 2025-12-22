ARG BUILD_FROM
FROM $BUILD_FROM

# Install Python and dependencies
RUN apk add --no-cache \
    python3 \
    py3-pip \
    py3-aiohttp \
    bash

# Copy requirements and install Python packages
COPY requirements.txt /tmp/
RUN pip3 install --no-cache-dir --break-system-packages -r /tmp/requirements.txt

# Copy application
COPY rootfs/ /

# Set permissions
RUN chmod a+x /app/run.py

WORKDIR /app

# Run the application directly (init: false disables s6-overlay)
CMD ["python3", "/app/run.py"]
