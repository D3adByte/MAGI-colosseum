FROM python:3.12-alpine
RUN adduser -D -u 10001 fixture
WORKDIR /app
COPY --chown=fixture:fixture server.py /app/server.py
USER fixture
EXPOSE 31337
CMD ["python", "/app/server.py"]
