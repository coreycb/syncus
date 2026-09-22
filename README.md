# syncus

Sync uncommitted git working-tree changes between the host and an Incus
container that has the same repository checked out at the same path.

Both sides must be on the same branch; `syncus` refuses to run otherwise.

## Install

```sh
pipx install /path/to/syncus
```

Editable install, so edits to `syncus.py` take effect immediately:

```sh
pipx install --editable /path/to/syncus
```

Upgrade in place after pulling changes:

```sh
pipx install --force /path/to/syncus
```

## Configuration

| Variable           | Description                    |
| ------------------ | ------------------------------ |
| `SYNCUS_CONTAINER` | Incus container to sync with.  |

```sh
SYNCUS_CONTAINER=my-container syncus push
```

Or export it for the session:

```sh
export SYNCUS_CONTAINER=my-container
```

## Usage

Run from inside the repository you want to sync:

```sh
syncus push    # copy host working-tree changes into the container
syncus pull    # copy container working-tree changes onto the host
```

The current working directory is used as the repository path on *both* sides,
so the checkout must live at the same absolute path in the container.

Git commands in the container run as your host username (`su - <user> -c ...`),
so that user must exist in the container too.

Each direction syncs two things:

1. **Commits.** If one side is ahead, the missing commits are transferred as a
   `git bundle` and the other side is fast-forwarded. If the two sides have
   diverged, `syncus` stops and leaves it to you to reconcile.
2. **Working-tree changes.** Files reported as modified, added, or renamed by
   `git status --porcelain` are copied; deleted files are deleted on the other
   side.
