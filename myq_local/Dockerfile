FROM ghcr.io/home-assistant/base@sha256:93ef607824e3f27e868f11b10938283a98bf880ed57bcf8eaa81c6c2d521f6f5

ARG BUILD_VERSION
ARG BUILD_ARCH

LABEL \
    io.hass.version="${BUILD_VERSION}" \
    io.hass.type="app" \
    io.hass.arch="${BUILD_ARCH}"

# Lock the entire installed package set, including transitive dependencies.
COPY apk.lock /app/apk.lock
RUN xargs apk add --no-cache < /app/apk.lock

WORKDIR /app
COPY bridge.py /app/bridge.py
COPY run.sh /run.sh
RUN chmod 0755 /run.sh

CMD ["/run.sh"]
