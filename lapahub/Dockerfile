ARG BUILD_FROM
FROM $BUILD_FROM

# Install Python and dependencies
RUN apk add --no-cache \
    python3 \
    py3-pip \
    py3-aiohttp \
    py3-requests

# Copy requirements and install Python packages
COPY requirements.txt /tmp/
RUN pip3 install --no-cache-dir -r /tmp/requirements.txt

# Copy application
COPY rootfs /

# Set permissions
RUN chmod a+x /app/run.py

WORKDIR /app

CMD ["python3", "/app/run.py"]
