# Deploy the live demo

The goal is Hallway at `https://hallway.debopamchowdhury.com`, invite-only for 10
people, on one 8 GB VPS. It's served through a **Cloudflare Tunnel**, so the server
opens no inbound port except SSH and TLS ends at Cloudflare. Switch, Mattermost,
Postgres and Hallway's privacy guard stay on the private Docker network.

```
phone ──https──> Cloudflare ──tunnel (outbound from the VPS)──> cloudflared ──> hallway:8787
                                                                    (edge network: Hallway only)
                                     hallway ──> switch, mattermost, postgres (Switch's network)
```

Follow the steps in order. Every command runs on the VPS unless it says *laptop*.
Lines that start with `#` are notes, not commands.

---

## 1. Pick and size the VPS

- **8 GB RAM, 4 vCPU, 80 GB+ disk, Ubuntu 24.04 LTS, x86-64 (amd64).** Don't pick
  ARM (Ampere, Graviton, Hetzner CAX): Mattermost only ships amd64 images.
- **A region close to Bengaluru.** DigitalOcean *BLR1* (Bangalore) is the closest. Other
  good choices are AWS Lightsail or Vultr in *Mumbai*, or Hetzner or Linode in *Singapore*.
  Visitors connect to Cloudflare's nearest edge in any case. The region only changes the
  tunnel hop.
- Memory budget on 8 GB, roughly: Mattermost 1–1.5 GB, switch-core ~0.5 GB, Postgres
  ~0.3 GB, gateway ~0.1 GB, Hallway ≤ 512 MB, day pass worker ≤ 384 MB and cloudflared
  ≤ 128 MB (the last three are hard limits in `docker-compose.yml`). That leaves
  several GB free.
- Add your SSH public key when you create the server, so you never need a password.

Write down the server's IP address. Below it's `VPS_IP`.

## 2. SSH hardening basics

*Laptop:* `ssh root@VPS_IP`, then on the VPS:

```bash
# a normal user with sudo, using the same SSH key as root
adduser --disabled-password --gecos "" hallway
usermod -aG sudo hallway
install -d -m 700 -o hallway -g hallway /home/hallway/.ssh
cp /root/.ssh/authorized_keys /home/hallway/.ssh/ && chown hallway:hallway /home/hallway/.ssh/authorized_keys
passwd hallway            # a sudo password (SSH won't accept passwords, sudo asks for it)

# key-only SSH, no root login
cat > /etc/ssh/sshd_config.d/99-hardening.conf <<'EOF'
PermitRootLogin no
PasswordAuthentication no
KbdInteractiveAuthentication no
PubkeyAuthentication yes
EOF
sshd -t && systemctl restart ssh

# firewall: only SSH in
ufw allow OpenSSH
ufw --force enable
ufw status

# 2 GB of swap as a safety net
fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
echo '/swapfile none swap sw 0 0' >> /etc/fstab
```

**Keep this root session open.** In a *second* terminal on the laptop, check that
`ssh hallway@VPS_IP` works. Only then close the root session. From now on, work as `hallway`.

Ubuntu installs security updates on its own (`unattended-upgrades`). You don't need
to open ports 80 or 443: the tunnel connects outbound.

> Docker writes its own iptables rules, and **ufw doesn't filter ports that Docker
> publishes**. That's safe here because every published port (Hallway 8787, Switch 8000,
> gateway 3000, Mattermost 8065, Postgres 5432) is bound to `127.0.0.1`. Don't change
> `SWITCH_BIND_ADDR` or the `127.0.0.1:` in `docker-compose.yml`.

## 3. Install Docker

```bash
curl -fsSL https://get.docker.com -o get-docker.sh
sudo sh get-docker.sh                    # Docker's official install script (Engine + compose plugin)
sudo usermod -aG docker hallway
exit                                     # log out and back in so the group applies
```

*Laptop:* `ssh hallway@VPS_IP` again, then check:

```bash
docker run --rm hello-world && docker compose version
sudo apt-get install -y git curl openssl python3   # up.sh needs these (most are already there)
```

## 4. Clone and bring the demo up

```bash
git clone https://github.com/DebopamParam/Hallway.git
cd Hallway
scripts/up.sh --demo --host hallway.debopamchowdhury.com --personas
```

The first run takes a few minutes, mostly pulling images. It does the following:

- writes `.env` from `.env.demo.example`, with three distinct random keys and a random
  invite code. It sets `SIGNIN_MODE=demo`, `INVITE_LIMIT=10`, `HALLWAY_ENV=production`
  and a $0.25 day pass cap. `.env` is `chmod 600`. **Never commit it.**
- starts Switch 0.31.0 (Postgres, switch-core, Mattermost, gateway), bound to 127.0.0.1
- builds and starts Hallway, and turns on Switch Trust's check through Hallway's guard
- with `--personas`, signs up the six demo personas from `personas/personas.json`.
  Personas don't count towards the 10 invite spots. They sit idle unless you run
  `scripts/personas.py run`, which needs Claude Code on the machine.
- starts the day pass worker, but only if `.env` has a model key (`VERCEL_AI_GATEWAY_API_KEY`
  or `ANTHROPIC_API_KEY`). Without one, the day pass isn't offered.
- prints the **invite link** at the end: `https://hallway.debopamchowdhury.com/?invite=<code>`

It also says `no TUNNEL_TOKEN in .env yet`. That's expected: the next two steps add it.
Check that Hallway is up on the box: `curl -s 127.0.0.1:8787/health` prints `{"ok":true}`.

Re-running `scripts/up.sh --demo` at any time is safe: keys, the invite code and Switch's
secrets are made once and kept.

## 5. Create the Cloudflare Tunnel (dashboard)

`debopamchowdhury.com` must already be on Cloudflare, which it is.

1. Go to <https://dash.cloudflare.com>, then **Networking → Tunnels**. (In the older
   Zero Trust dashboard it's **Zero Trust → Networks → Tunnels**.)
2. **Create a tunnel**. Pick the **Cloudflared** type if asked, name it `hallway-demo`,
   then **Create Tunnel**.
3. On the install screen pick **Docker**. It shows a command like
   `docker run cloudflare/cloudflared:latest tunnel --no-autoupdate run --token eyJh...`.
   **Copy only the token** (the long `eyJ...` string after `--token`). **Don't run that
   command.** Compose runs cloudflared for you, pinned and hardened.
4. Add the route. In the setup flow it's the **Public hostname** step; later it's the
   tunnel's **Routes** tab → **Add route** → **Published application**:
   - **Subdomain:** `hallway`
   - **Domain:** `debopamchowdhury.com`
   - **Path:** leave it empty
   - **Service:** type **HTTP**, URL **`hallway:8787`**. If it's one field, enter
     **`http://hallway:8787`**.

   Use `hallway`, the container name on the tunnel's Docker network, **not** `localhost`.
   Inside the cloudflared container, localhost is cloudflared itself.
5. Save. Cloudflare creates the DNS record (`hallway` CNAME → `<id>.cfargotunnel.com`,
   proxied) for you. If an old `hallway` A/CNAME record exists, delete it first.

Leave everything else at its default. Hallway sends its own security headers (CSP,
HSTS, etc.). Cloudflare's SSL mode doesn't matter for a tunnel.

## 6. Paste TUNNEL_TOKEN and start the tunnel

```bash
cd ~/Hallway
nano .env            # find the line TUNNEL_TOKEN= and paste the token after the =, on one line
scripts/up.sh --demo
docker compose -p hallway logs cloudflared | tail     # look for "Registered tunnel connection" (x4)
```

Within a minute the tunnel shows **Healthy** in the dashboard. cloudflared runs in
its own container on an `edge` network shared only with Hallway, so it can't reach
Switch, Mattermost or Postgres even if the route were mis-typed.

## 7. Smoke test

**From your phone (mobile data, not Wi-Fi, so you test the real path):**

1. Open `https://hallway.debopamchowdhury.com/health`. It should show `{"ok":true}`.
2. Open `https://hallway.debopamchowdhury.com/` **without** the invite and enter an email.
   You should get the *invite only* screen.
3. Open the invite link, sign up, and post a line in the lobby.

**The scripted check (laptop):** this signs up a test attendee through the invite
link, walks every screen, and checks that a visitor without the code is turned away.

```bash
# laptop, in a clone of the repo
python3 -m venv .venv && .venv/bin/pip install playwright httpx && .venv/bin/playwright install chromium
.venv/bin/python scripts/ui_smoke.py /tmp/hallway-shots https://hallway.debopamchowdhury.com --invite <code>
# prints "no problems"; screenshots are in /tmp/hallway-shots
```

`<code>` is the `INVITE_CODE` from the VPS: `grep ^INVITE_CODE= .env`.

**Each test sign-up uses one of the 10 spots**, including your phone test and the
`Uidev...` attendee from ui_smoke. When you're done testing, start clean, keeping the
same invite link:

```bash
scripts/demo-reset.sh --personas      # asks you to type "reset"
```

## 8. What to put in the submission

- **Live demo URL:** the invite link, `https://hallway.debopamchowdhury.com/?invite=<code>`.
  Anyone can open the plain URL, but only the invite link lets a new email join.
- **Description** (adapt it):

  > Live demo: https://hallway.debopamchowdhury.com/?invite=<code> (open it on your phone).
  > It's invite-only with 10 spots. Sign in with any email: there's no email step, the
  > sign-in link appears on screen. Six demo personas are already in the hallway. Pick
  > *Connect* to plug in your own agent over MCP (the key is on the Me tab), or browse
  > the lobby, rooms and people. If the demo is full you'll see a link to the
  > walkthrough video. It runs on a single VPS behind a Cloudflare Tunnel, on Switch 0.31.0.

Set `DEMO_VIDEO_URL=` in `.env` to your walkthrough video before you share the link
(`scripts/up.sh --demo` applies it). The "demo is full" screen links to it.

## 9. Run it: monitoring, reset, teardown, cost

```bash
cd ~/Hallway
docker compose -p hallway ps                          # hallway, cloudflared (and daypass): "healthy"
docker compose -p hallway logs -f --tail 100 hallway  # requests (visitor IPs), sign-ups, errors
docker compose -p hallway logs -f cloudflared
docker compose -p switch -f deploy/switch/docker-compose.yml ps
curl -s 127.0.0.1:8787/health; curl -s https://hallway.debopamchowdhury.com/health
curl -s 127.0.0.1:8787/api/event | python3 -m json.tool   # spots_left, attendees
docker stats --no-stream                               # memory per container
free -h; df -h /
```

Logs rotate on their own (3 × 10 MB per container). To check from anywhere, add a free
uptime monitor (Cloudflare Health Checks, UptimeRobot, …) on
`https://hallway.debopamchowdhury.com/health`.

**The Switch gateway dashboard** (agents, rooms) is only on the VPS's loopback. Reach it
through SSH: on the *laptop*, `ssh -L 3000:127.0.0.1:3000 hallway@VPS_IP`, then open
<http://localhost:3000>. The login is `GATEWAY_ADMIN_EMAIL` / `GATEWAY_ADMIN_PASSWORD`
in `deploy/switch/.env`.

**Restart after a reboot.** All services use `restart: unless-stopped`, so they come back
with Docker. If not, run `scripts/up.sh --demo`.

**Update to a newer Hallway:** `git pull && scripts/up.sh --demo`.

**Reset to a fresh demo** (every attendee, session and room record goes, and all 10
spots are free again; Switch's config and `.env` stay):

```bash
scripts/demo-reset.sh                 # asks for confirmation; same invite link
scripts/demo-reset.sh --new-invite    # also a new invite code: the old link stops working
scripts/demo-reset.sh --personas      # re-seed the personas afterwards
```

**Teardown** after the judging:

```bash
cd ~/Hallway
docker compose -p hallway --profile daypass --profile tunnel down -v      # Hallway + its data
docker compose -p switch -f deploy/switch/docker-compose.yml down -v      # Switch + Postgres + Mattermost data
```

Then, in Cloudflare, go to **Networking → Tunnels → hallway-demo → Delete** and check that
the `hallway` DNS record is gone. Finally, **destroy the VPS** in your provider's console.
Billing stops only when the server is destroyed, not when it's powered off.

**Cost:**

- The VPS is the only fixed cost: an 8 GB / 4 vCPU box is roughly US$20–50 a month
  depending on the provider. Most bill hourly, so a two-week demo costs about half that.
- The Cloudflare Tunnel and DNS are free.
- The day pass (only with a model key in `.env`) is capped at `DAYPASS_CAP_USD` per pass.
  The worst case is 10 spots × $0.25 = **$2.50** in model spend. Without a key it's $0.

## 10. Troubleshooting

**Cloudflare 502 Bad Gateway.** The tunnel is up, but cloudflared can't reach Hallway.
- The route's service must be `http://hallway:8787`, not `localhost`, not `https`, and not
  the VPS IP. Fix it in the dashboard (tunnel → Routes). It takes effect within seconds.
- Is Hallway healthy? `docker compose -p hallway ps` and `docker compose -p hallway logs hallway`.
  If it's restarting, see the next item.
- Was cloudflared started some other way (a `docker run` you pasted from the dashboard,
  or a systemd `cloudflared` service)? Remove it. It isn't on the `edge` network, and two
  connectors fight over the tunnel.

**Cloudflare error 1033 / tunnel "Down" or "Inactive".** cloudflared isn't connected.
Check `docker compose -p hallway logs cloudflared`:
- "Provided Tunnel token is not valid": the token was pasted incompletely or with a space
  or quotes. Re-copy it from the dashboard (tunnel → Edit / Configure → Docker) and
  run `scripts/up.sh --demo`.
- If cloudflared isn't running at all: is `TUNNEL_TOKEN=` filled in `.env`, and did you
  re-run `scripts/up.sh --demo`? It starts cloudflared only when the token is set.

**Hallway won't start: "... must be set to a random secret" or "... must be different secrets".**
On a public URL, Hallway refuses to start while `GUARD_API_KEY`, `HALLWAY_ADMIN_KEY` or
`DAYPASS_WORKER_KEY` is empty, `change-me` or `__RANDOM__`, or two of them are the same.
`scripts/up.sh --demo` fills in any that are missing. For a hand-edited `.env`, make each
one with `openssl rand -hex 24`. After changing `GUARD_API_KEY`, re-run `scripts/up.sh --demo`
so Switch's guard is pointed at it again.

**Hallway won't start: "SIGNIN_MODE=dev ... only runs on http://localhost".** `.env`
isn't the demo one. Run `mv .env .env.local`, then `scripts/up.sh --demo` to make a demo `.env`.

**"This demo is full".** All `INVITE_LIMIT` spots are taken: test sign-ups count too.
Run `scripts/demo-reset.sh`, or raise `INVITE_LIMIT` in `.env` and run `scripts/up.sh --demo`.

**People can sign up but don't appear in the lobby or rooms (the Mattermost team cap).**
Every attendee is two Mattermost team members (the person and their agent). Mattermost's
default team cap (50) is too small, so Hallway raises it at start to
`2 × MAX_ATTENDEES + headroom`. If the log says `could not check Mattermost's team size cap`,
or old attendees from earlier resets have filled the team, raise the cap by hand and restart Hallway:

```bash
docker exec switch-mattermost-1 mmctl --local config get TeamSettings.MaxUsersPerTeam
docker exec switch-mattermost-1 mmctl --local config set TeamSettings.MaxUsersPerTeam 500
docker compose -p hallway restart hallway
```

**Everything is slow or containers get killed.** Run `docker stats --no-stream` and `free -h`.
Hallway is capped at 512 MB. If the box itself is short on memory, stop the day pass
worker (`docker compose -p hallway stop daypass`) or move to a larger VPS.

**"Permission denied" on /data after upgrading from an older checkout.** The image now
runs as uid 10001. `scripts/up.sh` hands an old volume over to it on every run, so re-run it.
