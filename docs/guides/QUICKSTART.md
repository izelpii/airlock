# Quick Start Guide

## Prerequisites

Almost everything runs in a podman container and brings its own dependencies.
On the host you need:

- podman 4.7 or higher (on macOS, a running `podman machine`; give it ≥ 6 GB)
- curl and openssl (health checks and the gateway's certificate)
- Ollama, for photo descriptions and album prompts (optional for the rest)
- Python 3.10 to 3.13 (3.12 preferred) and ffmpeg, only for the services that
  can run natively (deepface, transcode); without them those run in containers too
- **On macOS: turn off the AirPlay Receiver** (System Settings → General →
  AirDrop & Handoff). It listens on port 5000, which the backend needs; with it
  on, the backend cannot start. `start-all.sh` stops and says so.

See the Prerequisites section of the [README](../../README.md) for the details, and the
[Installation guide](INSTALLATION.md) for every step explained, per platform.

## Quick Start (Automated)

The easiest way to start the application on a new machine:

```bash
./deploy/deploy.sh          # check and prepare this machine (once)
./start-all.sh --build      # build every image, then start
```

`deploy/deploy.sh` will:
1. Check podman, its mode, and the tools the stack needs
2. Write `deploy/airlock.env` if it does not exist (edit it to point at your
   photo library)
3. Create the host directories that get bind-mounted, and check you own them
4. Generate the gateway's TLS certificate (a private CA) if there is none
5. Build the virtual environments for the services that will run natively here

`start-all.sh` then builds what is stale and starts every service in order,
checking each one's health through the gateway. On a machine that has run it
before, `./start-all.sh` on its own is enough. `./deploy/deploy.sh --start`
does both steps in one go.

The application will be available at:
- Frontend: https://airlock.lan
- Backend API: https://airlock.lan/api

If those names do not resolve, install the hosts entries once:
`sudo systems/gateway/make-hosts.sh --install`

Install the CA certificate the gateway uses (`systems/gateway/certs/airlock-ca.pem`)
on each device, or browsers will warn. From another machine, use this
machine's LAN address: `https://<lan-address>/`.

## Manual Start

Each service under `systems/` starts on its own:

```bash
cd systems/<service>
./start-podman.sh            # (re)start its container; --build rebuilds the image
./start-host.sh              # backend-deepface and backend-transcode only: run natively
```

### Frontend outside the container (development only)

```bash
cd systems/frontend
npm install
npm start
```

The development server cannot reach `/api` on its own; the UI is normally
served through the gateway.

## First Steps

1. **Open the application** at https://airlock.lan

2. **Create the administrator account**: a new installation has no account and
   no default password, and offers "Welcome to Airlock Photos" with the form to
   create the first one. After that it asks you to sign in.

3. **Create your first catalog** (Admin ▸ Catalogs, **Create Catalog**):
   - Catalog Name, e.g. "Family"
   - How the catalog handles your files: copy them in, index them where they
     are, or move them in

4. **Add a storage route** (the catalog's **Storage routes** tab, **Add a
   storage route**): a name and a folder under `STORAGE_ROOT_HOST` (set in
   `deploy/airlock.env`; your home folder by default). This also creates the
   workflow **Import <route name>**.

5. **Run the import**:
   - Open **Workflows** and click the play button (▶) on the import workflow
   - Watch it on the **Import progress** page; photos appear while it runs
   - When it finishes, Create Events, Find Faces and the rest of the chain are
     queued after it

## Testing an Import

To try an import on a few files:

1. Put some photos and videos in a folder under `STORAGE_ROOT_HOST`, e.g.
   `~/Pictures/airlock-test`.

2. Add a storage route for that folder, as in step 4 above.

3. Run **Import airlock-test** and verify:
   - The photos show on the **Photos** page and the **Dashboard**
   - Files are copied under `catalogs/<catalog>/Images` and `Videos` (copy mode)
   - The catalog's database is `catalogs/<catalog>/workflow_results.db`:
   ```bash
   sqlite3 catalogs/<catalog>/workflow_results.db "SELECT COUNT(*) FROM file_contents;"
   ```

## Troubleshooting

### The page does not load
- Check that the gateway is up: `curl http://airlock.lan/gateway-health`
- Check what this machine is missing: `./deploy/deploy.sh --check`
- Rootless podman must be allowed ports 80 and 443; `deploy/deploy.sh` checks
  the unprivileged port floor and says how to lower it
- `./deploy/diagnose-first-run.sh` explains a sign-in form that cannot work

### A service won't start
- Check its log: `logs/<service>.log`, e.g. `logs/backend.log`
- See what is running: `podman ps`, and `./deploy/status.sh`
- Rebuild it: `./start-all.sh --build=<service>`

### Workflow execution fails
- Verify the storage route's folder exists and contains files
- Ensure the catalog's directory is writable
- Check the run on the **Workflow Executions** page
- View `logs/backend.log` for detailed error messages

### Progress not updating
- Check the browser console for Socket.IO connection errors
- Ensure the backend is running: `curl http://backend.airlock.lan/api/auth/session`
- Open the UI through the gateway (https://airlock.lan), not a service port

## Next Steps

- Read the full [README.md](../../README.md) for detailed documentation
- See [ARCHITECTURE.md](../architecture/ARCHITECTURE.md) for how the services fit together
- Set up backups for the catalog (the catalog's **Backup** tab)
- Check execution history on the **Workflow Executions** page

## Support

For issues or questions:
1. Check `logs/<service>.log` for error logs
2. Check the browser console for frontend errors
3. Run `./deploy/deploy.sh --check` to verify the prerequisites
4. Review the README.md for detailed information
