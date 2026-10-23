from __future__ import annotations

import asyncio
import hashlib
import os
import signal
import subprocess
from pathlib import Path

from .execution import sandbox, UncertainEffect
from .privacy import clean
from .store import atomic, encoded, now, uid


def git(repo, *args):
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(result.stderr.strip())
    return result.stdout.strip()


class Workspaces:
    def __init__(self, store):
        self.store = store

    def create(self, task, repository):
        repo = Path(repository).resolve()
        old = self.store.one("SELECT * FROM worktrees WHERE task_id=?", (task,))
        if old:
            if old["status"] in ("creating", "uncertain"):
                raise UncertainEffect(
                    "Existing worktree creation needs reconciliation: " + old["path"]
                )
            return old
        head = git(repo, "rev-parse", "HEAD")
        branch = "bloom/" + task
        path = self.store.config.root / "worktrees" / task
        path.parent.mkdir(exist_ok=True)
        self.store.db.execute(
            "INSERT INTO worktrees VALUES(?,?,?,?,?,?,?,?,?)",
            (uid("worktree"), task, str(repo), str(path), branch, head, None, "creating", now()),
        )
        try:
            git(repo, "worktree", "add", "-b", branch, str(path), head)
        except Exception as exc:
            self.store.db.execute(
                "UPDATE worktrees SET status='uncertain' WHERE task_id=?", (task,)
            )
            raise UncertainEffect(
                "Worktree creation must be inspected at " + str(path) + ": " + clean(str(exc))
            ) from exc
        self.store.db.execute("UPDATE worktrees SET status='active' WHERE task_id=?", (task,))
        return self.store.one("SELECT * FROM worktrees WHERE task_id=?", (task,))

    def submit(self, task):
        row = self.store.one("SELECT * FROM worktrees WHERE task_id=?", (task,))
        if not row:
            raise ValueError("Task has no worktree")
        if git(row["path"], "status", "--porcelain"):
            raise ValueError(
                "Commit the intended submission; dirty files are not a frozen submission"
            )
        head = git(row["path"], "rev-parse", "HEAD")
        self.store.db.execute(
            "UPDATE worktrees SET submitted_commit=?,status='submitted' WHERE id=?",
            (head, row["id"]),
        )
        return {"commit": head, "path": row["path"]}

    async def validate(self, task, command):
        row = self.store.one("SELECT * FROM worktrees WHERE task_id=?", (task,))
        if not row or not row["submitted_commit"]:
            raise ValueError("Submit a frozen worktree first")
        if git(row["path"], "rev-parse", "HEAD") != row["submitted_commit"] or git(
            row["path"], "status", "--porcelain"
        ):
            raise ValueError("Candidate changed since submission")
        release = uid("release")
        directory = self.store.config.workspace / "release-checks" / task / release
        directory.mkdir(parents=True, exist_ok=True)
        # Candidate source is readable; all writes must be inside disposable test state.
        python = self.store.config.project_python(row["path"], row["repository"])
        common = Path(
            git(row["repository"], "rev-parse", "--path-format=absolute", "--git-common-dir")
        )
        argv = sandbox(
            "read",
            Path(row["path"]),
            False,
            (directory,),
            extra_read=(python.parent.parent, python.resolve().parent.parent, common),
        ) + ["/bin/sh", "-c", command]
        env = self.store.config.child_env(
            "workspace",
            {
                "BLOOM_TEST_STATE": str(directory),
                "TMPDIR": str(directory),
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONPATH": str(Path(row["path"]) / "src"),
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_CONFIG_NOSYSTEM": "1",
            },
        )
        env.update(
            PATH=str(python.parent) + ":" + env["PATH"], VIRTUAL_ENV=str(python.parent.parent)
        )
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=row["path"],
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        identity = subprocess.run(
            ["ps", "-p", str(proc.pid), "-o", "lstart="], capture_output=True, text=True
        ).stdout.strip()
        atomic(
            directory / "process.json",
            encoded(
                {
                    "pid": proc.pid,
                    "start_identity": identity,
                    "command": clean(command),
                    "commit": row["submitted_commit"],
                    "started_at": now(),
                }
            ),
        )
        try:
            output, _ = await asyncio.wait_for(proc.communicate(), 600)
        except (TimeoutError, asyncio.CancelledError):
            for signum, wait in ((signal.SIGTERM, 5), (signal.SIGKILL, 5)):
                if proc.returncode is not None:
                    break
                actual = subprocess.run(
                    ["ps", "-p", str(proc.pid), "-o", "lstart="], capture_output=True, text=True
                ).stdout.strip()
                if not identity or actual != identity:
                    raise UncertainEffect(
                        "Candidate check identity changed; no signal sent. Inspect "
                        + str(directory)
                    )
                os.killpg(proc.pid, signum)
                try:
                    await asyncio.wait_for(proc.wait(), wait)
                except TimeoutError:
                    continue
            atomic(
                directory / "interrupted.json",
                encoded(
                    {
                        "status": "interrupted",
                        "exit_code": proc.returncode,
                        "partial_output_may_be_unavailable": True,
                        "at": now(),
                    }
                ),
            )
            self.store.event(
                "release.check_interrupted",
                {
                    "directory": str(directory),
                    "commit": row["submitted_commit"],
                    "exit_code": proc.returncode,
                },
                task,
            )
            raise
        evidence = {
            "commit": row["submitted_commit"],
            "command": command,
            "exit_code": proc.returncode,
            "output": clean(output.decode(errors="replace")),
            "contained": True,
            "python": str(python),
        }
        digest = self.store.put(evidence)
        self.store.db.execute(
            "INSERT INTO releases VALUES(?,?,?,?,?,?,?)",
            (
                release,
                row["repository"],
                git(row["repository"], "rev-parse", "HEAD"),
                row["submitted_commit"],
                "validated" if proc.returncode == 0 else "failed",
                digest,
                now(),
            ),
        )
        self.store.db.execute("INSERT INTO release_worktrees VALUES(?,?)", (release, row["id"]))
        diff = subprocess.run(
            [
                "git",
                "-C",
                row["repository"],
                "diff",
                "--binary",
                git(row["repository"], "rev-parse", "HEAD"),
                row["submitted_commit"],
            ],
            capture_output=True,
            check=True,
        ).stdout
        return {"release": release, "diff_sha256": self.store.put(diff, "text/x-diff"), **evidence}

    def review(self, release, agent, expected_diff_sha256, notes):
        row = self.store.one("SELECT * FROM releases WHERE id=?", (release,))
        if not row or row["status"] != "validated" or not notes.strip():
            raise ValueError("Review needs a passing frozen candidate and substantive review notes")
        diff = subprocess.run(
            [
                "git",
                "-C",
                row["repository"],
                "diff",
                "--binary",
                row["before_commit"],
                row["candidate_commit"],
            ],
            capture_output=True,
            check=True,
        ).stdout
        if hashlib.sha256(diff).hexdigest() != expected_diff_sha256:
            raise ValueError("Review diff does not match the exact candidate")
        self.store.db.execute(
            "INSERT OR REPLACE INTO release_reviews VALUES(?,?,?,?,?,?)",
            (
                release,
                agent,
                row["candidate_commit"],
                self.store.put(diff, "text/x-diff"),
                self.store.put(notes),
                now(),
            ),
        )
        return {"release": release, "reviewed_commit": row["candidate_commit"]}

    def integrate(self, release):
        row = self.store.one("SELECT * FROM releases WHERE id=?", (release,))
        if not row or row["status"] != "validated":
            raise ValueError("A passing frozen candidate is required")
        review = self.store.one("SELECT * FROM release_reviews WHERE release_id=?", (release,))
        if not review or review["candidate_commit"] != row["candidate_commit"]:
            raise ValueError("Review the exact tested candidate before integration")
        repo = row["repository"]
        if git(repo, "rev-parse", "HEAD") != row["before_commit"]:
            raise ValueError("Repository changed since validation; rebase/review the new candidate")
        # Git itself protects overlapping local edits. Unrelated notes need not
        # block a source release, and ignored files must not be overwritten.
        git(repo, "merge", "--ff-only", "--no-overwrite-ignore", row["candidate_commit"])
        self.store.db.execute("UPDATE releases SET status='integrated' WHERE id=?", (release,))
        return {"release": release, "commit": row["candidate_commit"], "restart_required": True}

    def rollback(self, release):
        row = self.store.one("SELECT * FROM releases WHERE id=?", (release,))
        if not row or row["status"] != "integrated":
            raise ValueError("No integrated release to roll back")
        repo = row["repository"]
        if git(repo, "rev-parse", "HEAD") != row["candidate_commit"]:
            raise ValueError("Refusing rollback over unrelated edits or a newer revision")
        changed = git(
            repo, "diff", "--name-only", "-z", row["before_commit"], row["candidate_commit"]
        ).split("\0")
        changed = [p for p in changed if p]
        if not changed or git(repo, "status", "--porcelain", "--", *changed):
            raise ValueError("Candidate paths contain new edits; rollback cannot overwrite them")
        git(
            repo,
            "restore",
            "--source",
            row["before_commit"],
            "--staged",
            "--worktree",
            "--",
            *changed,
        )
        git(repo, "commit", "--only", "-m", "Roll back Bloom release " + release, "--", *changed)
        self.store.db.execute("UPDATE releases SET status='rolled_back' WHERE id=?", (release,))
        return {"commit": git(repo, "rev-parse", "HEAD"), "restored_source": row["before_commit"]}
