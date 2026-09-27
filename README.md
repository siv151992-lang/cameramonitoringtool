# Camera Monitoring Tool

Monitors IP cameras on your LAN from a server on the same network. It answers
three questions:

1. **What cameras are on my network?** — finds them and keeps a list.
2. **Which ones are up right now?** — online / offline per IP address.
3. **Which SD cards have failed?** — reads the recording media status from each
   camera and alerts you when a card is failed, read-only or missing.

Built for a site of roughly **1000 cameras**. On default settings a full check
of 1000 cameras takes a few seconds on a normal day, and about 40 seconds in the
worst case where every camera is unreachable.

There is a web dashboard, a CSV export, and email / chat alerts.

---

## 5-minute quick start

Run these on the server that is on the same LAN as the cameras.

```bash
# 1. Get the code and install the two dependencies
git clone <this-repo> cameramonitoringtool
cd cameramonitoringtool
pip3 install -r requirements.txt

# 2. Create your settings file
cp config.example.yaml config.yaml
nano config.yaml          # set discovery.subnets and your camera username

# 3. Tell it the camera password (not stored in the file)
export CAMERA_PASSWORD='your-camera-password'

# 4. Find the cameras on your network
python3 camtool.py discover

# 5. Check them all once
python3 camtool.py scan

# 6. Run continuously with the dashboard
python3 camtool.py monitor --serve
```

Then open `http://<your-server-ip>:8080/` in a browser.

---

## Before you start

You need:

- A Linux or Windows server on the **same LAN** as the cameras.
- **Python 3.9 or newer**. Check with `python3 --version`.
- A camera **username and password**. A read-only *operator* account is enough
  and is safer than using the admin account. Without credentials the tool still
  reports online/offline, but it cannot read SD card status.
- The IP ranges your cameras use, for example `192.168.1.0/24`.

Only two Python packages are needed (`requests` and `PyYAML`). Everything else —
the web server, the database, the network scanning — uses Python's standard
library, so there is nothing else to install or maintain.

---

## Step by step

### Step 1 — Create your settings file

```bash
cp config.example.yaml config.yaml
```

Open `config.yaml` in a text editor. The three things you must set:

```yaml
site:
  name: "Estancia Office"          # shown on the dashboard and in alerts

discovery:
  subnets:
    - 192.168.1.0/24               # one line per network range your cameras use
    - 192.168.2.0/24

credentials:
  default:
    username: "admin"
    password: "${CAMERA_PASSWORD}" # read from an environment variable
```

`${CAMERA_PASSWORD}` means "read this from the environment", so the password is
never written into the file. Set it before running the tool:

```bash
export CAMERA_PASSWORD='your-camera-password'
```

If different floors or VLANs use different passwords, add overrides:

```yaml
credentials:
  default:
    username: "admin"
    password: "${CAMERA_PASSWORD}"
  overrides:
    - subnet: 192.168.5.0/24
      username: "operator"
      password: "${CAMERA_PASSWORD_FLOOR5}"
```

### Step 2 — Find your cameras

```bash
python3 camtool.py discover
```

This does two things at once:

- **Listens for ONVIF announcements.** Most cameras made in the last decade
  announce themselves. This finds cameras even on addresses you did not think
  to scan.
- **Sweeps the subnets** in your config for open camera ports (80, 554, 8000,
  37777, 443, 8080, 8899), which catches older cameras that do not speak ONVIF.

Results are written to `cameras.csv`. Running `discover` again later **adds new
cameras without touching the ones already in the file**, so it is safe to re-run
whenever cameras are installed.

To try it without saving anything:

```bash
python3 camtool.py discover --dry-run
python3 camtool.py discover --subnet 192.168.3.0/24    # scan one range only
```

### Adding cameras by hand

Discovery is convenient, not compulsory. If the server cannot sweep the camera
network, or you already have a list of addresses from your NVR, add them
directly:

```bash
python3 camtool.py add 10.10.12.64 --name "Reception Entrance" --location "Ground Floor Lobby"
```

Add `--check` to test it on the spot, which is the quickest way to confirm an
address, port and password before adding hundreds more:

```bash
python3 camtool.py add 10.10.12.64 --name "Reception" --check
```

```
Added 1, updated 0. cameras.csv now holds 1 camera(s).

Checking 1 camera(s) ...
  IP address   Name       Status  SD card  Detail
  -----------  ---------  ------  -------  -------------------------------
  10.10.12.64  Reception  online  ok       22104 MB free of 30436 MB
```

Other options: `--brand`, `--http-port`, `--rtsp-port`, `--username`,
`--password`, `--notes`, and `--disabled` to add a camera that is not installed
yet. An address that is already in the list is left alone unless you pass
`--update`.

**Adding many at once.** Put the addresses in a plain text file, one per line,
optionally with a name and location:

```
# exported from the NVR
10.10.12.64,Reception Entrance,Ground Floor
10.10.12.65,Parking Ramp,Basement
10.10.12.66
```

```bash
python3 camtool.py add --from-file camera-ips.txt
```

### Managing cameras from the dashboard

The dashboard can do all of this without the command line.

**+ Add camera** (top right) opens a form. Fill in the address, optionally a
name and location, and leave *Test the connection after adding* ticked — the
camera is probed straight away and the result is reported in the dialog, so a
wrong address or password is obvious immediately. Close it with the **×**,
Cancel, or the Escape key.

Every row has **Edit** and **Remove** links:

- **Edit** opens the same form filled in with that camera's details. Change the
  name, location, brand, ports or credentials and save. Leave the password
  blank to keep the existing one — the browser is never sent it.
  The IP address cannot be changed here, because it is the key under which
  history and events are recorded; to move a camera to a new address, remove it
  and add it again.
- **Check this camera** can be unticked to pause a camera without deleting it,
  which is the same as setting `enabled` to `no` in the CSV.
- **Remove** deletes it, after a confirmation.

This is on by default. To keep the dashboard strictly read-only, set:

```yaml
web:
  allow_editing: false
```

**Anyone who can open the dashboard can edit the camera list.** On a shared
network, either turn editing off or set a dashboard login:

```yaml
web:
  username: "operator"
  password: "${DASHBOARD_PASSWORD}"
```

**Removing cameras:**

```bash
python3 camtool.py remove 10.10.12.64
python3 camtool.py remove 10.10.12.64 10.10.12.65     # several at once
```

Removing also clears that camera's recorded status, so it disappears from the
dashboard rather than lingering as permanently offline.

### Step 3 — Name your cameras

Open `cameras.csv` in Excel, LibreOffice or a text editor. It looks like this:

```csv
ip,name,location,brand,http_port,rtsp_port,username,password,enabled,notes
192.168.1.11,Reception Entrance,Ground Floor Lobby,hikvision,80,554,,,yes,
192.168.1.21,Parking Ramp,Basement,dahua,80,554,,,yes,
```

| Column | What it does |
|---|---|
| `ip` | **Required.** The camera's address. |
| `name` | Friendly name used in the dashboard and alerts. |
| `location` | Floor, block or room. Makes alerts actionable. |
| `brand` | `auto`, `hikvision`, `dahua` or `axis`. Leave `auto` and it is detected on the first scan and written back here. |
| `http_port` | Web port. Usually 80. |
| `rtsp_port` | Video port. Usually 554. |
| `username` / `password` | Only if this camera differs from the config default. |
| `enabled` | `no` hides a camera from checks without deleting the row. |
| `notes` | Free text for your own use. |

Only `ip` is required — a file containing just a header `ip` and a list of
addresses works fine.

Filling in `name` and `location` is worth the effort: an alert saying
*"Parking Ramp, Basement is offline"* is far more useful than one saying
*"192.168.1.21 is offline"*.

### Step 4 — Run a check

```bash
python3 camtool.py scan
```

By default it prints only the cameras that need attention:

```
  Cameras           1000
  Online            987
  Offline           13
  SD card healthy   954
  SD card problems  6
  SD card unknown   40

Cameras needing attention:
  IP address     Name              Location      Status   SD card  Detail
  -------------  ----------------  ------------  -------  -------  ----------------------------
  192.168.1.21   Parking Ramp      Basement      online   failed   SD0: card has gone read-only
  192.168.2.64   Lift Lobby 2      1st Floor     OFFLINE  unknown  port 80: timed out
```

Use `--all` to list every camera.

### Step 5 — Run it continuously

```bash
python3 camtool.py monitor --serve
```

This checks every camera every 5 minutes, alerts on changes, and serves the
dashboard on port 8080. Open `http://<your-server-ip>:8080/` from any machine on
the LAN.

The dashboard shows counts at the top, a searchable and sortable table of every
camera, recent events, and a **Download CSV** button. It refreshes itself every
30 seconds.

Press `Ctrl+C` to stop.

### Step 6 — Run it as a background service

So it starts on boot and keeps running after you log out. On Linux with systemd:

```bash
sudo cp -r . /opt/cameramonitoringtool
sudo useradd --system --no-create-home camera-monitor
sudo chown -R camera-monitor /opt/cameramonitoringtool
sudo cp deploy/camera-monitor.service /etc/systemd/system/
sudo nano /etc/systemd/system/camera-monitor.service   # set CAMERA_PASSWORD
sudo systemctl daemon-reload
sudo systemctl enable --now camera-monitor
```

Check on it with:

```bash
sudo systemctl status camera-monitor
journalctl -u camera-monitor -f      # live log
```

---

## How the checks work

### Online / offline

The tool opens a **TCP connection** to each camera's web port, and to its RTSP
port if the web port does not answer. It does not use ping: many cameras are
configured to ignore ping while still serving video perfectly, and ping needs
administrator privileges on most systems.

A camera counts as **online** when one of its ports accepts a connection.

To avoid false alarms from a single dropped packet, a camera must fail
**two checks in a row** before it is reported offline. Change this with
`checks.offline_after_failures`.

If the address answers but refuses the connection on every port, the camera is
reported offline with a note that the address itself responded — that usually
means the camera is rebooting, its web service has crashed, or the IP has been
taken by another device.

### SD card health

There is no universal standard for reading SD card status, so the tool speaks
each vendor's own API:

| Brand | Endpoint used | Also covers |
|---|---|---|
| **Hikvision** | `/ISAPI/ContentMgmt/Storage` | Most ISAPI-compatible rebadges |
| **Dahua** | `/cgi-bin/storageDevice.cgi` | CP Plus, Amcrest, Lorex and other Dahua OEMs |
| **Axis** | `/axis-cgi/disks/list.cgi` | — |

Set a camera's `brand` to `auto` (the default) and the tool tries each API in
turn, then **writes the brand that answered back into `cameras.csv`** so later
checks go straight to the right one. Mixed-brand sites need no manual work.

A card is reported as:

| State | Meaning | Dashboard |
|---|---|---|
| **Healthy** | Card present, working, writable | green |
| **Failed** | Card reports an error, is unformatted, or has gone **read-only** | red |
| **Not present** | The camera answered but has no card in it | orange |
| **Unknown** | No credentials, camera offline, or an unsupported brand | grey |

The **read-only** case is worth calling out: as SD cards wear out they often
flip to read-only rather than failing outright. The camera looks perfectly
healthy and keeps streaming live video, but nothing is being recorded. This is
the failure people usually discover only when they go looking for footage. The
tool reports it as a failure.

Unrecognised status values are reported as *Unknown*, never as a failure, so
unusual firmware produces no false alarms.

By default the SD card is checked on **every** cycle, alongside the
reachability check, so a card failure is picked up within one interval — about
5 minutes on default settings. The SD check is the slower of the two, so if your
cameras are slow to answer you can run it less often by raising
`checks.storage_every_n_cycles` to 2 or 3.

---

## SNMP (optional)

The tool can also poll cameras over SNMP. It is off by default, because SNMP
has to be switched on in each camera first.

### You do not need a MIB file

A MIB is only a dictionary: it gives readable names to numeric OIDs like
`1.3.6.1.2.1.1.3.0`. SNMP itself works with the numbers, so a missing vendor
MIB does not stop you. Better still, the camera will list what it serves:

```bash
python3 camtool.py snmp 10.10.12.64 --walk
```

```
  OID                Value
  -----------------  ---------------------------------------------
  1.3.6.1.2.1.1.1.0  Hikvision IP Camera DS-2CD2143G0-I, V5.6.3
  1.3.6.1.2.1.1.3.0  98765432
  1.3.6.1.2.1.1.5.0  Reception Entrance
```

That is the ground truth for your firmware — more reliable than a MIB, which
only describes what *might* be present. Walk the vendor's private tree with
`--walk 1.3.6.1.4.1`.

First enable SNMP on the camera: **Configuration → Network → Advanced Settings
→ SNMP**, set v2c and a community string, and change it from `public`.

### Switching it on

```yaml
snmp:
  enabled: true
  version: "2c"
  community: "your-community-string"
  oids:
    uptime: "1.3.6.1.2.1.1.3.0"
    description: "1.3.6.1.2.1.1.1.0"
```

Two things it then does:

**A second opinion on reachability.** If a camera's web and video ports stop
answering but it still replies to SNMP, it is reported as online with a note
saying so. That distinguishes "the camera's web service has crashed" from
"the camera is off the network" — a useful difference when someone has to go
and look at it. Turn it off with `use_for_reachability: false`.

**SD card state, where the camera exposes it.** Only used when the vendor API
could not answer — no credentials, or an unsupported brand. Point it at the
right OID, found from a walk:

```yaml
snmp:
  sd_card_oid: "1.3.6.1.4.1.xxxxx.x.x.0"
  sd_card_ok_values: ["1", "ok", "normal"]
```

Anything not in `sd_card_ok_values` counts as a failure. Be aware that many
camera firmwares expose only the standard MIB-II tree over SNMP and say nothing
about storage — the walk will tell you whether yours is an exception. The
vendor API (ISAPI for Hikvision) remains the better source where it works, and
always takes precedence.

### Reading values by hand

```bash
python3 camtool.py snmp 10.10.12.64                        # the standard system values
python3 camtool.py snmp 10.10.12.64 --oid 1.3.6.1.2.1.1.3.0
python3 camtool.py snmp 10.10.12.64 --walk 1.3.6.1.4.1     # the vendor's private tree
python3 camtool.py snmp 10.10.12.64 --community secret --version 1
```

SNMP v1 and v2c are supported. v3 is not, and neither are traps — the tool
polls rather than listening.

## Setting up alerts

Alerts are sent when a camera **changes state** — goes offline, comes back, or
its SD card fails. Two things keep the volume sane:

- **One message per cycle.** If a switch dies and takes 40 cameras with it, you
  get one message listing 40 cameras, not 40 messages.
- **Rate limiting.** An ongoing problem is repeated every 6 hours
  (`alerts.min_repeat_hours`), not every 5 minutes. A camera that recovers and
  fails again alerts immediately.

### Email

```yaml
alerts:
  email:
    enabled: true
    smtp_host: "smtp.zoho.com"
    smtp_port: 587
    use_tls: true
    username: "monitoring@yourcompany.com"
    password: "${SMTP_PASSWORD}"
    from_address: "monitoring@yourcompany.com"
    to_addresses:
      - "facilities@yourcompany.com"
      - "security@yourcompany.com"
```

Then `export SMTP_PASSWORD='...'` and test it:

```bash
python3 camtool.py test-alert
```

Most mail providers require an **app-specific password** rather than your normal
login password when a script signs in.

### Chat (Zoho Cliq, Slack, Teams)

```yaml
alerts:
  webhook:
    enabled: true
    url: "${WEBHOOK_URL}"
    message_field: "text"
```

Create an incoming webhook in your chat tool, `export WEBHOOK_URL='https://...'`,
and test with `python3 camtool.py test-alert`. If messages arrive empty, your
chat tool expects a different field name — try `message` or `content` in
`message_field`.

---

## All commands

| Command | What it does |
|---|---|
| `camtool.py discover` | Find cameras and add them to `cameras.csv` |
| `camtool.py add <ip>` | Add a camera by hand, without discovery |
| `camtool.py remove <ip>` | Remove cameras from the list |
| `camtool.py scan` | Check every camera once and report |
| `camtool.py monitor` | Check on a loop and send alerts |
| `camtool.py monitor --serve` | The same, plus the web dashboard |
| `camtool.py serve` | Dashboard only, using the last recorded results |
| `camtool.py list` | Print the camera inventory |
| `camtool.py report` | Show the last results without re-checking |
| `camtool.py identify` | Read each camera's model and firmware |
| `camtool.py snmp <ip>` | Read SNMP values, or list every OID a camera exposes |
| `camtool.py test-alert` | Send a test email / chat message |

Useful options:

```bash
python3 camtool.py add 10.10.12.64 --check     # add one camera and test it immediately
python3 camtool.py add --from-file ips.txt     # bulk add from a list of addresses
python3 camtool.py scan --all                  # list every camera, not just problems
python3 camtool.py scan --no-storage           # reachability only, much faster
python3 camtool.py report --offline-only
python3 camtool.py report --sd-only            # just the SD card problems
python3 camtool.py report --csv today.csv      # export for a report
python3 camtool.py report --events 20          # last 20 state changes
python3 camtool.py monitor --interval 60       # check every minute
python3 camtool.py --config /etc/cam/config.yaml scan
```

Every command accepts `--help`.

---

## Tuning for 1000 cameras

The defaults are chosen for roughly this size, but here is what to adjust:

| Setting | Default | Notes |
|---|---|---|
| `checks.workers` | 100 | Cameras checked at once. Raising it speeds up a cycle but adds network load. 200 is reasonable on a wired gigabit LAN. |
| `checks.tcp_timeout` | 2.0 | Lower on a fast LAN (1.0) to finish sooner; raise if cameras over Wi-Fi or a VPN are wrongly reported offline. |
| `checks.interval_seconds` | 300 | How often a full cycle runs. Must be longer than a cycle takes. |
| `checks.storage_every_n_cycles` | 1 | SD card checked every cycle, so a failure is caught within one interval. Raise to 2 or 3 if the SD check makes a cycle too slow. |
| `discovery.workers` | 256 | Only used during `discover`. |
| `checks.history_retention_days` | 30 | History is pruned automatically. 1000 cameras at 5-minute intervals is roughly 300 MB per month. |

**What actually governs cycle time:** cameras that answer are checked in
milliseconds. Cameras that are *unplugged* cost the full `tcp_timeout` on each
of their two ports, and that is what dominates. Measured with 1000 cameras at
the default 100 workers and a 2-second timeout:

| Situation | Time for one cycle |
|---|---|
| 5% of cameras offline (a normal day) | about 4 seconds |
| All 1000 offline (a core switch is down) | about 40 seconds |
| All 1000 offline, `workers: 200` | about 20 seconds |

So the default 5-minute interval has a large margin even in the worst case. Run
`python3 camtool.py scan` once and check the time it reports — if a cycle ever
approaches `interval_seconds`, raise the interval or the worker count.

---

## Where things are stored

```
cameras.csv          your camera list - edit this freely, it is the source of truth
config.yaml          your settings
data/monitor.db      recorded status, history and events (SQLite)
```

`cameras.csv` and `config.yaml` are excluded from git by `.gitignore`, because
they contain your site's details. **Back up `cameras.csv`** — it is the one file
you cannot regenerate by hand once names and locations are filled in.

---

## Troubleshooting

**"Camera list not found"**
Run `python3 camtool.py discover` first, or add a camera by hand with
`python3 camtool.py add <ip>`, or copy `cameras.example.csv` to `cameras.csv`
and edit it.

**`discover` finds nothing**
- Check the subnet is right: run `ip addr` (Linux) or `ipconfig` (Windows) and
  confirm the server is on the same range you configured.
- Cameras on a different VLAN will not answer unless routing allows it.
- Try one known camera directly: `python3 camtool.py discover --subnet 192.168.1.64`
- If the sweep cannot reach them at all, add them by hand instead:
  `python3 camtool.py add 192.168.1.64 --check`

**Every camera shows SD card "Unknown"**
Almost always credentials. Check that:
- `CAMERA_PASSWORD` is exported in the same shell you are running the tool from.
- The account can read storage settings. Some restricted accounts cannot.
- Run `python3 camtool.py identify` — if that shows models, the login works and
  the brand may be unsupported.

**SD card says "authentication failed"**
The username or password is wrong for that camera. If one floor uses a different
password, add a `credentials.overrides` entry, or put the credentials in that
camera's row in `cameras.csv`.

**A camera is reported offline but works in the browser**
Check the `http_port` in `cameras.csv` matches the port you use in the browser.
Cameras behind NAT or on a slow link may need a larger `checks.tcp_timeout`.

**The dashboard will not open from another machine**
Set `web.host: "0.0.0.0"` in `config.yaml` (the default) and open port 8080 in
the server's firewall:
`sudo ufw allow 8080/tcp`

**Too many alert emails**
Raise `alerts.min_repeat_hours`, or raise `checks.offline_after_failures` to 3 so
brief blips are ignored.

---

## Security notes

- Passwords are read from environment variables by default and are never written
  to `config.yaml`, the database, or the log.
- Use a **read-only operator account** on the cameras. The tool only ever reads;
  it never changes camera settings.
- The dashboard can add and remove cameras (`web.allow_editing`, on by
  default). It cannot change camera settings or edit recorded history. Set
  `allow_editing: false` for a strictly read-only dashboard.
- Writes are refused unless the request comes from the dashboard's own page, so
  another website cannot alter your camera list through your browser.
- The dashboard has no login unless you set one. On a trusted LAN that is
  usually fine; to require a login, set `web.username` and `web.password` in
  `config.yaml`.
- Keep the dashboard on the internal network. It is not built to face the
  internet.

---

## Running the tests

```bash
python3 -m unittest discover -s tests -t .
```

The tests run against a built-in fake camera, so no real hardware is needed.

## Project layout

```
camtool.py                    the command line tool - start here
camera_monitor/
  config.py                   reads config.yaml
  inventory.py                reads and writes cameras.csv
  discovery.py                subnet sweep + ONVIF discovery
  health.py                   runs the checks, decides what changed
  database.py                 SQLite storage
  alerts.py                   email and chat notifications
  probes/
    reachability.py           the online/offline TCP check
    hikvision.py              SD card status via ISAPI
    dahua.py                  SD card status via CGI
    axis.py                   SD card status via VAPIX
    sdcard.py                 picks the right vendor, auto-detects brand
    onvif.py                  ONVIF discovery
    snmp.py                   SNMP v1/v2c client (GET, GETNEXT, walk)
  web/                        the dashboard (status API, add/remove endpoints)
deploy/camera-monitor.service systemd unit
tests/                        test suite with a fake camera
```

## Adding support for another camera brand

Copy `camera_monitor/probes/axis.py` (the shortest one), change the endpoint and
the parsing, then add it to `VENDORS` and `AUTO_ORDER` in
`camera_monitor/probes/sdcard.py`. Each vendor module needs one function,
`fetch_storage(...)`, returning a `StorageInfo`. Raise `UnsupportedDevice` when
the endpoint returns 404 so auto-detection moves on to the next brand.
