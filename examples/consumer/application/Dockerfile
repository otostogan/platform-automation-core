FROM python:3.13.7-alpine3.22@sha256:9ba6d8cbebf0fb6546ae71f2a1c14f6ffd2fdab83af7fa5669734ef30ad48844

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY --chmod=0555 app/app.py /app/bin/app

USER 10001:10001

EXPOSE 3000

ENTRYPOINT ["/app/bin/app"]
CMD ["serve"]
