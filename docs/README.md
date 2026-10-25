# Airlock Photos — documentation

Start with the [README](../README.md) at the top of the repository: what Airlock
is, how to run it, and its configuration.

## Getting started

- [Quick start](guides/QUICKSTART.md) — the shortest path to a running instance
- [Startup guide](guides/STARTUP_GUIDE.md) — every service, and how they start

## How it works

- [Architecture](architecture/ARCHITECTURE.md) — services, data, workflows, security
- [Information flow](architecture/INFORMATION_FLOW.md) — how data moves between them
- [Technology stack](architecture/TECHNOLOGY_STACK.md)
- [Ingestion](features/ingestion-architecture.md) — how files come into a catalog

## Albums and sharing

- [Albums](features/albums-enhancement-spec.md) — albums, deployments and galleries
- [Packing a deployment](features/PACKING-TOOL.md) — a gallery that runs anywhere Docker does
- [Publish an album on Amazon EC2](guides/PUBLISH_TO_EC2.md), and [AWS notes](features/aws.md)

## Backups

- [Backups](features/backups.md) — restic, borg and rsync, run by Airlock itself
- [Setting up a borg backup host](guides/BORG_INSTALL.md)
- [Backrest on the backup host](guides/BACKREST_INSTALL.md) (optional)

## Design notes

- [Accelerated services](specs/accelerated-services-spec.md) — native, container or GPU
- [Ports and routes](specs/redesign-ports-routes-spec.md) — the gateway and service names
- [Sign-in and users](specs/user-authentication-spec.md)
- [Pet recognition](specs/pet-recognition-spec.md)
- [The rename from Shoebox](specs/name-migration-spec.md)
