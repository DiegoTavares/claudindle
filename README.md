# Claudindle

Shows your Claude subscription usage (session, weekly all-models, weekly per-model)
on a jailbroken Kindle Paperwhite 7th gen, refreshed every 3 minutes.

Two parts:

- `server/` runs on the Raspberry Pi. It polls the Claude usage API, renders a
  1072x1448 grayscale PNG and serves it at `http://<pi>:8080/usage.png`.
- `kindle/` is a shell script for the Kindle. It downloads the PNG and draws it
  with `eips`. No binary, no credentials on the Kindle.

## Pi setup

```sh
sudo apt install -y python3-venv fonts-dejavu-core
git clone <this repo> ~/claudindle
cd ~/claudindle/server
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

Log the Pi in to your Claude account. Tokens from `claude setup-token` do not
work here: the usage endpoint requires a full login. The login is separate from
your other devices and refreshes itself.

```sh
.venv/bin/python claudindle.py login            # prints a URL; open it and sign in
.venv/bin/python claudindle.py login --code '<code shown in the browser>'
```

Credentials land in `~/.config/claudindle/credentials.json` (mode 600).

Test once, then install the service:

```sh
.venv/bin/python claudindle.py render --out /tmp/usage.png
sudo cp claudindle.service /etc/systemd/system/
sudo systemctl enable --now claudindle
curl -o /tmp/usage.png http://localhost:8080/usage.png
```

Edit `claudindle.service` if your user or paths differ. Timezone and refresh
interval are environment variables in that file.

### Without sudo (e.g. the Homebridge image's restricted terminal)

If the Pi already has Python 3 with Pillow (`python3 -c "import PIL"`), skip the
venv and use cron instead of systemd:

```sh
git clone https://github.com/DiegoTavares/claudindle.git ~/claudindle
python3 ~/claudindle/server/claudindle.py login        # then: login --code '<code>'
~/claudindle/server/run.sh
(crontab -l 2>/dev/null; echo "@reboot $HOME/claudindle/server/run.sh"; echo "*/5 * * * * $HOME/claudindle/server/run.sh") | crontab -
```

`run.sh` is idempotent, so the 5-minute cron line just restarts the server if it died.

## Kindle setup

Copy `kindle/claudindle.sh` to `/mnt/us/claudindle/` on the Kindle and, if you
use KUAL, copy `kindle/kual/claudindle/` to `/mnt/us/extensions/claudindle/`.

Set `SERVER` at the top of the script to your Pi's address, then either pick
"Start usage display" in KUAL or run over SSH:

```sh
/mnt/us/claudindle/claudindle.sh start
```

`stop` restores the screensaver and clears the screen. Logs go to
`/mnt/us/claudindle/claudindle.log`.

The script disables the screensaver while running so the Kindle stays awake.
Keep it on a charger for permanent use.

## Development on a Mac

`server/claudindle.py render` falls back to the Claude Code token in the macOS
Keychain, so you can iterate on the layout without a token file.
