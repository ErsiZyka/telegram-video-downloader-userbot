# ServerPanel script convention

- Place every new server-side script or project in `/home/ersi/condivisa/server_scripts/`.
- ServerPanel automatically lists every direct project directory and supported script file (`.py`, `.sh`, `.js`, `.ts`) in that folder.
- To give a listed project Start/Restart/Stop controls, create its systemd service and add its metadata to `server_scripts/scripts.json`.
- Keep existing projects in place unless moving them is explicitly requested; use a symlink under `server_scripts/` instead.
