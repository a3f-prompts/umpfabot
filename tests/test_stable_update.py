# SPDX-License-Identifier: MIT
#
# Integration tests for "umpfabot stable-update", run with pytest. Everything
# runs locally: the upstream, umpf and BSP repositories are created in a
# temporary directory and pull requests are kept in a JSON file
# (--forge-state).
#
# They need an umpf with range-diff, e.g. with patches/ applied: $UMPF or a
# checkout of umpf next to this repository.

import glob
import hashlib
import http.server
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse

import pytest

# for umpfabot, which runs with the same interpreter
pytest.importorskip("yaml", reason="PyYAML not found, skipping the umpfabot tests")

HERE = os.path.dirname(os.path.abspath(__file__))
UMPFABOT = os.path.join(HERE, os.pardir, "umpfabot")
PATCHES = sorted(glob.glob(os.path.join(HERE, os.pardir, "patches", "*.patch")))
UMPF = os.path.abspath(os.environ.get("UMPF") or
                       os.path.join(HERE, os.pardir, os.pardir, "umpf", "umpf"))
ID = "linux-acme"
SERIES = "recipes-kernel/linux/linux-acme/patches/series.inc"
RECIPE = "recipes-kernel/linux/linux-acme_1.0.bb"
DEFCONFIG = "recipes-kernel/linux/linux-acme/defconfig"
TODAY = time.strftime("%Y%m%d", time.gmtime())

MAKEFILE = """\
VERSION = 1
PATCHLEVEL = 0
SUBLEVEL = 0
EXTRAVERSION =

olddefconfig:
\tsed -i '/^CONFIG_TEST_/d' $(O)/.config
\techo 'CONFIG_TEST_SUBLEVEL=$(SUBLEVEL)' >> $(O)/.config
\techo 'CONFIG_TEST_TOOLCHAIN="$(CROSS_COMPILE)gcc $(ARCH) $(UMPFABOT_TEST_SDK)"' >> $(O)/.config

# like kconfig, which fills in the defaults before writing the minimal configuration
savedefconfig: olddefconfig
\tgrep -v '^CONFIG_TEST_TOOLCHAIN' $(O)/.config > $(O)/defconfig
"""

# 'docker run ... --entrypoint bash <image> -c <script>' on the host: umpfabot mounts
# the directories at the same path. Each call is logged as a JSON line.
FAKE_DOCKER = """\
#!{python}
import json, os, sys
with open(os.environ["UMPFABOT_TEST_DOCKER_LOG"], "a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\\n")
if sys.argv[1] == "pull":
    sys.exit(0)
args = sys.argv[2:]
while args[0].startswith("-"):
    opt = args.pop(0)
    if opt in ("-v", "-w", "--security-opt", "--entrypoint"):
        value = args.pop(0)
        if opt == "-w":
            os.chdir(value)
image = args.pop(0)
os.execvp("bash", ["bash", *args])
"""

DEFAULT_IMAGE = "docker.io/tuxmake/arm64_gcc-14"

CONFIG = """\
version: 1
author: umpfabot test <umpfabot@example.com>
stable-update:
  - id: linux-acme
    series: {series}
    recipe: {recipe}
    repository: {linux}
    compare-url: "https://example.com/linux/compare/{{old}}..{{new}}"
    upstream:
      repository: {upstream}
      changelog-url: "https://example.com/ChangeLog-{{version}}"
    tarball:
      url: "file://{dl}/linux-{{version}}.tar.gz"
    kconfig:
      defconfig: {defconfig}
      savedefconfig: {savedefconfig}
    sdk:
      path: {sdk}
      env:
        ARCH: arm64
        CROSS_COMPILE: aarch64-test-
    labels: [kernel]
    pending: {pending}
{container}"""


@pytest.fixture(scope="session", autouse=True)
def umpf_with_range_diff():
    if not os.access(UMPF, os.X_OK):
        pytest.fail(f"umpf not found at {UMPF}, set $UMPF")
    usage = subprocess.run([UMPF, "--help"], text=True, stdout=subprocess.PIPE).stdout
    if not re.search(r"^\s+range-diff\s", usage, re.M):
        pytest.fail(f"{UMPF} has no range-diff command, set $UMPF to an umpf "
                    "with patches/ applied")


class Fixture:
    def __init__(self, tmp, pending="stack", savedefconfig=False, container=""):
        self.tmp = tmp
        self.env = dict(os.environ, HOME=os.path.join(tmp, "home"), TZ="UTC",
                        GIT_CONFIG_NOSYSTEM="1", GIT_AUTHOR_NAME="Dev",
                        GIT_AUTHOR_EMAIL="dev@example.com", GIT_COMMITTER_NAME="Dev",
                        GIT_COMMITTER_EMAIL="dev@example.com")
        for var in ("GITHUB_ACTIONS", "GITHUB_STEP_SUMMARY", "GIT_DIR", "GIT_WORK_TREE"):
            self.env.pop(var, None)
        os.makedirs(self.env["HOME"])
        bin_dir = self.path("bin")
        self.write(bin_dir, "docker", FAKE_DOCKER.format(python=sys.executable))
        os.chmod(os.path.join(bin_dir, "docker"), 0o755)
        self.docker_log = self.path("docker.log")
        self.env["PATH"] = bin_dir + os.pathsep + self.env["PATH"]
        self.env["UMPFABOT_TEST_DOCKER_LOG"] = self.docker_log
        self.up = self.path("upstream")
        self.linux = self.path("linux.git")
        self.bsp = self.path("bsp.git")
        self.dl = self.path("dl")
        self.forge_file = self.path("forge.json")
        os.makedirs(self.dl)

        # upstream with the 1.0 release and a newer 1.1 series
        self.git("init", "-q", "-b", "master", self.up, cwd=tmp)
        self.write(self.up, "Makefile", MAKEFILE)
        self.write(self.up, "README", "Linux kernel\n")
        self.write(self.up, "drv.c", "a\nb\nc\n")
        self.commit(self.up, "Linux 1.0")
        self.tag(self.up, "v1.0")
        self.git("checkout", "-q", "-b", "stable", cwd=self.up)
        self.git("checkout", "-q", "-b", "next", "v1.0", cwd=self.up)
        self.write(self.up, "next.c", "next\n")
        self.commit(self.up, "Linux 1.1")
        self.tag(self.up, "v1.1")
        self.git("checkout", "-q", "stable", cwd=self.up)
        self.tarball("v1.0")

        # the umpf repository: base, topic branches and the initial utag
        self.git("init", "-q", "--bare", self.linux, cwd=tmp)
        # umpfabot uses a partial clone
        self.git("config", "uploadpack.allowFilter", "true", cwd=self.linux)
        self.kernel = self.path("kernel")
        self.git("clone", "-q", self.up, self.kernel, cwd=tmp)
        self.git("remote", "add", "acme", self.linux, cwd=self.kernel)
        self.git("checkout", "-q", "-b", "topic/foo", "v1.0", cwd=self.kernel)
        self.write(self.kernel, "foo.c", "foo\n")
        self.commit(self.kernel, "foo: add driver")
        self.write(self.kernel, "drv.c", "a\nB-topic\nc\n")
        self.commit(self.kernel, "drv: change b")
        self.git("checkout", "-q", "-b", "topic/bar", "v1.0", cwd=self.kernel)
        self.write(self.kernel, "bar.c", "bar\n")
        self.commit(self.kernel, "bar: add driver")
        self.write(self.kernel, "useries", "# umpf-base: v1.0\n# umpf-name: 1.0/acme\n"
                   "# umpf-topic: topic/foo\n# umpf-end\n")
        utag = self.umpf_tag("useries", epoch=1767225600)  # 2026-01-01
        self.git("push", "-q", "acme", "v1.0", utag, utag + "-flat", "topic/foo",
                 "topic/bar", cwd=self.kernel)

        # the BSP
        self.dev = self.path("bsp-dev")
        self.git("init", "-q", "--bare", "-b", "main", self.bsp, cwd=tmp)
        self.git("clone", "-q", self.bsp, self.dev, cwd=tmp)
        self.git("checkout", "-q", "-b", "main", cwd=self.dev)
        self.umpf("format-patch", "--bb", f"--patchdir={self.dev}/{os.path.dirname(SERIES)}",
                  utag)
        self.write(self.dev, RECIPE, 'SRC_URI = "file://linux-${UMPF_BASE}.tar.gz"\n'
                   f'SRC_URI[sha256sum] = "{self.sha256("v1.0")}"\n'
                   "require linux-acme/patches/series.inc\n")
        self.write(self.dev, DEFCONFIG, "CONFIG_FOO=y\n")
        self.sdk = self.path("sdk")
        self.write(self.sdk, "environment-setup-test",
                   "export UMPFABOT_TEST_SDK=sdk-ok\nexport ARCH=wrong\n"
                   f"export PATH={self.sdk}/bin:$PATH\n")
        self.write(self.sdk, "bin/aarch64-test-gcc", "#!/bin/sh\necho 'aarch64-test-gcc 1.0'\n")
        os.chmod(os.path.join(self.sdk, "bin/aarch64-test-gcc"), 0o755)
        self.write(self.dev, ".github/umpfabot.yml", CONFIG.format(
            series=SERIES, recipe=RECIPE, defconfig=DEFCONFIG, linux=f"file://{self.linux}",
            upstream=self.up, dl=self.dl, sdk=self.sdk, pending=pending,
            savedefconfig=str(savedefconfig).lower(), container=container))
        self.commit(self.dev, "initial BSP")
        self.git("push", "-q", "origin", "main", cwd=self.dev)

        # what actions/checkout would provide
        self.ci = self.path("bsp-ci")
        self.git("clone", "-q", self.bsp, self.ci, cwd=tmp)

    def path(self, name):
        return os.path.join(self.tmp, name)

    def docker_runs(self):
        if not os.path.exists(self.docker_log):
            return []
        with open(self.docker_log) as log:
            calls = [json.loads(line) for line in log]
        return [c for c in calls if c[0] == "run"]

    def git(self, *args, cwd=None, check=True):
        proc = subprocess.run(["git", *args], cwd=cwd or self.tmp, env=self.env, text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if check and proc.returncode:
            raise AssertionError(f"git {' '.join(args)} failed:\n{proc.stderr}")
        return proc.stdout.strip()

    def umpf(self, *args, env=None, check=True):
        proc = subprocess.run([UMPF, *args], cwd=self.kernel, env=env or self.env, text=True,
                              stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT)
        if check and proc.returncode:
            raise AssertionError(f"umpf {' '.join(args)} failed:\n{proc.stdout}")
        return proc.stdout

    def umpf_tag(self, series, epoch, version=1, base=None):
        env = dict(self.env, SOURCE_DATE_EPOCH=str(epoch))
        args = ["--local", f"--version={version}"] + ([f"--base={base}"] if base else [])
        self.umpf(*args, "tag", series, env=env)
        return self.git("describe", "--exact-match", "HEAD", cwd=self.kernel)

    def write(self, repo, name, content):
        path = os.path.join(repo, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(content)

    def commit(self, repo, msg):
        self.git("add", "-A", cwd=repo)
        self.git("commit", "-q", "-m", msg, cwd=repo)

    def tag(self, repo, tag):
        self.git("tag", "-a", "-m", tag, tag, cwd=repo)

    def tarball(self, tag):
        version = tag[1:]
        with open(os.path.join(self.dl, f"linux-{version}.tar.gz"), "wb") as f:
            subprocess.run(["git", "archive", "--format=tar.gz", f"--prefix=linux-{version}/",
                            tag], cwd=self.up, env=self.env, stdout=f, check=True)

    def sha256(self, tag):
        with open(os.path.join(self.dl, f"linux-{tag[1:]}.tar.gz"), "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()

    def release(self, tag, conflict=False, tarball=True):
        """publish a new stable release upstream"""
        sublevel = tag.rsplit(".", 1)[1]
        with open(os.path.join(self.up, "Makefile")) as f:
            makefile = f.read()
        self.write(self.up, "Makefile", "\n".join(
            f"SUBLEVEL = {sublevel}" if line.startswith("SUBLEVEL =") else line
            for line in makefile.split("\n")))
        self.write(self.up, f"fix-{sublevel}.c", "fix\n")
        if conflict:
            self.write(self.up, "drv.c", "a\nB-stable\nc\n")
        self.commit(self.up, f"Linux {tag[1:]}")
        self.tag(self.up, tag)
        if tarball:
            self.tarball(tag)

    def bot(self, *args, check=True, umpf=UMPF):
        proc = subprocess.run(
            [sys.executable, UMPFABOT, "-C", self.ci, "-w", self.path("work"),
             "--forge-state", self.forge_file, *(["--umpf", umpf] if umpf else []),
             "stable-update", *args],
            env=self.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        if check and proc.returncode:
            raise AssertionError(f"umpfabot failed:\n{proc.stdout}")
        return proc

    # forge simulation
    def forge(self):
        if not os.path.exists(self.forge_file):
            return {"pulls": [], "comments": {}}
        with open(self.forge_file) as f:
            return json.load(f)

    def save_forge(self, state):
        with open(self.forge_file, "w") as f:
            json.dump(state, f, indent=2)

    def pr(self, number):
        return next(p for p in self.forge()["pulls"] if p["number"] == number)

    def open_prs(self):
        return [p for p in self.forge()["pulls"] if p["state"] == "open"]

    def comments(self, number):
        return self.forge()["comments"].get(str(number), [])

    def set_pr(self, number, **fields):
        state = self.forge()
        next(p for p in state["pulls"] if p["number"] == number).update(fields)
        self.save_forge(state)

    def merge(self, number, delete_branch=False):
        pr = self.pr(number)
        self.git("fetch", "-q", "origin", cwd=self.dev)
        self.git("checkout", "-q", pr["base"], cwd=self.dev)
        self.git("reset", "-q", "--hard", f"origin/{pr['base']}", cwd=self.dev)
        self.git("merge", "-q", "--no-ff", "-m", f"Merge #{number}", f"origin/{pr['head']}",
                 cwd=self.dev)
        self.git("push", "-q", "origin", pr["base"], cwd=self.dev)
        self.set_pr(number, state="closed", merged=True)
        if delete_branch:
            # GitHub retargets pull requests based on a branch deleted on merge
            state = self.forge()
            for p in state["pulls"]:
                if p["state"] == "open" and p["base"] == pr["head"]:
                    p["base"] = pr["base"]
            self.save_forge(state)
            self.git("push", "-q", "origin", "--delete", pr["head"], cwd=self.dev)

    def push_to(self, branch, name="fixup.txt"):
        """a developer pushes to a branch"""
        self.git("fetch", "-q", "origin", cwd=self.dev)
        self.git("checkout", "-q", "-B", branch, f"origin/{branch}", cwd=self.dev)
        self.write(self.dev, name, "fixup\n")
        self.commit(self.dev, "developer change")
        self.git("push", "-q", "origin", branch, cwd=self.dev)

    def show(self, ref, path):
        return self.git("show", f"{ref}:{path}", cwd=self.bsp)

    def linux_tags(self):
        return self.git("tag", "-l", cwd=self.linux).split()

    def branch(self, tag):
        return f"umpfabot/stable-update/{ID}/{tag}"

    def message(self, tag):
        return self.git("log", "-1", "--format=%B", self.branch(tag), cwd=self.bsp)

    def meta(self, number):
        body = self.pr(number)["body"]
        start = body.index("<!-- umpfabot-metadata: ") + len("<!-- umpfabot-metadata: ")
        return json.loads(body[start:body.index(" -->", start)])


def bot_utag(version=90):
    """the utag umpfabot creates today"""
    return f"1.0/acme/{TODAY}-{version}"


@pytest.fixture
def pending():
    return "stack"


@pytest.fixture
def savedefconfig():
    return False


@pytest.fixture
def container():
    return ""


@pytest.fixture
def f(tmp_path, pending, savedefconfig, container):
    return Fixture(str(tmp_path), pending, savedefconfig, container)


class TestBasic:
    def test_up_to_date(self, f):
        f.bot()
        assert f.open_prs() == []

    def test_clean_update(self, f):
        f.release("v1.0.1")
        proc = f.bot()

        utag = bot_utag()
        tags = f.linux_tags()
        for tag in (utag, utag + "-flat", "v1.0.1"):
            assert tag in tags

        prs = f.open_prs()
        assert len(prs) == 1
        pr = prs[0]
        branch = f.branch("v1.0.1")
        assert (pr["head"], pr["base"], pr["draft"]) == (branch, "main", False)
        assert pr["title"] == f"{ID}: update to v1.0.1"
        assert pr["labels"] == ["kernel"]

        series = f.show(branch, SERIES)
        assert "# umpf-base: v1.0.1\n" in series
        assert f"# umpf-version: {utag}\n" in series
        assert 'UMPF_BASE = "1.0.1"\n' in series
        assert f'SRC_URI[sha256sum] = "{f.sha256("v1.0.1")}"' in f.show(branch, RECIPE)
        # olddefconfig ran in the SDK environment, with the configured variables
        assert f.show(branch, DEFCONFIG) == (
            "CONFIG_FOO=y\nCONFIG_TEST_SUBLEVEL=1\n"
            'CONFIG_TEST_TOOLCHAIN="aarch64-test-gcc arm64 sdk-ok"')
        # in the default container, with the SDK mounted, after the default prepare
        [docker_run] = f.docker_runs()
        assert DEFAULT_IMAGE in docker_run
        assert f"{f.sdk}:{f.sdk}:ro" in docker_run
        assert "aarch64-test-gcc 1.0" in proc.stdout

        msg = f.git("log", "-1", "--format=%B", branch, cwd=f.bsp)
        assert msg.startswith(f"{ID}: update to v1.0.1\n")
        assert f"https://example.com/linux/compare/1.0/acme/20260101-1..{utag}" in msg
        assert f"umpf range-diff 1.0/acme/20260101-1 {utag}" in msg
        assert f"git range-diff v1.0..'1.0/acme/20260101-1^' v1.0.1..'{utag}^'" in msg
        assert (f.git("log", "-1", "--format=%an <%ae>", branch, cwd=f.bsp) ==
                "umpfabot test <umpfabot@example.com>")
        assert f.git("rev-parse", f"{branch}^", cwd=f.bsp) == f.git("rev-parse", "main", cwd=f.bsp)

        body = pr["body"]
        assert "ChangeLog-1.0.1" in body
        assert "foo: add driver" in body  # range-diff
        assert "CONFIG_TEST_SUBLEVEL" in body  # olddefconfig changes
        assert f"`make olddefconfig` in `{DEFAULT_IMAGE}` changed" in body
        assert f.meta(1)["head"] == f.git("rev-parse", branch, cwd=f.bsp)

        # the instructions in the commit message actually work
        review = f.path("review")
        f.git("init", "-q", review)
        f.git("fetch", "-q", f.linux, "tag", "v1.0", "tag", "1.0/acme/20260101-1",
              "tag", "v1.0.1", "tag", utag, cwd=review)
        out = subprocess.run([UMPF, "range-diff", "1.0/acme/20260101-1", utag, "--",
                              "--no-color", "--no-patch"], cwd=review, env=f.env, text=True,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True).stdout
        assert "foo: add driver" in out

        # nothing to do the second time
        heads = f.git("ls-remote", f.bsp, cwd=f.tmp)
        f.bot()
        assert len(f.forge()["pulls"]) == 1
        assert f.git("ls-remote", f.bsp, cwd=f.tmp) == heads
        assert f.linux_tags() == tags

    def test_sdk_installer(self, f):
        installer = f.path("sdk.sh")
        f.write(f.tmp, "sdk.sh",
                "while [ $# -gt 0 ]; do [ \"$1\" = -d ] && dir=\"$2\"; shift; done\n"
                "mkdir -p \"$dir\" && echo >> \"$0.count\"\n"
                "echo 'export UMPFABOT_TEST_SDK=installed' > \"$dir/environment-setup-x\"\n"
                f"echo 'export PATH={f.sdk}/bin:$PATH' >> \"$dir/environment-setup-x\"\n")
        with open(installer, "rb") as inst:
            sha256 = hashlib.sha256(inst.read()).hexdigest()
        config = os.path.join(f.ci, ".github/umpfabot.yml")
        with open(config) as c:
            text = c.read().replace(f"path: {f.sdk}",
                                    f"installer: {installer}\n      sha256: {sha256}")
        with open(config, "w") as c:
            c.write(text)
        f.release("v1.0.1")
        f.bot()
        assert "aarch64-test-gcc arm64 installed" in f.show(f.branch("v1.0.1"), DEFCONFIG)
        f.release("v1.0.2")
        f.bot()
        assert "installed" in f.show(f.branch("v1.0.2"), DEFCONFIG)
        with open(installer + ".count") as count:
            assert count.read() == "\n"  # installed once

    def test_newest_in_series(self, f):
        f.release("v1.0.1")
        f.release("v1.0.2")
        f.bot()
        prs = f.open_prs()
        assert [p["head"] for p in prs] == [f.branch("v1.0.2")]

    def test_tarball_missing(self, f):
        f.release("v1.0.1", tarball=False)
        proc = f.bot()
        assert "not available yet" in proc.stdout
        assert f.open_prs() == []
        f.tarball("v1.0.1")
        f.bot()
        assert len(f.open_prs()) == 1

    def test_conflict(self, f):
        f.release("v1.0.1", conflict=True)
        f.bot()

        prs = f.open_prs()
        assert len(prs) == 1
        assert prs[0]["draft"]
        branch = f.branch("v1.0.1")
        # no umpf tag, but the base for resolving the conflict
        assert bot_utag() not in f.linux_tags()
        assert "v1.0.1" in f.linux_tags()

        # only UMPF_BASE and the checksum changed
        diff = f.git("diff", "-U0", "main", branch, cwd=f.bsp)
        changed = [line for line in diff.splitlines()
                   if line[:1] in "+-" and line[:3] not in ("+++", "---")]
        assert sorted(changed) == sorted([
            '-SRC_URI[sha256sum] = "%s"' % f.sha256("v1.0"),
            '+SRC_URI[sha256sum] = "%s"' % f.sha256("v1.0.1"),
            '-UMPF_BASE = "1.0"',
            '+UMPF_BASE = "1.0.1"'])
        body = prs[0]["body"]
        assert "topic/foo" in body
        assert "drv: change b" in body
        assert "drv.c" in body
        assert "umpf --identical --override=topic/foo=topic/foo --base=v1.0.1" in body
        msg = f.git("log", "-1", "--format=%B", branch, cwd=f.bsp)
        assert "Conflict while rebasing topic topic/foo" in msg
        assert f.meta(1)["conflict"]

        # the commands to update the kernel configuration work
        step = body[body.index("3. Update the kernel configuration in the container"):]
        commands = step[step.index("```sh\n") + 6:step.index("   ```\n")]
        commands = "".join(line[3:] for line in commands.splitlines(True))
        commands = commands.replace("<bsp>", f.dev).replace("<sdk>", f.sdk)
        f.git("fetch", "-q", "origin", "tag", "v1.0.1", cwd=f.kernel)
        f.git("checkout", "-q", "--detach", "v1.0.1", cwd=f.kernel)
        subprocess.run(["bash", "-e", "-c", commands], cwd=f.kernel, env=f.env, check=True)
        with open(os.path.join(f.dev, DEFCONFIG)) as defconfig:
            assert "CONFIG_TEST_SUBLEVEL=1\n" in defconfig.read()
        assert DEFAULT_IMAGE in f.docker_runs()[-1]

    def test_dry_run(self, f):
        f.release("v1.0.1")
        heads = f.git("ls-remote", f.bsp, cwd=f.tmp)
        tags = f.linux_tags()
        proc = f.bot("--dry-run")
        assert "would create pull request" in proc.stdout
        assert f.git("ls-remote", f.bsp, cwd=f.tmp) == heads
        assert f.linux_tags() == tags
        assert f.open_prs() == []
        # the dry-run tag is forgotten again
        f.bot()
        assert bot_utag() in f.linux_tags()

    def test_tag_version_collision(self, f):
        f.release("v1.0.1")
        # someone else already used the version umpfabot would use
        f.git("push", "-q", "acme", f"v1.0:refs/tags/{bot_utag()}", cwd=f.kernel)
        f.bot()
        assert bot_utag(91) in f.show(f.branch("v1.0.1"), SERIES)

    def test_closed_is_ignored(self, f):
        f.release("v1.0.1")
        f.bot()
        f.set_pr(1, state="closed")
        proc = f.bot()
        assert "closed without merging" in proc.stdout
        assert f.open_prs() == []
        f.bot("--force")
        assert len(f.open_prs()) == 1

    def test_explicit_version(self, f):
        f.release("v1.0.1")
        f.release("v1.0.2")
        f.bot("--id", ID, "--version", "v1.0.1")
        assert [p["head"] for p in f.open_prs()] == [f.branch("v1.0.1")]
        proc = f.bot("--id", ID, "--version", "v1.1", check=False)
        assert proc.returncode != 0
        assert "not a stable release" in proc.stdout

    def test_obsolete(self, f):
        f.release("v1.0.1")
        f.bot()
        # main is updated to v1.0.1 in some other way
        f.git("fetch", "-q", "origin", cwd=f.dev)
        f.git("push", "-q", "origin", f"origin/{f.branch('v1.0.1')}:refs/heads/main",
              cwd=f.dev)
        f.bot()
        assert f.pr(1)["state"] == "closed"
        assert any("no longer needed" in c for c in f.comments(1))
        f.release("v1.0.2")
        f.bot()
        prs = f.open_prs()
        assert [(p["head"], p["base"]) for p in prs] == [(f.branch("v1.0.2"), "main")]

    def test_refresh_when_main_changes(self, f):
        f.release("v1.0.1")
        f.bot()
        old_head = f.meta(1)["head"]

        # main gets a new umpf with an additional topic
        f.git("fetch", "-q", "acme", "--tags", cwd=f.kernel)
        f.write(f.kernel, "useries2", "# umpf-base: v1.0\n# umpf-name: 1.0/acme\n"
                "# umpf-topic: topic/foo\n# umpf-topic: topic/bar\n# umpf-end\n")
        utag = f.umpf_tag("useries2", epoch=1767312000)  # 2026-01-02
        f.git("push", "-q", "acme", utag, cwd=f.kernel)
        f.git("checkout", "-q", "main", cwd=f.dev)
        f.git("pull", "-q", "origin", "main", cwd=f.dev)
        f.umpf("format-patch", "--bb", "--update",
               f"--patchdir={f.dev}/{os.path.dirname(SERIES)}", utag)
        f.commit(f.dev, "add topic/bar")
        f.git("push", "-q", "origin", "main", cwd=f.dev)

        f.bot()
        assert len(f.forge()["pulls"]) == 1
        meta = f.meta(1)
        assert meta["head"] != old_head
        assert meta["from"]["umpf"] == utag
        series = f.show(f.branch("v1.0.1"), SERIES)
        assert "# umpf-topic: topic/bar" in series
        assert "# umpf-base: v1.0.1" in series
        assert any("Recreated" in c for c in f.comments(1))

    def test_refresh_conflicting(self, f):
        f.release("v1.0.1")
        f.bot()
        old = f.meta(1)
        # the forge says the pull request conflicts with main, umpf is unchanged
        f.git("checkout", "-q", "main", cwd=f.dev)
        f.git("pull", "-q", "origin", "main", cwd=f.dev)
        f.write(f.dev, DEFCONFIG, "CONFIG_FOO=y\nCONFIG_BAR=y\n")
        f.commit(f.dev, "enable bar")
        f.git("push", "-q", "origin", "main", cwd=f.dev)
        f.set_pr(1, mergeable=False)
        f.bot()
        meta = f.meta(1)
        assert meta["head"] != old["head"]
        assert meta["to"]["umpf"] == old["to"]["umpf"]  # the utag is reused
        assert "CONFIG_BAR=y" in f.show(f.branch("v1.0.1"), DEFCONFIG)

    def test_edited_is_left_alone(self, f):
        f.release("v1.0.1")
        f.bot()
        f.push_to(f.branch("v1.0.1"))
        head = f.git("rev-parse", f.branch("v1.0.1"), cwd=f.bsp)
        f.set_pr(1, mergeable=False)
        f.bot()
        f.bot()
        assert f.git("rev-parse", f.branch("v1.0.1"), cwd=f.bsp) == head
        stale = [c for c in f.comments(1) if "out of date" in c]
        assert len(stale) == 1


class TestStack:
    """newer releases are stacked on pending pull requests, the default"""

    def test_stack(self, f):
        f.release("v1.0.1")
        f.bot()
        head1 = f.pr(1)["head_sha"]
        f.release("v1.0.2")
        f.bot()

        prs = f.open_prs()
        assert len(prs) == 2
        assert f.pr(1)["head_sha"] == head1  # untouched
        pr2 = f.pr(2)
        assert pr2["base"] == f.branch("v1.0.1")
        assert "stacked on #1" in pr2["body"]
        assert f.meta(2)["from"]["umpf"] == bot_utag(90)
        assert f"# umpf-version: {bot_utag(91)}" in f.show(f.branch("v1.0.2"), SERIES)
        assert any("#2 is stacked on top" in c for c in f.comments(1))

        # merging the lower one retargets the upper one, which stays as is
        f.merge(1)
        f.bot()
        pr2 = f.pr(2)
        assert pr2["base"] == "main"
        assert f.meta(2)["head"] == f.git("rev-parse", f.branch("v1.0.2"), cwd=f.bsp)
        assert not any("Recreated" in c for c in f.comments(2))

    def test_stack_merged_with_branch_deletion(self, f):
        f.release("v1.0.1")
        f.bot()
        f.release("v1.0.2")
        f.bot()
        f.merge(1, delete_branch=True)
        f.bot()
        assert f.pr(2)["base"] == "main"
        assert len(f.open_prs()) == 1

    def test_lower_closed(self, f):
        f.release("v1.0.1")
        f.bot()
        f.release("v1.0.2")
        f.bot()
        f.set_pr(1, state="closed")
        f.bot()
        pr2 = f.pr(2)
        assert pr2["base"] == "main"
        # recreated on top of main
        assert f.meta(2)["from"]["umpf"] == "1.0/acme/20260101-1"
        assert (f.git("rev-parse", f"{f.branch('v1.0.2')}^", cwd=f.bsp) ==
                f.git("rev-parse", "main", cwd=f.bsp))

    def test_unedited_draft_is_superseded(self, f):
        f.release("v1.0.1", conflict=True)
        f.bot()
        assert f.pr(1)["draft"]
        f.release("v1.0.2")
        f.bot()
        assert f.pr(1)["state"] == "closed"
        assert "Superseded by #2." in "".join(f.comments(1))
        assert f.pr(2)["base"] == "main"
        assert f.pr(2)["draft"]  # still conflicts

    def test_draft_by_developer_is_kept(self, f):
        f.release("v1.0.1")
        f.bot()
        f.set_pr(1, draft=True)  # not to be merged yet
        f.release("v1.0.2")
        f.bot()
        assert (f.pr(1)["state"], f.pr(1)["draft"]) == ("open", True)
        assert f.pr(2)["base"] == f.branch("v1.0.1")

    def test_edited_draft_waits(self, f):
        f.release("v1.0.1", conflict=True)
        f.bot()
        f.push_to(f.branch("v1.0.1"))
        f.release("v1.0.2")
        f.bot()
        f.bot()
        assert [p["number"] for p in f.open_prs()] == [1]
        assert len([c for c in f.comments(1) if "v1.0.2 is available" in c]) == 1
        # the developer is done and marks it ready, now a new one is stacked on top
        f.set_pr(1, draft=False)
        f.bot()
        assert f.pr(2)["base"] == f.branch("v1.0.1")

    def test_conflict_resolution(self, f):
        """follow the instructions of a conflict pull request"""
        f.release("v1.0.1", conflict=True)
        f.bot()
        branch = f.branch("v1.0.1")
        hashinfo = re.search(r"git checkout -b topic/foo (\S+)", f.pr(1)["body"]).group(1)

        f.git("fetch", "-q", "origin", cwd=f.dev)
        f.git("checkout", "-q", "-B", branch, f"origin/{branch}", cwd=f.dev)
        f.git("fetch", "-q", "acme", "--tags", cwd=f.kernel)
        f.git("checkout", "-q", "-b", "fixed", hashinfo, cwd=f.kernel)
        f.git("rebase", "-q", "v1.0.1", cwd=f.kernel, check=False)
        f.write(f.kernel, "drv.c", "a\nB-topic\nc\n")
        f.git("add", "drv.c", cwd=f.kernel)
        f.git("-c", "core.editor=true", "rebase", "--continue", cwd=f.kernel)
        f.umpf("--identical", "--override=topic/foo=fixed", "--base=v1.0.1", "tag",
               f"{f.dev}/{SERIES}")
        f.umpf("format-patch", "--bb", "--update",
               f"--patchdir={f.dev}/{os.path.dirname(SERIES)}")
        utag = f.git("describe", "--exact-match", cwd=f.kernel)
        f.git("push", "-q", "acme", utag, f"{utag}-flat", cwd=f.kernel)
        f.git("add", "-A", os.path.dirname(SERIES), cwd=f.dev)
        f.git("commit", "-q", "--amend", "--no-edit", cwd=f.dev)
        f.git("push", "-q", "--force", "origin", f"HEAD:{branch}", cwd=f.dev)
        f.set_pr(1, draft=False)

        # the next update is based on the resolution and applies
        f.release("v1.0.2")
        f.bot()
        pr2 = f.pr(2)
        assert (pr2["base"], pr2["draft"]) == (branch, False)
        assert f.meta(2)["from"]["umpf"] == utag
        fixed = f.git("rev-parse", "fixed", cwd=f.kernel)
        assert f"# umpf-hashinfo: {fixed}" in f.show(f.branch("v1.0.2"), SERIES)

        f.merge(1)
        f.bot()
        assert f.pr(2)["base"] == "main"
        assert not any("Recreated" in c for c in f.comments(2))

    def test_lower_recreated_upper_follows(self, f):
        f.release("v1.0.1")
        f.bot()
        f.release("v1.0.2")
        f.bot()
        # main changes in a way that conflicts with the lower one
        f.git("checkout", "-q", "main", cwd=f.dev)
        f.git("pull", "-q", "origin", "main", cwd=f.dev)
        f.write(f.dev, DEFCONFIG, "CONFIG_FOO=y\nCONFIG_BAR=y\n")
        f.commit(f.dev, "enable bar")
        f.git("push", "-q", "origin", "main", cwd=f.dev)
        f.set_pr(1, mergeable=False)
        utag2 = f.meta(2)["to"]["umpf"]
        f.bot()
        # both reuse their utags, the upper one is rebased onto the lower one
        assert f.meta(1)["to"]["umpf"] == bot_utag(90)
        assert f.meta(2)["to"]["umpf"] == utag2
        assert (f.git("rev-parse", f"{f.branch('v1.0.2')}^", cwd=f.bsp) ==
                f.git("rev-parse", f.branch("v1.0.1"), cwd=f.bsp))
        assert f.meta(2)["head"] == f.git("rev-parse", f.branch("v1.0.2"), cwd=f.bsp)
        assert "CONFIG_BAR=y" in f.show(f.branch("v1.0.2"), DEFCONFIG)

    def test_limit(self, f):
        with open(os.path.join(f.ci, ".github/umpfabot.yml"), "a") as config:
            config.write("    open-pull-requests-limit: 1\n")
        f.release("v1.0.1")
        f.bot()
        f.release("v1.0.2")
        proc = f.bot()
        assert "already 1 open pull requests" in proc.stdout
        assert len(f.open_prs()) == 1


class TestSupersede:
    @pytest.fixture
    def pending(self):
        return "supersede"

    def test_supersede(self, f):
        f.release("v1.0.1")
        f.bot()
        f.release("v1.0.2")
        f.bot()
        assert f.pr(1)["state"] == "closed"
        assert "Superseded by #2." in "".join(f.comments(1))
        assert f.git("ls-remote", f.bsp, f"refs/heads/{f.branch('v1.0.1')}", cwd=f.tmp) == ""
        assert (f.pr(2)["base"], f.pr(2)["state"]) == ("main", "open")

    def test_edited_waits(self, f):
        f.release("v1.0.1")
        f.bot()
        f.push_to(f.branch("v1.0.1"))
        f.release("v1.0.2")
        f.bot()
        assert [p["number"] for p in f.open_prs()] == [1]
        assert any("edited by someone other than umpfabot" in c for c in f.comments(1))
        f.merge(1)
        f.bot()
        prs = f.open_prs()
        assert [(p["head"], p["base"]) for p in prs] == [(f.branch("v1.0.2"), "main")]


class TestContainer:
    def test_on_the_runner(self, f):
        with open(os.path.join(f.ci, ".github/umpfabot.yml"), "a") as config:
            config.write("    container: false\n")
        f.release("v1.0.1")
        proc = f.bot()
        assert f.docker_runs() == []
        assert "aarch64-test-gcc 1.0" not in proc.stdout  # no prepare
        assert f.show(f.branch("v1.0.1"), DEFCONFIG) == (
            "CONFIG_FOO=y\nCONFIG_TEST_SUBLEVEL=1\n"
            'CONFIG_TEST_TOOLCHAIN="aarch64-test-gcc arm64 sdk-ok"')
        assert "`make olddefconfig` changed" in f.pr(1)["body"]

    def test_image_and_prepare(self, f):
        # prepare runs in the same shell as make, so it can set up an SDK
        with open(os.path.join(f.ci, ".github/umpfabot.yml"), "a") as config:
            config.write("    container:\n"
                         "      image: registry.example.com/kernel-builder:1\n"
                         "      prepare: |\n"
                         "        echo preparing\n"
                         "        export UMPFABOT_TEST_SDK=prepared\n")
        f.release("v1.0.1")
        proc = f.bot()
        [docker_run] = f.docker_runs()
        assert "registry.example.com/kernel-builder:1" in docker_run
        assert "preparing" in proc.stdout
        assert 'CONFIG_TEST_TOOLCHAIN="aarch64-test-gcc arm64 prepared"' in \
            f.show(f.branch("v1.0.1"), DEFCONFIG)

    def test_prepare_fails(self, f):
        with open(os.path.join(f.ci, ".github/umpfabot.yml"), "a") as config:
            config.write("    container:\n      prepare: no-such-command\n")
        f.release("v1.0.1")
        proc = f.bot(check=False)
        assert proc.returncode != 0
        assert "no-such-command" in proc.stdout
        assert f.open_prs() == []

    def test_invalid(self, f):
        with open(os.path.join(f.ci, ".github/umpfabot.yml"), "a") as config:
            config.write("    container: ubuntu\n")
        proc = f.bot(check=False)
        assert "'container' must be a mapping or false" in proc.stdout


class TestSavedefconfig:
    @pytest.fixture
    def savedefconfig(self):
        return True

    def test_savedefconfig(self, f):
        f.release("v1.0.1")
        proc = f.bot()
        # savedefconfig alone, it fills in the defaults like olddefconfig
        assert f"make savedefconfig in {DEFAULT_IMAGE}..." in proc.stdout
        assert "make olddefconfig" not in proc.stdout
        assert f.show(f.branch("v1.0.1"), DEFCONFIG) == "CONFIG_FOO=y\nCONFIG_TEST_SUBLEVEL=1"
        assert "make savedefconfig" in f.message("v1.0.1").replace("\n", " ")
        body = f.pr(1)["body"]
        assert f"`make savedefconfig` in `{DEFAULT_IMAGE}` changed `{DEFCONFIG}`:" in body
        assert "+CONFIG_TEST_SUBLEVEL=1" in body


class UmpfRepo:
    """an umpf repository with the patches of umpfabot on a pull request ref"""

    def __init__(self, f):
        self.f = f
        self.path = f.path("umpf")
        with open(UMPF) as src:
            umpf = src.read()
        f.git("init", "-q", "-b", "master", self.path)
        # upstream: the umpf that umpfabot's patches apply on
        f.write(self.path, "umpf", umpf)
        for patch in reversed(PATCHES):
            f.git("apply", "--reverse", "--include=umpf", patch, cwd=self.path)
        f.commit(self.path, "umpf")
        self.upstream = f.git("rev-parse", "HEAD", cwd=self.path)
        f.write(self.path, "umpf", umpf)
        f.commit(self.path, "umpf: add range-diff command")
        self.with_range_diff = f.git("rev-parse", "HEAD", cwd=self.path)
        # like a pull request on GitHub, not on any branch
        f.git("update-ref", "refs/pull/1/head", "HEAD", cwd=self.path)
        f.git("reset", "-q", "--hard", "HEAD^", cwd=self.path)

    def configure(self, version=None):
        with open(os.path.join(self.f.ci, ".github/umpfabot.yml"), "a") as config:
            config.write(f"umpf:\n  repository: file://{self.path}\n")
            if version:
                config.write(f'  version: "{version}"\n')


class TestUmpfVersion:
    """umpfabot fetches the configured umpf version instead of using --umpf"""

    @pytest.fixture
    def umpf_repo(self, f):
        return UmpfRepo(f)

    def test_newest_with_patches(self, f, umpf_repo):
        umpf_repo.configure()
        f.release("v1.0.1")
        proc = f.bot(umpf=None)
        patches = ", ".join(os.path.basename(p) for p in PATCHES)
        assert f"using umpf {umpf_repo.upstream[:12]} with {patches}\n" in proc.stdout
        assert f"umpf range-diff 1.0/acme/20260101-1 {bot_utag()}" in f.message("v1.0.1")
        assert "<summary>umpf range-diff</summary>" in f.pr(1)["body"]

        # the newest umpf has the patches already
        f.git("reset", "-q", "--hard", umpf_repo.with_range_diff, cwd=umpf_repo.path)
        f.release("v1.0.2")
        proc = f.bot(umpf=None)
        assert f"using umpf {umpf_repo.with_range_diff[:12]}\n" in proc.stdout
        assert "umpf range-diff" in f.message("v1.0.2")

    def test_patch_does_not_apply(self, f, umpf_repo):
        path = os.path.join(umpf_repo.path, "umpf")
        with open(path) as src:
            umpf = src.read()
        old = 'if [ "$(<${STATE}/command)" = "format_patch" ]'
        assert old in umpf
        f.write(umpf_repo.path, "umpf",
                umpf.replace(old, 'if [[ "$(<${STATE}/command)" == format_patch ]]'))
        f.commit(umpf_repo.path, "umpf: use [[ ]]")
        sha = f.git("rev-parse", "HEAD", cwd=umpf_repo.path)
        umpf_repo.configure()
        f.release("v1.0.1")
        proc = f.bot(umpf=None)
        assert (f"warning: {os.path.basename(PATCHES[0])} does not apply on umpf "
                f"{sha[:12]}, using umpf without patches") in proc.stdout
        assert f"using umpf {sha[:12]}\n" in proc.stdout
        assert "umpf has no range-diff command" in proc.stdout
        assert f.pr(1)["state"] == "open"
        assert "umpf range-diff" not in f.message("v1.0.1")

    def test_pinned_commit(self, f, umpf_repo):
        sha = umpf_repo.with_range_diff
        umpf_repo.configure(sha)
        f.release("v1.0.1")
        proc = f.bot(umpf=None)
        assert f"using umpf {sha[:12]}\n" in proc.stdout
        assert os.access(f.path(f"work/umpf/{sha}/umpf"), os.X_OK)
        assert f"umpf range-diff 1.0/acme/20260101-1 {bot_utag()}" in f.message("v1.0.1")
        assert "<summary>umpf range-diff</summary>" in f.pr(1)["body"]

        # a pinned commit is only fetched once
        os.rename(umpf_repo.path, umpf_repo.path + ".gone")
        f.release("v1.0.2")
        proc = f.bot(umpf=None)
        assert "fetching umpf" not in proc.stdout
        assert f.pr(2)["base"] == f.branch("v1.0.1")

    def test_without_range_diff(self, f, umpf_repo):
        """a configured version is used as is, without patches"""
        umpf_repo.configure("master")
        f.release("v1.0.1")
        proc = f.bot(umpf=None)
        assert f"using umpf {umpf_repo.upstream[:12]} (master)\n" in proc.stdout
        assert "umpf has no range-diff command, skipping it" in proc.stdout
        assert "range-diff failed" not in proc.stdout
        utag = bot_utag()
        plain = f"git range-diff v1.0..'1.0/acme/20260101-1^' v1.0.1..'{utag}^'"
        msg = f.message("v1.0.1")
        assert f"New umpf: {utag}" in msg
        assert msg.endswith(f"\n  {plain}"), msg
        assert "umpf range-diff" not in msg
        assert "without umpf" not in msg
        body = f.pr(1)["body"]
        assert plain in body
        assert "umpf range-diff" not in body
        assert "without umpf" not in body

        # the plain git instructions work
        review = f.path("review")
        f.git("init", "-q", review)
        f.git("fetch", "-q", f.linux, "tag", "v1.0", "tag", "1.0/acme/20260101-1",
              "tag", "v1.0.1", "tag", utag, cwd=review)
        out = subprocess.run(["sh", "-c", plain + " --no-patch"], cwd=review, env=f.env,
                             text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             check=True).stdout
        assert "foo: add driver" in out

    def test_version_not_found(self, f, umpf_repo):
        umpf_repo.configure("nonexistent")
        f.release("v1.0.1")
        proc = f.bot(umpf=None, check=False)
        assert proc.returncode == 1
        assert "umpf version 'nonexistent' not found" in proc.stdout
        assert f.open_prs() == []


class FakeGitHub(http.server.ThreadingHTTPServer):
    """just enough of the GitHub API for umpfabot, one item per page"""

    def __init__(self, fixture):
        super().__init__(("127.0.0.1", 0), FakeGitHubHandler)
        self.f = fixture
        self.pulls = []
        self.comments = {}
        self.requests = []
        self.lock = threading.Lock()
        threading.Thread(target=self.serve_forever, daemon=True).start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server_address[1]}"

    def head_sha(self, branch):
        out = self.f.git("ls-remote", self.f.bsp, f"refs/heads/{branch}", cwd=self.f.tmp)
        return out.split()[0] if out else None

    def json(self, pr):
        sha = self.head_sha(pr["head"]) if pr["state"] == "open" else None
        pr["sha"] = sha or pr["sha"]
        return {"number": pr["number"], "title": pr["title"], "body": pr["body"],
                "head": {"ref": pr["head"], "sha": pr["sha"],
                         "repo": {"full_name": "acme/bsp"}},
                "base": {"ref": pr["base"]}, "state": pr["state"],
                "merged_at": "2026-01-01T00:00:00Z" if pr["merged"] else None,
                "draft": pr["draft"], "html_url": f"https://github.test/pull/{pr['number']}",
                "node_id": f"PR_{pr['number']}", "mergeable": pr["mergeable"]}


class FakeGitHubHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, code, data=None, headers=()):
        body = json.dumps(data).encode() if data is not None else b""
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def page(self, items, url, query):
        page = int(query.get("page", ["1"])[0])
        headers = []
        if page < len(items):
            query = dict(query, page=[str(page + 1)])
            headers.append(("Link", f'<{self.server.url}{url.path}?'
                            f'{urllib.parse.urlencode(query, doseq=True)}>; rel="next"'))
        self.reply(200, items[page - 1:page], headers)

    def handle_any(self, method):
        gh = self.server
        url = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(url.query)
        length = int(self.headers.get("Content-Length") or 0)
        data = json.loads(self.rfile.read(length)) if length else None
        if self.headers.get("Authorization") != "Bearer secret":
            return self.reply(401, {"message": "Bad credentials"})
        with gh.lock:
            gh.requests.append((method, url.path, data))
            parts = url.path.strip("/").split("/")
            if parts == ["graphql"]:
                number = int(data["variables"]["id"][3:])
                draft = "convertPullRequestToDraft" in data["query"]
                gh.pulls[number - 1]["draft"] = draft
                return self.reply(200, {"data": {}})
            if parts[:3] != ["repos", "acme", "bsp"]:
                return self.reply(404, {"message": "Not Found"})
            parts = parts[3:]
            if parts == [] and method == "GET":
                return self.reply(200, {"default_branch": "main"})
            if parts == ["pulls"] and method == "GET":
                prs = [p for p in gh.pulls if p["state"] == query["state"][0]]
                if "head" in query:
                    prs = [p for p in prs if f"acme:{p['head']}" == query["head"][0]]
                return self.page([gh.json(p) for p in prs], url, query)
            if parts == ["pulls"] and method == "POST":
                if not gh.head_sha(data["head"]):
                    return self.reply(422, {"message": "head does not exist"})
                pr = {"number": len(gh.pulls) + 1, "title": data["title"],
                      "body": data["body"], "head": data["head"], "base": data["base"],
                      "state": "open", "merged": False, "draft": data["draft"],
                      "mergeable": None, "sha": None, "labels": [],
                      "maintainer_can_modify": data["maintainer_can_modify"]}
                gh.pulls.append(pr)
                return self.reply(201, gh.json(pr))
            number = int(parts[1])
            pr = gh.pulls[number - 1]
            if parts[0] == "pulls" and len(parts) == 2:
                if method == "PATCH":
                    pr.update(data)
                return self.reply(200, gh.json(pr))
            if parts[2:] == ["comments"]:
                comments = gh.comments.setdefault(number, [])
                if method == "POST":
                    comments.append(data["body"])
                    return self.reply(201, {})
                return self.page([{"body": c} for c in comments], url, query)
            if parts[2:] == ["labels"]:
                pr["labels"] += data["labels"]
                return self.reply(200, [])
            return self.reply(200, {})

    def do_GET(self):
        self.handle_any("GET")

    def do_POST(self):
        self.handle_any("POST")

    def do_PATCH(self):
        self.handle_any("PATCH")


class TestGitHub:
    @pytest.fixture
    def gh(self, f):
        gh = FakeGitHub(f)
        for var in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy",
                    "ALL_PROXY"):
            f.env.pop(var, None)
        f.env.update(GITHUB_API_URL=gh.url, GITHUB_GRAPHQL_URL=gh.url + "/graphql")
        yield gh
        gh.shutdown()
        gh.server_close()

    def bot(self, f, *args):
        proc = subprocess.run(
            [sys.executable, UMPFABOT, "-C", f.ci, "-w", f.path("work"), "--repo", "acme/bsp",
             "--umpf", UMPF, "stable-update", *args],
            env=dict(f.env, UMPFABOT_TOKEN="secret"), text=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        if proc.returncode:
            raise AssertionError(f"umpfabot failed:\n{proc.stdout}")
        return proc

    def test_github(self, f, gh):
        f.release("v1.0.1")
        self.bot(f)
        pr1 = gh.pulls[0]
        assert ((pr1["head"], pr1["base"], pr1["draft"], pr1["maintainer_can_modify"]) ==
                (f.branch("v1.0.1"), "main", False, True))
        assert pr1["labels"] == ["kernel"]

        f.release("v1.0.2")
        self.bot(f)
        assert gh.pulls[1]["base"] == f.branch("v1.0.1")
        assert any("#2 is stacked" in c for c in gh.comments[1])
        # several open pull requests and comments need more than one page
        self.bot(f)
        assert len(gh.pulls) == 2
        assert len(gh.comments[1]) == 1

    def test_github_ready_for_review(self, f, gh):
        f.release("v1.0.1", conflict=True)
        self.bot(f)
        assert gh.pulls[0]["draft"]
        # main drops the conflicting topic, so the update applies now
        f.write(f.kernel, "useries2", "# umpf-base: v1.0\n# umpf-name: 1.0/acme\n"
                "# umpf-topic: topic/bar\n# umpf-end\n")
        utag = f.umpf_tag("useries2", epoch=1767312000)
        f.git("push", "-q", "acme", utag, cwd=f.kernel)
        f.git("checkout", "-q", "main", cwd=f.dev)
        f.git("pull", "-q", "origin", "main", cwd=f.dev)
        f.umpf("format-patch", "--bb", "--update",
               f"--patchdir={f.dev}/{os.path.dirname(SERIES)}", utag)
        f.commit(f.dev, "drop topic/foo")
        f.git("push", "-q", "origin", "main", cwd=f.dev)
        self.bot(f)
        assert not gh.pulls[0]["draft"]
        assert ("POST", "/graphql") in [r[:2] for r in gh.requests]
        assert "# umpf-topic: topic/bar" in f.show(f.branch("v1.0.1"), SERIES)

