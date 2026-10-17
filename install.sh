#!/bin/sh
# install.sh v0.1 — 2026-10-17
# Changelog:
# - v0.1 (2026-10-17): First version: RouterLM in ~/routerlm-main, started with the Mac by launchd, page opened.
#   Pasting the line again updates it; settings and API keys stay (Rene: "slice 7 is the install line").
#
# Install or update RouterLM on a Mac. In Terminal, paste:
#   curl -fsSL https://raw.githubusercontent.com/rene-kropf/routerlm/main/install.sh | sh
# Remove it (the routerlm-main folder, with its settings and API keys, stays until you put it in the Trash):
#   launchctl bootout gui/$(id -u)/io.github.rene-kropf.routerlm; rm ~/Library/LaunchAgents/io.github.rene-kropf.routerlm.plist
#
# Files fetched by curl carry no quarantine mark, so Gatekeeper doesn't stop them (a double-click starter in the
# downloaded ZIP is rejected). Everything sits in main(), so a download cut short runs nothing.

main() {
  set -eu
  DIR="$HOME/routerlm-main"
  LABEL=io.github.rene-kropf.routerlm
  PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
  DOMAIN="gui/$(id -u)"

  if [ "$(uname)" != Darwin ]; then
    echo "This line sets RouterLM up on a Mac. On Linux, download it from github.com/rene-kropf/routerlm and start it with:"
    echo "  cd routerlm-main && python3 routerlm.py serve"
    exit 1
  fi

  # Python comes with Apple's command line tools; without them /usr/bin/python3 only offers to install them.
  PY=/usr/bin/python3
  if ! xcode-select -p >/dev/null 2>&1; then
    PY=$(command -v python3 || true)
    if [ -z "$PY" ] || [ "$PY" = /usr/bin/python3 ]; then
      xcode-select --install 2>/dev/null || true
      echo "RouterLM needs Python, which comes with Apple's command line tools."
      echo "A window offers to install them. Click Install, wait for it to finish, then paste the same line again."
      exit 1
    fi
  fi

  if [ -f "$DIR/routerlm.py" ]; then
    echo "Updating RouterLM in $DIR. Your settings and API keys stay."
  else
    echo "Installing RouterLM in $DIR."
  fi
  mkdir -p "$DIR/logs" "$HOME/Library/LaunchAgents"
  # The repo holds no settings or API keys, so unpacking over the folder leaves them alone.
  curl -fsSL https://github.com/rene-kropf/routerlm/archive/refs/heads/main.tar.gz | tar -xz -C "$DIR" --strip-components 1

  cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array><string>$PY</string><string>$DIR/routerlm.py</string><string>serve</string></array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$DIR/logs/serve.log</string>
  <key>StandardErrorPath</key><string>$DIR/logs/serve.log</string>
</dict>
</plist>
EOF

  # Stop the running copy, if any, and start the new one. bootout returns before the old one is gone.
  launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
  i=0
  while launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1 && [ $i -lt 20 ]; do sleep 0.5; i=$((i + 1)); done
  launchctl bootstrap "$DOMAIN" "$PLIST"

  # The address Settings saved; a first start writes 127.0.0.1:8090.
  LISTEN=$("$PY" -c 'import json, sys; print(json.load(open(sys.argv[1])).get("listen", "127.0.0.1:8090"))' \
    "$DIR/routerlm.json" 2>/dev/null || echo 127.0.0.1:8090)
  URL="http://$LISTEN/"
  i=0
  until curl -s -o /dev/null --max-time 2 "$URL"; do
    i=$((i + 1))
    if [ $i -ge 30 ]; then
      echo "RouterLM didn't answer at $URL. Its last messages, from $DIR/logs/serve.log:"
      tail -n 5 "$DIR/logs/serve.log" 2>/dev/null || true
      exit 1
    fi
    sleep 0.5
  done
  echo "RouterLM is running at $URL and starts with this Mac. Opening it in your browser."
  open "$URL"
}

main "$@"
