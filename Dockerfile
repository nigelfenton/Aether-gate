#
# Aether-gate — container image.
# Copyright (C) 2026 Nigel Fenton (G0JKN). GPL-3.0-or-later.
#
# Two targets, mirroring the --no-sdr split deploy/install-pi.sh already makes,
# because the gate's dependencies are per-adapter rather than global:
#
#   lan  (default)  Icom LAN rigs + sim. numpy only, no native libraries.
#   full            adds hamlib (CAT rigs) and SoapySDR + rtl-sdr-blog (dongles).
#
#   docker build --target lan  -t aether-gate:lan  .
#   docker build --target full -t aether-gate:full .
#
# Multi-arch: amd64 and arm64 (a Pi 5 is the natural home for this).
#
# RUN IT WITH HOST NETWORKING (--network host, or network_mode: host). This is a
# requirement, not a preference: discovery is a UDP broadcast to 255.255.255.255,
# AE unicasts back to the advertised address, and the gate opens the VITA-49
# stream toward AE's ephemeral port. Bridge NAT breaks all three, so the radio
# either never appears in AE's chooser or appears and carries no data. That also
# makes this image Linux-only -- Docker Desktop on macOS/Windows does not give a
# container the host's real broadcast domain. See docs/DOCKER.md.

# ---------------------------------------------------------------- lan -------
FROM python:3.13-slim-trixie AS lan

# Debian 13 (trixie) + Python 3.13 is the stack deploy/install-pi.sh pins the Pi
# appliance against, so the container and the bare-metal appliance agree.
LABEL org.opencontainers.image.title="aether-gate" \
      org.opencontainers.image.description="Put any radio into AetherSDR" \
      org.opencontainers.image.source="https://github.com/nigelfenton/Aether-gate" \
      org.opencontainers.image.licenses="GPL-3.0-or-later"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN pip install --no-cache-dir numpy

WORKDIR /app
COPY aether_gate/ /app/aether_gate/
COPY LICENSE README.md /app/

# Unprivileged: every port the gate binds is above 1024 (4991/4992 data, 873x
# control panel), so it never needs root.
RUN useradd --uid 10001 --create-home gate && chown -R gate:gate /app
USER gate

# Documentation only -- host networking ignores published ports.
#   4992/udp discovery + control/data   4991/udp AE's dax_tx TX audio
#   8731/tcp control panel              4992/tcp AE control connection
EXPOSE 4992/udp 4992/tcp 4991/udp 8731/tcp

# EXEC FORM IS LOAD-BEARING. It makes Python PID 1, so `docker stop`'s SIGTERM
# reaches the handler in __main__.py, which closes the adapter and sends the
# RS-BA1 0x05 disconnect that releases the radio's session. Under shell form
# /bin/sh would be PID 1, swallow the signal, and every stop would strand a
# phantom session that blocks the next start. Pair with stop_grace_period.
ENTRYPOINT ["python", "-m", "aether_gate"]
CMD []

# --------------------------------------------------------------- full -------
FROM lan AS full
USER root

# Pins copied verbatim from deploy/install-pi.sh -- the versions proven on the
# Pi5 appliance. The apt librtlsdr does not drive an RTL-SDR V4 properly, which
# is why the blog fork is built from source there and here.
ARG RTLSDR_REPO=https://github.com/rtlsdrblog/rtl-sdr-blog.git
ARG RTLSDR_COMMIT=aed0ea1
ARG SOAPY_REPO=https://github.com/pothosware/SoapySDR.git
ARG SOAPY_COMMIT=1551ea0
ARG SOAPYRTL_REPO=https://github.com/pothosware/SoapyRTLSDR.git
ARG SOAPYRTL_COMMIT=b1f568d

# One RUN: the toolchain has to be gone in the same layer it was added, or the
# image still carries it.
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        build-essential cmake git pkg-config libusb-1.0-0-dev swig \
        python3-dev libhamlib-utils; \
    mkdir -p /tmp/src; cd /tmp/src; \
    git clone "$RTLSDR_REPO" rtl-sdr-blog; \
    cd rtl-sdr-blog; git checkout -q "$RTLSDR_COMMIT"; \
    cmake -B build -DINSTALL_UDEV_RULES=ON -DDETACH_KERNEL_DRIVER=ON; \
    cmake --build build -j"$(nproc)"; cmake --install build; cd /tmp/src; \
    git clone "$SOAPY_REPO" SoapySDR; \
    cd SoapySDR; git checkout -q "$SOAPY_COMMIT"; \
    cmake -B build; cmake --build build -j"$(nproc)"; cmake --install build; cd /tmp/src; \
    git clone "$SOAPYRTL_REPO" SoapyRTLSDR; \
    cd SoapyRTLSDR; git checkout -q "$SOAPYRTL_COMMIT"; \
    cmake -B build; cmake --build build -j"$(nproc)"; cmake --install build; \
    ldconfig; \
    apt-get purge -y build-essential cmake git swig python3-dev; \
    apt-get autoremove -y; \
    rm -rf /var/lib/apt/lists/* /tmp/src

# A dongle also needs the device passed in and access to it, e.g.
#   docker run --network host --device /dev/bus/usb --group-add plugdev ...
USER gate
