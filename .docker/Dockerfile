ARG QGIS_TEST_VERSION=latest
FROM qgis/qgis:${QGIS_TEST_VERSION}

# Install system dependencies
RUN apt-get clean && \
    rm -rf /var/lib/apt/lists/* && \
    apt-get update -o Acquire::Retries=3 && \
    apt-get install -y --no-install-recommends \
        python3-pip \
        xvfb \
        xauth \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Copy and install Python test dependencies
COPY ./requirements_test.txt /tmp/
RUN pip3 install --no-cache-dir -r /tmp/requirements_test.txt --break-system-packages

# Set environment variables
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8
ENV IS_DOCKER_CONTAINER=true
ENV QT_QPA_PLATFORM=offscreen
ENV DISPLAY=:99

WORKDIR /usr/src
