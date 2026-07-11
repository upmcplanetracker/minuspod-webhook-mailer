FROM python:3.13-alpine

# No third-party deps — stdlib only (http.server, smtplib, json)

RUN addgroup -S mailer && adduser -S mailer -G mailer
WORKDIR /app
COPY mailer.py .

USER mailer

EXPOSE 8080

ENTRYPOINT ["python3", "/app/mailer.py"]
