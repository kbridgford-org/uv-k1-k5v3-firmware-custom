FROM ubuntu:22.04

ARG QEMU_REV=b67b00e6b4c7831a3f5bc684bc0df7a9bfd1bd56

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential ca-certificates git libfdt-dev libglib2.0-dev \
        libpixman-1-dev ninja-build patch pkg-config python3 \
        python3-flask zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

COPY upstream/ /opt/uvk5/
COPY patches/ /tmp/patches/
WORKDIR /opt/uvk5

RUN git init /tmp/qemu \
    && git -C /tmp/qemu remote add origin https://gitlab.com/qemu-project/qemu.git \
    && git -C /tmp/qemu fetch --depth 1 origin "${QEMU_REV}" \
    && git -C /tmp/qemu checkout --detach FETCH_HEAD \
    && test "$(git -C /tmp/qemu rev-parse HEAD)" = "${QEMU_REV}" \
    && patch --batch -d /opt/uvk5 -p1 < /tmp/patches/uvk5-emulator-runtime.patch \
    && QEMU_SRC=/tmp/qemu bash /opt/uvk5/tools/setup_qemu.sh \
    && cp /tmp/qemu/build/qemu-system-arm /usr/local/bin/qemu-system-arm \
    && rm -rf /tmp/qemu

ENTRYPOINT ["python3", "/opt/uvk5/tools/webui.py"]
CMD ["--host", "0.0.0.0", "--port", "8080", "--qmp", "/tmp/uvk5-qmp.sock", "--qemu", "/usr/local/bin/qemu-system-arm"]
