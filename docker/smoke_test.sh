#!/usr/bin/env bash
# Fresh appdata, bundled geographic inputs, and the same non-root UID as Unraid.
set -euo pipefail
image="${1:?image required}"
container="roadtoassetto-ci-${RANDOM}"
state="$(mktemp -d)"
cleanup() {
  docker logs "$container" || true
  docker rm -f "$container" >/dev/null 2>&1 || true
  # Ownership is reset before removing only the temporary directory made above.
  sudo chown -R "$(id -u):$(id -g)" "$state"
  rm -rf -- "$state"
}
trap cleanup EXIT
sudo chown 99:100 "$state"
sudo chmod 770 "$state"
docker run -d --name "$container" --user 99:100 --read-only --init \
  --cap-drop ALL --security-opt no-new-privileges:true \
  --tmpfs /tmp:rw,nosuid,size=1g --memory 8g --cpus 4 --pids-limit 256 \
  --mount "type=bind,source=$state,target=/data" "$image" >/dev/null
for attempt in $(seq 1 45); do
  status="$(docker inspect --format '{{.State.Health.Status}}' "$container")"
  if [ "$status" = healthy ]; then break; fi
  if [ "$(docker inspect --format '{{.State.Running}}' "$container")" != true ]; then exit 1; fi
  sleep 2
done
test "$status" = healthy
docker exec -i "$container" python - <<'PY'
import json
import os
from pathlib import Path
from urllib.request import urlopen
assert (os.getuid(), os.getgid()) == (99, 100)
base = 'http://127.0.0.1:8765'
def read(path):
    with urlopen(base + path, timeout=15) as response:
        assert response.status == 200
        return response.read()
health = json.loads(read('/api/health'))
catalog = json.loads(read('/api/catalog'))
assert health['status'] == 'ok'
assert catalog['templates'][0]['capabilities']['building_basements']
assert len(read('/api/templates/altenhain/map.jpg')) > 10000
assert len(read('/api/templates/altenhain/preview.png')) > 10000
assert b'Road' in read('/')
assert json.loads(read('/api/jobs')) == {'jobs': []}
Path('/data/persistence-check.txt').write_text('survives restart', encoding='utf-8')
print('Bundled dataset, catalog, images, UI and UID 99:100 verified')
PY
docker restart "$container" >/dev/null
docker exec "$container" python -c "from pathlib import Path; assert Path('/data/persistence-check.txt').read_text() == 'survives restart'; print('Persistent appdata verified')"
