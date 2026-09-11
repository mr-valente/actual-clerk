# The lab

A throwaway copy of the live Actual + Clerk stack, run from `lab/compose.yml`,
so bank-link operations can be exercised against a real budget without
touching the live one.

## What is in it

| Container | Image | Port | Data |
| --- | --- | --- | --- |
| `actual-lab` | `actualbudget/actual-server:26.9.0` (matches the pinned API) | `30130` | `lab/data/actual`, a copy of the live server directory |
| `clerk-lab` | built from this checkout | `30131` | `lab/data/clerk`, a copy of the live Clerk data directory |

`lab/data/` and `lab/.env` are ignored by git. `lab/.env` carries the Actual
password and budget sync ID (lifted from the copied Clerk settings) plus the
Plaid **sandbox** client id and secret. The compose file forces notifications,
the local model, and the morning digest off, and points `ACTUAL_URL` at the lab
server, so the lab cannot send a real report or load the model server.

The copied Actual server still holds the live SimpleFIN token as a server
secret, and the copied Clerk settings still hold the SimpleFIN access URL.
Both are read-only against the banks, so the lab's own hourly sync keeps
pulling real SimpleFIN data into the lab budget. That is deliberate: the
migration tooling has to be tested against real link state.

## Commands

```bash
docker compose -f lab/compose.yml up -d --build   # start, or rebuild after code changes
docker compose -f lab/compose.yml logs -f clerk-lab
docker compose -f lab/compose.yml down            # keeps lab/data
```

Refreshing the copy from the live data (with the live containers stopped):

```bash
docker compose -f lab/compose.yml down
rm -rf lab/data
mkdir -p lab/data
cp -a "$MNT_HOME/.local/share/actual-budget/data" lab/data/actual
cp -a "$MNT_HOME/.local/share/actual-budget/clerk/data" lab/data/clerk
rm -f lab/data/clerk/actual-api.lock
```

## Driving the worker directly

The host has no Node, but the Clerk image does. The worker speaks a
line-delimited JSON protocol on stdin/stdout, so a new worker method can be
tried against the lab budget before any Python is written:

```bash
docker cp src/actual_clerk/actual_worker.mjs clerk-lab:/app/src/actual_clerk/actual_worker_dev.mjs
docker exec clerk-lab mkdir -p /tmp/scratch-api
docker exec -i clerk-lab node /app/src/actual_clerk/actual_worker_dev.mjs <<'EOF'
{"id":1,"method":"initialize","params":{"serverUrl":"http://actual-lab:5006","password":"…","syncId":"…","dataDir":"/tmp/scratch-api"}}
{"id":2,"method":"listAccountsDetailed","params":{}}
{"id":3,"method":"shutdown","params":{}}
EOF
```

Replies are the lines prefixed `@@actual-clerk-rpc@@`. The scratch `dataDir`
keeps this session's budget cache apart from the running Clerk's, which holds
an exclusive lock on its own.

## Worker tests without Node on the host

```bash
scripts/worker-tests.sh      # npm ci + npm test inside node:22
```
