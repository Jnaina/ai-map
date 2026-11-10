#!/bin/bash
# Serve the map locally at http://localhost:8765
cd "$(dirname "$0")/web" && exec python3 -m http.server "${1:-8765}" --bind 127.0.0.1
