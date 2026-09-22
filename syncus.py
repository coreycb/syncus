#!/usr/bin/env python3

import getpass
import os
import shlex
import shutil
import subprocess
import sys

CONTAINER = os.environ.get("SYNCUS_CONTAINER")
PATH = os.getcwd()
USER = getpass.getuser()

COLOR = sys.stderr.isatty() and "NO_COLOR" not in os.environ


def red(text):
    return f"\033[1;31m{text}\033[0m" if COLOR else text


def error(message, *details):
    """Print an error, plus any indented detail lines, to stderr."""
    print(red(f"Error: {message}"), file=sys.stderr)

    for detail in details:
        for line in detail.strip().splitlines():
            print(f"  {line}", file=sys.stderr)


def fail(message, *details):
    error(message, *details)
    sys.exit(1)


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


def in_head(file):
    """Does HEAD contain this path? Checked on the host; HEADs match."""
    return git(["cat-file", "-e", f"HEAD:{file.rstrip('/')}"],
               check=False).returncode == 0


def changed_files(source, target):
    """Files to sync from source to target, mapped to the source's status.

    Files changed only on the target are reverted to HEAD: marked "D" if HEAD
    lacks them, otherwise recopied, since the clean source copy matches HEAD.
    """
    files = git_status(source)

    for file in git_status(target):
        if file not in files:
            files[file] = "  " if in_head(file) else "D"

    return files


def check_branches():
    if not CONTAINER:
        fail("SYNCUS_CONTAINER env var must be set")

    host_branch = git_branch()
    container_branch = git_branch(CONTAINER)

    if not host_branch or not container_branch:
        fail("one side is not on a branch (possibly detached HEAD).")

    if host_branch != container_branch:
        fail("branches do not match:",
             f"Host:      {host_branch}",
             f"Container: {container_branch}")

    print(f"Branch: {host_branch}")

    return host_branch


def is_ancestor(old, new, container=None):
    """Is commit old reachable from commit new, on the given side?"""
    return git(["merge-base", "--is-ancestor", old, new],
               container, check=False).returncode == 0


def sync_commits(branch, to_container):
    """Fast-forward one side's branch to the other's, via a git bundle.

    The host and container cannot reach each other over the network, so the
    commits travel as a bundle file copied with incus. Only fast-forwards
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
        fail("the two sides have diverged; reconcile manually.",
             f"Host:      {git_head()}",
             f"Container: {git_head(CONTAINER)}")

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
            fail("fast-forward failed:", merge.stderr)
    finally:
        run(["incus", "file", "delete", remote_bundle], check=False)

        if os.path.exists(bundle):
            os.remove(bundle)


def transfer(action, file, cmd, failures):
    """Run one file transfer, recording rather than raising on failure."""
    print(f"{action + ':':<8}{file}")
    result = run(cmd, check=False)

    if result.returncode != 0:
        print(red(f"        FAILED: {file}"), file=sys.stderr)
        failures.append((file, result.stderr or result.stdout))


def report(failures, total):
    if not failures:
        return

    print(file=sys.stderr)
    error(f"{len(failures)} of {total} file(s) failed to sync:")

    for file, output in failures:
        print(f"  {file}", file=sys.stderr)

        for line in output.strip().splitlines():
            print(f"      {line}", file=sys.stderr)

    sys.exit(1)


def push():
    branch = check_branches()
    sync_commits(branch, to_container=True)
    files = changed_files(None, CONTAINER)

    if not files:
        print("Files: up to date.")
        return

    failures = []

    for file, status in sorted(files.items()):
        local = os.path.join(PATH, file)
        remote = f"{CONTAINER}{os.path.join(PATH, file)}"

        if "D" in status and not os.path.exists(local):
            transfer("Delete", file,
                     ["incus", "file", "delete", "--force", remote], failures)
        elif os.path.isdir(local):
            transfer("Push", f"{file}/",
                     ["incus", "file", "push", "-r", local, remote], failures)
        else:
            transfer("Push", file,
                     ["incus", "file", "push", local, remote], failures)

    report(failures, len(files))


def pull():
    branch = check_branches()
    sync_commits(branch, to_container=False)
    files = changed_files(CONTAINER, None)

    if not files:
        print("Files: up to date.")
        return

    failures = []

    for file, status in sorted(files.items()):
        remote = f"{CONTAINER}{os.path.join(PATH, file)}"
        local = os.path.join(PATH, file)

        if "D" in status:
            print(f"Delete: {file}")

            try:
                if os.path.isdir(local):
                    shutil.rmtree(local)
                elif os.path.exists(local):
                    os.remove(local)
            except OSError as e:
                print(red(f"        FAILED: {file}"), file=sys.stderr)
                failures.append((file, str(e)))
        elif os.path.isdir(local):
            transfer("Pull", file,
                     ["incus", "file", "pull", "-r", remote, local], failures)
        else:
            transfer("Pull", file,
                     ["incus", "file", "pull", remote, local], failures)

    report(failures, len(files))


def main():
    # Keep stdout progress and stderr failures interleaved when piped.
    sys.stdout.reconfigure(line_buffering=True)

    if len(sys.argv) != 2 or sys.argv[1] not in ("push", "pull"):
        print(f"Usage: {os.path.basename(sys.argv[0])} push|pull")
        sys.exit(1)

    try:
        if sys.argv[1] == "push":
            push()
        else:
            pull()
    except subprocess.CalledProcessError as e:
        fail(f"command failed (exit {e.returncode}):",
             shlex.join(e.cmd), e.stderr or e.stdout or "")
    except FileNotFoundError as e:
        fail(f"command not found: {e.filename}")


if __name__ == "__main__":
    main()
