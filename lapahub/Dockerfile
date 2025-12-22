ARG BUILD_FROM
FROM $BUILD_FROM

# Install additional dependencies
RUN apk add --no-cache bash

# Copy requirements and install Python packages
COPY requirements.txt /tmp/
RUN pip3 install --no-cache-dir -r /tmp/requirements.txt

# Copy application
COPY rootfs/app /app

# Set permissions
RUN chmod a+x /app/run.py

WORKDIR /app

CMD ["python3", "/app/run.py"]
