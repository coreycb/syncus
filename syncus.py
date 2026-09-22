#!/usr/bin/env python3

import getpass
import os
import shlex
import subprocess
import sys

CONTAINER = os.environ.get("SYNCUS_CONTAINER")
PATH = os.getcwd()
USER = getpass.getuser()


def run(cmd, check=True):
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=check,
    )


def incus_exec(container, *cmd):
    """Build an incus exec command line that runs cmd as USER."""
    return [
        "incus", "exec", container, "--",
        "su", "-", USER, "-c", shlex.join(cmd),
    ]


def git(args, container=None, check=True):
    """Run a git command against PATH, on the host or inside a container."""
    cmd = ["git", "-C", PATH, *args]

    if container:
        cmd = incus_exec(container, *cmd)

    return run(cmd, check=check)


def git_branch(container=None):
    return git(["branch", "--show-current"], container).stdout.strip()


def git_head(container=None):
    return git(["rev-parse", "HEAD"], container).stdout.strip()


def git_status(container=None):
    files = {}

    for line in git(["status", "--porcelain"], container).stdout.splitlines():
        status = line[:2]
        path = line[3:]

        if " -> " in path:
            old, new = path.split(" -> ", 1)
            files[old] = "D"
            files[new] = status
        else:
            files[path] = status

    return files


def check_branches():
    if not CONTAINER:
        print("Error: CONTAINER env var must be set")
        sys.exit(1)

    host_branch = git_branch()
    container_branch = git_branch(CONTAINER)

    if not host_branch or not container_branch:
        print("Error: one side is not on a branch (possibly detached HEAD).")
        sys.exit(1)

    if host_branch != container_branch:
        print("Error: branches do not match:")
        print(f"  Host:      {host_branch}")
        print(f"  Container: {container_branch}")
        sys.exit(1)

    print(f"Branch: {host_branch}")

    return host_branch


def is_ancestor(old, new, container=None):
    """Is commit old reachable from commit new, on the given side?"""
    return git(["merge-base", "--is-ancestor", old, new],
               container, check=False).returncode == 0


def sync_commits(branch, to_container):
    """Fast-forward one side's branch to the other's, via a git bundle.

    The host and container cannot reach each other over the network, so the
    commits travel as a bundle file copied with incus.  Only fast-forwards
    are performed; if the two sides have diverged this bails out.
    """
    source = None if to_container else CONTAINER
    target = CONTAINER if to_container else None

    source_head = git_head(source)
    target_head = git_head(target)

    if source_head == target_head:
        print("Commits: up to date.")
        return

    if not is_ancestor(target_head, source_head, source):
        print("Error: the two sides have diverged; reconcile manually.")
        print(f"  Host:      {git_head()}")
        print(f"  Container: {git_head(CONTAINER)}")
        sys.exit(1)

    count = git(["rev-list", "--count", f"{target_head}..{source_head}"],
                source).stdout.strip()
    print(f"Commits: fast-forwarding {count} commit(s).")

    bundle = f"/tmp/syncus-{os.getpid()}.bundle"
    remote_bundle = f"{CONTAINER}{bundle}"

    git(["bundle", "create", bundle, branch, "--not", target_head], source)

    try:
        if to_container:
            run(["incus", "file", "push", "--mode=0644", bundle,
                 remote_bundle])
        else:
            run(["incus", "file", "pull", remote_bundle, bundle])

        git(["fetch", bundle, branch], target)

        merge = git(["merge", "--ff-only", "FETCH_HEAD"], target, check=False)

        if merge.returncode != 0:
            print("Error: fast-forward failed:")
            print(merge.stderr.strip())
            sys.exit(1)
    finally:
        run(["incus", "file", "delete", remote_bundle], check=False)

        if os.path.exists(bundle):
            os.remove(bundle)


def push():
    branch = check_branches()
    sync_commits(branch, to_container=True)
    files = git_status()

    if not files:
        print("Files: up to date.")
        return

    for file, status in sorted(files.items()):
        local = os.path.join(PATH, file)
        remote = f"{CONTAINER}{os.path.join(PATH, file)}"

        if "D" in status and not os.path.exists(local):
            print(f"Delete: {file}")
            subprocess.run(["incus", "file", "delete", remote], check=True)
        elif os.path.isdir(local):
            print(f"Push:   {file}/")
            subprocess.run(
                ["incus", "file", "push", "-r", local, remote],
                check=True,
            )
        else:
            print(f"Push:   {file}")
            subprocess.run(
                ["incus", "file", "push", local, remote],
                check=True,
            )


def pull():
    branch = check_branches()
    sync_commits(branch, to_container=False)
    files = git_status(CONTAINER)

    if not files:
        print("Files: up to date.")
        return

    for file, status in sorted(files.items()):
        remote = f"{CONTAINER}{os.path.join(PATH, file)}"
        local = os.path.join(PATH, file)

        if "D" in status:
            print(f"Delete: {file}")
            if os.path.exists(local):
                if os.path.isdir(local):
                    import shutil
                    shutil.rmtree(local)
                else:
                    os.remove(local)
        else:
            print(f"Pull:   {file}")
            if os.path.isdir(local):
                subprocess.run(
                    ["incus", "file", "pull", "-r", remote, local],
                    check=True,
                )
            else:
                subprocess.run(
                    ["incus", "file", "pull", remote, local],
                    check=True,
                )


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in ("push", "pull"):
        print(f"Usage: {os.path.basename(sys.argv[0])} push|pull")
        sys.exit(1)

    if sys.argv[1] == "push":
        push()
    else:
        pull()


if __name__ == "__main__":
    main()
