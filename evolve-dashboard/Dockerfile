ARG BUILD_FROM
FROM $BUILD_FROM

# Only the Python stdlib is used, so the runtime stays small and the image
# builds without a wheel toolchain on every architecture.
RUN apk add --no-cache python3

COPY app /opt/momentum
COPY run.sh /run.sh
RUN chmod a+x /run.sh

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

CMD [ "/run.sh" ]
