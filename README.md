# minuspod-mailer

A tiny, dependency-free webhook receiver that turns [MinusPod](https://github.com/ttlequals0/MinusPod)'s
default webhook payloads into plain-text emails, relayed through an SMTP
server you already run (e.g. Postfix).

Built for rootless Podman + Quadlet, but the container is plain OCI and
will run under any container runtime.

## Why

MinusPod can POST webhooks on episode processing completion, episode failure, LLM auth failure, exhausted
provider spend limits, and structural per-minute rate-limit errors. If you
just want non-successful events to become an email in your inbox warning you of a problem, this does exactly that.

## How it works

```
MinusPod ─POST /webhook ─▶ minuspod-mailer ─SMTP ─▶ your mail relay ─▶ inbox
```

`minuspod-mailer` listens on one HTTP endpoint (`/webhook`, dual-stack
IPv4/IPv6), parses the `event` field, and formats one of four event types
into an email:

- `Episode Failed`
- `Auth Failure`
- `Limit Exceeded`
- `Rate Limit Structural`

`Episode Processed` (success) and MinusPod's built-in webhook test ping
(`"test": true` in the payload) are intentionally ignored: they're logged
but never emailed, so test-clicks in the MinusPod UI won't spam your inbox.

No authentication or payload signature verification is implemented. This
is meant to run **bridge-internal only** on the same container network as
MinusPod, with no host port published so the webhook endpoint is never
exposed beyond that network. If you need it reachable more broadly, put it
behind a reverse proxy or Cloudflare tunnel and add your own auth in front.

## Requirements

- MinusPod configured to send webhooks with default payloads.
- An SMTP relay reachable from the container (e.g., Postfix, msmtp relay, etc.)
- No TLS/auth is implemented — plain SMTP on your internal network is assumed.
- Podman with Quadlet (or adapt `minuspod-mailer.container` to plain `docker run`/Compose — see below).
- A free port 8080. To check if that is free `ss -tulpn | grep -i "8080"`.  If no output, it is free. If 8080 is being used, search the same way for a free port until you find one with no output. Then edit `Dockerfile`, `mailer.py`, and `env.example` replacing the default `8080` port.

## Quick start (rootless Podman + Quadlet)

```bash
git clone https://github.com/upmcplanetracker/minuspod-mailer.git
cd minuspod-mailer

# Build the image
podman build -t localhost/minuspod-mailer:latest .

# Configure
cp .env.example minuspod-mailer.env
$EDITOR minuspod-mailer.env   # set MAIL_FROM, MAIL_TO, SMTP_HOST, SMTP_PORT

mkdir -p ~/.config/containers/systemd
cp minuspod-mailer.env ~/.config/containers/systemd/
chmod 600 ~/.config/containers/systemd/minuspod-mailer.env

# Edit minuspod-mailer.container's Network= line to match the network your
# MinusPod container is on, then:
cp minuspod-mailer.container ~/.config/containers/systemd/

systemctl --user daemon-reload
systemctl --user start minuspod-mailer
journalctl --user -u minuspod-mailer
# or
# podman logs minuspod-mailer
```

### Firewall note

If your mail relay and MinusPod run on the same host and your container
network is a netavark bridge (not `pasta`), traffic from the container to
the host's SMTP port will look like it's arriving from the bridge subnet,
not `127.0.0.1`. You will likely need a firewall rule scoped to that
subnet, e.g.:

```
sudo ufw allow from <container-bridge-subnet> to any port 25 proto tcp comment 'minuspod-mailer -> smtp relay'
```

Find your bridge subnet with:

```
podman network inspect <your-network-name>
```

### Without Quadlet (plain `podman run` / Docker)

```bash
podman run -d \
  --name minuspod-mailer \
  --network minuspod.network \
  --env-file minuspod-mailer.env \
  --read-only \
  --cap-drop=ALL \
  --security-opt no-new-privileges \
  localhost/minuspod-mailer:latest
```

## Registering the webhook in MinusPod

In MinusPod's Settings > Webhooks:

- **URL**: `http://minuspod-mailer:8080/webhook` (container DNS name, if on
  the same network — otherwise use whatever address/port you've exposed)
- **Events**: check `Episode Failed`, `Auth Failure`, `Limit Exceeded`,
  `Rate Limit Structural`. Leave `Episode Processed` unchecked unless you
  want success emails too (you'll need to add a formatter for it — see
  below).
- **Payload Template**: leave blank (defaults)
- **Content-Type**: `application/json` (default)
- **Secret**: not used by this receiver — leave blank

## Configuration reference

All configuration is via environment variables (see `.env.example`):

| Variable | Required | Default | Description |
|---|---|---|---|
| `MAIL_FROM` | yes | — | From address |
| `MAIL_TO` | yes | — | To address |
| `SMTP_HOST` | no | `host.containers.internal` | SMTP relay hostname |
| `SMTP_PORT` | no | `25` | SMTP relay port |
| `LISTEN_PORT` | no | `8080` | Port the HTTP server listens on |

## Extending

Adding a formatter for a new event type (e.g. `Episode Processed`, or a
future MinusPod event) means adding one function and one dict entry in
`mailer.py`:

```
def fmt_episode_processed(p: dict) -> tuple[str, str]:
    podcast = p.get("podcast", {})
    episode = p.get("episode", {})
    subject = f"[MinusPod] Processed: {episode.get('title')}"
    body = f"Ads removed: {episode.get('ads_removed')}\n..."
    return subject, body

FORMATTERS["Episode Processed"] = fmt_episode_processed
```
